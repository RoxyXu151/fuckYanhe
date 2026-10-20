"""Official browser sign-in; credentials remain on the official page."""

import threading
import time
from urllib.parse import urlsplit

from ..portal import OFFICIAL_SITE, PortalError, YanheClient


class BrowserLogin:
    def __init__(self, *, client_factory=YanheClient, login_url=OFFICIAL_SITE, headless=False):
        self.client_factory = client_factory
        self.login_url = login_url
        self.headless = headless
        self._stops = []

    def start(self, session):
        if session.login_status.get('state') in ('opening', 'waiting'):
            return
        stop = threading.Event()
        self._stops.append(stop)
        session.login_stop = stop
        session.login_status = {'state': 'opening', 'message': '正在打开官方登录窗口…'}
        threading.Thread(target=self._run, args=(session, stop), daemon=True, name='yanhe-official-login').start()

    def _run(self, session, stop):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = None
                for channel in ('msedge', 'chrome', None):
                    try:
                        browser = playwright.chromium.launch(channel=channel, headless=self.headless)
                        break
                    except Exception:
                        continue
                if browser is None:
                    raise PortalError('无法启动浏览器。请安装 Edge/Chrome，或运行 python -m playwright install chromium。', 'browser', 503)
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    response = page.goto(self.login_url, wait_until='domcontentloaded', timeout=45000)
                    message = '请在新窗口完成延河课堂登录，完成后本页自动连接。'
                    if response is not None and response.status >= 400:
                        message = '官网暂时拒绝访问，请检查校园网/VPN，在官方窗口刷新后继续登录。'
                    session.login_status = {'state': 'waiting', 'message': message}
                    deadline = time.monotonic() + 600
                    while time.monotonic() < deadline and not stop.is_set():
                        pages = context.pages
                        if not pages:
                            raise PortalError('登录窗口已关闭，可重新打开登录。', 'browser', 400)
                        for candidate in pages:
                            if urlsplit(candidate.url).hostname != urlsplit(self.login_url).hostname:
                                continue
                            try:
                                token = candidate.evaluate('''() => {
                                    try { const a = JSON.parse(localStorage.getItem('auth') || 'null');
                                          return typeof a?.token === 'string' ? a.token : ''; }
                                    catch { return ''; }
                                }''')
                            except Exception:
                                continue
                            if token:
                                client = self.client_factory(token)
                                user = client.verify()
                                if stop.is_set():
                                    return
                                session.portal, session.user = client, user
                                session.login_status = {'state': 'connected', 'message': '已连接延河课堂'}
                                return
                        page.wait_for_timeout(600)
                    if not stop.is_set():
                        raise PortalError('登录等待已超时，请重新打开官方登录窗口。', 'browser', 408)
                finally:
                    browser.close()
        except PortalError as exc:
            session.login_status = {'state': 'error', 'message': str(exc)}
        except Exception:
            session.login_status = {'state': 'error', 'message': '官方登录窗口未能完成连接，请检查网络后重试，或使用登录状态导入。'}
        finally:
            if stop.is_set() and session.portal is None:
                session.login_status = {'state': 'disconnected', 'message': '登录已取消'}

    def close(self):
        for stop in self._stops:
            stop.set()
