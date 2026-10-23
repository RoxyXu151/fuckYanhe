"""Official browser sign-in; credentials remain on the official page."""

import threading
import time
import json
import csv
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from ..portal import OFFICIAL_SITE, PortalError, YanheClient


class BrowserLogin:
    def __init__(self, *, client_factory=YanheClient, login_url=OFFICIAL_SITE, headless=False,
                 profile_dir=None, on_connected=None):
        self.client_factory = client_factory
        self.login_url = login_url
        self.headless = headless
        self._stops = []
        self.profile_dir = Path(profile_dir).resolve() if profile_dir else None
        self.on_connected = on_connected
        self._profile_lock = threading.Lock()

    def _configure_profile(self):
        if not self.profile_dir:
            return
        # A dedicated official-site profile, never the user's general browser profile.
        default = self.profile_dir / 'Default'
        default.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            # Browser state also contains session data. Do not inherit broad D-drive ACLs.
            identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'],
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            sid = next(csv.reader(identity.strip().splitlines()))[1]
            if not sid.startswith('S-1-'):
                raise OSError('Cannot identify the current Windows user')
            result = subprocess.run(['icacls', str(self.profile_dir), '/inheritance:r', '/grant:r',
                f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '/T', '/Q'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise OSError('Cannot protect the official browser profile')
        preferences = default / 'Preferences'
        try:
            data = json.loads(preferences.read_text('utf-8')) if preferences.exists() else {}
        except (ValueError, OSError):
            data = {}
        data['credentials_enable_service'] = False
        data.setdefault('profile', {})['password_manager_enabled'] = False
        temporary = preferences.with_suffix('.pending')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        temporary.replace(preferences)

    def start(self, session):
        if session.login_status.get('state') in ('opening', 'waiting'):
            return
        stop = threading.Event()
        self._stops.append(stop)
        session.login_stop = stop
        session.login_status = {'state': 'opening', 'message': '正在打开官方登录窗口…'}
        threading.Thread(target=self._run, args=(session, stop), daemon=True, name='yanhe-official-login').start()

    def _run(self, session, stop):
        if not self._profile_lock.acquire(blocking=False):
            session.login_status = {'state': 'waiting', 'message': '官方登录窗口已打开，请先在现有窗口完成连接。'}
            return
        try:
            self._configure_profile()
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = None
                for channel in ('msedge', 'chrome', None):
                    try:
                        if self.profile_dir:
                            browser = playwright.chromium.launch_persistent_context(
                                user_data_dir=str(self.profile_dir), channel=channel, headless=self.headless,
                                accept_downloads=False, args=['--disable-save-password-bubble'])
                        else:
                            browser = playwright.chromium.launch(channel=channel, headless=self.headless)
                        break
                    except Exception:
                        continue
                if browser is None:
                    raise PortalError('无法启动浏览器。请安装 Edge/Chrome，或运行 python -m playwright install chromium。', 'browser', 503)
                try:
                    context = browser if self.profile_dir else browser.new_context()
                    page = context.new_page()
                    response = page.goto(self.login_url, wait_until='domcontentloaded', timeout=45000)
                    message = '请在新窗口完成延河课堂登录，完成后本页自动连接。'
                    if response is not None and response.status >= 400:
                        message = '官网暂时拒绝访问，请检查校园网/VPN，在官方窗口刷新后继续登录。'
                    session.login_status = {'state': 'waiting', 'message': message}
                    deadline = time.monotonic() + 600
                    next_verify = 0
                    while time.monotonic() < deadline and not stop.is_set():
                        pages = context.pages
                        if not pages:
                            raise PortalError('登录窗口已关闭，可重新打开登录。', 'browser', 400)
                        for candidate in pages:
                            if urlsplit(candidate.url).hostname != urlsplit(self.login_url).hostname:
                                continue
                            try:
                                token = candidate.evaluate('''() => {
                                    try { const a = JSON.parse(localStorage.getItem('auth') || 'null');
                                          return typeof a?.token === 'string' ? a.token : ''; }
                                    catch { return ''; }
                                }''')
                            except Exception:
                                continue
                            if token and time.monotonic() >= next_verify:
                                client = self.client_factory(token)
                                try:
                                    user = client.verify()
                                except PortalError as exc:
                                    next_verify = time.monotonic() + 5
                                    if exc.kind == 'auth':
                                        candidate.evaluate("() => localStorage.removeItem('auth')")
                                        candidate.reload(wait_until='domcontentloaded')
                                        session.login_status = {'state': 'waiting', 'message': '原校园会话已过期，请在官方窗口重新登录。'}
                                    else:
                                        session.login_status = {'state': 'waiting', 'message': '官网暂时无法验证会话，请检查校园网/VPN；原会话已保留。'}
                                    continue
                                if stop.is_set():
                                    return
                                session.portal, session.user = client, user
                                session.login_status = {'state': 'connected', 'message': '已连接延河课堂'}
                                if self.on_connected:
                                    self.on_connected(session)
                                return
                        page.wait_for_timeout(600)
                    if not stop.is_set():
                        raise PortalError('登录等待已超时，请重新打开官方登录窗口。', 'browser', 408)
                finally:
                    browser.close()
        except PortalError as exc:
            session.login_status = {'state': 'error', 'message': str(exc)}
        except Exception:
            session.login_status = {'state': 'error', 'message': '官方登录窗口未能完成连接，请检查网络后重试，或使用登录状态导入。'}
        finally:
            self._profile_lock.release()
            if stop.is_set() and session.portal is None:
                session.login_status = {'state': 'disconnected', 'message': '登录已取消'}

    def close(self):
        for stop in self._stops:
            stop.set()
