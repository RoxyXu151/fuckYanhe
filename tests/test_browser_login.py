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
