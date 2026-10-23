import sys
from types import SimpleNamespace
import threading

import httpx
import pytest

pytest.importorskip('playwright')
from yanhe_sentinel.portal import OFFICIAL_SITE, YanheClient
from yanhe_sentinel.web.auth import BrowserLogin
from yanhe_sentinel.web.app import Session
from test_portal import FakePlatform, TOKEN


def test_official_browser_login_adopts_only_verified_session(monkeypatch):
    visited, inspected, launches, closed = [], [], [], []
    class Page:
        url = OFFICIAL_SITE
        def goto(self, url, **kwargs):
            visited.append(url)
        def evaluate(self, script):
            inspected.append(script)
            return TOKEN
    page = Page()
    browser = SimpleNamespace(new_context=lambda: SimpleNamespace(new_page=lambda: page, pages=[page]),
                              close=lambda: closed.append(True))
    def launch(**kwargs):
        launches.append(kwargs)
        return browser
    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        def __exit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: Playwright()))
    platform = FakePlatform()
    manager = BrowserLogin(client_factory=lambda token: YanheClient(token,
        transport=httpx.MockTransport(platform), min_interval=0))
    session = Session()
    manager._run(session, threading.Event())
    assert visited == [OFFICIAL_SITE]
    assert launches[0]['headless'] is False
    assert 'localStorage' in inspected[0] and 'password' not in inspected[0]
    assert session.portal and session.user['display_name'] == '合成测试用户'
    assert session.login_status['state'] == 'connected'
    assert TOKEN not in str(session.login_status)
    assert closed == [True]


def test_persistent_official_profile_disables_password_saving_and_calls_verified_hook(monkeypatch, tmp_path):
    import json
    launches, verified, closed = [], [], []
    class Page:
        url = OFFICIAL_SITE
        def goto(self, url, **kwargs):
            assert url == OFFICIAL_SITE
        def evaluate(self, script):
            assert 'password' not in script
            return TOKEN
    page = Page()
    context = SimpleNamespace(new_page=lambda: page, pages=[page], close=lambda: closed.append(True))
    def launch(**kwargs):
        launches.append(kwargs)
        return context
    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        def __exit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: Playwright()))
    manager = BrowserLogin(profile_dir=tmp_path / 'official-browser',
        on_connected=lambda session: verified.append(session.user['owner_key']),
        client_factory=lambda token: YanheClient(token, transport=httpx.MockTransport(FakePlatform()), min_interval=0))
    manager._run(Session(), threading.Event())
    assert verified and closed == [True]
    assert launches[0]['user_data_dir'] == str((tmp_path / 'official-browser').resolve())
    assert launches[0]['accept_downloads'] is False
    preferences = json.loads((tmp_path / 'official-browser/Default/Preferences').read_text())
    assert preferences['credentials_enable_service'] is False
    assert preferences['profile']['password_manager_enabled'] is False
    assert TOKEN not in str(preferences)
