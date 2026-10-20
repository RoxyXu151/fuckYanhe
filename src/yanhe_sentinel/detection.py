import hashlib
import json
from pathlib import Path

from .schema import Event, Extraction, Report, Source
from .providers import ProviderError
from .timeline import chunks, intervals


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def verify(extraction, chunk):
    lookup = {s.id: s for s in chunk}
    for c in extraction.events:
        if len(c.segment_ids) != len(set(c.segment_ids)) or any(i not in lookup for i in c.segment_ids):
            raise ProviderError('模型证据引用了不存在或重复的字幕片段')
        chosen = [lookup[i] for i in c.segment_ids]
        positions = [next(i for i, s in enumerate(chunk) if s.id == sid) for sid in c.segment_ids]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            raise ProviderError('跨段原句必须引用连续字幕，不得跳过中间上下文')
        if c.segment_ids != [s.id for s in chunk if s.id in c.segment_ids]:
            raise ProviderError('模型证据片段顺序不匹配')
        # Exact quotation from each claimed cue prevents irrelevant IDs supplying fake times.
        joined = '\n'.join(s.text for s in chosen)
        if c.quote not in joined:
            raise ProviderError('模型证据原句与字幕不匹配')
        offset = joined.index(c.quote)
        end = offset + len(c.quote)
        cursor = 0
        for s in chosen:
            if end <= cursor or offset >= cursor + len(s.text):
                raise ProviderError('引述未覆盖所声明的全部片段')
            cursor += len(s.text) + 1


def make_event(c, chunk, source_path):
    selected = [s for s in chunk if s.id in c.segment_ids]
    starts = [s.start for s in selected if s.start is not None]
    ends = [s.end for s in selected if s.end is not None]
    start, end = (min(starts), max(ends)) if starts else (None, None)
    status = c.status
    if c.polarity == 'uncertain' or c.tense == 'unspecified':
        status = 'needs_review'
    elif status != 'canceled' and (c.polarity == 'negated' or c.tense == 'past'):
        status = 'informational'
    reasons = ['实验性检测；原文可追踪不代表转写无误']
    if c.due_text:
        reasons.append('截止表达保留原文，未自动换算日期')
    if start is None:
        reasons.append('TXT 无原始时间戳')
    return Event(event_id='evt-' + digest([c.type, c.segment_ids, c.quote])[:16], type=c.type,
        severity='critical' if c.type in ('attendance', 'homework', 'exam') else 'medium' if c.type == 'resource' else 'high',
        polarity=c.polarity, tense=c.tense, status=status, summary=c.summary,
        action=c.action if status == 'actionable' else None, due_text=c.due_text,
        start_seconds=start, end_seconds=end,
        sources=[Source(path=str(source_path), segment_ids=c.segment_ids, quote=c.quote)],
        review_reason='；'.join(reasons), listen_start_seconds=max(0, start - 20) if start is not None else None,
        listen_end_seconds=end + 20 if end is not None else None)


def analyze(segments, provider, source_path: Path, output: Path, lecture_date=None, use_cache=True):
    warnings = ['仅分析所提供字幕，无法判断字幕漏句、静音、未录制活动或画面通知；未检测到不代表未发生。',
                'analyzed_intervals 仅表示已处理字幕片段区间，片段间停顿不视为录播缺失。']
    simulated = provider.name == 'mock'
    if simulated:
        warnings.append('MOCK：仅匹配固定合成样例，用于链路自检，不具备真实语义检测能力。')
    if lecture_date is None:
        warnings.append('课程日期未知，相对日期需人工核验。')
    if any(s.start is None for s in segments):
        warnings.append('TXT 没有时间轴，时间戳为 null，不生成虚构时间。')
    if any(len(s.text) > 12000 for s in segments):
        warnings.append('存在超长单条字幕，完整发送该片段，可能超出模型上下文限制。')
    all_events, completed, failed = [], set(), []
    cache = output / '.cache'
    if use_cache:
        cache.mkdir(parents=True, exist_ok=True)
    for idx, chunk in enumerate(chunks(segments)):
        key = digest([provider.identity, str(lecture_date), [s.model_dump() for s in chunk]])
        cache_file = cache / (key + '.json')
        try:
            extraction = None
            if use_cache and cache_file.exists():
                try:
                    extraction = Extraction.model_validate_json(cache_file.read_text('utf-8'))
                    verify(extraction, chunk)
                except (ValueError, ProviderError):
                    extraction = None
            if extraction is None:
                extraction = provider.extract(chunk, lecture_date)
                verify(extraction, chunk)
                if use_cache:
                    cache_file.write_text(extraction.model_dump_json(indent=2), encoding='utf-8')
            all_events.extend(make_event(c, chunk, source_path) for c in extraction.events)
            completed.update(s.id for s in chunk)
        except ProviderError as exc:
            failed.append(idx)
            warnings.append(f'分块 {idx} 失败：{exc}')
    # Overlapping windows merge only matching category AND shared source evidence.
    merged = []
    for event in all_events:
        ids = set(event.sources[0].segment_ids)
        existing = next((e for e in merged if e.type == event.type and
            any(ids.intersection(src.segment_ids) for src in e.sources)), None)
        if existing is None:
            merged.append(event)
            continue
        if (existing.polarity, existing.status, existing.action) != (event.polarity, event.status, event.action):
            existing.status = 'needs_review'
            existing.action = None
            existing.review_reason += '；重叠窗口解释不一致，请核对所有原句'
        for source in event.sources:
            if source not in existing.sources:
                existing.sources.append(source)
        if event.start_seconds is not None:
            existing.start_seconds = min(existing.start_seconds, event.start_seconds)
            existing.end_seconds = max(existing.end_seconds, event.end_seconds)
            existing.listen_start_seconds = max(0, existing.start_seconds - 20)
            existing.listen_end_seconds = existing.end_seconds + 20
    for event in merged:
        if any(e.type == event.type and (e.polarity != event.polarity or e.status == 'canceled')
               and e is not event for e in merged):
            event.review_reason += '；同类事项出现否定/取消/不同表述，可能有变更，须交叉核对'
            if event.status == 'actionable':
                event.status, event.action = 'needs_review', None
    status = 'failed' if not completed else 'partial' if failed else 'completed'
    if not segments:
        warnings.append('字幕为空，无法得出检测结论。')
    return Report(lecture_id=source_path.stem, lecture_date=lecture_date, provider=provider.name,
        simulated=simulated, analysis_status=status,
        coverage_status='partial' if failed and completed else 'unknown',
        detection_status='events_detected' if merged else 'inconclusive' if simulated or failed or not completed else 'no_event_detected',
        provided_subtitle_end_seconds=max((s.end for s in segments if s.end is not None), default=None),
        analyzed_intervals=intervals([s for s in segments if s.id in completed]),
        total_segments=len(segments), analyzed_segment_ids=sorted(completed), failed_chunk_ids=failed,
        warnings=warnings, events=merged)
