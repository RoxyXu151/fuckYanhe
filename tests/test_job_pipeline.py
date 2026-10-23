import json
import threading
import time
import pytest
from pathlib import Path
from types import SimpleNamespace

from yanhe_sentinel.web.jobs import JobRunner, atomic_json
from yanhe_sentinel.web.models import JobRequest, ModelSettings


def settings():
    return ModelSettings(asr_deployment='self-hosted', event_deployment='self-hosted',
        asr_base_url='http://127.0.0.1:18765/v1', asr_model='fixture-asr',
        event_base_url='http://127.0.0.1:18766/v1', event_model='fixture-text',
        event_provider='openai-compatible')


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Pipeline did not reach expected state')


def test_parallel_downloads_keep_going_while_analysis_is_busy(tmp_path):
    release_downloads, release_analysis = threading.Event(), threading.Event()
    download_started, analyzed, downloaded = [], [], []
    class Runner(JobRunner):
        def _prepare(self, identifier):
            download_started.append(identifier)
            assert release_downloads.wait(5)
            downloaded.append(identifier)
        def _execute(self, identifier):
            analyzed.append(identifier)
            assert release_analysis.wait(5)
            self._update(identifier, status='completed', stage='completed')
    runner = Runner(tmp_path / 'jobs', download_workers=2, analysis_workers=1)
    try:
        for i in range(3):
            runner.submit(JobRequest(upload_id=str(i)), settings(), local_path=tmp_path / f'{i}.mp4')
        wait_until(lambda: len(download_started) == 2)
        assert not analyzed
        release_downloads.set()
        wait_until(lambda: len(downloaded) == 3 and len(analyzed) == 1)
        assert sum(j['stage'] == 'waiting_analysis' for j in runner.list()) == 2
        release_analysis.set()
        wait_until(lambda: all(j['status'] == 'completed' for j in runner.list()))
    finally:
        release_downloads.set()
        release_analysis.set()
        runner.close()


def save_pending(root, identifier, *, prepared=False, canceled=False):
    directory = root / identifier
    directory.mkdir(parents=True)
    request = JobRequest(course_id='course', session_id='session', video_id=identifier)
    data = dict(id=identifier, owner='same-account', source='yanhe', status='queued', stage='queued',
        request=request.model_dump(), created_at=1, updated_at=1, logs=[], cancel_requested=canceled,
        model_config=settings().public(), media_file=None)
    if prepared:
        (directory / 'media').mkdir()
        (directory / 'media/lecture.mp4').write_bytes(b'previously downloaded media')
        data['prepared'] = dict(selected='media/lecture.mp4', audio=None, lecture_date='2026-10-22')
    atomic_json(directory / 'job.json', data)


def test_restart_restores_downloaded_analysis_without_login_or_redownload(tmp_path):
    root = tmp_path / 'jobs'
    identifier = 'a' * 32
    save_pending(root, identifier, prepared=True)
    calls = []
    class Runner(JobRunner):
        def _prepare(self, job_id):
            assert self._prepared(job_id)
        def _execute(self, job_id):
            calls.append(job_id)
            self._update(job_id, status='completed', stage='completed')
    runner = Runner(root)
    try:
        assert runner.get(identifier)['status'] == 'interrupted'
        runner.resume_pending(settings())
        wait_until(lambda: runner.get(identifier)['status'] == 'completed')
        assert calls == [identifier]
        runner.resume_pending(settings())
        assert calls == [identifier]
    finally:
        runner.close()


def test_restart_waits_for_same_account_and_does_not_resume_user_cancellations(tmp_path):
    root = tmp_path / 'jobs'
    pending, canceled = 'a' * 32, 'b' * 32
    save_pending(root, pending)
    save_pending(root, canceled, canceled=True)
    runner = JobRunner(root)
    queued = []
    runner.queue.put = queued.append
    try:
        runner.resume_pending(settings())
        runner.resume_pending(settings(), client=SimpleNamespace(user={'owner_key': 'other-account'}))
        assert not queued
        runner.resume_pending(settings(), client=SimpleNamespace(user={'owner_key': 'same-account'}))
        runner.resume_pending(settings(), client=SimpleNamespace(user={'owner_key': 'same-account'}))
        assert queued == [pending]
        assert not runner.get(canceled)['resume_requested']
        persisted = (root / pending / 'job.json').read_text('utf-8')
        assert 'api_key' not in json.loads(persisted)['model_config']
    finally:
        runner.close()


@pytest.mark.parametrize('initial_status', ['partial', 'failed'])
def test_automatic_failed_asr_repair_is_bounded_and_survives_restart(tmp_path, initial_status):
    root = tmp_path / 'jobs'
    identifier = 'a' * 32
    save_pending(root, identifier, prepared=True)
    runner = JobRunner(root)
    profile = settings().model_copy(update={'asr_model': 'Qwen3-ASR-1.7B+ForcedAligner-0.6B'})
    client = SimpleNamespace(user={'owner_key': 'same-account'})
    queued = []
    runner.queue.put = queued.append
    runner.analysis_queue.put_nowait = queued.append
    directory = root / identifier / 'analysis'
    directory.mkdir()
    atomic_json(directory / 'asr_progress.json', {'failed_chunks': 2})
    try:
        runner._update(identifier, status=initial_status, model_config=profile.public())
        assert runner.schedule_repairs(profile, client=client) == 1
        assert runner.schedule_repairs(profile, client=client) == 0
        runner._update(identifier, status='partial')
        assert runner.schedule_repairs(profile, client=client) == 1
        runner._update(identifier, status='partial')
        assert runner.schedule_repairs(profile, client=client) == 0
        assert queued == [identifier, identifier]
    finally:
        runner.close()
    restarted = JobRunner(root)
    try:
        assert restarted.schedule_repairs(profile, client=client) == 0
        assert restarted.get(identifier)['repair_attempts'] == 2
        # Completed ASR is not rerun merely because notice analysis remains partial.
        restarted._update(identifier, repair_attempts=0)
        atomic_json(directory / 'asr_progress.json', {'failed_chunks': 0})
        assert restarted.schedule_repairs(profile, client=client) == 0
    finally:
        restarted.close()
