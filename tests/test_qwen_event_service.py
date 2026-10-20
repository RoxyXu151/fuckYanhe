"""Exercise the deployed event endpoint contract without loading GPU weights."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

spec = importlib.util.spec_from_file_location('qwen_event_service',
    Path(__file__).parents[1] / 'scripts/qwen_event_service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def test_event_service_auth_precedes_inference(monkeypatch):
    monkeypatch.setattr(service, 'service_key', 'test-token')
    monkeypatch.setattr(service, 'complete', lambda _: pytest.fail('No unauthorized inference'))
    response = TestClient(service.app).post('/v1/chat/completions',
        json={'model': service.MODEL, 'messages': [{'role': 'user', 'content': 'test'}]})
    assert response.status_code == 401


def test_event_service_matches_workstation_contract_and_hides_failures(monkeypatch):
    monkeypatch.setattr(service, 'service_key', 'test-token')
    def invalid(_):
        raise ValueError('private model output')
    monkeypatch.setattr(service, 'complete', invalid)
    response = TestClient(service.app).post('/v1/chat/completions',
        headers={'Authorization': 'Bearer test-token'}, json={'model': service.MODEL,
        'messages': [{'role': 'user', 'content': 'test'}], 'temperature': 0,
        'response_format': {'type': 'json_object'}})
    assert response.status_code == 422
    assert 'private' not in response.text
