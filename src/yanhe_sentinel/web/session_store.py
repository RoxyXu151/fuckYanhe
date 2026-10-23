"""OS-protected school sessions. Passwords are never accepted or stored here."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import threading
import time

from ..portal import PortalError


class MemorySessionStore:
    enabled = False

    def __init__(self):
        self.token = None

    def read(self):
        return self.token

    def write(self, token):
        self.token = token

    def delete(self):
        self.token = None


class WindowsSessionStore:
    """Generic credential scoped to one workspace and the current Windows user."""
    enabled = True

    def __init__(self, root, *, target=None):
        identity = os.path.normcase(str(Path(root).resolve())).encode('utf-8')
        self.target = target or 'YanheClassroomSentinel/session/' + hashlib.sha256(identity).hexdigest()[:32]

    @staticmethod
    def _api():
        if os.name != 'nt':
            raise OSError('Windows Credential Manager is unavailable')
        class Credential(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        pointer = ctypes.POINTER(Credential)
        api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        return api, Credential

    def read(self):
        api, credential_type = self._api()
        pointer = ctypes.POINTER(credential_type)()
        if not api.CredReadW(self.target, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return None
            raise OSError('Cannot read the saved school session')
        try:
            credential = pointer.contents
            if not 0 < credential.CredentialBlobSize <= 2560:
                raise OSError('Invalid saved school session')
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode('utf-8')
        finally:
            api.CredFree(pointer)

    def write(self, token):
        encoded = token.encode('utf-8')
        if not 0 < len(encoded) <= 2560:
            raise OSError('School session exceeds the OS credential size limit')
        api, credential_type = self._api()
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = credential_type()
        credential.Type, credential.Persist = 1, 2
        credential.TargetName = self.target
        credential.Comment = 'Verified Yanhe Classroom session token; no password'
        credential.UserName = 'Yanhe Classroom session'
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise OSError('Cannot save the school session in Windows Credential Manager')

    def delete(self):
        api, _ = self._api()
        if not api.CredDeleteW(self.target, 1, 0) and ctypes.get_last_error() != 1168:
            raise OSError('Cannot remove the saved school session')


class SavedSession:
    def __init__(self, store, client_factory, *, on_restored=None):
        self.store, self.client_factory = store, client_factory
        self.lock = threading.RLock()
        self.client = self.user = None
        self.generation = 0
        self.next_retry = 0
        self.worker = None
        self.closed = False
        self.status = {'state': 'disconnected', 'message': '尚未连接延河课堂'}
        self.on_restored = on_restored

    def start(self):
        with self.lock:
            if self.closed or self.client or time.monotonic() < self.next_retry or (self.worker and self.worker.is_alive()):
                return
            self.next_retry = time.monotonic() + 60
            self.worker = threading.Thread(target=self.restore, daemon=True, name='yanhe-saved-session')
            self.worker.start()

    def restore(self):
        with self.lock:
            generation = self.generation
        try:
            token = self.store.read()
            if not token:
                return
            with self.lock:
                if generation != self.generation:
                    return
                self.status = {'state': 'checking', 'message': '正在向官方验证保存的登录状态…'}
            client = self.client_factory(token)
            user = client.verify()
            with self.lock:
                if generation == self.generation and not self.closed:
                    self.client, self.user = client, user
                    self.status = {'state': 'connected', 'message': '已验证并自动恢复校园会话'}
                    if self.on_restored:
                        self.on_restored(client)
        except PortalError as exc:
            with self.lock:
                if generation != self.generation:
                    return
                if exc.kind == 'auth':
                    try:
                        self.store.delete()
                    except OSError:
                        pass
                    self.status = {'state': 'expired', 'message': '保存的校园会话已过期，请在官方窗口重新登录。'}
                else:
                    self.status = {'state': 'unavailable', 'message': '暂时无法验证校园会话，已保留保存状态；联网后自动重试。'}
        except (OSError, ValueError):
            with self.lock:
                if generation == self.generation:
                    self.status = {'state': 'unavailable', 'message': '保存的会话暂不可用；可以在官方窗口重新连接。'}

    def attach(self, session):
        if session.portal or session.login_status.get('state') in ('opening', 'waiting'):
            return
        self.start()
        with self.lock:
            if self.client:
                session.portal, session.user = self.client, self.user
            session.login_status = dict(self.status)

    def remember(self, session):
        with self.lock:
            self.generation += 1
            self.client, self.user = session.portal, session.user
            try:
                self.store.write(session.portal._token)
                message = '已连接；校园会话已安全保存，不保存密码。' if self.store.enabled else '已连接延河课堂'
            except OSError:
                message = '已连接；系统凭据保存暂不可用，本次会话仍可继续。'
            self.status = {'state': 'connected', 'message': message}
            session.login_status = dict(self.status)

    def forget(self, *, expired=False):
        with self.lock:
            self.generation += 1
            self.client = self.user = None
            self.next_retry = float('inf')
            self.store.delete()
            self.status = {'state': 'expired' if expired else 'disconnected',
                           'message': '校园会话已过期，请重新登录。' if expired else '已退出并清除保存的校园会话'}

    def close(self):
        with self.lock:
            self.closed = True
            self.generation += 1
