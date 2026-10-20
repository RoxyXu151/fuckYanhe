"""Study API contracts use synthetic transcripts and model fixtures only."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yanhe_sentinel.schema import Segment
from yanhe_sentinel.web.app import create_app
from yanhe_sentinel.web.jobs import atomic_json
from yanhe_sentinel.web.learning import grounded, windows

JOB = 'b'*32


def segments(count=4, timed=True):
    return [Segment(id=i+1, start=float(i*4) if timed else None,
                    end=float(i*4+3) if timed else None, text=f'合成知识点 {i+1}：演示定义。') for i in range(count)]


def output(group):
    return {'chapters':[{'title':'合成章节','start_id':group[0]['id'],'end_id':group[-1]['id'],
             'summary':'基于文字稿的测试摘要。','points':[{'text':'合成知识点','segment_ids':[group[0]['id']]}]}]}


def add_job(app, count=4, plain=False):
    root=app.state.jobs.root/JOB
    (root/'analysis').mkdir(parents=True)
    job={'id':JOB,'title':'合成测试课次','source':'local','status':'completed','stage':'completed',
         'created_at':0,'request':{},'media_file':None}
    if plain:
        (root/'media').mkdir()
        (root/'media/source.txt').write_text('合成文字稿一\n合成文字稿二',encoding='utf8')
        job['media_file']='media/source.txt'
    else:
        atomic_json(root/'analysis/transcript.json',{'segments':[s.model_dump() for s in segments(count)]})
    atomic_json(root/'job.json',job)
    app.state.jobs.jobs[JOB]=job
    return root


def configure(client):
    client.headers['X-CSRF-Token']=client.get('/api/state').json()['csrf']
    response=client.put('/api/settings',json={'event_deployment':'self-hosted','event_provider':'openai-compatible',
                 'event_base_url':'http://127.0.0.1:9124/v1','event_model':'fixture-study'})
    assert response.status_code==200


def wait(client,status):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        value=client.get(f'/api/jobs/{JOB}/learning').json()
        if value['status']==status:
            return value
        time.sleep(.025)
    pytest.fail('Study generation did not reach '+status)


@pytest.mark.parametrize('failure',['unknown','gap','overlap','outside','duplicate','empty'])
def test_reject_ungrounded_chapters(failure):
    group=segments()
    raw=output([s.model_dump() for s in group]);chapter=raw['chapters'][0]
    if failure=='unknown':chapter['end_id']=999
    if failure=='gap':chapter['start_id']=2
    if failure=='overlap':raw['chapters'].append(dict(chapter))
    if failure=='outside':chapter['points'][0]['segment_ids']=[999]
    if failure=='duplicate':chapter['points'][0]['segment_ids']=[1,1]
    if failure=='empty':raw['chapters']=[]
    with pytest.raises(ValueError):grounded(raw,group)


def test_grounding_uses_actual_text_and_time_not_model_quotes():
    group=segments();raw=output([s.model_dump() for s in group]);chapter=grounded(raw,group)[0]
    assert chapter['start']==0 and chapter['end']==15
    assert chapter['points'][0]['sources']==[group[0].model_dump()]
    assert sum(map(len,windows(segments(501))))==501
    assert all(len(window)<=160 for window in windows(segments(501)))


def test_study_end_to_end_without_asr_or_school_login(tmp_path,monkeypatch):
    calls=[]
    def complete(provider,messages,validator):
        calls.append(messages)
        group=json.loads(messages[1]['content'])['segments']
        return validator(output(group))
    monkeypatch.setattr('yanhe_sentinel.web.learning.OpenAICompatibleProvider.complete',complete)
    app=create_app(tmp_path,testing=True);directory=add_job(app)
    with TestClient(app) as client:
        configure(client)
        before=client.get(f'/api/jobs/{JOB}/learning').json()
        assert before['status']=='idle' and before['total_segments']==4
        result=client.post(f'/api/jobs/{JOB}/learning',json={'language':'en'})
        assert result.status_code==200
        result=wait(client,'completed')
        assert result['language']=='en' and len(result['chapters'])==1
        assert 'untrusted source data' in calls[0][0]['content']
        assert result['chapters'][0]['points'][0]['sources'][0]['text']==segments()[0].text
        transcript=client.get(f'/api/jobs/{JOB}/transcript?q=2&limit=1').json()
        assert transcript['total']==1 and transcript['items'][0]['id']==2
        assert client.get(f'/api/jobs/{JOB}/transcript?chapter=0').json()['total']==4
        exported=client.get(result['export'])
        assert exported.status_code==200 and '00:00:00' in exported.text
        assert client.get(f'/api/jobs/{JOB}/files/analysis/.learning-cache/private.json').status_code==403
        assert client.post(f'/api/jobs/{JOB}/learning',json={'language':'en'}).status_code==200
        time.sleep(.05);assert len(calls)==1
        assert client.get(f'/api/jobs/{JOB}/transcript?limit=1000').status_code==400
        payload=json.loads((directory/'analysis/transcript.json').read_text('utf8'))
        payload['segments'][0]['text']='合成新版本原文'
        atomic_json(directory/'analysis/transcript.json',payload)
        stale=client.get(f'/api/jobs/{JOB}/learning').json()
        assert stale['status']=='outdated' and not stale['chapters']
    with TestClient(create_app(tmp_path,testing=True)) as client:
        assert client.get(f'/api/jobs/{JOB}/learning').json()['status']=='outdated'


def test_partial_generation_reuses_windows_after_restart(tmp_path,monkeypatch):
    calls=[]
    def interrupted(provider,messages,validator):
        group=json.loads(messages[1]['content'])['segments'];calls.append(group[0]['id'])
        if len(calls)==2:raise RuntimeError('fixture-secret-must-not-leak')
        return validator(output(group))
    monkeypatch.setattr('yanhe_sentinel.web.learning.OpenAICompatibleProvider.complete',interrupted)
    app=create_app(tmp_path,testing=True);add_job(app,170)
    with TestClient(app) as client:
        configure(client);client.post(f'/api/jobs/{JOB}/learning',json={})
        failed=wait(client,'failed')
        assert failed['current']==1 and len(failed['chapters'])==1
        assert 'fixture-secret' not in json.dumps(failed)
    with TestClient(create_app(tmp_path,testing=True)) as client:
        configure(client);client.post(f'/api/jobs/{JOB}/learning',json={})
        completed=wait(client,'completed')
        assert completed['current']==2 and len(completed['chapters'])==2
        assert calls==[1,161,161]


def test_plain_transcript_and_mutation_guards(tmp_path,monkeypatch):
    app=create_app(tmp_path,testing=True);add_job(app,plain=True)
    def complete(provider,messages,validator):
        return validator(output(json.loads(messages[1]['content'])['segments']))
    monkeypatch.setattr('yanhe_sentinel.web.learning.OpenAICompatibleProvider.complete',complete)
    with TestClient(app) as client:
        data=client.get(f'/api/jobs/{JOB}/learning').json()
        assert data['total_segments']==2 and not data['has_timestamps'] and data['video'] is None
        assert client.post(f'/api/jobs/{JOB}/learning',json={}).status_code==403
        assert client.post(f'/api/jobs/{JOB}/learning/cancel').status_code==403
        client.headers['X-CSRF-Token']=client.get('/api/state').json()['csrf']
        assert client.post(f'/api/jobs/{JOB}/learning',json={}).status_code==400
        configure(client)
        assert client.post(f'/api/jobs/{JOB}/learning',json={'language':'invalid'}).status_code==400
        assert client.post(f'/api/jobs/{JOB}/learning',json={}).status_code==200
        completed=wait(client,'completed')
        assert completed['chapters'][0]['start'] is None
        assert client.get(f'/api/jobs/{JOB}/transcript?chapter=10').status_code==400

def test_cancel_retains_inflight_success_and_resume_uses_it(tmp_path,monkeypatch):
    import threading
    entered,release=threading.Event(),threading.Event()
    calls=[]
    def complete(provider,messages,validator):
        group=json.loads(messages[1]['content'])['segments'];calls.append(group[0]['id'])
        if len(calls)==1:
            entered.set()
            assert release.wait(5)
        return validator(output(group))
    monkeypatch.setattr('yanhe_sentinel.web.learning.OpenAICompatibleProvider.complete',complete)
    app=create_app(tmp_path,testing=True);add_job(app,170)
    with TestClient(app) as client:
        configure(client);client.post(f'/api/jobs/{JOB}/learning',json={})
        assert entered.wait(5)
        # Repeated starts do not queue a second request for the same lecture.
        client.post(f'/api/jobs/{JOB}/learning',json={})
        assert client.post(f'/api/jobs/{JOB}/learning/cancel').status_code==200
        release.set();interrupted=wait(client,'interrupted')
        assert interrupted['current']==1 and calls==[1]
        client.post(f'/api/jobs/{JOB}/learning',json={});wait(client,'completed')
        assert calls==[1,161]

def test_model_receives_grounding_error_before_retry(tmp_path,monkeypatch):
    import httpx
    requests=[]
    monkeypatch.setattr('yanhe_sentinel.providers.time.sleep',lambda _:None)
    def post(client,url,**kwargs):
        messages=kwargs['json']['messages'];requests.append(json.loads(json.dumps(messages)))
        group=json.loads(messages[1]['content'])['segments'];result=output(group)
        if len(requests)==1:result['chapters'][0]['points'][0]['segment_ids']=[999]
        return httpx.Response(200,request=httpx.Request('POST',url),json={'choices':[{'message':{'content':json.dumps(result)}}]})
    app=create_app(tmp_path,testing=True);add_job(app)
    with TestClient(app) as client:
        configure(client)
        # Patch only the provider's outgoing client after local TestClient setup.
        monkeypatch.setattr(httpx.Client,'post',post)
        app.state.study.start(JOB,next(iter(app.state.sessions.values())).settings,
                              __import__('yanhe_sentinel.web.learning',fromlist=['StudyRequest']).StudyRequest())
        completed=wait(client,'completed')
        assert completed['current']==1 and len(requests)==2
        assert 'Validation failed' in requests[1][-1]['content']
        repair=json.loads(requests[1][-1]['content'])
        assert repair['required_output_shape']['chapters'][0]['start_id']==1
        assert repair['required_output_shape']['chapters'][0]['end_id']==4
        assert len(requests[1])==2
