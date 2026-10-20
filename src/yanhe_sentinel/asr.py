"""Resumable local faster-whisper transcription on the original media timeline."""

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import time
import wave

from pydantic import Field

from .detection import digest
from .media_tools import MediaError, extract_audio, local_file, probe_media, select_audio, wave_duration
from .reporting import stamp
from .schema import Interval, MediaQuality, Segment, StrictModel

PIPELINE_VERSION = 'asr-1'
SAMPLE_RATE = 16000


class ASRCue(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


class ChunkResult(StrictModel):
    key: str
    index: int
    begin: float
    end: float
    cues: list[ASRCue]
    silence: list[Interval]
    timing_warnings: list[str] = Field(default_factory=list)
    provider_model: str | None = None
    provider_name: str | None = None
    timestamp_quality: str | None = None


@dataclass
class ASROptions:
    backend: str = 'openai-compatible'
    model: str = ''
    device: str = 'cpu'
    compute_type: str = 'int8'
    language: str = 'zh'
    chunk_seconds: int = 300
    context_seconds: int = 5
    beam_size: int = 5
    cpu_threads: int = 4
    download_root: str | None = None
    local_files_only: bool = False
    revision: str | None = None
    vad_filter: bool = True

    def __post_init__(self):
        if self.backend == 'faster-whisper' and not self.model:
            self.model = 'small'

    def validate(self):
        if self.backend not in ('faster-whisper', 'openai-compatible', 'groq', 'gemini'):
            raise MediaError('不支持所选转写服务。')
        if self.backend != 'faster-whisper' and self.local_files_only:
            raise MediaError('--asr-local-only 仅适用于显式选择的 --asr-provider faster-whisper，不会改用云端。')
        if self.backend != 'faster-whisper' and self.model and Path(self.model).is_dir():
            raise MediaError('本地模型目录需要显式选择 --asr-provider faster-whisper。')
        if not 15 <= self.chunk_seconds <= 1800 or not 0 <= self.context_seconds < self.chunk_seconds / 2:
            raise MediaError('ASR 分块需为 15–1800 秒，重叠上下文需小于块长的一半。')
        if not 1 <= self.beam_size <= 10 or not 1 <= self.cpu_threads <= 64:
            raise MediaError('ASR beam-size 需为 1–10，CPU 线程需为 1–64。')


@dataclass
class Transcription:
    segments: list[Segment]
    quality: MediaQuality
    audio_path: Path


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def atomic_text(path: Path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    temporary.write_text(text, encoding='utf-8')
    for attempt in range(5):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            # Windows readers/antivirus can briefly hold the destination open.
            if attempt == 4:
                raise
            time.sleep(.05 * 2 ** attempt)


def merged_intervals(items):
    result = []
    for item in sorted(items, key=lambda x: x.start_seconds):
        if result and item.start_seconds <= result[-1].end_seconds + .01:
            result[-1].end_seconds = max(result[-1].end_seconds, item.end_seconds)
        else:
            result.append(item.model_copy())
    return result


def map_interval(start, end, offset, duration):
    start, end = max(0, start + offset), min(duration, end + offset)
    return Interval(start_seconds=start, end_seconds=end) if end > start else None


def missing_transcript(segments, processed, minimum=15):
    """No transcript is a review signal, never proof of missing recording or silence."""
    result = []
    for span in processed:
        cursor = span.start_seconds
        for s in sorted(segments, key=lambda x: x.start):
            if s.end <= cursor or s.start >= span.end_seconds:
                continue
            if s.start - cursor >= minimum:
                result.append(Interval(start_seconds=cursor, end_seconds=min(s.start, span.end_seconds)))
            cursor = max(cursor, min(s.end, span.end_seconds))
        if span.end_seconds - cursor >= minimum:
            result.append(Interval(start_seconds=cursor, end_seconds=span.end_seconds))
    return result


def silence_intervals(samples, begin):
    import numpy as np
    # 1 s RMS windows; retain >=3 s low-energy stretches. Quiet speech may be included.
    result, run_start = [], None
    for index in range(0, len(samples), SAMPLE_RATE):
        window = samples[index:index + SAMPLE_RATE]
        quiet = float(np.sqrt(np.mean(window.astype('float64') ** 2))) < 10 ** (-45 / 20)
        if quiet and run_start is None:
            run_start = index / SAMPLE_RATE
        if not quiet and run_start is not None:
            if index / SAMPLE_RATE - run_start >= 3:
                result.append(Interval(start_seconds=begin + run_start, end_seconds=begin + index / SAMPLE_RATE))
            run_start = None
    if run_start is not None and len(samples) / SAMPLE_RATE - run_start >= 3:
        result.append(Interval(start_seconds=begin + run_start, end_seconds=begin + len(samples) / SAMPLE_RATE))
    return result


def load_model(options):
    if options.backend != 'faster-whisper':
        from .cloud_asr import CloudRecognizer
        return CloudRecognizer(options)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise MediaError('未安装转写依赖。请运行 pip install -e ".[asr]"。') from None
    try:
        return WhisperModel(options.model, device=options.device, compute_type=options.compute_type,
            cpu_threads=options.cpu_threads, download_root=options.download_root,
            local_files_only=options.local_files_only, revision=options.revision)
    except Exception:
        raise MediaError('ASR 模型加载失败：检查网络、模型路径、设备与精度。可使用 --asr-model 本地模型目录，或设置 HF_ENDPOINT 切换可信模型源。') from None


def transcribe(media_path: Path, output: Path, options: ASROptions, *, audio_source=None,
               audio_stream=None, audio_offset=None, ffmpeg=None, ffprobe=None,
               use_cache=True, model_factory=None, progress=print) -> Transcription:
    options.validate()
    try:
        import numpy as np
    except ImportError:
        raise MediaError('未安装转写依赖。请运行 pip install -e ".[asr]"。') from None
    media_path = local_file(media_path)
    source_path = local_file(audio_source) if audio_source else media_path
    if audio_source and audio_offset is None:
        raise MediaError('独立音轨需要显式设置 --audio-offset（已确认同步也需填写 0）；不能假定音画同步。')
    if audio_offset is not None and not math.isfinite(audio_offset):
        raise MediaError('音轨偏移必须为有限秒数。')
    media_info = probe_media(media_path, ffprobe)
    audio_info = probe_media(source_path, ffprobe) if source_path != media_path else media_info
    stream = select_audio(audio_info, audio_stream)
    offset = stream.start_seconds + (audio_offset or 0)
    alignment = 'user_offset' if audio_source or audio_offset is not None else 'same_media'
    duration = media_info.duration_seconds
    warnings = ['语音识别可能漏掉日期、数字和否定词，字幕证据必须回听原媒体。',
                '画面通知与未录入所选音轨的声音不在 ASR 检测范围内。']
    if options.backend != 'faster-whisper':
        warnings.append('使用云端转写：所选音轨按块发送到用户配置的服务；原视频不整段上传。')
    native_gemini = False
    if options.backend == 'gemini':
        from .cloud_asr import cloud_settings
        native_gemini = cloud_settings(options.backend, options.model)[2] == 'gemini-3.5-transcribe'
    if alignment == 'user_offset':
        warnings.append('独立音轨/手动偏移由用户提供，尚未自动验证音画同步；偏移正值表示声音发生在视频更晚的位置。')
    if len(audio_info.audio_streams) > 1 and audio_stream is None:
        warnings.append('存在多个音轨，默认选择标为 default 的音轨，否则选择首个音轨；其他音轨未分析。')
    output.mkdir(parents=True, exist_ok=True)
    atomic_text(output / 'media.json', json.dumps({'media': media_info.model_dump(),
        'audio_source': audio_info.model_dump(), 'selected_stream': stream.index,
        'offset_seconds': offset, 'alignment': alignment}, ensure_ascii=False, indent=2))
    progress('正在校验媒体内容并准备本地音轨…')
    input_hash = file_hash(source_path)
    cache_root = output / '.cache' / 'asr'
    audio_key = digest([PIPELINE_VERSION, input_hash, stream.index])
    audio_path = cache_root / audio_key / 'audio.wav'
    try:
        decoded_duration = wave_duration(audio_path) if use_cache and audio_path.exists() else 0
    except (OSError, EOFError, wave.Error, MediaError):
        decoded_duration = 0
    if not decoded_duration:
        decoded_duration = extract_audio(source_path, stream.index, audio_path, ffmpeg)
    if stream.duration_seconds is not None and abs(decoded_duration - stream.duration_seconds) > max(1, .01 * stream.duration_seconds):
        warnings.append('解码音频时长与流元数据不一致，可能有断流或时间戳异常；不能据此判定完整录制。')
    if offset > .5 or offset + decoded_duration < duration - .5 or offset < -.5:
        warnings.append('所选音轨在视频时间轴上存在未覆盖或超出范围的区间。')
    if offset + decoded_duration < duration - .1:
        warnings.append(f'所选音轨解码后比视频时间轴末尾短 {duration - max(0, offset + decoded_duration):.3f} 秒；该尾部未覆盖。')
    try:
        engine_version = version('faster-whisper' if options.backend == 'faster-whisper' else 'httpx')
    except Exception:
        engine_version = 'unavailable'
    model_path = Path(options.model)
    model_identity = options.model
    if options.backend == 'faster-whisper' and model_path.is_dir():
        model_identity = [str(model_path.resolve()), [(str(p.relative_to(model_path)), p.stat().st_size,
            p.stat().st_mtime_ns) for p in sorted(model_path.rglob('*')) if p.is_file() and '.cache' not in p.relative_to(model_path).parts]]
    settings = {**vars(options), 'model': model_identity}
    if options.backend != 'faster-whisper':
        from .cloud_asr import cloud_settings
        _, remote_base, remote_model = cloud_settings(options.backend, options.model)
        settings['remote_identity'] = [remote_base, remote_model]
    key = digest([PIPELINE_VERSION, audio_key, engine_version, settings])
    chunk_root = cache_root / key
    chunk_root.mkdir(parents=True, exist_ok=True)
    actual_model = options.model if options.backend == 'faster-whisper' else remote_model
    timing_quality = 'generative_estimate' if options.backend == 'gemini' and not native_gemini else 'asr_estimate'
    alternatives = []
    resume_entries = []
    profile = {k: v for k, v in settings.items() if k not in ('backend', 'model', 'remote_identity')}
    resume_file = output / 'asr_resume.json'
    if use_cache and options.backend != 'faster-whisper' and resume_file.exists():
        try:
            manifest = json.loads(resume_file.read_text('utf-8'))
            if manifest['audio_key'] == audio_key:
                for entry in manifest['caches']:
                    prior = entry['settings']
                    prior_profile = {k: v for k, v in prior.items() if k not in ('backend', 'model', 'remote_identity')}
                    if prior_profile != profile or prior['backend'] not in ('gemini', 'groq', 'openai-compatible'):
                        continue
                    prior_key = digest([PIPELINE_VERSION, audio_key, engine_version, prior])
                    if entry['key'] != prior_key or prior_key == key:
                        continue
                    prior_model = prior['remote_identity'][1]
                    prior_timing = 'generative_estimate' if prior['backend'] == 'gemini' and prior_model != 'gemini-3.5-transcribe' else 'asr_estimate'
                    alternatives.append((cache_root / prior_key, prior_key, prior_model, prior_timing, prior['backend']))
                    resume_entries.append(entry)
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            alternatives, resume_entries = [], []
    if use_cache and options.backend == 'gemini' and remote_model == 'gemini-3.8-flash':
        # Only reuse the exact same source, service endpoint and chunk profile.
        alternate_settings = {**settings, 'model': 'gemini-3.5-transcribe',
            'remote_identity': [remote_base, 'gemini-3.5-transcribe']}
        alternate_key = digest([PIPELINE_VERSION, audio_key, engine_version, alternate_settings])
        alternatives.append((cache_root / alternate_key, alternate_key, 'gemini-3.5-transcribe', 'asr_estimate', 'gemini'))
        resume_entries.append({'key': alternate_key, 'settings': alternate_settings})
    if use_cache and options.backend != 'faster-whisper':
        resume_entries.append({'key': key, 'settings': settings})
        atomic_text(resume_file, json.dumps({'audio_key': audio_key,
            'caches': list({entry['key']: entry for entry in resume_entries}.values())}, ensure_ascii=False, indent=2))
    total = math.ceil(decoded_duration / options.chunk_seconds)
    completed, failed, all_cues, all_silences, processed = [], [], [], [], []
    errors = []
    chunk_sources = []
    def checkpoint(current, state='running'):
        atomic_text(output / 'asr_progress.json', json.dumps({'status': state, 'current': current,
            'total': total, 'completed_chunks': len(completed), 'failed_chunks': len(failed),
            'completed_chunk_ids': completed, 'failed_chunk_ids': failed,
            'errors': errors}, ensure_ascii=False, indent=2))
    model, model_failed = None, False
    with wave.open(str(audio_path), 'rb') as audio:
        for index in range(total):
            checkpoint(index + 1)
            begin = index * options.chunk_seconds
            end = min(decoded_duration, begin + options.chunk_seconds)
            read_begin = max(0, begin - options.context_seconds)
            read_end = min(decoded_duration, end + options.context_seconds)
            chunk_file = chunk_root / f'{index:05}.json'
            result = None
            candidates = [(chunk_root, key, actual_model, timing_quality, options.backend), *alternatives] if use_cache else []
            for cached_root, cached_key, cached_model, cached_timing, cached_provider in candidates:
                cached_file = cached_root / f'{index:05}.json'
                if not cached_file.exists():
                    continue
                try:
                    result = ChunkResult.model_validate_json(cached_file.read_text('utf-8'))
                    if (result.key, result.index, result.begin, result.end) != (cached_key, index, begin, end):
                        result = None
                    elif any(c.end < c.start or c.start < read_begin or c.end > read_end + .1 for c in result.cues):
                        result = None
                except (OSError, ValueError):
                    result = None
                if result is not None:
                    result.provider_model = result.provider_model or cached_model
                    result.provider_name = result.provider_name or cached_provider
                    result.timestamp_quality = result.timestamp_quality or cached_timing
                    if cached_key != key:
                        result.key = key
                        atomic_text(chunk_file, result.model_dump_json(indent=2))
                    break
            if result is None:
                progress(f'转写分块 {index + 1}/{total}（{stamp(begin)} → {stamp(end)}）…')
                audio.setpos(round(read_begin * SAMPLE_RATE))
                raw = audio.readframes(round((read_end - read_begin) * SAMPLE_RATE))
                samples = np.frombuffer(raw, dtype='<i2').astype('float32') / 32768
                if abs(len(samples) / SAMPLE_RATE - (read_end - read_begin)) > .1:
                    failed.append(index)
                    message = f'ASR 分块 {index} 的 PCM 数据不完整。'
                    warnings.append(message)
                    errors.append({'chunk': index + 1, 'message': message})
                    checkpoint(index + 1)
                    progress(message)
                    continue
                core = samples[round((begin-read_begin)*SAMPLE_RATE):round((end-read_begin)*SAMPLE_RATE)]
                silence = silence_intervals(core, begin)
                try:
                    cues = []
                    timing_warnings = []
                    # Exact digital silence cannot carry speech; avoid hallucinations and downloads.
                    if np.max(np.abs(samples), initial=0) > .0001:
                        if model_failed:
                            raise MediaError('此前模型/服务不可用，保留已有分块；请修正配置后重试。')
                        if model is None:
                            try:
                                model = (model_factory or load_model)(options)
                            except MediaError:
                                model_failed = True
                                raise
                        try:
                            cloud_context = {'required_start': begin - read_begin, 'required_end': end - read_begin,
                                '_cache_directory': str(chunk_root / 'short') if use_cache else None} if options.backend != 'faster-whisper' else {}
                            iterator, information = model.transcribe(samples, language=None if options.language == 'auto' else options.language,
                                beam_size=options.beam_size, vad_filter=options.vad_filter,
                                condition_on_previous_text=False, word_timestamps=False, **cloud_context)
                            timing_warnings = list(getattr(information, 'timing_warnings', []))
                            for item in iterator:
                                if not math.isfinite(float(item.start)) or not math.isfinite(float(item.end)):
                                    raise MediaError('ASR 返回了非有限时间戳。')
                                start = max(read_begin, read_begin + float(item.start))
                                stop = min(read_end, read_begin + float(item.end))
                                center = (start + stop) / 2
                                text = item.text.strip()
                                if text and stop > start and begin <= center < end:
                                    cues.append(ASRCue(start=start, end=stop, text=text,
                                        avg_logprob=getattr(item, 'avg_logprob', None),
                                        no_speech_prob=getattr(item, 'no_speech_prob', None)))
                        except MediaError:
                            raise
                        except Exception:
                            raise MediaError('ASR 推理失败；检查内存、模型/设备及音轨，成功分块已保留。') from None
                    result = ChunkResult(key=key, index=index, begin=begin, end=end, cues=cues, silence=silence,
                        timing_warnings=timing_warnings, provider_model=actual_model,
                        provider_name=options.backend, timestamp_quality=timing_quality)
                    if use_cache:
                        atomic_text(chunk_file, result.model_dump_json(indent=2))
                except MediaError as exc:
                    if options.backend != 'faster-whisper':
                        from .cloud_asr import CloudAuthError, CloudRateLimitError
                        if isinstance(exc, (CloudAuthError, CloudRateLimitError)):
                            model_failed = True
                    failed.append(index)
                    warnings.append(f'ASR 分块 {index} 失败：{exc}')
                    errors.append({'chunk': index + 1, 'message': str(exc)})
                    checkpoint(index + 1)
                    progress(f'转写分块 {index + 1}/{total} 失败：{exc}；成功分块已保留。')
                    continue
            else:
                progress(f'复用转写分块 {index + 1}/{total}。')
            completed.append(index)
            chunk_sources.append({'index': index, 'start_seconds': begin, 'end_seconds': end,
                'model': result.provider_model or actual_model,
                'provider': result.provider_name or options.backend,
                'timestamp_quality': result.timestamp_quality or timing_quality})
            warnings.extend(f'ASR 分块 {index + 1}：{message}' for message in result.timing_warnings)
            checkpoint(index + 1)
            all_cues.extend(result.cues)
            all_silences.extend(result.silence)
            span = map_interval(begin, end, offset, duration)
            if span:
                processed.append(span)
    checkpoint(total, 'partial' if failed else 'completed')
    atomic_text(output / 'asr_chunks.json', json.dumps(chunk_sources, ensure_ascii=False, indent=2))
    used_models = sorted({item['model'] for item in chunk_sources})
    used_providers = sorted({item['provider'] for item in chunk_sources})
    used_timing = {item['timestamp_quality'] for item in chunk_sources}
    if 'generative_estimate' in used_timing:
        warnings.append('Gemini 音频时间戳是生成式估计，尚未逐词强制对齐，必须用原音频核验。')
    if len(used_models) > 1 or len(used_providers) > 1:
        warnings.append('字幕复用了不同模型的成功分块；逐块模型与时间戳类型见 asr_chunks.json，需回听核验。')
    segments, low_quality = [], []
    for item in sorted(all_cues, key=lambda c: (c.start, c.end)):
        span = map_interval(item.start, item.end, offset, duration)
        if span is None:
            continue
        if any(s.text == item.text and s.start < span.end_seconds and s.end > span.start_seconds for s in segments[-5:]):
            continue
        segment = Segment(id=len(segments) + 1, start=span.start_seconds, end=span.end_seconds, text=item.text)
        segments.append(segment)
        if (item.avg_logprob is not None and item.avg_logprob < -1) or (item.no_speech_prob is not None and item.no_speech_prob > .6):
            low_quality.append(segment.id)
    processed = merged_intervals(processed)
    silences = merged_intervals([span for s in all_silences
        if (span := map_interval(s.start_seconds, s.end_seconds, offset, duration)) is not None])
    gaps = missing_transcript(segments, processed)
    if silences:
        warnings.append('存在连续低音量区间（1 秒 RMS、低于 -45 dBFS、持续至少 3 秒）；也可能是轻声说话，需人工核验。')
    if gaps:
        warnings.append('存在至少 15 秒无字幕区间；可能是停顿、静音或识别漏句，不能据字幕空白断言无通知。')
    if low_quality:
        warnings.append('部分字幕的 ASR 分值偏低，请重点回听对应字幕编号。')
    if not segments:
        warnings.append('未产生有效语音字幕，不能据此得出没有重要事项的结论。')
    quality = MediaQuality(input_path=str(media_path), audio_source_path=str(source_path),
        audio_stream_index=stream.index, media_duration_seconds=duration,
        decoded_audio_seconds=decoded_duration, audio_offset_seconds=offset, alignment=alignment,
        transcription_status='completed' if not failed else 'partial' if completed else 'failed',
        asr_model=' + '.join(used_models) if used_models else actual_model,
        asr_device=options.device if options.backend == 'faster-whisper' else 'cloud',
        asr_compute_type=options.compute_type if options.backend == 'faster-whisper' else 'service',
        asr_provider=' + '.join(used_providers) if used_providers else options.backend,
        timestamp_quality='generative_estimate' if 'generative_estimate' in used_timing else 'asr_estimate',
        asr_language=options.language, total_chunks=total, completed_chunk_ids=completed, failed_chunk_ids=failed,
        processed_intervals=processed, silence_intervals=silences, no_transcript_intervals=gaps,
        low_quality_segment_ids=low_quality, warnings=warnings)
    transcript = Transcription(segments=segments, quality=quality, audio_path=audio_path)
    write_transcription(transcript, output)
    return transcript


def write_transcription(transcription, output):
    srt = '\n\n'.join(f'{s.id}\n{stamp(s.start).replace(".", ",")} --> {stamp(s.end).replace(".", ",")}\n{s.text}'
                      for s in transcription.segments)
    atomic_text(output / 'transcript.srt', srt + ('\n' if srt else ''))
    atomic_text(output / 'transcript.json', json.dumps({'segments': [s.model_dump() for s in transcription.segments],
        'media': transcription.quality.model_dump()}, ensure_ascii=False, indent=2))
    atomic_text(output / 'asr_quality.json', transcription.quality.model_dump_json(indent=2))


def attach_media(report, transcription):
    """Combine independent ASR and extraction status without hiding either failure."""
    q = transcription.quality
    report.media = q
    report.lecture_id = Path(q.input_path).stem
    report.media_duration_seconds = q.media_duration_seconds
    report.warnings.extend(q.warnings)
    if q.transcription_status == 'failed':
        report.analysis_status = 'failed'
    elif q.transcription_status == 'partial' and report.analysis_status == 'completed':
        report.analysis_status = 'partial'
    complete = (len(q.processed_intervals) == 1 and q.processed_intervals[0].start_seconds <= .1 and
                q.processed_intervals[0].end_seconds >= q.media_duration_seconds - .1)
    if q.transcription_status == 'completed' and report.analysis_status == 'completed' and complete:
        report.coverage_status = 'complete_for_provided_media'
    else:
        report.coverage_status = 'partial' if q.processed_intervals else 'unknown'
    if not report.events and (q.transcription_status != 'completed' or not transcription.segments
                             or not complete or q.no_transcript_intervals or q.low_quality_segment_ids):
        report.detection_status = 'inconclusive'
    for event in report.events:
        if event.listen_end_seconds is not None:
            event.listen_end_seconds = min(event.listen_end_seconds, q.media_duration_seconds)
        if any(sid in q.low_quality_segment_ids for source in event.sources for sid in source.segment_ids):
            event.review_reason += '；该事件包含低分 ASR 字幕，优先回听'
    return report
