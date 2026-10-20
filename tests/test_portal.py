import json
from pathlib import Path
import threading

import httpx
import pytest

pytest.importorskip('m3u8')
from yanhe_sentinel.download import DownloadCanceled, VODDownloader
from yanhe_sentinel.media_tools import ffmpeg_executable, probe_media, run_tool
from yanhe_sentinel.portal import PortalError, YanheClient, course_identifier

TOKEN = 'a' * 32


@pytest.fixture
def hls_media(tmp_path):
    pytest.importorskip('imageio_ffmpeg')
    directory = tmp_path / 'hls'
    directory.mkdir()
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
        '-i', 'color=c=black:s=128x72:r=10', '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=16000',
        '-t', '2.2', '-c:v', 'libx264', '-g', '10', '-c:a', 'aac', '-hls_time', '1', '-hls_list_size', '0',
        '-hls_segment_filename', directory / 'part-%03d.ts', directory / 'index.m3u8'])
    return directory


class FakePlatform:
    def __init__(self, hls=None):
        self.hls, self.requests, self.empty, self.code = hls, [], False, 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == 'cvideo.yanhekt.cn':
            assert 'authorization' not in request.headers
            assert request.url.params['Xvideo_Token'] == 'test-playback-token'
            file = self.hls / Path(request.url.path).name
            content = file.read_bytes()
            if 'range' in request.headers:
                start, end = map(int, request.headers['range'].removeprefix('bytes=').split('-'))
                return httpx.Response(206, content=content[start:end + 1],
                    headers={'Content-Range': f'bytes {start}-{end}/{len(content)}'})
            return httpx.Response(200, content=content)
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        if self.code:
            return httpx.Response(200, json={'code': self.code, 'data': None, 'message': 'not exposed'})
        path = request.url.path
        if path == '/v1/user':
            data = {'nickname': '合成测试用户', 'badge': 'fixture-account'}
        elif path in ('/v2/course/private/list', '/v2/course/list'):
            data = {'data': [{'id': 100, 'name_zh': '合成课堂', 'professors': ['测试教师']}], 'total': 1, 'last_page': 1}
        elif path == '/v1/course':
            data = {'id': 100, 'name_zh': '合成课堂', 'professors': [{'name': '测试教师'}]}
        elif path == '/v2/course/session/list':
            data = [] if self.empty else [{'id': 200, 'title': '合成课次', 'started_at': '2026-10-19 08:00:00',
                'videos': [{'id': 300, 'duration': 2.2,
                    'main': 'https://cvideo.yanhekt.cn/fixture/main/index.m3u8',
                    'vga': 'https://cvideo.yanhekt.cn/fixture/screen/index.m3u8'}]}]
        elif path == '/v1/auth/video/token':
            data = {'token': 'test-playback-token'}
        elif path == '/v1/video':
            data = {'id': 300, 'audio': ''}
        else:
            raise AssertionError('Unexpected API path: ' + path)
        return httpx.Response(200, json={'code': 0, 'data': data})


def platform_client(platform):
    client = YanheClient(TOKEN, transport=httpx.MockTransport(platform), min_interval=0)
    client.verify()
    return client


def test_authenticated_courses_and_authorized_session_list():
    platform = FakePlatform()
    client = platform_client(platform)
    assert client.user['display_name'] == '合成测试用户'
    assert TOKEN not in repr(client)
    assert client.courses()['items'][0]['title'] == '合成课堂'
    assert any(r.url.path == '/v2/course/private/list' for r in platform.requests)
    session = client.sessions('https://www.yanhekt.cn/course/100')['items'][0]
    assert session['signals'] == ['camera', 'screen']
    assert 'main_url' not in session
    assert client.recording('100', '200', '300').session_id == '200'


def test_empty_authorized_list_has_no_by_id_fallback():
    platform = FakePlatform()
    client = platform_client(platform)
    platform.empty = True
    assert not client.sessions('100')['items']
    with pytest.raises(PortalError):
        client.recording('100', '999', '300')
    assert not any(r.url.path == '/v1/video' for r in platform.requests)
    assert not any(r.url.path == '/v1/auth/video/token' for r in platform.requests)


@pytest.mark.parametrize('code,kind', [(61101113, 'auth'), (61101114, 'busy'), (13001001, 'auth')])
def test_expired_login_and_busy_are_distinct(code, kind):
    platform = FakePlatform()
    platform.code = code
    with pytest.raises(PortalError) as error:
        platform_client(platform)
    assert error.value.kind == kind


@pytest.mark.parametrize('value', ['https://evil.example/course/100', 'https://www.yanhekt.cn/session/200', '../100', 'abc'])
def test_course_input_does_not_accept_arbitrary_url(value):
    with pytest.raises(PortalError):
        course_identifier(value)


def test_actual_hls_download_and_remux_reuses_valid_files(hls_media, tmp_path):
    platform = FakePlatform(hls_media)
    client = platform_client(platform)
    recording = client.recording('100', '200', '300')
    output = tmp_path / 'downloaded/lecture.mp4'
    output.parent.mkdir()
    downloader = VODDownloader(client)
    assert downloader.video(recording.screen_url, output) == output
    assert probe_media(output).duration_seconds > 2
    request_count = len(platform.requests)
    downloader.video(recording.screen_url, output)
    assert len(platform.requests) == request_count
    for file in output.parent.rglob('*'):
        if file.is_file() and file.suffix in ('.json', '.m3u8'):
            text = file.read_text('utf-8')
            assert TOKEN not in text and 'test-playback-token' not in text and 'Xvideo_Token' not in text


def test_download_cancel_before_any_network():
    platform = FakePlatform()
    client = platform_client(platform)
    cancel = threading.Event()
    cancel.set()
    downloader = VODDownloader(client, cancel=cancel)
    with pytest.raises(DownloadCanceled):
        downloader._bytes('https://cvideo.yanhekt.cn/index.m3u8')
    assert len(platform.requests) == 1


@pytest.mark.parametrize('format', ['encrypted-ts', 'fmp4', 'single-file'])
def test_vod_formats_with_actual_decode(format, tmp_path):
    """Exercise real decrypt/remux and byte ranges, rather than playlist shape alone."""
    pytest.importorskip('imageio_ffmpeg')
    directory = tmp_path / 'source'
    directory.mkdir()
    options = []
    extension = 'ts'
    if format == 'encrypted-ts':
        key_file = directory / 'key.bin'
        key_file.write_bytes(bytes(range(16)))
        info = directory / 'key-info.txt'
        info.write_text('key.bin\n' + key_file.as_posix() + '\n', encoding='utf-8')
        options = ['-hls_key_info_file', str(info)]
    elif format == 'fmp4':
        extension = 'm4s'
        options = ['-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', str(directory / 'init.mp4')]
    else:
        options = ['-hls_flags', 'single_file']
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
        '-i', 'color=c=blue:s=128x72:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000',
        '-t', '2.2', '-c:v', 'libx264', '-g', '10', '-c:a', 'aac', '-hls_time', '1',
        '-hls_list_size', '0', *options, '-hls_segment_filename', directory / ('all.ts' if format == 'single-file' else 'part-%03d.' + extension),
        directory / 'index.m3u8'])
    if format == 'fmp4':
        playlist_file = directory / 'index.m3u8'
        # FFmpeg writes the absolute local path into EXT-X-MAP; simulate its served relative URI.
        text = playlist_file.read_text('utf-8')
        text = text.replace(str(directory / 'init.mp4'), 'init.mp4')
        playlist_file.write_text(text, encoding='utf-8')
    platform = FakePlatform(directory)
    client = platform_client(platform)
    recording = client.recording('100', '200', '300')
    output = tmp_path / 'output/lecture.mp4'
    output.parent.mkdir()
    VODDownloader(client).video(recording.screen_url, output)
    media = probe_media(output)
    assert media.video_end_seconds and media.audio_streams and media.duration_seconds > 2
    # A successful container probe is insufficient: decode both tracks to catch corrupt ranges/keys.
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-xerror',
        '-i', output, '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])
    if format == 'single-file':
        assert any('range' in request.headers for request in platform.requests)


@pytest.mark.parametrize('url', ['https://127.0.0.1/media', 'https://evil.example/video.mp4',
    'file:///C:/Windows/system.ini', 'https://cvideo.yanhekt.cn.evil.example/file', 'https://user:password@cvideo.yanhekt.cn/file'])
def test_media_source_validation(url):
    client = platform_client(FakePlatform())
    with pytest.raises(PortalError):
        client.signed_media_url(url)


def test_http_403_is_not_misreported_as_expired_login():
    client = YanheClient(TOKEN, transport=httpx.MockTransport(lambda request: httpx.Response(403)), min_interval=0)
    with pytest.raises(PortalError) as error:
        client.verify()
    assert error.value.kind == 'permission'


def test_empty_user_response_is_not_accepted_as_authenticated():
    client = YanheClient(TOKEN, transport=httpx.MockTransport(lambda request: httpx.Response(200,
        json={'code': 0, 'data': {}})), min_interval=0)
    with pytest.raises(PortalError) as error:
        client.verify()
    assert error.value.kind == 'verification'
