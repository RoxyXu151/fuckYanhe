"""Authenticated Yanhe course access. Never uses anonymous or by-id permission fallbacks."""

from dataclasses import dataclass, field
from hashlib import md5, sha256
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

PUBLIC_CLIENT_ID = '1138b69dfef641d9d7ba49137d2d4875'
API_BASE = 'https://cbiz.yanhekt.cn'
OFFICIAL_SITE = 'https://www.yanhekt.cn'


class PortalError(Exception):
    def __init__(self, message, kind='platform', status=502):
        super().__init__(message)
        self.kind, self.status = kind, status


def identifier(value):
    value = str(value).strip()
    if not re.fullmatch(r'\d{1,12}', value):
        raise PortalError('课程或课次编号格式不正确。', 'input', 400)
    return value


def course_identifier(value):
    value = str(value).strip()
    if value.isdigit():
        return identifier(value)
    url = urlsplit(value)
    match = re.fullmatch(r'/course/(\d{1,12})/?', url.path)
    if url.scheme != 'https' or url.hostname not in ('www.yanhekt.cn', 'yanhekt.cn') or not match:
        raise PortalError('请填写课程编号或延河课堂 /course/… 课程链接。', 'input', 400)
    return match[1]


def normalize_token(value):
    value = value.strip()
    if value.startswith('Bearer '):
        value = value[7:].strip()
    if not re.fullmatch(r'[A-Za-z0-9._~-]{16,4096}', value):
        raise PortalError('登录状态格式无效，请重新在官方页面登录。', 'auth', 401)
    return value


def permission_denied(data):
    if not isinstance(data, dict):
        return False
    if any(key in data and data[key] in (False, 0, '0', 'false') for key in ('can_watch', 'can_play', 'has_permission', 'accessible')):
        return True
    return any(data.get(key) in (True, 1, '1', 'true') for key in ('is_locked', 'permission_denied'))


@dataclass
class Recording:
    course_id: str
    session_id: str
    video_id: str
    title: str
    started_at: str
    ended_at: str
    duration: float
    main_url: str = field(repr=False)
    screen_url: str = field(repr=False)

    def public(self):
        return {key: getattr(self, key) for key in ('course_id', 'session_id', 'video_id', 'title',
                'started_at', 'ended_at', 'duration')} | {
            'signals': [kind for kind, url in [('camera', self.main_url), ('screen', self.screen_url)] if url]}


class YanheClient:
    def __init__(self, token, *, transport=None, min_interval=.3):
        self._token = normalize_token(token)
        self._transport = transport
        self._lock = threading.RLock()
        self._next_request = 0.0
        self._min_interval = min_interval
        self._media_token = ''
        self._media_token_expires = 0
        self._recordings = {}
        self.user = None

    def __repr__(self):
        return 'YanheClient(<session in memory>)'

    def headers(self):
        return {'Origin': OFFICIAL_SITE, 'Referer': OFFICIAL_SITE + '/',
            'xdomain-client': 'web_user', 'Xclient-Version': 'v1',
            'Xclient-Timestamp': str(int(time.time())),
            'Xclient-Signature': md5((PUBLIC_CLIENT_ID + '_v1_undefined').encode()).hexdigest(),
            'Authorization': 'Bearer ' + self._token,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'}

    def _get(self, path, params=None):
        with self._lock:
            for attempt in range(3):
                wait = self._next_request - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._next_request = time.monotonic() + self._min_interval
                try:
                    with httpx.Client(timeout=25, follow_redirects=False, transport=self._transport) as client:
                        response = client.get(API_BASE + path, params=params, headers=self.headers())
                    if response.status_code == 401:
                        raise PortalError('登录已失效或平台拒绝访问，请重新登录后重试。', 'auth', 401)
                    if response.status_code == 403:
                        raise PortalError('平台拒绝访问，请在官网确认课程权限及校园网/VPN 状态；此响应不能单独证明登录过期。', 'permission', 403)
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        raise PortalError('延河课堂暂时繁忙或限流，请稍后重试；这不代表登录过期。', 'busy', 503)
                    response.raise_for_status()
                    payload = response.json()
                    code = str(payload.get('code'))
                    if code in ('61101113', '13001001'):
                        raise PortalError('登录已失效，请重新在官方页面登录。', 'auth', 401)
                    if code == '61101114':
                        raise PortalError('平台当前繁忙或访问受限，请检查校园网/VPN 后重试；暂不能判断登录是否有效。', 'busy', 503)
                    if code != '0':
                        raise PortalError('平台未允许此次查询，请在官网确认当前账户能查看该课程。', 'permission', 403)
                    return payload.get('data')
                except PortalError:
                    raise
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise PortalError('无法连接延河课堂，请检查网络、校园网或 VPN。', 'network', 502) from None

    def verify(self):
        data = self._get('/v1/user')
        if not isinstance(data, dict) or not any(data.get(key) for key in ('badge', 'id', 'nickname')):
            raise PortalError('平台没有返回可核验的用户信息，请检查网络后重试；暂不能判断登录是否有效。', 'verification', 502)
        badge = str(data.get('badge') or data.get('id') or self._token)
        self.user = {'display_name': str(data.get('nickname') or '已登录用户')[:80],
                     'owner_key': sha256(('yanhe:' + badge).encode()).hexdigest()}
        return self.user

    def courses(self, page=1, query='', mine=True):
        params = {'page': page, 'page_size': 24}
        if mine:
            path = '/v2/course/private/list'
            params.update(user_relationship_type=1, with_introduction='true')
        else:
            path = '/v2/course/list'
        if query:
            params['keyword'] = query[:100]
        data = self._get(path, params)
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise PortalError('平台课程列表格式发生变化，请更新适配器。')
        courses = []
        for item in data['data']:
            if not isinstance(item, dict) or permission_denied(item):
                continue
            professors = item.get('professors') or []
            names = [p if isinstance(p, str) else str(p.get('name') or '') for p in professors]
            courses.append({'id': identifier(item['id']), 'title': str(item.get('name_zh') or '未命名课程'),
                'professors': names, 'school_year': str(item.get('school_year') or ''),
                'semester': str(item.get('semester') or ''), 'college': str(item.get('college_name') or '')})
        return {'items': courses, 'page': page, 'last_page': int(data.get('last_page') or 1),
                'total': int(data.get('total') or len(courses))}

    def sessions(self, course_id):
        course_id = course_identifier(course_id)
        course = self._get('/v1/course', {'id': course_id, 'with_professor_badges': 'true'})
        if not isinstance(course, dict) or permission_denied(course):
            raise PortalError('当前账户无法查看该课程。', 'permission', 403)
        data = self._get('/v2/course/session/list', {'course_id': course_id})
        if not isinstance(data, list):
            raise PortalError('平台课次列表格式发生变化，请更新适配器。')
        # Do not fall back to anonymous session/video lookup when the authorized list is empty.
        recordings = []
        for item in data:
            if not isinstance(item, dict) or permission_denied(item):
                continue
            for video in item.get('videos') or []:
                if permission_denied(video):
                    continue
                recording = Recording(course_id, identifier(item['id']), identifier(video['id']),
                    str(item.get('title') or '未命名课次'), str(item.get('started_at') or ''),
                    str(item.get('ended_at') or ''), float(video.get('duration') or 0),
                    str(video.get('main') or ''), str(video.get('vga') or ''))
                self._recordings[(recording.session_id, recording.video_id)] = recording
                recordings.append(recording.public())
        return {'course': {'id': course_id, 'title': str(course.get('name_zh') or '课程'),
                'professors': [str(p.get('name') or '') if isinstance(p, dict) else str(p)
                               for p in course.get('professors') or []]},
                'items': recordings, 'notice': '' if recordings else '当前账户没有可用录播，可能没有录像、尚未归档或没有课程权限。'}

    def recording(self, course_id, session_id, video_id):
        course_id, session_id, video_id = map(identifier, (course_id, session_id, video_id))
        # Re-read the authorized list immediately before download, including on retry.
        self._recordings.clear()
        self.sessions(course_id)
        result = self._recordings.get((session_id, video_id))
        if not result or result.course_id != course_id:
            raise PortalError('所选课次已不可用或当前账户没有权限，请刷新视频列表。', 'permission', 403)
        return result

    def microphone_url(self, recording):
        data = self._get('/v1/video', {'id': recording.video_id})
        if not isinstance(data, dict) or permission_denied(data):
            raise PortalError('当前账户无法读取该课次音轨。', 'permission', 403)
        url = str(data.get('audio') or '')
        if not url:
            raise PortalError('该课次没有独立话筒录音，请选择视频原音轨。', 'no_audio', 400)
        return url

    def signed_media_url(self, url, *, initial_playlist=False):
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if parsed.scheme not in ('https', 'http') or not (host == 'yanhekt.cn' or host.endswith('.yanhekt.cn')):
            raise PortalError('平台返回了未支持的媒体地址，已停止下载。', 'media_origin', 400)
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise PortalError('媒体地址格式不正确。', 'media_origin', 400)
        with self._lock:
            if not self._media_token or time.time() >= self._media_token_expires:
                data = self._get('/v1/auth/video/token', {'id': 0})
                if not isinstance(data, dict) or not data.get('token'):
                    raise PortalError('平台未颁发播放凭据，请重新登录后重试。', 'auth', 401)
                self._media_token = str(data['token'])
                self._media_token_expires = min(float(data.get('expired_at') or time.time() + 300), time.time() + 300) - 10
            path = parsed.path
            marker = md5((PUBLIC_CLIENT_ID + '_100').encode()).hexdigest()
            if initial_playlist and marker not in path.split('/'):
                bits = path.split('/')
                bits.insert(-1, marker)
                path = '/'.join(bits)
            timestamp = str(int(time.time()))
            params = dict(parse_qsl(parsed.query))
            params.update(Xvideo_Token=self._media_token, Xclient_Timestamp=timestamp,
                Xclient_Signature=md5((PUBLIC_CLIENT_ID + '_v1_' + timestamp).encode()).hexdigest(),
                Xclient_Version='v1', Platform='yhkt_user')
            # Always validate TLS. No HTTP downgrade, IP pinning, or certificate bypass.
            return urlunsplit(('https', host, path, urlencode(params), ''))

    def media_transport(self):
        return self._transport
