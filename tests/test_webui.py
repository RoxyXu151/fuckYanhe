import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import zipfile

import pytest

pytest.importorskip('fastapi')
pytest.importorskip('playwright')
from fastapi.testclient import TestClient

from yanhe_sentinel.portal import YanheClient
from yanhe_sentinel.web.app import create_app
from yanhe_sentinel.web.models import ModelSettings
from test_portal import FakePlatform, TOKEN, hls_media
import httpx


@pytest.fixture(autouse=True)
def clear_model_environment(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith(('YANHE_', 'DEEPSEEK_')) or key in ('GEMINI_API_KEY', 'GROQ_API_KEY'):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def cloud_server():
    calls, entered, block = [], threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            calls.append(self.path)
            if self.path.endswith('/audio/transcriptions'):
                assert b'RIFF' in body
                entered.set()
                if block.is_set():
                    time.sleep(10)
                data = {'segments': [{'start': .2, 'end': 1.6, 'text': '第三章作业下周五提交。'}]}
            elif self.path.endswith('/interactions'):
                payload = json.loads(body)
                assert payload['input'][0]['type'] == 'audio' and payload['store'] is False
                data = {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
                    {'type': 'text', 'text': json.dumps({'segments': [{'start': .2, 'end': 1.6, 'text': '第三章作业下周五提交。'}]})}]}]}
            else:
                payload = json.loads(body)
                message = payload['messages'][1]['content']
                if isinstance(message, list):
                    assert message[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
                    events = []
                else:
                    segment = json.loads(message)['segments'][0]
                    events = [{'type': 'homework', 'polarity': 'affirmed', 'tense': 'future', 'status': 'actionable',
                        'summary': '完成并提交第三章作业', 'action': '完成第三章作业并提交', 'due_text': '下周五',
                        'segment_ids': [segment['id']], 'quote': segment['text']}]
                data = {'choices': [{'message': {'content': json.dumps({'events': events}, ensure_ascii=False)}}]}
            raw = json.dumps(data, ensure_ascii=False).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (ConnectionError, OSError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {'base': f'http://127.0.0.1:{server.server_port}', 'calls': calls, 'entered': entered, 'block': block}
    server.shutdown()
    server.server_close()


def bootstrap(client):
    state = client.get('/api/state').json()
    client.headers['X-CSRF-Token'] = state['csrf']
    return state


def wait_for_job(client, job_id, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get('/api/jobs/' + job_id).json()
        if job['status'] in ('completed', 'partial', 'failed', 'canceled'):
            return job
        time.sleep(.1)
    pytest.fail('Job did not finish within test deadline')


def settings_for(server, backend='openai-compatible'):
    return {'asr_provider': backend, 'asr_base_url': server['base'] + ('/v1beta' if backend == 'gemini' else '/v1'),
        'asr_model': 'fixture-audio', 'asr_api_key': 'private-audio-test-key', 'event_provider': 'openai-compatible',
        'event_base_url': server['base'] + '/v1', 'event_model': 'fixture-text-vision',
        'event_api_key': 'private-text-test-key'}


@pytest.mark.parametrize('backend', ['openai-compatible', 'gemini'])
def test_full_authenticated_video_to_report_web_api(hls_media, cloud_server, tmp_path, backend):
    platform = FakePlatform(hls_media)
    app = create_app(tmp_path / 'workspace', testing=True,
                     client_factory=lambda token: YanheClient(token, transport=httpx.MockTransport(platform), min_interval=0))
    with TestClient(app) as client:
        state = bootstrap(client)
        assert not state['auth']['connected']
        assert client.get('/').status_code == 200
        assert client.get('/api/courses').status_code == 401
        assert client.post('/api/auth/token', json={'token': TOKEN}).status_code == 200
        assert client.get('/api/courses').json()['items'][0]['id'] == '100'
        sessions = client.get('/api/courses/100/sessions').json()
        assert sessions['items'][0]['session_id'] == '200'
        result = client.put('/api/settings', json=settings_for(cloud_server, backend))
        assert result.status_code == 200 and result.json()['asr_key_configured']
        assert 'private-audio-test-key' not in result.text
        body = {'course_id': '100', 'session_id': '200', 'video_id': '300', 'signal': 'screen', 'vision': True}
        response = client.post('/api/jobs', json=body)
        assert response.status_code == 200, response.text
        job_id = response.json()['id']
        finished = wait_for_job(client, job_id)
        assert finished['status'] == 'completed', finished
        report_response = client.get(f'/api/jobs/{job_id}/report')
        assert report_response.status_code == 200
        output = report_response.json()
        assert output['report']['events'][0]['type'] == 'homework'
        assert output['report']['media']['asr_provider'] == backend
        assert output['report']['visual']['analyzed_frame_seconds']
        assert output['links']['subtitles'] and output['links']['video']
        assert client.get(output['links']['subtitles']).status_code == 200
        video = client.get(output['links']['video'], headers={'Range': 'bytes=0-31'})
        assert video.status_code == 206 and len(video.content) == 32
        event_id = output['report']['events'][0]['event_id']
        clip = output['event_links'][event_id]['clip']
        assert client.get(clip).content.startswith(b'RIFF')
        assert client.put(f'/api/jobs/{job_id}/reviews/{event_id}', json={'reviewed': True}).status_code == 200
        assert client.get(f'/api/jobs/{job_id}/report').json()['reviews'][event_id]
        bundle = client.get(f'/api/jobs/{job_id}/export')
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert 'analysis/events.json' in archive.namelist()
            assert not any('.cache' in file for file in archive.namelist())
        request_count = len(cloud_server['calls'])
        duplicate = client.post('/api/jobs', json=body).json()
        assert duplicate['id'] == job_id
        assert len(cloud_server['calls']) == request_count
        persisted = (tmp_path / 'workspace/jobs' / job_id / 'job.json').read_text('utf-8')
        assert TOKEN not in persisted and 'private-audio-test-key' not in persisted and 'private-text-test-key' not in persisted
        assert 'private-audio-test-key' not in client.get('/api/state').text
        assert client.delete('/api/auth').status_code == 200
        assert not client.get('/api/auth').json()['connected']


def test_csrf_origin_and_host_protection(tmp_path):
    with TestClient(create_app(tmp_path, testing=True)) as client:
        bootstrap(client)
        assert client.post('/api/demo', headers={'X-CSRF-Token': ''}).status_code == 403
        assert client.post('/api/demo', headers={'Origin': 'https://untrusted.example'}).status_code == 403
        assert client.get('/api/state', headers={'Host': 'untrusted.example'}).status_code == 403


def test_secrets_stay_in_session_and_empty_fields_retain_them(tmp_path, cloud_server):
    with TestClient(create_app(tmp_path, testing=True)) as client:
        bootstrap(client)
        assert client.put('/api/settings', json=settings_for(cloud_server)).status_code == 200
        assert client.put('/api/settings', json={'event_api_key': ''}).json()['event_key_configured']
        assert 'private-text-test-key' not in client.get('/api/state').text
        invalid = client.post('/api/auth/token', json={'token': {'secret': 'do-not-echo-this-key'}})
        assert invalid.status_code == 400 and 'do-not-echo-this-key' not in invalid.text
        assert not client.delete('/api/settings/secrets').json()['event_key_configured']


def test_local_subtitle_upload_runs_without_portal(tmp_path, cloud_server):
    with TestClient(create_app(tmp_path / 'app', testing=True)) as client:
        bootstrap(client)
        client.put('/api/settings', json=settings_for(cloud_server))
        subtitle = '1\n00:00:01,000 --> 00:00:03,000\n第三章作业下周五提交。\n'
        upload = client.post('/api/uploads', files={'file': ('sample.srt', subtitle.encode(), 'text/plain')})
        assert upload.status_code == 200
        submitted = client.post('/api/jobs', json={'upload_id': upload.json()['id']})
        assert submitted.status_code == 200
        job = wait_for_job(client, submitted.json()['id'])
        assert job['status'] == 'completed'
        assert client.get(f'/api/jobs/{job["id"]}/report').json()['report']['events']


def test_demo_requires_no_credentials_and_reports_simulation(tmp_path):
    with TestClient(create_app(tmp_path, testing=True)) as client:
        bootstrap(client)
        response = client.post('/api/demo')
        job = wait_for_job(client, response.json()['id'])
        report = client.get(f'/api/jobs/{job["id"]}/report').json()['report']
        assert job['status'] == 'completed'
        assert report['simulated'] and len(report['events']) == 4


def test_session_expiry_does_not_expose_platform_token(tmp_path):
    platform = FakePlatform()
    app = create_app(tmp_path, testing=True,
        client_factory=lambda token: YanheClient(token, transport=httpx.MockTransport(platform), min_interval=0))
    with TestClient(app) as client:
        bootstrap(client)
        client.post('/api/auth/token', json={'token': TOKEN})
        platform.code = 61101113
        response = client.get('/api/courses')
        assert response.status_code == 401 and TOKEN not in response.text
        assert not client.get('/api/auth').json()['connected']


def test_environment_profiles_keep_credentials_separate(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'deepseek-key')
    monkeypatch.setenv('YANHE_API_KEY', 'another-key')
    monkeypatch.setenv('YANHE_BASE_URL', 'https://another.example/v1')
    settings = ModelSettings.from_environment()
    assert settings.event_base_url == 'https://api.deepseek.com'
    assert settings.event_api_key.get_secret_value() == 'deepseek-key'


def test_cancel_stops_analysis_process(tmp_path, cloud_server):
    cloud_server['block'].set()
    from yanhe_sentinel.media_tools import ffmpeg_executable, run_tool
    source = tmp_path / 'tone.wav'
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
              '-i', 'sine=frequency=220:sample_rate=16000', '-t', '2', source])
    with TestClient(create_app(tmp_path / 'app', testing=True)) as client:
        bootstrap(client)
        client.put('/api/settings', json=settings_for(cloud_server))
        upload = client.post('/api/uploads', files={'file': ('tone.wav', source.read_bytes(), 'audio/wav')}).json()
        job = client.post('/api/jobs', json={'upload_id': upload['id']}).json()
        assert cloud_server['entered'].wait(10)
        assert client.post(f'/api/jobs/{job["id"]}/cancel').status_code == 200
        assert wait_for_job(client, job['id'], timeout=12)['status'] == 'canceled'
