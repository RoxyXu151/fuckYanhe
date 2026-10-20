"""Product behavior checks: private persistence, non-generative checks and recovery."""
import json
import os
from pathlib import Path
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from yanhe_sentinel.web.app import create_app
from yanhe_sentinel.web.jobs import atomic_json
from yanhe_sentinel.web.models import ModelSettings
from yanhe_sentinel.web.product import Preferences
from test_learning import JOB, add_job, configure, output, wait


@pytest.fixture(autouse=True)
def clear_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('YANHE_', 'DEEPSEEK_')) or name in ('GEMINI_API_KEY','GROQ_API_KEY'):
            monkeypatch.delenv(name,raising=False)


def test_remember_only_nonsecret_settings_and_never_forward_environment_key(tmp_path,monkeypatch):
    monkeypatch.setenv('YANHE_API_KEY','fixture-env-private')
    monkeypatch.setenv('YANHE_BASE_URL','https://original.example/v1')
    monkeypatch.setenv('YANHE_MODEL','original-model')
    with TestClient(create_app(tmp_path,testing=True)) as client:
        client.headers['X-CSRF-Token']=client.get('/api/state').json()['csrf']
        saved=client.put('/api/settings',json={'event_provider':'openai-compatible',
            'event_base_url':'https://new.example/v1','event_model':'new-model',
            'event_api_key':'fixture-new-private','remember_defaults':True})
        assert saved.status_code==200 and saved.json()['defaults_saved']
        content=(tmp_path/'preferences.json').read_text('utf8')
        assert 'private' not in content and 'api_key' not in content
    with TestClient(create_app(tmp_path,testing=True)) as client:
        state=client.get('/api/state').json()
        assert state['settings']['event_model']=='new-model'
        assert not state['settings']['event_key_configured']
        client.headers['X-CSRF-Token']=state['csrf']
        assert client.put('/api/settings',json={'remember_defaults':False}).status_code==200
        assert not (tmp_path/'preferences.json').exists()
    atomic_json(tmp_path/'preferences.json',{'event_api_key':'fixture-file-must-not-load'})
    assert Preferences(tmp_path).load().event_api_key.get_secret_value()=='fixture-env-private'


@pytest.mark.parametrize('status,expected',[(200,'available'),(401,'auth_error'),(429,'rate_limited'),(404,'unsupported'),(503,'unavailable'),(302,'redirect')])
def test_connection_check_is_read_only_and_sanitized(tmp_path,monkeypatch,status,expected):
    app=create_app(tmp_path,testing=True)
    with TestClient(app) as client:
        configure(client)
        calls=[]
        def get(transport,url,**kwargs):
            calls.append((url,kwargs))
            return httpx.Response(status,request=httpx.Request('GET',url),json={'data':[{'id':'fixture-study'}],'sensitive':'fixture-private-body'})
        # TestClient.get must remain untouched; patch only outgoing HTTP via its base class method.
        original=httpx.Client.get
        def dispatch(transport,url,**kwargs):
            if isinstance(transport,TestClient):return original(transport,url,**kwargs)
            return get(transport,url,**kwargs)
        monkeypatch.setattr(httpx.Client,'get',dispatch)
        response=client.post('/api/settings/check/event',json={})
        assert response.json()['status']==expected
        assert 'private' not in response.text
        assert len(calls)==1 and calls[0][0].endswith('/models')
        assert not any(key in calls[0][1] for key in ('json','files','data'))
        assert client.post('/api/settings/check/event',json={'event_base_url':'http://private.example/v1'}).status_code==400


def test_notebook_progress_bookmarks_survive_restart_and_detect_changed_source(tmp_path):
    app=create_app(tmp_path,testing=True);directory=add_job(app)
    with TestClient(app) as client:
        csrf=client.get('/api/state').json()['csrf']
        assert client.put(f'/api/jobs/{JOB}/study-note',json={'text':'private note'}).status_code==403
        client.headers['X-CSRF-Token']=csrf
        assert client.put(f'/api/jobs/{JOB}/study-progress',json={'position':9.2,'speed':1.5}).status_code==200
        assert client.put(f'/api/jobs/{JOB}/study-note',json={'text':'<script>my private note</script>'}).status_code==200
        bookmark=client.post(f'/api/jobs/{JOB}/bookmarks',json={'segment_id':2}).json()['bookmarks'][0]
        assert bookmark['position']==4 and bookmark['quote'].startswith('合成知识点 2')
        assert client.post(f'/api/jobs/{JOB}/bookmarks',json={'segment_id':999}).status_code==400
        assert client.post(f'/api/jobs/{JOB}/bookmarks',json={'segment_id':2,'quote':'invented'}).status_code==400
        assert client.put(f'/api/jobs/{JOB}/study-progress',json={'position':-1}).status_code==400
        assert client.get(f'/api/jobs/{JOB}/files/study-records.json').status_code==403
    with TestClient(create_app(tmp_path,testing=True)) as client:
        client.headers['X-CSRF-Token']=client.get('/api/state').json()['csrf']
        records=client.get(f'/api/jobs/{JOB}/study-records').json()
        assert records['position']==9.2 and records['speed']==1.5 and len(records['bookmarks'])==1
        assert '<script>' in records['note'] and not records['progress_stale']
        source=json.loads((directory/'analysis/transcript.json').read_text('utf8'))
        source['segments'][1]['start']=4.5
        atomic_json(directory/'analysis/transcript.json',source)
        assert client.get(f'/api/jobs/{JOB}/study-records').json()['bookmarks'][0]['stale']
        source['segments'][1]['text']='合成修订原文'
        atomic_json(directory/'analysis/transcript.json',source)
        changed=client.get(f'/api/jobs/{JOB}/study-records').json()
        assert changed['progress_stale'] and changed['bookmarks'][0]['stale']
        refreshed=client.put(f'/api/jobs/{JOB}/study-progress',json={'position':5,'speed':1}).json()
        assert not refreshed['progress_stale'] and refreshed['bookmarks'][0]['stale']
        assert not client.delete(f'/api/jobs/{JOB}/bookmarks/{bookmark["id"]}').json()['bookmarks']


def test_failed_regeneration_can_restore_completed_notes_without_model_call(tmp_path,monkeypatch):
    calls=[]
    def complete(provider,messages,validator):
        calls.append(1)
        if len(calls)>1:raise RuntimeError('fixture-service-secret')
        return validator(output(json.loads(messages[1]['content'])['segments']))
    monkeypatch.setattr('yanhe_sentinel.web.learning.OpenAICompatibleProvider.complete',complete)
    app=create_app(tmp_path,testing=True);add_job(app)
    with TestClient(app) as client:
        configure(client);client.post(f'/api/jobs/{JOB}/learning',json={});first=wait(client,'completed')
        client.post(f'/api/jobs/{JOB}/learning',json={'force':True});failed=wait(client,'failed')
        assert failed['previous_available'] and 'secret' not in json.dumps(failed)
        restored=client.post(f'/api/jobs/{JOB}/learning/restore').json()
        assert restored['status']=='completed' and restored['chapters']==first['chapters'] and len(calls)==2


def test_batch_retry_deduplicates_and_does_not_rerun_completed_jobs(tmp_path):
    app=create_app(tmp_path,testing=True);directory=add_job(app)
    original=app.state.jobs.jobs[JOB]
    pending={**original,'id':'c'*32,'status':'canceled','source':'demo','request':{'upload_id':'fixture'},'owner':'local'}
    (app.state.jobs.root/pending['id']).mkdir()
    app.state.jobs.jobs[pending['id']]=pending
    queued=[];app.state.jobs.queue.put=lambda identifier:queued.append(identifier)
    with TestClient(app) as client:
        configure(client)
        result=client.post('/api/jobs/retry-many',json={'job_ids':[JOB,pending['id'],pending['id'],'x'*32]}).json()
        assert len(result['jobs'])==1 and result['skipped']==[JOB] and len(result['errors'])==1
        assert queued==[pending['id']]
        app.state.jobs.jobs[pending['id']]['status']='canceled'
        outcomes=[]
        def retry():
            try:app.state.jobs.retry(pending['id'],ModelSettings());outcomes.append('queued')
            except ValueError:outcomes.append('rejected')
        threads=[threading.Thread(target=retry) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        assert sorted(outcomes)==['queued','rejected']


def test_health_and_connection_checks_enforce_csrf(tmp_path):
    with TestClient(create_app(tmp_path,testing=True)) as client:
        health=client.get('/api/health').json()
        assert health['storage_ready'] and isinstance(health['free_bytes'],int)
        assert str(tmp_path) not in json.dumps(health)
        assert client.post('/api/settings/check/event',json={}).status_code==403
