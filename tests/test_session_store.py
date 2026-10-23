import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from yanhe_sentinel.portal import PortalError, YanheClient
from yanhe_sentinel.web.app import create_app, Session
from yanhe_sentinel.web.session_store import MemorySessionStore, WindowsSessionStore, SavedSession
from test_portal import FakePlatform, TOKEN
from test_webui import bootstrap, clear_model_environment


def factory(platform):
    return lambda token: YanheClient(token, transport=httpx.MockTransport(platform), min_interval=0)


def test_restart_validates_and_restores_session_without_password_or_token_files(tmp_path):
    store, platform = MemorySessionStore(), FakePlatform()
    with TestClient(create_app(tmp_path, testing=True, session_store=store, client_factory=factory(platform))) as client:
        bootstrap(client)
        assert client.post('/api/auth/token', json={'token': TOKEN}).status_code == 200
    assert store.read() == TOKEN
    with TestClient(create_app(tmp_path, testing=True, session_store=store, client_factory=factory(platform))) as client:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = client.get('/api/auth')
            if response.json()['connected']:
                break
            time.sleep(.01)
        assert response.json()['connected']
        assert TOKEN not in response.text
        bootstrap(client)
        assert client.delete('/api/auth').status_code == 200
        assert store.read() is None
        assert not client.get('/api/auth').json()['connected']
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


@pytest.mark.parametrize('code,deleted,state', [(61101113, True, 'expired'), (61101114, False, 'unavailable')])
def test_only_official_expiry_removes_saved_session(code, deleted, state):
    store = MemorySessionStore()
    store.write(TOKEN)
    platform = FakePlatform()
    platform.code = code
    saved = SavedSession(store, factory(platform))
    saved.restore()
    assert (store.read() is None) is deleted
    assert saved.status['state'] == state
    assert TOKEN not in str(saved.status)
    saved.close()


def test_logout_during_background_validation_does_not_reconnect():
    entered, release = threading.Event(), threading.Event()
    class Client:
        def verify(self):
            entered.set()
            assert release.wait(3)
            return {'display_name': 'fixture', 'owner_key': 'fixture'}
    store = MemorySessionStore()
    store.write(TOKEN)
    saved = SavedSession(store, lambda token: Client())
    saved.start()
    assert entered.wait(3)
    saved.forget()
    release.set()
    saved.worker.join(3)
    session = Session()
    saved.attach(session)
    assert session.portal is None and store.read() is None
    saved.close()


def test_os_storage_failure_keeps_current_login_and_never_writes_fallback(tmp_path):
    class Unavailable(MemorySessionStore):
        enabled = True
        def write(self, token):
            raise OSError('synthetic OS failure')
    saved = SavedSession(Unavailable(), factory(FakePlatform()))
    client = factory(FakePlatform())(TOKEN)
    session = Session(portal=client, user=client.verify())
    saved.remember(session)
    assert session.portal is client and saved.client is client
    assert '系统凭据保存暂不可用' in session.login_status['message']
    assert TOKEN not in str(saved.status)
    assert not list(tmp_path.iterdir())
    saved.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Credential Manager integration')
def test_native_windows_credential_round_trip_isolated_fixture(tmp_path):
    store = WindowsSessionStore(tmp_path, target='YanheClassroomSentinel/test/' + uuid.uuid4().hex)
    token = 'synthetic-session-' + uuid.uuid4().hex
    try:
        assert store.read() is None
        store.write(token)
        assert store.read() == token
        assert WindowsSessionStore(tmp_path, target=store.target).read() == token
    finally:
        store.delete()
    assert store.read() is None
