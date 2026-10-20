import re
from pathlib import Path

from .schema import Segment

STAMP = r'(?:\d{2,}:)?\d{2}:\d{2}[,.]\d{3}'
TIMING = re.compile(rf'^({STAMP})\s+-->\s+({STAMP})(?:\s+.*)?$')


def seconds(value: str) -> float:
    parts = value.replace(',', '.').split(':')
    if len(parts) == 2:
        parts.insert(0, '0')
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    if m >= 60 or s >= 60:
        raise ValueError('字幕时间格式错误')
    return h * 3600 + m * 60 + s


def read_subtitles(path: Path, encoding='utf-8-sig') -> list[Segment]:
    text = path.read_text(encoding=encoding).replace('\r\n', '\n').replace('\r', '\n')
    if '\ufffd' in text or '\x00' in text:
        raise ValueError('字幕包含乱码或空字符，请指定正确的 --encoding')
    suffix = path.suffix.lower()
    if suffix == '.txt':
        return [Segment(id=i + 1, start=None, end=None, text=line.strip())
                for i, line in enumerate(text.splitlines()) if line.strip()]
    if suffix not in ('.srt', '.vtt'):
        raise ValueError('仅支持 SRT、VTT、TXT；音视频转写尚未接入')
    segments = []
    for block in re.split(r'\n[ \t]*\n', text.strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        if suffix == '.vtt' and (lines[0].startswith(('WEBVTT', 'NOTE')) or lines[0] in ('STYLE', 'REGION')):
            if any('-->' in line for line in lines):
                raise ValueError('VTT 元数据与字幕之间缺少空行')
            continue
        index = next((i for i, line in enumerate(lines) if TIMING.match(line.strip())), None)
        if index is None or index > 1:
            raise ValueError('字幕块缺少合法时间戳，已停止以避免静默漏读')
        match = TIMING.match(lines[index].strip())
        start, end = seconds(match[1]), seconds(match[2])
        content = '\n'.join(lines[index + 1:]).strip()
        if not content or end < start or '-->' in content:
            raise ValueError('字幕块为空、时间倒置或缺少分隔空行')
        segments.append(Segment(id=len(segments) + 1, start=start, end=end, text=content))
    return segments
