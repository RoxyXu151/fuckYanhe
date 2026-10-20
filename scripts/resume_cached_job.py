"""Resume already downloaded media from the local machine, without portal access.

Run with the WebUI stopped. This is a local operator utility, not a web endpoint;
it cannot fetch a new recording or change web account authorization rules.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import threading
import time

from yanhe_sentinel.schema import Report
from yanhe_sentinel.web.jobs import JobRunner, TERMINAL
from yanhe_sentinel.web.models import ModelSettings

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('job_id')
parser.add_argument('--root', type=Path, default=Path('runs/webui/jobs'))
args = parser.parse_args()
settings = ModelSettings.from_environment()
settings.ready()
runner = JobRunner(args.root)
try:
    job = runner.get(args.job_id)
    if job['status'] not in TERMINAL or job['source'] != 'yanhe':
        raise ValueError('Only a stopped downloaded Yanhe job can be resumed')
    directory = runner.root / args.job_id
    video = (directory / (job['media_file'] or 'missing')).resolve()
    video.relative_to(directory)
    if not video.is_file():
        raise ValueError('The original downloaded recording is missing')
    output = directory / 'analysis'
    previous = Report.model_validate_json((output / 'events.json').read_text('utf-8'))
    date = previous.lecture_date
    if date is None:
        raise ValueError('The original report must record the lecture date')
    history = directory / 'history' / str(time.time_ns())
    history.mkdir(parents=True)
    for name in ('events.json', 'quality.json', 'report.md', 'asr_quality.json', 'asr_chunks.json'):
        source = output / name
        if source.is_file():
            shutil.copyfile(source, history / name)
    command = [sys.executable, '-m', 'yanhe_sentinel', 'analyze', '--video', str(video),
        '--provider', settings.event_provider, '--asr-provider', settings.asr_provider,
        '--asr-model', settings.asr_model, '--output', str(output), '--lecture-date', str(date)]
    source_kind = job['request']['audio_source']
    if source_kind != 'original':
        audio = directory / 'media' / ('microphone.aac' if source_kind == 'microphone' else 'camera.mp4')
        if not audio.is_file():
            raise ValueError('The selected downloaded audio source is missing')
        command += ['--audio-source', str(audio), '--audio-offset', str(job['request']['audio_offset'])]
    if job['request']['vision']:
        raise ValueError('This utility supports audio analysis only')
    if job['request']['export_clips']:
        command.append('--export-clips')
    runner.cancels[args.job_id] = threading.Event()
    runner._update(args.job_id, status='running', stage='preparing', cancel_requested=False,
        model_config=settings.public(), message='正在使用 远程模型服务器 处理已下载音频')
    code = runner._command(args.job_id, command, settings.environment(),
        [settings.asr_api_key.get_secret_value(), settings.event_api_key.get_secret_value()])
    report = Report.model_validate_json((output / 'events.json').read_text('utf-8'))
    status = 'completed' if code == 0 and report.analysis_status == 'completed' else 'partial'
    runner._update(args.job_id, status=status, stage=status, current=0, total=0,
        message=f'发现 {len(report.events)} 个需留意的事项', event_count=len(report.events),
        detection_status=report.detection_status, simulated=report.simulated)
    print(json.dumps({'status': status, 'events': len(report.events), 'exit_code': code}))
    sys.exit(code)
finally:
    runner.close()
