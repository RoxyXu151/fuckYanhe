import hashlib
import json
from pathlib import Path
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from yanhe_sentinel.web.archive import RemoteArchive, archive_path, create_archive_app
from yanhe_sentinel.web.app import create_app
from yanhe_sentinel.web.jobs import JobRunner

KEY = 'synthetic-archive-key-' * 3
JOB = 'd' * 32


def storage_fixture(tmp_path):
    remote = TestClient(create_archive_app(tmp_path / 'server', KEY, reserve_bytes=0))
    def transport(request):
        answer = remote.request(request.method, request.url.raw_path.decode(),
                                content=request.read(), headers=dict(request.headers))
        return httpx.Response(answer.status_code, stream=httpx.ByteStream(answer.content), headers=answer.headers)
    archive = RemoteArchive(tmp_path / 'local', 'http://127.0.0.1:18767', KEY,
                            transport=httpx.MockTransport(transport))
    root = archive.root / JOB
    (root / 'media').mkdir(parents=True)
    (root / 'analysis').mkdir()
    (root / 'job.json').write_text(json.dumps({'id': JOB, 'source': 'local', 'status': 'completed',
        'stage': 'completed', 'title': 'Synthetic recording', 'created_at': 0, 'updated_at': 0,
        'media_file': 'media/lecture.mp4', 'request': {}}))
    (root / 'media/lecture.mp4').write_bytes(b'synthetic-video-0123456789')
    (root / 'analysis/transcript.srt').write_text('synthetic source')
    return archive, remote, root


def test_archive_verifies_before_eviction_and_restores(tmp_path):
    archive, remote, root = storage_fixture(tmp_path)
    original = (root / 'media/lecture.mp4').read_bytes()
    result = archive.sync(JOB)
    assert result['files'] == 3
    assert not (root / 'media/lecture.mp4').exists()
    assert (root / 'analysis/transcript.srt').read_text() == 'synthetic source'
    assert (tmp_path / 'server' / JOB / 'media/lecture.mp4').read_bytes() == original
    assert archive.index(JOB)['media/lecture.mp4']['sha256'] == hashlib.sha256(original).hexdigest()
    archive.restore(JOB)
    assert (root / 'media/lecture.mp4').read_bytes() == original
    remote.close()


def test_archive_auth_checksum_path_and_free_space(tmp_path):
    archive, remote, root = storage_fixture(tmp_path)
    assert remote.get('/health').status_code == 401
    headers = {'Authorization': 'Bearer ' + KEY, 'X-Content-SHA256': '0' * 64}
    assert remote.put(f'/files/{JOB}/test.mp4', content=b'wrong checksum', headers=headers).status_code == 422
    assert not (tmp_path / 'server' / JOB / 'test.mp4').exists()
    assert not list((tmp_path / 'server').rglob('*.pending'))
    with pytest.raises(ValueError):
        archive_path(tmp_path, JOB, '../outside')
    with pytest.raises(ValueError):
        archive_path(tmp_path, JOB, '/absolute')
    with pytest.raises(ValueError):
        archive_path(tmp_path, '../escape', 'x')
    with TestClient(create_archive_app(tmp_path / 'full', KEY, reserve_bytes=10**20)) as full:
        assert full.put(f'/files/{JOB}/test.mp4', content=b'x', headers=headers).status_code == 507
    def fail(request):
        return httpx.Response(503)
    archive.transport = httpx.MockTransport(fail)
    with pytest.raises(httpx.HTTPStatusError):
        archive.sync(JOB)
    assert (root / 'media/lecture.mp4').exists()
    remote.close()


def test_workbench_proxies_remote_ranges_and_blocks_private_metadata(tmp_path):
    archive, remote, root = storage_fixture(tmp_path)
    original = (root / 'media/lecture.mp4').read_bytes()
    archive.sync(JOB)
    # Keep the test data inside the workbench's expected jobs directory.
    app = create_app(archive.root.parent, testing=True)
    app.state.jobs.root = archive.root
    app.state.jobs.jobs[JOB] = json.loads((root / 'job.json').read_text())
    app.state.jobs.archive = archive
    with TestClient(app) as client:
        answer = client.get(f'/api/jobs/{JOB}/files/media/lecture.mp4', headers={'Range': 'bytes=3-8'})
        assert answer.status_code == 206 and answer.content == original[3:9]
        assert answer.headers['Content-Range'] == f'bytes 3-8/{len(original)}'
        assert client.get(f'/api/jobs/{JOB}/files/archive-index.json').status_code == 403
        assert client.get(f'/api/jobs/{JOB}/files/unknown.mp4').status_code == 404
    remote.close()


def test_archive_outage_holds_next_job_and_retains_originals(tmp_path):
    runner = JobRunner(tmp_path / 'jobs', download_workers=1)
    release, blocked = threading.Event(), threading.Event()
    first, second = 'a' * 32, 'b' * 32
    calls, executed = [], []
    class Archive:
        def sync(self, job_id, **options):
            calls.append(job_id)
            if len(calls) == 1:
                raise OSError('synthetic storage outage')
            return {'files': 1, 'bytes': 4}
    runner.archive = Archive()
    def paused(timeout):
        blocked.set()
        return release.wait(3)
    runner.stopping.wait = paused
    def execute(job_id):
        executed.append(job_id)
        runner._update(job_id, status='completed', stage='completed', message='complete')
    runner._execute = execute
    for identifier in (first, second):
        folder = runner.root / identifier
        folder.mkdir()
        (folder / 'source.mp4').write_bytes(b'keep')
        runner.jobs[identifier] = {'id': identifier, 'source': 'local', 'status': 'queued',
            'stage': 'queued', 'message': 'waiting', 'created_at': 0}
        runner.cancels[identifier] = threading.Event()
        runner.queue.put(identifier)
    try:
        assert blocked.wait(3)
        assert executed == [first] and runner.get(first)['stage'] == 'archiving'
        assert (runner.root / first / 'source.mp4').read_bytes() == b'keep'
        assert 'synthetic' not in runner.get(first)['message']
        release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not runner.get(second).get('remote_archived'):
            time.sleep(.02)
        assert executed == [first, second] and runner.get(second).get('remote_archived'), 'Archive recovery did not finish within the test deadline'
    finally:
        release.set()
        runner.close()
