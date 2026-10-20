"""Local media tools. No shell invocation, account access, or URL input."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import wave

from .schema import AudioStream, MediaInfo


class MediaError(Exception):
    """Only application-authored, safe messages may be exposed to the user."""


def local_file(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise MediaError('本地媒体文件不存在或不是普通文件。')
    return path


def run_tool(command, timeout=120):
    try:
        result = subprocess.run([str(part) for part in command], capture_output=True,
            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except FileNotFoundError:
        raise MediaError('找不到媒体工具，请安装 ASR 依赖或设置 FFmpeg/ffprobe 路径。') from None
    except (OSError, subprocess.TimeoutExpired):
        raise MediaError('媒体工具启动失败或超时。请检查文件与工具路径。') from None
    if result.returncode:
        raise MediaError('媒体工具处理失败，可能为损坏文件、不可解码音轨或无效流编号；未打印第三方日志。')
    return result.stdout


def ffmpeg_executable(explicit=None):
    configured = explicit or os.environ.get('YANHE_FFMPEG')
    if configured:
        return str(local_file(Path(configured)))
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        raise MediaError('缺少 FFmpeg。安装 pip install -e ".[asr]"，或设置 YANHE_FFMPEG。') from None


def probe_media(path: Path, ffprobe=None) -> MediaInfo:
    path = local_file(path)
    configured = ffprobe or os.environ.get('YANHE_FFPROBE')
    executable = str(local_file(Path(configured))) if configured else shutil.which('ffprobe')
    if executable:
        raw = run_tool([executable, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path])
        try:
            data = json.loads(raw)
            origin = float(data['format'].get('start_time', 0))
            audio = [AudioStream(index=s['index'], codec=s.get('codec_name', 'unknown'),
                channels=s.get('channels', 0), sample_rate=int(s.get('sample_rate', 0)),
                start_seconds=float(s.get('start_time', origin)) - origin,
                duration_seconds=float(s['duration']) if s.get('duration') not in (None, 'N/A') else None,
                default=bool(s.get('disposition', {}).get('default', 0)),
                language=s.get('tags', {}).get('language'))
                for s in data['streams'] if s.get('codec_type') == 'audio']
            video = next((s for s in data['streams'] if s.get('codec_type') == 'video'), None)
            video_end = (float(video.get('start_time', origin)) - origin + float(video['duration'])) if video and video.get('duration') not in (None, 'N/A') else None
            return MediaInfo(path=str(path), duration_seconds=float(data['format']['duration']),
                             audio_streams=audio, probe_engine='ffprobe', video_end_seconds=video_end)
        except (ValueError, KeyError, TypeError):
            raise MediaError('ffprobe 无法提供有效的媒体时长或音轨元数据。') from None
    # PyAV ships native FFmpeg libraries, allowing a portable, non-admin Windows install.
    try:
        import av
    except ImportError:
        raise MediaError('缺少 ffprobe 与 PyAV，请安装 ASR 依赖或设置 YANHE_FFPROBE。') from None
    try:
        with av.open(str(path)) as container:
            origin = float(container.start_time or 0) / av.time_base
            duration = float(container.duration or 0) / av.time_base
            if duration <= 0:
                duration = max((float(s.duration * s.time_base) for s in container.streams
                                if s.duration is not None and s.time_base is not None), default=0)
            audio = []
            for stream in container.streams.audio:
                audio.append(AudioStream(index=stream.index, codec=stream.codec_context.name,
                    channels=len(stream.layout.channels), sample_rate=stream.rate or 0,
                    start_seconds=float((stream.start_time or 0) * stream.time_base) - origin,
                    duration_seconds=float(stream.duration * stream.time_base) if stream.duration else None,
                    default=bool(stream.disposition & av.stream.Disposition.default),
                    language=stream.metadata.get('language')))
            video = next(iter(container.streams.video), None)
            video_end = float(((video.start_time or 0) + video.duration) * video.time_base) - origin if video is not None and video.duration is not None else None
            return MediaInfo(path=str(path), duration_seconds=duration, audio_streams=audio,
                             probe_engine='PyAV/FFmpeg', video_end_seconds=video_end)
    except Exception:
        raise MediaError('无法读取媒体元数据，文件可能损坏或时长未知。') from None


def select_audio(info: MediaInfo, index=None) -> AudioStream:
    if not info.audio_streams:
        raise MediaError('媒体没有音轨；不能通过无音轨视频判断课堂是否出现通知。')
    if index is not None:
        found = next((s for s in info.audio_streams if s.index == index), None)
        if found is None:
            raise MediaError('所选流编号不是该文件的音轨；使用 inspect 查看全局流编号。')
        return found
    return sorted(info.audio_streams, key=lambda s: (not s.default, s.index))[0]


def wave_duration(path):
    with wave.open(str(path), 'rb') as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
            raise MediaError('缓存音频不是 16kHz 单声道 PCM，请使用 --no-cache 重新提取。')
        if Path(path).stat().st_size < audio.getnframes() * 2 + 44:
            raise MediaError('缓存音轨数据被截断，需要重新提取。')
        return audio.getnframes() / audio.getframerate()


def extract_audio(source, stream_index, destination: Path, ffmpeg=None):
    executable = ffmpeg_executable(ffmpeg)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.pending.wav')
    # Reset initial timestamp, preserve subsequent timestamp gaps via async resampling.
    run_tool([executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-xerror', '-err_detect', 'explode', '-i', source, '-map', f'0:{stream_index}', '-vn',
        '-af', 'asetpts=PTS-STARTPTS,aresample=16000:async=1:first_pts=0',
        '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', temporary], timeout=7200)
    try:
        duration = wave_duration(temporary)
    except (OSError, wave.Error, EOFError):
        raise MediaError('提取后的 PCM 音轨无法读取，可能解码失败或磁盘空间不足。') from None
    if duration <= 0:
        raise MediaError('音轨解码后为空。')
    temporary.replace(destination)
    return duration


def export_clip(audio_path, output: Path, start, end, ffmpeg=None):
    if start < 0 or end <= start:
        raise MediaError('回听片段时间无效。')
    run_tool([ffmpeg_executable(ffmpeg), '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-y', '-ss', f'{start:.6f}', '-i', audio_path, '-t', f'{end-start:.6f}',
        '-c:a', 'pcm_s16le', output])
