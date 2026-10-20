import argparse
from datetime import date
from importlib.resources import files
import os
from pathlib import Path

from .detection import analyze
from .inputs import read_subtitles
from .media_tools import MediaError, export_clip, probe_media
from .providers import DeepSeekProvider, MockProvider, OpenAICompatibleProvider, ProviderError, vision_provider
from .reporting import export
from .schema import Report, VisualQuality


def media_options(parser, allow_subtitle=False):
    inputs = parser.add_mutually_exclusive_group(required=True)
    if allow_subtitle:
        inputs.add_argument('--subtitle', type=Path, help='本地 SRT/VTT/TXT')
    inputs.add_argument('--video', type=Path, help='本地视频（MP4/MKV 等）')
    inputs.add_argument('--audio', type=Path, help='本地音频（MP3/WAV 等）')
    parser.add_argument('--audio-source', type=Path, help='优先使用的独立话筒/录像音源')
    parser.add_argument('--audio-stream', type=int, help='音源中的全局音轨流编号，见 inspect')
    parser.add_argument('--audio-offset', type=float, help='音频零点相对主视频的偏移秒数；独立音源必填')
    parser.add_argument('--asr-provider', choices=['openai-compatible', 'groq', 'gemini', 'faster-whisper'],
        default=os.environ.get('YANHE_ASR_PROVIDER', 'openai-compatible'), help='默认云端；faster-whisper 为可选本地转写')
    parser.add_argument('--asr-model', help='云端转写模型名；本地后端时为模型名或模型目录')
    parser.add_argument('--asr-device', choices=['cpu', 'cuda', 'auto'], default='cpu')
    parser.add_argument('--asr-compute-type', default='int8')
    parser.add_argument('--asr-language', default='zh', help='语言代码或 auto')
    parser.add_argument('--asr-chunk-seconds', type=int, default=300)
    parser.add_argument('--asr-context-seconds', type=int, default=5)
    parser.add_argument('--asr-beam-size', type=int, default=5)
    parser.add_argument('--asr-cpu-threads', type=int, default=4)
    parser.add_argument('--asr-model-dir', help='下载模型缓存目录')
    parser.add_argument('--asr-revision', help='下载模型的固定 revision/commit')
    parser.add_argument('--asr-local-only', action='store_true', help='只使用已下载模型')
    parser.add_argument('--no-vad', action='store_true', help='关闭语音活动检测')
    parser.add_argument('--ffmpeg', help='FFmpeg 可执行文件路径')
    parser.add_argument('--ffprobe', help='ffprobe 可执行文件路径；缺省时可用 PyAV')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--no-cache', action='store_true')


def failure_report(path, output, message, *, provider='not_started', lecture_date=None, segments=None, transcription=None):
    segments = segments or []
    report = Report(lecture_id=path.stem, lecture_date=lecture_date, provider=provider,
        simulated=provider == 'mock', analysis_status='failed', coverage_status='unknown',
        detection_status='inconclusive', provided_subtitle_end_seconds=max(
            (s.end for s in segments if s.end is not None), default=None),
        analyzed_intervals=[], total_segments=len(segments), analyzed_segment_ids=[],
        failed_chunk_ids=[], warnings=[message, '本次分析未完成，不能据此判断没有重要事项。'], events=[])
    if transcription:
        from .asr import attach_media
        attach_media(report, transcription)
        report.coverage_status = 'partial' if transcription.quality.processed_intervals else 'unknown'
    export(report, output)


def parser_for_cli():
    parser = argparse.ArgumentParser(description='延河课堂重要事项雷达：本地音视频转写、字幕证据与通知检测')
    sub = parser.add_subparsers(dest='command', required=True)
    demo = sub.add_parser('demo', help='离线合成字幕自检（非语义检测）')
    demo.add_argument('--output', type=Path, default=Path('runs/demo'))
    run = sub.add_parser('analyze', help='本地字幕/音视频 → 重要事项报告')
    media_options(run, allow_subtitle=True)
    run.add_argument('--lecture-date', type=date.fromisoformat)
    run.add_argument('--provider', choices=['mock', 'openai-compatible', 'deepseek'], default='deepseek')
    run.add_argument('--encoding', default='utf-8-sig')
    run.add_argument('--export-clips', action='store_true', help='保存高风险事件前后约 20 秒回听 WAV')
    run.add_argument('--vision', action='store_true', help='把抽样截图发送到视觉模型辅助核验')
    run.add_argument('--vision-only', action='store_true', help='只分析视频抽样画面，不转写声音')
    run.add_argument('--vision-provider', choices=['deepseek', 'openai-compatible'], default='deepseek')
    run.add_argument('--vision-interval', type=float, default=30)
    run.add_argument('--vision-max-frames', type=int, default=240)
    transcribe_parser = sub.add_parser('transcribe', help='只转写本地音视频，不调用事件检测模型')
    media_options(transcribe_parser)
    inspect = sub.add_parser('inspect', help='查看本地媒体时长与音轨流编号')
    inspect.add_argument('--media', required=True, type=Path)
    inspect.add_argument('--ffprobe')
    webui = sub.add_parser('webui', help='打开本地 Web 工作台：登录、选课、转写与报告')
    webui.add_argument('--port', type=int, default=8765)
    webui.add_argument('--data-dir', type=Path, default=Path('runs/webui'))
    webui.add_argument('--no-browser', action='store_true')
    return parser


def main(argv=None):
    args = parser_for_cli().parse_args(argv)
    if args.command == 'webui':
        try:
            from .web.app import serve
        except ImportError:
            print('请先安装 Web 工作台依赖：pip install -e ".[web,media]"')
            return 2
        if not 1 <= args.port <= 65535:
            print('端口需为 1–65535')
            return 2
        serve(args.port, args.data_dir, not args.no_browser)
        return 0
    path, transcription, segments = Path('input'), None, []
    lecture_date = getattr(args, 'lecture_date', None)
    provider_name = getattr(args, 'provider', 'not_started')
    try:
        if args.command == 'analyze' and (args.vision or args.vision_only):
            from .vision import frame_times
            frame_times(1, args.vision_interval, args.vision_max_frames)
        if args.command == 'inspect':
            print(probe_media(args.media, args.ffprobe).model_dump_json(indent=2))
            return 0
        if args.command == 'demo':
            path = Path(str(files('yanhe_sentinel').joinpath('data/demo.srt')))
            provider, lecture_date, encoding, use_cache = MockProvider(), date(2026, 10, 18), 'utf-8-sig', True
            provider_name = 'mock'
            segments = read_subtitles(path, encoding)
        else:
            use_cache = not args.no_cache
            subtitle = getattr(args, 'subtitle', None)
            if subtitle:
                path = subtitle.resolve()
                if args.audio_source or args.audio_stream is not None or args.audio_offset is not None or args.export_clips or args.vision or args.vision_only:
                    raise MediaError('音轨选择/偏移/回听片段需要 --video 或 --audio，不能仅搭配字幕。')
                segments = read_subtitles(path, args.encoding)
            else:
                from .asr import ASROptions, transcribe
                path = (args.video or args.audio).resolve()
                if args.command == 'analyze' and (args.vision or args.vision_only) and not args.video:
                    raise MediaError('画面辅助需要使用 --video 输入。')
                options = ASROptions(backend=args.asr_provider,
                    model=args.asr_model or ('small' if args.asr_provider == 'faster-whisper' else ''), device=args.asr_device,
                    compute_type=args.asr_compute_type, language=args.asr_language,
                    chunk_seconds=args.asr_chunk_seconds, context_seconds=args.asr_context_seconds,
                    beam_size=args.asr_beam_size, cpu_threads=args.asr_cpu_threads,
                    download_root=args.asr_model_dir, local_files_only=args.asr_local_only,
                    revision=args.asr_revision, vad_filter=not args.no_vad)
                if not getattr(args, 'vision_only', False):
                    print('本地可选转写：首次使用可能下载模型。' if args.asr_provider == 'faster-whisper' else
                          '使用云端转写：将上传所选音轨的分块音频，不上传完整视频。', flush=True)
                    transcription = transcribe(path, args.output, options, audio_source=args.audio_source,
                        audio_stream=args.audio_stream, audio_offset=args.audio_offset, ffmpeg=args.ffmpeg,
                        ffprobe=args.ffprobe, use_cache=use_cache, progress=lambda msg: print(msg, flush=True))
                    segments = transcription.segments
                if args.command == 'transcribe':
                    print(f'转写 {transcription.quality.transcription_status}：{len(segments)} 段字幕；{args.output.resolve()}')
                    return 0 if transcription.quality.transcription_status == 'completed' and segments else 2
            provider = (MockProvider() if args.provider == 'mock' else DeepSeekProvider()
                        if args.provider == 'deepseek' else OpenAICompatibleProvider()) if not getattr(args, 'vision_only', False) else None
        source_path = args.output.resolve() / 'transcript.srt' if transcription else path
        if getattr(args, 'vision_only', False):
            if args.export_clips:
                raise MediaError('--vision-only 不提取音轨，不能搭配 --export-clips。')
            info = probe_media(path, args.ffprobe)
            report = Report(lecture_id=path.stem, lecture_date=lecture_date, provider='vision-only', simulated=False,
                analysis_status='completed', coverage_status='unknown', detection_status='inconclusive',
                media_duration_seconds=info.duration_seconds, provided_subtitle_end_seconds=None,
                analyzed_intervals=[], total_segments=0, analyzed_segment_ids=[], failed_chunk_ids=[],
                warnings=['仅分析抽样截图，未分析课堂声音；无检出不能证明无重要事项。'], events=[])
        else:
            if args.command != 'demo':
                print('正在分析字幕中的重要事项…', flush=True)
            report = analyze(segments, provider, source_path, args.output, lecture_date, use_cache)
        if transcription:
            from .asr import attach_media
            attach_media(report, transcription)
        if args.command == 'analyze' and (args.vision or args.vision_only):
            from .vision import inspect_frames
            try:
                visual_provider = vision_provider(args.vision_provider)
                inspect_frames(report, path, args.output, visual_provider, segments,
                    interval=args.vision_interval, maximum=args.vision_max_frames, ffmpeg=args.ffmpeg,
                    ffprobe=args.ffprobe, use_cache=use_cache, progress=lambda msg: print(msg, flush=True))
            except (ProviderError, MediaError) as exc:
                message = f'画面辅助未完成：{exc}'
                report.warnings.append(message)
                report.analysis_status = 'partial' if report.events else 'failed'
                report.coverage_status = 'partial' if report.events else 'unknown'
                report.visual = VisualQuality(input_path=str(path), provider=args.vision_provider,
                    model=os.environ.get('YANHE_VISION_MODEL') or 'not_configured',
                    sampling_interval_seconds=args.vision_interval, requested_frame_seconds=[],
                    analyzed_frame_seconds=[], failed_frame_seconds=[], warnings=[message])
                args.output.mkdir(parents=True, exist_ok=True)
                (args.output / 'vision_quality.json').write_text(report.visual.model_dump_json(indent=2), encoding='utf-8')
                if not report.events:
                    report.detection_status = 'inconclusive'
        clip_failed = False
        if transcription and args.export_clips:
            clip_dir = args.output / 'clips'
            clip_dir.mkdir(exist_ok=True)
            for event in report.events:
                if event.severity not in ('critical', 'high') or event.listen_start_seconds is None:
                    continue
                offset = transcription.quality.audio_offset_seconds
                start = max(0, event.listen_start_seconds - offset)
                end = min(transcription.quality.decoded_audio_seconds, event.listen_end_seconds - offset)
                try:
                    clip = clip_dir / f'{event.event_id}.wav'
                    export_clip(transcription.audio_path, clip, start, end, args.ffmpeg)
                    event.audio_clip_path = str(clip.resolve())
                except MediaError as exc:
                    clip_failed = True
                    report.warnings.append(f'事件 {event.event_id} 回听片段导出失败：{exc}')
        export(report, args.output)
        print(f'{"[MOCK 模拟] " if report.simulated else ""}{report.analysis_status}: {len(report.events)} 个事件；{args.output.resolve()}')
        return 0 if report.analysis_status == 'completed' and not clip_failed else 2
    except (MediaError, ProviderError) as exc:
        message = str(exc)
    except KeyboardInterrupt:
        message = '操作已中断，成功的转写/检测分块已缓存，重新执行原命令可继续。'
    except (OSError, ValueError, LookupError):
        message = '请检查本地文件路径、字幕格式/编码、输出目录与模型环境配置；未打印第三方错误正文。'
    if args.command != 'inspect':
        try:
            failure_report(path, args.output, message, provider=provider_name, lecture_date=lecture_date,
                           segments=segments, transcription=transcription)
        except (OSError, ValueError):
            pass
    print(f'处理失败：{message}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
