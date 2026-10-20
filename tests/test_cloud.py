"""Cloud contract tests. All HTTP is intercepted; no account or charge is required."""

import base64
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import httpx
import pytest

np = pytest.importorskip('numpy')
pytest.importorskip('av')
pytest.importorskip('imageio_ffmpeg')

from yanhe_sentinel.asr import ASROptions
from yanhe_sentinel.cli import main, parser_for_cli
from yanhe_sentinel.cloud_asr import CloudRecognizer
from yanhe_sentinel.media_tools import MediaError, ffmpeg_executable, run_tool
from yanhe_sentinel.providers import DeepSeekProvider, vision_provider
from yanhe_sentinel.schema import Report


@pytest.fixture(autouse=True)
def cloud_environment(monkeypatch):
    for name in ('YANHE_API_KEY', 'YANHE_BASE_URL', 'YANHE_MODEL', 'YANHE_ASR_PROVIDER', 'YANHE_ASR_API_KEY',
                 'YANHE_ASR_BASE_URL', 'YANHE_ASR_MODEL', 'YANHE_VISION_API_KEY', 'YANHE_VISION_BASE_URL',
                 'YANHE_VISION_MODEL', 'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_MODEL', 'GEMINI_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('yanhe_sentinel.cloud_asr.time.sleep', lambda _: None)
    monkeypatch.setattr('yanhe_sentinel.providers.time.sleep', lambda _: None)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: pytest.fail('Cloud path must never load local model weights')))


def response(url, data, status=200):
    return httpx.Response(status, request=httpx.Request('POST', url), json=data)


def configure_audio(monkeypatch):
    monkeypatch.setenv('YANHE_ASR_API_KEY', 'fake-asr-secret')
    monkeypatch.setenv('YANHE_ASR_BASE_URL', 'https://audio.example.invalid/v1')
    monkeypatch.setenv('YANHE_ASR_MODEL', 'hosted-transcriber')


def test_default_cli_uses_cloud_and_deepseek():
    args = parser_for_cli().parse_args(['analyze', '--video', 'movie.mp4', '--output', 'runs/test'])
    assert args.asr_provider == 'openai-compatible'
    assert args.provider == 'deepseek'
    assert args.asr_model is None


def test_audio_upload_and_timestamps(monkeypatch):
    configure_audio(monkeypatch)
    captured = []
    def post(self, url, **kwargs):
        captured.append(kwargs)
        return response(url, {'segments': [{'start': .2, 'end': 1.8, 'text': '下周交作业',
                           'tokens': [1, 2], 'avg_logprob': -.3}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    cues, _ = CloudRecognizer(ASROptions(backend='openai-compatible', model='')).transcribe(np.ones(32000, dtype='float32') * .1, language='zh')
    assert list(cues)[0].start == .2
    with wave.open(io.BytesIO(captured[0]['files']['file'][1])) as audio:
        assert audio.getframerate() == 16000
        assert audio.getnchannels() == 1
    assert captured[0]['data']['response_format'] == 'verbose_json'
    assert captured[0]['data']['timestamp_granularities[]'] == 'segment'


@pytest.mark.parametrize('segments', [[{'text': 'no times'}], [{'start': -1, 'end': 1, 'text': 'bad'}],
                                     [{'start': 0, 'end': 99, 'text': 'out of bounds'}]])
def test_cloud_does_not_fabricate_missing_or_invalid_timestamps(monkeypatch, segments):
    configure_audio(monkeypatch)
    monkeypatch.setattr(httpx.Client, 'post', lambda self, url, **kw: response(url, {'segments': segments}))
    with pytest.raises(MediaError):
        CloudRecognizer(ASROptions(backend='openai-compatible', model='')).transcribe(np.ones(32000), language='zh')


def test_gemini_audio_format_and_approximate_timestamps(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-gemini-secret')
    captured = []
    def post(self, url, **kwargs):
        captured.append((url, kwargs))
        return response(url, {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
            {'type': 'text', 'text': json.dumps({'segments': [{'start': .1, 'end': 1, 'text': '考试取消'}]})}]}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    cues, _ = CloudRecognizer(ASROptions(backend='gemini', model='')).transcribe(np.ones(32000) * .1, language='zh')
    assert list(cues)[0].text == '考试取消'
    url, kwargs = captured[0]
    assert url.endswith('/v1beta/interactions')
    assert kwargs['json']['store'] is False
    bounds = kwargs['json']['response_format']['properties']['segments']['items']['properties']
    assert bounds['start']['minimum'] == 0 and bounds['end']['maximum'] == 2
    audio = kwargs['json']['input'][0]
    assert audio['type'] == 'audio' and audio['mime_type'] == 'audio/wav'
    assert base64.b64decode(audio['data']).startswith(b'RIFF')


def test_gemini_incomplete_result_is_failure(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-gemini-secret')
    monkeypatch.setattr(httpx.Client, 'post', lambda self, url, **kw: response(url, {'status': 'incomplete'}))
    with pytest.raises(MediaError, match='未完成'):
        CloudRecognizer(ASROptions(backend='gemini', model='')).transcribe(np.ones(32000), language='zh')


def test_gemini_retries_invalid_timestamps_without_fabricating_them(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-gemini-secret')
    calls = []
    def post(self, url, **kwargs):
        calls.append(kwargs)
        segment = {'start': 0, 'end': 99 if len(calls) == 1 else 1.5, 'text': '下周交作业'}
        return response(url, {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
            {'type': 'text', 'text': json.dumps({'segments': [segment]})}]}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    cues, _ = CloudRecognizer(ASROptions(backend='gemini', model='')).transcribe(np.ones(32000))
    assert len(calls) == 2
    assert list(cues)[0].end == 1.5
    assert '上次输出未通过时间戳校验' in calls[1]['json']['input'][1]['text']


def test_gemini_auth_failure_is_not_retried_as_format_failure(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-gemini-secret')
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        return response(url, {'error': 'fake-gemini-secret'}, status=403)
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(MediaError):
        CloudRecognizer(ASROptions(backend='gemini', model='')).transcribe(np.ones(32000))
    assert len(calls) == 1


def test_lite_long_audio_uses_short_requests_and_preserves_audio_offsets(monkeypatch, tmp_path):
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-gemini-secret')
    durations = []
    def post(self, url, **kwargs):
        audio = base64.b64decode(kwargs['json']['input'][0]['data'])
        with wave.open(io.BytesIO(audio)) as reader:
            durations.append(reader.getnframes() / reader.getframerate())
        return response(url, {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
            {'type': 'text', 'text': json.dumps({'segments': [{'start': 3, 'end': 4, 'text': '考试不能用计算器'}]})}]}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    model = CloudRecognizer(ASROptions(backend='gemini', model='gemini-3.5-flash-lite'))
    samples = np.ones(310 * 16000, dtype='float32') * .1
    cues, info = model.transcribe(samples, language='zh', _cache_directory=str(tmp_path))
    assert durations == [122, 124, 72]
    assert [c.start for c in cues] == [3, 121, 241]
    assert '120 秒' in info.timing_warnings[0]
    monkeypatch.setattr(httpx.Client, 'post', lambda *_a, **_kw: pytest.fail('All child chunks should be cached'))
    cached, _ = model.transcribe(samples, language='zh', _cache_directory=str(tmp_path))
    assert [c.end for c in cached] == [4, 122, 242]


def test_lite_resume_keeps_successful_children_after_quota_failure(monkeypatch, tmp_path):
    from yanhe_sentinel.cloud_asr import CloudRateLimitError
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-gemini-secret')
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        if len(calls) == 2:
            return httpx.Response(429, json={'error': {}}, headers={'Retry-After': '3600'}, request=httpx.Request('POST', url))
        return response(url, {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
            {'type': 'text', 'text': json.dumps({'segments': [{'start': 3, 'end': 4, 'text': '作业'}]})}]}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    model = CloudRecognizer(ASROptions(backend='gemini', model='gemini-3.5-flash-lite'))
    samples = np.ones(310 * 16000, dtype='float32') * .1
    with pytest.raises(CloudRateLimitError):
        model.transcribe(samples, language='zh', _cache_directory=str(tmp_path))
    assert len(list(tmp_path.glob('*.json'))) == 1
    cues, _ = model.transcribe(samples, language='zh', _cache_directory=str(tmp_path))
    assert len(calls) == 4
    assert len(list(cues)) == 3


def test_native_gemini_word_timestamps_are_used_directly(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-gemini-secret')
    captured = []
    def post(self, url, **kwargs):
        captured.append(kwargs['json'])
        return response(url, {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [{
            'type': 'text', 'text': '下周交作业。', 'annotations': [
                {'type': 'word_info', 'text': '下周', 'start_offset': '0.200s', 'end_offset': '0.600s'},
                {'type': 'word_info', 'text': '交作业。', 'start_offset': '0.700s', 'end_offset': '1.500s'}]}]}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    cues, _ = CloudRecognizer(ASROptions(backend='gemini', model='gemini-3.5-transcribe')).transcribe(np.ones(32000))
    cue = list(cues)[0]
    assert (cue.start, cue.end, cue.text) == (.2, 1.5, '下周交作业。')
    assert captured[0]['generation_config']['transcription_config']['mode'] == {
        'type': 'verbatim', 'timestamp_granularities': ['word']}
    assert 'response_format' not in captured[0] and len(captured[0]['input']) == 1


def test_native_gemini_text_without_word_times_is_not_accepted():
    from yanhe_sentinel.cloud_asr import native_word_segments
    with pytest.raises(MediaError):
        native_word_segments({'steps': [{'type': 'model_output', 'content': [{'text': '没有时间戳'}]}]}, 10)
    with pytest.raises(MediaError):
        native_word_segments({'steps': [{'type': 'model_output', 'content': [{'annotations': [
            {'type': 'word_info', 'text': '作业', 'start_offset': '0s', 'end_offset': '99s'}]}]}]}, 10)


def test_native_overlapping_words_preserve_text_and_native_time_envelope():
    from yanhe_sentinel.cloud_asr import native_word_segments
    result = native_word_segments({'steps': [{'type': 'model_output', 'content': [{'annotations': [
        {'type': 'word_info', 'text': '提交', 'start_offset': '.2s', 'end_offset': '.8s'},
        {'type': 'word_info', 'text': '作业。', 'start_offset': '.4s', 'end_offset': '.7s'}]}]}]}, 2)
    assert result == {'segments': [{'start': .2, 'end': .8, 'text': '提交作业。'}]}


def test_native_empty_annotations_and_untimed_punctuation_do_not_lose_timed_words():
    from yanhe_sentinel.cloud_asr import native_word_segments
    result = native_word_segments({'steps': [{'type': 'model_output', 'content': [{'annotations': [
        {'type': 'word_info', 'text': ''},
        {'type': 'word_info', 'text': '作业', 'start_offset': '.2s', 'end_offset': '.8s'},
        {'type': 'word_info', 'text': '。'}]}]}]}, 2)
    assert result == {'segments': [{'start': .2, 'end': .8, 'text': '作业。'}]}
    with pytest.raises(MediaError, match='缺少时间戳'):
        native_word_segments({'steps': [{'type': 'model_output', 'content': [{'annotations': [
            {'type': 'word_info', 'text': '作业'}]}]}]}, 2)


def test_native_bad_trailing_context_does_not_discard_required_core():
    from yanhe_sentinel.cloud_asr import native_word_segments
    raw = {'steps': [{'type': 'model_output', 'content': [{'annotations': [
        {'type': 'word_info', 'text': '作业。', 'start_offset': '10s', 'end_offset': '11s'},
        {'type': 'word_info', 'text': '重叠尾词', 'start_offset': '309.9s', 'end_offset': '20s'}]}]}]}
    result = native_word_segments(raw, 310, required_start=5, required_end=305)
    assert result['segments'] == [{'start': 10, 'end': 11, 'text': '作业。'}]
    assert '相邻块' in result['_timing_warnings'][0]
    with pytest.raises(MediaError):
        native_word_segments(raw, 310)  # No adjacent core covers the bad word in this case.


def test_native_small_reversed_interval_has_explicit_review_warning():
    from yanhe_sentinel.cloud_asr import native_word_segments
    raw = {'steps': [{'type': 'model_output', 'content': [{'annotations': [
        {'type': 'word_info', 'text': '作业。', 'start_offset': '6.9s', 'end_offset': '6.8s'}]}]}]}
    result = native_word_segments(raw, 310)
    assert result['segments'] == [{'start': 6.8, 'end': 6.9, 'text': '作业。'}]
    assert '回听核验' in result['_timing_warnings'][0]
    raw['steps'][0]['content'][0]['annotations'][0]['end_offset'] = '1s'
    with pytest.raises(MediaError):
        native_word_segments(raw, 310)


def test_native_timing_warnings_survive_transcription_cache(synthetic_video, tmp_path):
    from yanhe_sentinel.asr import transcribe
    options = ASROptions(backend='gemini', model='gemini-3.5-transcribe', chunk_seconds=15)
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter([
        SimpleNamespace(start=.2, end=1.5, text='交作业')]), SimpleNamespace(timing_warnings=['时间标注需复核'])))
    out = tmp_path / 'timing'
    for _ in range(2):
        result = transcribe(synthetic_video, out, options, model_factory=lambda _: model, progress=lambda _: None)
        assert any('时间标注需复核' in w for w in result.quality.warnings)


def test_auth_redaction_and_no_retry(monkeypatch):
    configure_audio(monkeypatch)
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        return response(url, {'error': 'fake-asr-secret'}, status=401)
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(MediaError) as exc:
        CloudRecognizer(ASROptions(backend='openai-compatible', model='')).transcribe(np.ones(32000), language='zh')
    assert 'fake-asr-secret' not in str(exc.value)
    assert len(calls) == 1


def test_rate_limit_retries(monkeypatch):
    configure_audio(monkeypatch)
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        return response(url, {} if len(calls) < 3 else {'segments': []}, status=429 if len(calls) < 3 else 200)
    monkeypatch.setattr(httpx.Client, 'post', post)
    assert not list(CloudRecognizer(ASROptions(backend='openai-compatible', model='')).transcribe(np.ones(32000))[0])
    assert len(calls) == 3


def test_rate_limit_respects_service_retry_interval(monkeypatch):
    configure_audio(monkeypatch)
    calls, sleeps = [], []
    monkeypatch.setattr('yanhe_sentinel.cloud_asr.time.sleep', sleeps.append)
    def post(self, url, **kwargs):
        calls.append(url)
        result = response(url, {'segments': []}, status=429 if len(calls) == 1 else 200)
        result.headers['Retry-After'] = '17'
        return result
    monkeypatch.setattr(httpx.Client, 'post', post)
    CloudRecognizer(ASROptions(backend='openai-compatible', model='')).transcribe(np.ones(32000))
    assert sleeps == [17]


def test_long_rate_limit_does_not_retry_before_service_allows(monkeypatch):
    configure_audio(monkeypatch)
    from yanhe_sentinel.cloud_asr import CloudRateLimitError
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        result = response(url, {}, status=429)
        result.headers['Retry-After'] = '120'
        return result
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(CloudRateLimitError, match='120'):
        CloudRecognizer(ASROptions(backend='openai-compatible', model='')).transcribe(np.ones(32000))
    assert len(calls) == 1


def test_deepseek_profile(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-deepseek-secret')
    provider = DeepSeekProvider()
    assert provider.base == 'https://api.deepseek.com'
    assert provider.model == 'deepseek-flash'
    assert provider.name == 'deepseek'


def test_independent_vision_configuration_does_not_require_text_key(monkeypatch):
    monkeypatch.setenv('YANHE_VISION_API_KEY', 'fake-vision-secret')
    monkeypatch.setenv('YANHE_VISION_BASE_URL', 'https://vision.example.invalid/v1')
    monkeypatch.setenv('YANHE_VISION_MODEL', 'vision-model')
    assert vision_provider('openai-compatible').model == 'vision-model'


@pytest.fixture
def synthetic_video(tmp_path):
    path = tmp_path / 'synthetic.mp4'
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
        '-i', 'color=c=black:s=128x72:r=10', '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=16000',
        '-t', '2', '-c:v', 'libx264', '-c:a', 'aac', path])
    return path


def candidate(kind='homework', quote='下周交作业', **extra):
    return dict(type=kind, polarity='affirmed', tense='future', status='actionable',
                summary='下周交作业', action='提交作业', due_text='下周', quote=quote, **extra)


def test_cloud_audio_text_and_vision_pipeline(monkeypatch, synthetic_video, tmp_path):
    configure_audio(monkeypatch)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-deepseek-secret')
    requests = []
    def post(self, url, **kwargs):
        requests.append(url)
        if url.endswith('/audio/transcriptions'):
            return response(url, {'segments': [{'start': .2, 'end': 1.8, 'text': '下周交作业'}]})
        data = kwargs['json']
        if isinstance(data['messages'][1]['content'], list):
            # Deliberately artificial OCR on a black frame: evidence MUST remain unverified.
            extracted = {'events': [candidate(kind='attendance', quote='请扫码签到')]}
            assert data['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
        else:
            extracted = {'events': [candidate(segment_ids=[1])]}
        return response(url, {'choices': [{'message': {'content': json.dumps(extracted)}}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    out = tmp_path / 'out'
    arguments = ['analyze', '--video', str(synthetic_video), '--vision', '--export-clips', '--output', str(out)]
    assert main(arguments) == 0
    report = Report.model_validate_json((out / 'events.json').read_text('utf-8'))
    assert report.media.asr_provider == 'openai-compatible'
    assert report.media.asr_device == 'cloud'
    assert report.visual.model == 'deepseek-flash'
    assert {e.type for e in report.events} == {'homework', 'attendance'}
    visual = next(e for e in report.events if e.type == 'attendance')
    assert visual.evidence_status == 'visual_needs_review'
    assert all(not source.quote_verified and source.kind == 'frame' for source in visual.sources)
    assert visual.needs_review and visual.status == 'needs_review'
    assert all(e.audio_clip_path and Path(e.audio_clip_path).is_file() for e in report.events)
    count = len(requests)
    assert main(arguments) == 0
    assert len(requests) == count  # successful cloud chunks and vision frames resume from cache


def test_vision_only_skips_audio_and_local_models(monkeypatch, synthetic_video, tmp_path):
    monkeypatch.setenv('YANHE_VISION_API_KEY', 'fake-vision-secret')
    monkeypatch.setattr('yanhe_sentinel.asr.transcribe', lambda *a, **k: pytest.fail('vision-only must not transcribe'))
    monkeypatch.setattr(httpx.Client, 'post', lambda self, url, **kw: response(url, {
        'choices': [{'message': {'content': json.dumps({'events': [candidate()]})}}]}))
    out = tmp_path / 'out'
    assert main(['analyze', '--video', str(synthetic_video), '--vision-only', '--output', str(out)]) == 0
    report = Report.model_validate_json((out / 'events.json').read_text('utf-8'))
    assert report.media is None
    assert report.visual.input_path == str(synthetic_video)
    assert report.coverage_status == 'unknown'
    assert not (out / 'transcript.srt').exists()


def test_missing_cloud_key_has_no_local_fallback(monkeypatch, synthetic_video, tmp_path):
    monkeypatch.setattr(httpx.Client, 'post', lambda *args, **kw: pytest.fail('No request without configuration'))
    out = tmp_path / 'out'
    assert main(['analyze', '--video', str(synthetic_video), '--provider', 'mock', '--output', str(out)]) == 2
    report = Report.model_validate_json((out / 'events.json').read_text('utf-8'))
    assert report.media.transcription_status == 'failed'
    assert report.detection_status == 'inconclusive'
    assert any('YANHE_ASR_API_KEY' in warning for warning in report.warnings)


def test_visual_failure_preserves_audio_events(monkeypatch, synthetic_video, tmp_path):
    configure_audio(monkeypatch)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-deepseek-secret')
    def post(self, url, **kwargs):
        if url.endswith('/audio/transcriptions'):
            return response(url, {'segments': [{'start': .2, 'end': 1.8, 'text': '下周交作业'}]})
        if isinstance(kwargs['json']['messages'][1]['content'], list):
            return response(url, {'error': 'private error'}, 400)
        return response(url, {'choices': [{'message': {'content': json.dumps({'events': [candidate(segment_ids=[1])]})}}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    out = tmp_path / 'out'
    assert main(['analyze', '--video', str(synthetic_video), '--vision', '--output', str(out)]) == 2
    report = Report.model_validate_json((out / 'events.json').read_text('utf-8'))
    assert report.events[0].type == 'homework'
    assert report.analysis_status == 'partial'
    assert report.visual.failed_frame_seconds


@pytest.mark.parametrize('status,expected_calls', [(401, 1), (429, 3)])
def test_terminal_cloud_error_stops_remaining_audio_uploads(monkeypatch, tmp_path, status, expected_calls):
    configure_audio(monkeypatch)
    source = tmp_path / 'long.wav'
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=16000', '-t', '32', '-c:a', 'pcm_s16le', source])
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        return response(url, {'error': 'fake-asr-secret'}, status)
    monkeypatch.setattr(httpx.Client, 'post', post)
    out = tmp_path / 'out'
    assert main(['analyze', '--audio', str(source), '--asr-chunk-seconds', '15', '--provider', 'mock', '--output', str(out)]) == 2
    assert len(calls) == expected_calls
    report = Report.model_validate_json((out / 'events.json').read_text('utf-8'))
    assert report.media.failed_chunk_ids == [0, 1, 2]
    checkpoint = json.loads((out / 'asr_progress.json').read_text('utf-8'))
    assert checkpoint['status'] == 'partial'
    assert checkpoint['completed_chunks'] == 0
    assert checkpoint['failed_chunks'] == 3
    assert [e['chunk'] for e in checkpoint['errors']] == [1, 2, 3]
    assert 'fake-asr-secret' not in (out / 'asr_progress.json').read_text('utf-8')


def test_frame_cannot_claim_quote_verified():
    from pydantic import ValidationError
    from yanhe_sentinel.schema import Source
    with pytest.raises(ValidationError):
        Source(kind='frame', path='frame.jpg', segment_ids=[], quote='作业')


def test_local_only_cannot_silently_upload_audio():
    with pytest.raises(MediaError, match='不会改用云端'):
        ASROptions(backend='gemini', local_files_only=True).validate()


def test_invalid_frame_options_fail_before_any_paid_request(monkeypatch, synthetic_video, tmp_path):
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **kw: pytest.fail('Must validate before upload'))
    assert main(['analyze', '--video', str(synthetic_video), '--vision', '--vision-interval', 'nan',
                 '--output', str(tmp_path / 'out')]) == 2


def test_truncated_text_response_is_not_completed(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-deepseek-secret')
    monkeypatch.setattr(httpx.Client, 'post', lambda self, url, **kw: response(url, {
        'choices': [{'finish_reason': 'length', 'message': {'content': '{"events":[]}'}}]}))
    from yanhe_sentinel.providers import ProviderError
    with pytest.raises(ProviderError):
        DeepSeekProvider().complete([{'role': 'user', 'content': 'json'}])


def test_deepseek_does_not_mix_another_services_url_or_key(monkeypatch):
    monkeypatch.setenv('YANHE_API_KEY', 'another-service-key')
    monkeypatch.setenv('YANHE_BASE_URL', 'https://another.example.invalid/v1')
    monkeypatch.setenv('YANHE_MODEL', 'another-model')
    from yanhe_sentinel.providers import ProviderError
    with pytest.raises(ProviderError):
        DeepSeekProvider()
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fake-deepseek-secret')
    provider = DeepSeekProvider()
    assert provider.base == 'https://api.deepseek.com'
    assert provider.key == 'fake-deepseek-secret'
    assert vision_provider('deepseek').base == 'https://api.deepseek.com'
