"""Per-user Windows watchdog. No passwords; logs contain lifecycle events only."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import re
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'runs' / 'remote'
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
CONFIG = json.loads((STATE / 'supervisor.json').read_text('utf-8-sig')) if (STATE / 'supervisor.json').exists() else {}
SSH_HOST = CONFIG.get('SshHost', 'model-server')
REMOTE_DIR = CONFIG.get('RemoteDirectory', 'classroom-models')
ASR_PORT = int(CONFIG.get('AsrRemotePort', 18765))
EVENT_PORT = int(CONFIG.get('EventRemotePort', 18766))
if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', SSH_HOST) or not re.fullmatch(r'[a-zA-Z0-9_./~-]+', REMOTE_DIR) or not 1 <= ASR_PORT <= 65535 or not 1 <= EVENT_PORT <= 65535:
    raise ValueError('Invalid remote configuration')
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy(url):
    try:
        with OPENER.open(url, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def spawn(args, log):
    return subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=log, creationflags=HIDDEN)


def main():
    import msvcrt
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / 'supervisor.lock').open('a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'1')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', handlers=[
        RotatingFileHandler(STATE / 'supervisor.log', maxBytes=2_000_000, backupCount=2, encoding='utf-8')])
    (STATE / 'supervisor.pid').write_text(str(os.getpid()))
    logging.info('Supervisor started')
    tunnel = web = None
    failures = 0
    last_remote_check = 0
    with (STATE / 'tunnel-error.log').open('ab', buffering=0) as tunnel_log, \
         (ROOT / 'runs/workspace-supervised.log').open('ab', buffering=0) as web_log:
        while not (STATE / 'supervisor.stop').exists():
            try:
                asr = healthy('http://127.0.0.1:18765/health')
                text = healthy('http://127.0.0.1:18766/health')
                failures = 0 if asr and text else failures + 1
                if failures and (tunnel is None or tunnel.poll() is not None or failures >= 3):
                    if tunnel is not None and tunnel.poll() is None:
                        tunnel.terminate()
                        tunnel.wait(timeout=10)
                    tunnel = spawn(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
                        '-o', 'ServerAliveCountMax=3', '-L', f'127.0.0.1:18765:127.0.0.1:{ASR_PORT}',
                        '-L', f'127.0.0.1:18766:127.0.0.1:{EVENT_PORT}', SSH_HOST], tunnel_log)
                    logging.info('Model tunnel launched pid=%s', tunnel.pid)
                    failures = 0
                if not asr and time.monotonic() - last_remote_check >= 120:
                    last_remote_check = time.monotonic()
                    # Restart only our ASR if its remote health probe fails. Never touch shared vLLM.
                    check = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        SSH_HOST, f'curl -fsS --max-time 5 http://127.0.0.1:{ASR_PORT}/health >/dev/null || '
                        f'(cd {REMOTE_DIR} && bash start_model_services.sh)'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, creationflags=HIDDEN)
                    logging.info('Remote ASR health/recovery check exit=%s', check.returncode)
                if asr and text and not healthy('http://127.0.0.1:8765/api/state') and (web is None or web.poll() is not None):
                    web = spawn(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(ROOT / 'scripts/start_remote_workbench.ps1'),
                                 '-SshHost', SSH_HOST, '-RemoteDirectory', REMOTE_DIR,
                                 '-AsrRemotePort', str(ASR_PORT), '-EventRemotePort', str(EVENT_PORT),
                                 '-EventModel', CONFIG.get('EventModel', 'Qwen3-4B'),
                                 '-DataDirectory', CONFIG.get('DataDirectory', str(ROOT / 'runs/webui'))], web_log)
                    logging.info('Workbench launched pid=%s', web.pid)
            except Exception as exc:
                logging.warning('Recovery check failed: %s', type(exc).__name__)
            time.sleep(10)
    logging.info('Supervisor stopped; existing workbench left running')


if __name__ == '__main__':
    main()
