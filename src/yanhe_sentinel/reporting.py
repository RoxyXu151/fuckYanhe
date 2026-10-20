import json
from pathlib import Path

from .schema import Report


def stamp(seconds):
    if seconds is None:
        return '时间未知'
    milliseconds = round(seconds * 1000)
    h, rest = divmod(milliseconds, 3600000)
    m, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return f'{h:02}:{m:02}:{s:02}.{ms:03}'


def safe(text):
    return str(text).replace('<', '&lt;').replace('>', '&gt;').replace('[', '\\[').replace(']', '\\]')


def export(report: Report, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    # Serialize through schema once more before writing artifacts.
    Report.model_validate_json(report.model_dump_json())
    (output / 'events.json').write_text(report.model_dump_json(indent=2), encoding='utf-8')
    quality = report.model_dump(mode='json', exclude={'events'})
    quality['analyzed_subtitle_seconds'] = sum(i.end_seconds - i.start_seconds for i in report.analyzed_intervals)
    (output / 'quality.json').write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'events.schema.json').write_text(json.dumps(Report.model_json_schema(), ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 延河课堂重要事项报告', '', '**实验性结果，需人工复核。**', '',
             f'课次：{safe(report.lecture_id)} | 日期：{report.lecture_date or "未知"}',
             f'分析：{report.analysis_status} | 媒体覆盖：{report.coverage_status} | 检测：{report.detection_status}',
             f'已处理字幕：{len(report.analyzed_segment_ids)}/{report.total_segments} 段；字幕区间合计 {quality["analyzed_subtitle_seconds"]:.3f} 秒（不是完整课堂时长）。', '']
    if report.simulated:
        lines.extend(['**模拟演示：固定合成样例，不是真实课程/真实模型结果。**', ''])
    if report.media:
        q = report.media
        lines.extend(['## 原媒体与转写', '',
            f'- 原媒体：{safe(q.input_path)}；时长 {q.media_duration_seconds:.3f} 秒',
            f'- 所选音源：{safe(q.audio_source_path)}；全局音轨流编号 {q.audio_stream_index}',
            f'- 时间对齐：{q.alignment}；音轨偏移 {q.audio_offset_seconds:+.3f} 秒',
            f'- 转写服务：{q.asr_provider} / {safe(q.asr_model)} / {q.asr_device} / {q.asr_language}；时间戳：{q.timestamp_quality}',
            f'- 转写状态：{q.transcription_status}；成功 {len(q.completed_chunk_ids)}/{q.total_chunks} 块；失败编号 {q.failed_chunk_ids}',
            '- 覆盖仅对应所选音轨及提供的媒体，不能判断画面通知或真实课堂是否完整录制。', ''])
        for label, spans in [('已转写音轨区间', q.processed_intervals), ('疑似低音量区间', q.silence_intervals),
                             ('连续无字幕区间', q.no_transcript_intervals)]:
            lines += [label + '：' + ('；'.join(f'{stamp(i.start_seconds)} → {stamp(i.end_seconds)}' for i in spans) or '无'), '']
    if report.visual:
        v = report.visual
        lines += ['## 多模态画面辅助', '',
            f'- 原视频：{safe(v.input_path)}',
            f'- 服务：{v.provider} / {safe(v.model)}；实际抽样间隔 {v.sampling_interval_seconds:.3f} 秒',
            f'- 已分析 {len(v.analyzed_frame_seconds)}/{len(v.requested_frame_seconds)} 帧；失败时间 {v.failed_frame_seconds}',
            '- 抽样不能覆盖所有画面；画面模型识别文字需打开截图核验。', '']
    lines.extend(['## 覆盖与风险', ''] + ['- ' + safe(w) for w in report.warnings] + [''])
    if not report.events:
        lines += ['NO_EVENT_DETECTED：所提供字幕未检出事件。' if report.detection_status == 'no_event_detected'
                  else 'INCONCLUSIVE：无法得出无事件结论。', '']
    for n, event in enumerate(report.events, 1):
        lines += [f'## {n}. {safe(event.summary)}', '',
            f'- 类别：{event.type}；严重度：{event.severity}；状态：{event.status}',
            f'- 肯否：{event.polarity}；时态：{event.tense}',
            f'- 时间：{stamp(event.start_seconds)} → {stamp(event.end_seconds)}',
            f'- 行动：{safe(event.action or "待核验/无需新行动")}',
            f'- 截止原文：{safe(event.due_text or "未给出")}；绝对日期：未计算',
            f'- 人工核验：是；{safe(event.review_reason)}',
            f'- 建议回听：{stamp(event.listen_start_seconds)} → {stamp(event.listen_end_seconds)}（终点以视频实际长度为准）', '']
        if event.audio_clip_path:
            lines += [f'本地回听音频：{safe(event.audio_clip_path)}（应同时核对原媒体与字幕）', '']
        for source in event.sources:
            label = f'字幕编号 {source.segment_ids}' if source.kind == 'subtitle' else '截图；模型读取文字未自动核验'
            lines += [f'来源：{safe(source.path)}；{label}', '',
                      *['> ' + safe(line) for line in (source.quote or '未读出文字，请打开原图核验').splitlines()], '']
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
