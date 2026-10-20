"""Service selection tests use synthetic data and intercepted HTTP only."""
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from yanhe_sentinel.asr import ASROptions
from yanhe_sentinel.cloud_asr import CloudRecognizer
from yanhe_sentinel.providers import OpenAICompatibleProvider, ProviderError
from yanhe_sentinel.schema import Segment
from yanhe_sentinel.web.app import create_app
from yanhe_sentinel.web.models import ModelSettings


@pytest.fixture(autouse=True)
def clear_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('YANHE_', 'DEEPSEEK_')) or name in ('GEMINI_API_KEY', 'GROQ_API_KEY'):
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize('asr_hosted,event_hosted', [(False, False), (True, False), (False, True), (True, True)])
def test_mix_cloud_and_self_hosted_services(monkeypatch, asr_hosted, event_hosted):
    """Each service independently enforces authentication and forwards only its own key."""
    np = pytest.importorskip('numpy')
    settings = ModelSettings(
        asr_deployment='self-hosted' if asr_hosted else 'cloud',
        event_deployment='self-hosted' if event_hosted else 'cloud',
        asr_provider='openai-compatible', event_provider='openai-compatible',
        asr_base_url='http://127.0.0.1:9123/v1', asr_model='fixture-asr',
        event_base_url='http://127.0.0.1:9124/v1', event_model='fixture-events',
        asr_api_key='' if asr_hosted else 'fixture-audio-key',
        event_api_key='' if event_hosted else 'fixture-event-key')
    settings.ready()
    for key, value in settings.environment().items():
        if key.startswith(('YANHE_', 'DEEPSEEK_')):
            monkeypatch.setenv(key, value)
    requests=[]
    def post(client, url, **kwargs):
        requests.append((url, kwargs.get('headers', {})))
        if url.endswith('/audio/transcriptions'):
            result={'segments':[{'start':0.1,'end':0.9,'text':'合成测试字幕。'}]}
        else:
            result={'choices':[{'message':{'content':json.dumps({'events':[]})}}]}
        return httpx.Response(200,request=httpx.Request('POST',url),json=result)
    monkeypatch.setattr(httpx.Client,'post',post)
    cues, _=CloudRecognizer(ASROptions(backend='openai-compatible')).transcribe(np.ones(16000,dtype='float32')*.1)
    assert list(cues)[0].text=='合成测试字幕。'
    OpenAICompatibleProvider().extract([Segment(id=0,start=0,end=1,text='合成测试字幕。')])
    assert requests[0][1]==({} if asr_hosted else {'Authorization':'Bearer fixture-audio-key'})
    assert requests[1][1]==({} if event_hosted else {'Authorization':'Bearer fixture-event-key'})


def test_cloud_requires_keys_and_self_hosted_requires_compatible_provider():
    with pytest.raises(ValueError):
        ModelSettings(event_base_url='https://model.example/v1',event_model='test').ready(subtitles=True)
    with pytest.raises(ValidationError):
        ModelSettings(asr_deployment='self-hosted',asr_provider='gemini')
    with pytest.raises(ValidationError):
        ModelSettings(event_deployment='self-hosted',event_provider='deepseek')
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider(base='http://127.0.0.1:9124/v1',model='test')


def test_self_hosted_environment_without_dummy_key(monkeypatch):
    for key,value in {'YANHE_EVENT_DEPLOYMENT':'self-hosted','YANHE_ASR_DEPLOYMENT':'self-hosted',
                      'YANHE_BASE_URL':'http://127.0.0.1:9124/v1','YANHE_MODEL':'fixture-events',
                      'YANHE_ASR_BASE_URL':'http://127.0.0.1:9123/v1','YANHE_ASR_MODEL':'fixture-asr'}.items():
        monkeypatch.setenv(key,value)
    settings=ModelSettings.from_environment()
    assert settings.event_provider=='openai-compatible'
    settings.ready()
    assert not settings.public()['event_key_configured']


def test_endpoint_switch_does_not_reuse_previous_credentials(tmp_path):
    with TestClient(create_app(tmp_path,testing=True)) as client:
        state=client.get('/api/state').json();client.headers['X-CSRF-Token']=state['csrf']
        settings={'asr_provider':'openai-compatible','asr_base_url':'https://audio.example/v1',
                  'asr_model':'test','asr_api_key':'fixture-old-audio',
                  'event_provider':'openai-compatible','event_base_url':'https://text.example/v1',
                  'event_model':'test','event_api_key':'fixture-old-event'}
        assert client.put('/api/settings',json=settings).status_code==200
        assert client.put('/api/settings',json={'event_api_key':''}).json()['event_key_configured']
        response=client.put('/api/settings',json={'event_deployment':'self-hosted',
            'event_base_url':'http://127.0.0.1:9124/v1','event_api_key':''})
        assert response.status_code==200 and not response.json()['event_key_configured']
        assert response.json()['asr_key_configured']
        assert 'fixture-old' not in response.text
        session=next(iter(client.app.state.sessions.values()))
        session.settings.ready()
        assert 'fixture-old-event' not in json.dumps(session.settings.environment())
