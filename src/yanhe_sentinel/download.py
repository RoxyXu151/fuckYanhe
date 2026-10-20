"""Authorized VOD downloads with TLS verification, bounded retries and local-only remuxing."""

from hashlib import sha256
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urljoin, urlsplit

import httpx

from .media_tools import ffmpeg_executable, MediaError
from .portal import OFFICIAL_SITE, PortalError


class DownloadCanceled(Exception):
    pass


def checksum(path):
    h = sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def resource_key(url, byte_range=None):
    parsed = urlsplit(url)
    # Never persist playback tokens, signed queries, or the account token.
    return sha256(json.dumps([parsed.hostname, parsed.path, byte_range]).encode()).hexdigest()


class VODDownloader:
    def __init__(self, client, *, cancel=None, progress=None):
        self.client = client
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda **kwargs: None)

    def check_canceled(self):
        if self.cancel.is_set():
            raise DownloadCanceled()

    def _transfer(self, url, target, *, initial=False, byte_range=None, maximum=2 * 1024**3):
        for attempt in range(3):
            self.check_canceled()
            target.seek(0)
            target.truncate()
            signed = self.client.signed_media_url(url, initial_playlist=initial)
            headers = {'Origin': OFFICIAL_SITE, 'Referer': OFFICIAL_SITE + '/'}
            if byte_range:
                headers['Range'] = f'bytes={byte_range[0]}-{byte_range[1]}'
            try:
                with httpx.Client(timeout=httpx.Timeout(30, connect=15), follow_redirects=False,
                                  transport=self.client.media_transport()) as http:
                    with http.stream('GET', signed, headers=headers) as response:
                        if response.status_code in (401, 403):
                            raise PortalError('平台拒绝访问此录播，请在官网确认当前账户能正常播放；不会绕过权限。', 'permission', 403)
                        if response.is_redirect:
                            raise PortalError('媒体发生重定向，请刷新课次后重试；不会把播放凭据转发到未知地址。', 'redirect', 502)
                        response.raise_for_status()
                        if byte_range and response.status_code != 206:
                            raise PortalError('媒体服务器没有正确响应分段范围请求。')
                        expected = int(response.headers.get('Content-Length') or 0)
                        if expected > maximum:
                            raise PortalError('媒体响应大小超出当前安全处理上限。', 'size', 400)
                        total = 0
                        for block in response.iter_bytes(256 * 1024):
                            self.check_canceled()
                            total += len(block)
                            if total > maximum:
                                raise PortalError('媒体响应大小超出当前处理上限。', 'size', 400)
                            target.write(block)
                        if (expected and total != expected) or total == 0:
                            raise httpx.ReadError('incomplete media')
                        if byte_range and total != byte_range[1] - byte_range[0] + 1:
                            raise httpx.ReadError('incomplete range')
                        return str(response.url)
            except (PortalError, DownloadCanceled):
                raise
            except (httpx.HTTPError, ValueError, OSError):
                if attempt == 2:
                    raise PortalError('录播下载失败或分片不完整，成功分片已保留；请检查网络后重试。', 'download', 502) from None
                if self.cancel.wait(2 ** attempt):
                    raise DownloadCanceled()

    def _bytes(self, url, *, initial=False, maximum=2 * 1024**2):
        target = io.BytesIO()
        final = self._transfer(url, target, initial=initial, maximum=maximum)
        return target.getvalue(), final

    def _file(self, url, path, *, byte_range=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        meta_path = path.with_suffix(path.suffix + '.json')
        key = resource_key(url, byte_range)
        if path.is_file() and meta_path.is_file():
            try:
                saved = json.loads(meta_path.read_text('utf-8'))
                if saved['resource'] == key and saved['size'] == path.stat().st_size and saved['sha256'] == checksum(path):
                    return
            except (ValueError, KeyError, OSError):
                pass
        temporary = path.with_suffix(path.suffix + '.part')
        with temporary.open('w+b') as target:
            self._transfer(url, target, byte_range=byte_range)
        temporary.replace(path)
        meta_path.write_text(json.dumps({'resource': key, 'size': path.stat().st_size,
            'sha256': checksum(path)}), encoding='utf-8')

    def direct(self, url, destination):
        self.progress(stage='downloading', current=0, total=1, message='正在获取课堂音轨…')
        self._file(url, destination)
        self.progress(stage='downloading', current=1, total=1, message='课堂音轨已就绪')
        return destination

    def video(self, url, destination: Path):
        if urlsplit(url).path.lower().endswith('.mp4'):
            return self.direct(url, destination)
        import m3u8
        source_key = resource_key(url)
        complete_meta = destination.with_suffix('.download.json')
        if destination.is_file() and complete_meta.is_file():
            try:
                metadata = json.loads(complete_meta.read_text('utf-8'))
                if metadata['resource'] == source_key and metadata['sha256'] == checksum(destination):
                    self.progress(stage='downloading', current=1, total=1, message='复用已下载的录播')
                    return destination
            except (ValueError, KeyError, OSError):
                pass
        body, base = self._bytes(url, initial=True)
        playlist = m3u8.loads(body.decode('utf-8-sig'))
        for _ in range(4):
            if not playlist.is_variant:
                break
            if not playlist.playlists:
                raise PortalError('平台没有返回可播放的录播清单。')
            variant = max(playlist.playlists, key=lambda item: item.stream_info.bandwidth or 0)
            body, base = self._bytes(urljoin(base, variant.uri))
            playlist = m3u8.loads(body.decode('utf-8-sig'))
        if playlist.is_variant or not playlist.segments:
            raise PortalError('无法解析录播分片清单。')
        if not playlist.is_endlist:
            raise PortalError('该录像尚未结束或仍为直播，请在录播归档后重试。', 'live', 400)
        work = destination.parent / '.download' / source_key
        work.mkdir(parents=True, exist_ok=True)
        key_names = {}
        for index, key in enumerate(playlist.keys):
            if key is None or key.method in (None, 'NONE'):
                continue
            if key.method != 'AES-128' or key.keyformat not in (None, 'identity'):
                raise PortalError('该媒体使用了未支持的保护方式；不提供 DRM 绕过。', 'protected', 400)
            key_url = urljoin(base, key.uri)
            key_bytes, _ = self._bytes(key_url, maximum=64)
            if len(key_bytes) != 16:
                raise PortalError('平台没有提供有效的标准 HLS 播放密钥。', 'protected', 403)
            local_name = f'key-{index}.key'
            (work / local_name).write_bytes(key_bytes)
            key_names[key.uri] = local_name
        ranges = {}
        local_lines = ['#EXTM3U', '#EXT-X-VERSION:7',
            '#EXT-X-TARGETDURATION:' + str(max(1, math.ceil(max(s.duration for s in playlist.segments)))),
            '#EXT-X-MEDIA-SEQUENCE:' + str(int(playlist.media_sequence or 0))]
        previous_key, previous_map = None, None
        for index, segment in enumerate(playlist.segments):
            self.check_canceled()
            original = urljoin(base, segment.uri)
            byte_range = None
            if segment.byterange:
                length, _, offset = segment.byterange.partition('@')
                start = int(offset) if offset else ranges.get(original, 0)
                byte_range = (start, start + int(length) - 1)
                ranges[original] = byte_range[1] + 1
            name = f'segment-{index:06}.ts'
            self._file(original, work / name, byte_range=byte_range)
            if segment.discontinuity:
                local_lines.append('#EXT-X-DISCONTINUITY')
            key = segment.key
            current_key = (key.uri, key.iv) if key and key.method == 'AES-128' else None
            if current_key != previous_key:
                if current_key:
                    iv = key.iv
                    if iv and not re.fullmatch(r'0[xX][0-9a-fA-F]{1,32}', iv):
                        raise PortalError('HLS 播放密钥的 IV 格式无效。')
                    local_lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{key_names[key.uri]}"' + (f',IV={iv}' if iv else ''))
                else:
                    local_lines.append('#EXT-X-KEY:METHOD=NONE')
                previous_key = current_key
            init = segment.init_section
            if init:
                init_url = urljoin(base, init.uri)
                init_range = None
                if init.byterange:
                    length, _, offset = init.byterange.partition('@')
                    start = int(offset or 0)
                    init_range = (start, start + int(length) - 1)
                init_name = 'init-' + resource_key(init_url, init_range)[:16] + '.mp4'
                self._file(init_url, work / init_name, byte_range=init_range)
                if init_name != previous_map:
                    local_lines.append(f'#EXT-X-MAP:URI="{init_name}"')
                    previous_map = init_name
            if not math.isfinite(segment.duration) or segment.duration <= 0:
                raise PortalError('录播分片时长格式无效。')
            local_lines.extend([f'#EXTINF:{segment.duration:.6f},', name])
            self.progress(stage='downloading', current=index + 1, total=len(playlist.segments),
                message=f'获取录播分片 {index + 1}/{len(playlist.segments)}')
        # Rebuild a minimal allowlisted playlist: no untrusted local paths or network URLs.
        local_lines.append('#EXT-X-ENDLIST')
        local_playlist = '\n'.join(local_lines) + '\n'
        playlist_path = work / 'local.m3u8'
        playlist_path.write_text(local_playlist, encoding='utf-8')
        self.progress(stage='merging', current=0, total=0, message='正在合并录播分片…')
        temporary = destination.with_suffix('.pending.mp4')
        command = [ffmpeg_executable(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-protocol_whitelist', 'file,crypto,data', '-allowed_extensions', 'ALL', '-i', str(playlist_path.resolve()),
            '-c', 'copy', '-movflags', '+faststart', str(temporary.resolve())]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        while process.poll() is None:
            if self.cancel.wait(.25):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise DownloadCanceled()
        if process.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
            raise MediaError('录播合并失败，成功分片已保留；请重试或检查 FFmpeg。')
        temporary.replace(destination)
        complete_meta.write_text(json.dumps({'resource': source_key, 'sha256': checksum(destination)}), encoding='utf-8')
        return destination
