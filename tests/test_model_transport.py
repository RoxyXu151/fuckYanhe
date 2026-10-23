import httpx
import pytest

from yanhe_sentinel.cloud_asr import post_with_retry


@pytest.mark.parametrize('url,trust,read_timeout', [
    ('http://127.0.0.1:18768/v1/audio/transcriptions', False, 600),
    ('http://localhost:18768/v1/audio/transcriptions', False, 600),
    ('https://api.example.org/v1/audio/transcriptions', True, 180),
])
def test_upload_proxy_and_wait_policy(monkeypatch, url, trust, read_timeout):
    original = httpx.Client
    calls = []
    def client(**kwargs):
        calls.append(kwargs)
        return original(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'segments': []})))
    monkeypatch.setattr(httpx, 'Client', client)
    assert post_with_retry(url, files={'file': ('test.wav', b'audio')}) == {'segments': []}
    assert calls[0]['trust_env'] is trust
    assert calls[0]['timeout'].read == read_timeout
