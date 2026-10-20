import json
from pathlib import Path
import time
import uuid

from fastapi.testclient import TestClient
import httpx

from yanhe_sentinel.portal import PortalError, YanheClient
from yanhe_sentinel.web.app import create_app
from yanhe_sentinel.web.library import CourseLibrary
from test_portal import FakePlatform, TOKEN, hls_media
from test_webui import bootstrap, clear_model_environment, cloud_server, settings_for, wait_for_job


class QueueRecorder:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        self.jobs = []

    def list(self):
        return self.jobs

    def submit(self, request, settings, *, client=None, title=''):
        job = {'id': uuid.uuid4().hex, 'source': 'yanhe', 'status': 'queued', 'stage': 'queued',
            'owner': client.user['owner_key'], 'request': request.model_dump(), 'title': title,
            'model_config': settings.public(), 'created_at': time.time(), 'updated_at': time.time()}
        self.jobs.append(job)
        return job

    def cancel_owner(self, owner):
        for job in self.jobs:
            if job['owner'] == owner and job['status'] in ('queued', 'running'):
                job['status'] = 'canceled'

    def close(self):
        pass


def configured_client(tmp_path, platform=None):
    platform = platform or FakePlatform()
    runner = QueueRecorder(tmp_path/'jobs')
    app = create_app(tmp_path/'workspace', testing=True, runner=runner,
        client_factory=lambda token: YanheClient(token, transport=httpx.MockTransport(platform), min_interval=0))
    return app, runner


def authenticate(client):
    bootstrap(client)
    assert client.post('/api/auth/token', json={'token': TOKEN}).status_code == 200
    assert client.put('/api/settings', json=settings_for({'base': 'http://127.0.0.1:12345'})).status_code == 200


def test_course_metadata_survives_logout_and_restart_without_credentials(tmp_path):
    app, runner = configured_client(tmp_path)
    with TestClient(app) as client:
        authenticate(client)
        client.get('/api/courses')
        client.get('/api/courses/100/sessions')
        assert client.patch('/api/library/100', json={'notes': '复核进度', 'starred': True}).status_code == 200
        client.delete('/api/auth')
        course = client.get('/api/library').json()['items'][0]
        assert course['title'] == '合成课堂' and course['total_sessions'] == 1
        assert course['notes'] == '复核进度' and course['starred']
    second = create_app(tmp_path/'workspace', testing=True)
    with TestClient(second) as client:
        assert client.get('/api/state').json()['library'][0]['notes'] == '复核进度'
    raw = (tmp_path/'workspace/library.json').read_text('utf-8')
    assert TOKEN not in raw and 'private-audio-test-key' not in raw and 'https://' not in raw


def test_library_whitelists_metadata_and_archive_does_not_remove_reports(tmp_path):
    library = CourseLibrary(tmp_path/'library.json')
    library.remember({'id': '100', 'title': '测试', 'token': 'private', 'professors': []},
        [{'course_id': '100', 'session_id': '200', 'video_id': '300', 'title': '课次',
          'main_url': 'https://private.invalid/?token=private', 'signals': ['screen']}])
    job = {'id': 'fixture-job', 'source': 'yanhe', 'request': {'course_id': '100',
        'session_id': '200', 'video_id': '300'}, 'title': '课次', 'status': 'completed',
        'event_count': 3, 'updated_at': 1}
    library.update('100', {'archived': True}, [job])
    saved = library.snapshot([job])[0]
    assert saved['archived'] and saved['completed_sessions'] == 1 and saved['event_count'] == 3
    assert saved['sessions'][0]['completed_job_id'] == 'fixture-job'
    assert 'private' not in (tmp_path/'library.json').read_text('utf-8')


def test_cross_course_batch_is_deduplicated_and_does_not_echo_keys(tmp_path):
    app, runner = configured_client(tmp_path)
    with TestClient(app) as client:
        authenticate(client)
        result = client.post('/api/batches', json={'course_ids': ['100', '101', '100']})
        assert result.status_code == 200, result.text
        assert len(result.json()['jobs']) == 2
        assert {j['request']['course_id'] for j in runner.jobs} == {'100', '101'}
        assert all(j['request']['signal'] == 'screen' for j in runner.jobs)
        duplicate = client.post('/api/batches', json={'course_ids': ['100', '101']}).json()
        assert duplicate['jobs'] == [] and len(duplicate['reused']) == 2
        assert len(runner.jobs) == 2
        assert 'private-audio-test-key' not in result.text and TOKEN not in result.text


def test_batch_skips_completed_recording_even_if_audio_options_changed(tmp_path):
    app, runner = configured_client(tmp_path)
    with TestClient(app) as client:
        authenticate(client)
        client.post('/api/batches', json={'course_ids': ['100']})
        runner.jobs[0]['status'] = 'completed'
        runner.jobs[0]['request']['audio_source'] = 'microphone'
        result = client.post('/api/batches', json={'course_ids': ['100']}).json()
        assert not result['jobs'] and len(result['skipped']) == 1
        assert len(runner.jobs) == 1
        client.post('/api/batches', json={'course_ids': ['100'], 'skip_completed': False})
        assert len(runner.jobs) == 2 and runner.jobs[-1]['request']['force']


def test_batch_requires_csrf_login_and_valid_models_before_queueing(tmp_path):
    app, runner = configured_client(tmp_path)
    with TestClient(app) as client:
        assert client.post('/api/batches', json={'course_ids': ['100']}).status_code == 403
        bootstrap(client)
        assert client.post('/api/batches', json={'course_ids': ['100']}).status_code == 401
        client.post('/api/auth/token', json={'token': TOKEN})
        assert client.post('/api/batches', json={'course_ids': ['100']}).status_code == 400
        assert runner.jobs == []


def test_library_rejects_unknown_fields_and_preserves_completed_version(tmp_path):
    app, runner = configured_client(tmp_path)
    with TestClient(app) as client:
        authenticate(client)
        client.get('/api/courses/100/sessions')
        assert client.patch('/api/library/100', json={'token': 'not-allowed'}).status_code == 400
        client.post('/api/batches', json={'course_ids': ['100']})
        first = runner.jobs[0]
        first['status'] = 'completed'
        client.post('/api/batches', json={'course_ids': ['100'], 'skip_completed': False})
        saved = client.get('/api/library').json()['items'][0]
        assert saved['completed_sessions'] == 1
        assert saved['sessions'][0]['completed_job_id'] == first['id']
        assert saved['sessions'][0]['latest_job_id'] != first['id']


def test_batch_runs_real_media_pipeline_and_can_be_viewed_offline(hls_media, cloud_server, tmp_path):
    platform = FakePlatform(hls_media)
    app = create_app(tmp_path/'workspace', testing=True,
        client_factory=lambda token: YanheClient(token, transport=httpx.MockTransport(platform), min_interval=0))
    with TestClient(app) as client:
        authenticate(client)
        client.put('/api/settings', json=settings_for(cloud_server))
        result = client.post('/api/batches', json={'course_ids': ['100']}).json()
        job_id = result['jobs'][0]['id']
        assert wait_for_job(client, job_id)['status'] == 'completed'
        client.delete('/api/auth')
        course = client.get('/api/library').json()['items'][0]
        assert course['completed_sessions'] == 1
        assert client.get(f'/api/jobs/{job_id}/report').json()['report']['events']
        assert client.get(f'/api/jobs/{job_id}/export').status_code == 200


def test_batch_expired_auth_clears_connection_and_queues_nothing(tmp_path, monkeypatch):
    app, runner = configured_client(tmp_path)
    with TestClient(app) as client:
        authenticate(client)
        portal = next(iter(app.state.sessions.values())).portal
        def expired(_):
            raise PortalError('登录已过期', 'auth', 401)
        monkeypatch.setattr(portal, 'sessions', expired)
        assert client.post('/api/batches', json={'course_ids': ['100']}).status_code == 401
        assert not client.get('/api/auth').json()['connected']
        assert not runner.jobs


def test_batch_permission_failure_is_local_and_size_limit_is_preflight(tmp_path, monkeypatch):
    app, runner = configured_client(tmp_path)
    with TestClient(app) as client:
        authenticate(client)
        portal = next(iter(app.state.sessions.values())).portal
        original = portal.sessions
        def selective(course_id):
            if course_id == '999':
                raise PortalError('server-private-detail', 'permission', 403)
            return original(course_id)
        monkeypatch.setattr(portal, 'sessions', selective)
        response = client.post('/api/batches', json={'course_ids': ['999', '100']})
        assert len(response.json()['jobs']) == 1 and len(response.json()['errors']) == 1
        assert response.json()['errors'][0]['course_id'] == '999'
        assert 'server-private-detail' not in response.text
        runner.jobs.clear()
        sample = original('100')
        recording = sample['items'][0]
        sample['items'] = [{**recording, 'video_id': str(1000+i)} for i in range(101)]
        monkeypatch.setattr(portal, 'sessions', lambda _: sample)
        assert client.post('/api/batches', json={'course_ids': ['100']}).status_code == 400
        assert not runner.jobs
