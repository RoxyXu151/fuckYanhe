"""Real local decoding, synthetic waveforms, injected ASR for repeatable edge cases."""

import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

np = pytest.importorskip('numpy')
pytest.importorskip('av')
pytest.importorskip('imageio_ffmpeg')

from yanhe_sentinel.asr import ASROptions, atomic_text, attach_media, transcribe
from yanhe_sentinel.cli import main
from yanhe_sentinel.detection import analyze
from yanhe_sentinel.media_tools import MediaError, ffmpeg_executable, probe_media, run_tool, select_audio
from yanhe_sentinel.providers import MockProvider
from yanhe_sentinel.schema import Extraction, Report


@pytest.fixture
def tone(tmp_path):
    path = tmp_path / '课堂 音频.wav'
    samples = (.15 * np.sin(2 * np.pi * 220 * np.arange(32 * 16000) / 16000) * 32767).astype('<i2')
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(samples.tobytes())
    return path


class FakeModel:
    def __init__(self, fail_at=None):
        self.calls = 0
        self.fail_at = fail_at

    def transcribe(self, samples, **kwargs):
        self.calls += 1
        def iterator():
            if self.calls == self.fail_at:
                raise RuntimeError('private third party log')
            yield SimpleNamespace(start=.5, end=min(3, len(samples) / 16000),
                text='第三题留给大家自己做。', avg_logprob=-.2, no_speech_prob=.1)
        return iterator(), SimpleNamespace()


@pytest.mark.parametrize('failures', [2, 5])
def test_atomic_checkpoint_retries_temporary_windows_lock_without_losing_previous_data(tmp_path, monkeypatch, failures):
    path = tmp_path / 'progress.json'
    path.write_text('previous', encoding='utf-8')
    original_replace, calls = Path.replace, []
    def locked(source, target):
        calls.append(target)
        if len(calls) <= failures:
            raise PermissionError('temporarily locked')
        return original_replace(source, target)
    monkeypatch.setattr(Path, 'replace', locked)
    monkeypatch.setattr('yanhe_sentinel.asr.time.sleep', lambda _: None)
    if failures == 5:
        with pytest.raises(PermissionError):
            atomic_text(path, 'new')
        assert path.read_text('utf-8') == 'previous'
    else:
        atomic_text(path, 'new')
        assert path.read_text('utf-8') == 'new'
        assert len(calls) == 3


def run_transcription(tone, output, model=None, **kwargs):
    return transcribe(tone, output, ASROptions(backend='faster-whisper', model='fake', chunk_seconds=15, context_seconds=0),
                      model_factory=lambda _: model or FakeModel(), progress=lambda _: None, **kwargs)


def test_real_audio_decode_and_original_timestamps(tone, tmp_path):
    result = run_transcription(tone, tmp_path / 'out')
    assert result.quality.total_chunks == 3
    assert result.quality.transcription_status == 'completed'
    assert [s.start for s in result.segments] == [.5, 15.5, 30.5]
    assert len(result.quality.processed_intervals) == 1
    assert result.quality.processed_intervals[0].end_seconds == pytest.approx(32)
    text = (tmp_path / 'out/transcript.srt').read_text('utf-8')
    assert '00:00:15,500' in text
    assert (tmp_path / 'out/asr_quality.json').exists()
    assert (tmp_path / 'out/media.json').exists()


def test_resume_successful_chunks_without_model_calls(tone, tmp_path):
    first_model = FakeModel(fail_at=2)
    first = run_transcription(tone, tmp_path / 'out', first_model)
    assert first.quality.transcription_status == 'partial'
    assert first.quality.failed_chunk_ids == [1]
    second_model = FakeModel()
    second = run_transcription(tone, tmp_path / 'out', second_model)
    assert second_model.calls == 1
    assert second.quality.transcription_status == 'completed'
    assert [s.start for s in second.segments] == [.5, 15.5, 30.5]


@pytest.mark.parametrize('force,change_source,change_endpoint', [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
def test_gemini_resume_native_cache_with_actual_model_provenance(tone, tmp_path, monkeypatch, force, change_source, change_endpoint):
    output = tmp_path / 'out'
    monkeypatch.setenv('YANHE_ASR_BASE_URL', 'https://example.org/v1beta')
    native = ASROptions(backend='gemini', model='gemini-3.5-transcribe', chunk_seconds=15, context_seconds=0)
    transcribe(tone, output, native, model_factory=lambda _: FakeModel(), progress=lambda _: None)
    if change_source:
        with wave.open(str(tone), 'rb') as audio:
            samples = bytearray(audio.readframes(audio.getnframes()))
        samples[100:102] = b'\xff\x7f'
        with wave.open(str(tone), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(samples)
    if change_endpoint:
        monkeypatch.setenv('YANHE_ASR_BASE_URL', 'https://different.example.org/v1beta')
    model = FakeModel()
    flash = ASROptions(backend='gemini', model='gemini-3.8-flash', chunk_seconds=15, context_seconds=0)
    result = transcribe(tone, output, flash, use_cache=not force, model_factory=lambda _: model, progress=lambda _: None)
    reused = not (force or change_source)
    assert model.calls == (0 if reused else 3)
    expected = 'gemini-3.5-transcribe' if reused else 'gemini-3.8-flash'
    assert result.quality.asr_model == expected
    assert result.quality.timestamp_quality == ('asr_estimate' if reused else 'generative_estimate')
    chunks = json.loads((output / 'asr_chunks.json').read_text('utf-8'))
    assert len(chunks) == 3
    assert {chunk['model'] for chunk in chunks} == {expected}
    if reused:
        again = transcribe(tone, output, flash, model_factory=lambda _: pytest.fail('Migrated cache must retain provenance'), progress=lambda _: None)
        assert again.quality.asr_model == expected


def test_changed_media_invalidates_cache(tone, tmp_path):
    run_transcription(tone, tmp_path / 'out')
    with wave.open(str(tone), 'rb') as original:
        data = bytearray(original.readframes(original.getnframes()))
    data[100:102] = b'\xff\x7f'
    with wave.open(str(tone), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(data)
    model = FakeModel()
    run_transcription(tone, tmp_path / 'out', model)
    assert model.calls == 3


def test_mixed_gemini_legacy_cache_keeps_per_chunk_sources(tone, tmp_path, monkeypatch):
    output = tmp_path / 'out'
    monkeypatch.setenv('YANHE_ASR_BASE_URL', 'https://example.org/v1beta')
    native = ASROptions(backend='gemini', model='gemini-3.5-transcribe', chunk_seconds=15, context_seconds=0)
    first = transcribe(tone, output, native, model_factory=lambda _: FakeModel(fail_at=2), progress=lambda _: None)
    assert first.quality.failed_chunk_ids == [1]
    for path in (output / '.cache/asr').rglob('*.json'):
        data = json.loads(path.read_text('utf-8'))
        data.pop('provider_model', None)
        data.pop('provider_name', None)
        data.pop('timestamp_quality', None)
        path.write_text(json.dumps(data), encoding='utf-8')
    model = FakeModel()
    flash = ASROptions(backend='gemini', model='gemini-3.8-flash', chunk_seconds=15, context_seconds=0)
    result = transcribe(tone, output, flash, model_factory=lambda _: model, progress=lambda _: None)
    assert model.calls == 1
    assert result.quality.transcription_status == 'completed'
    assert result.quality.asr_model == 'gemini-3.5-transcribe + gemini-3.8-flash'
    assert result.quality.timestamp_quality == 'generative_estimate'
    chunks = json.loads((output / 'asr_chunks.json').read_text('utf-8'))
    assert [chunk['model'] for chunk in chunks] == ['gemini-3.5-transcribe', 'gemini-3.8-flash', 'gemini-3.5-transcribe']


@pytest.mark.parametrize('change_profile,tamper_manifest', [(False, False), (True, False), (False, True)])
def test_resume_on_another_cloud_only_uploads_missing_matching_audio(tone, tmp_path, monkeypatch, change_profile, tamper_manifest):
    output = tmp_path / 'out'
    native = ASROptions(backend='gemini', model='gemini-3.5-transcribe', chunk_seconds=15, context_seconds=0)
    transcribe(tone, output, native, model_factory=lambda _: FakeModel(fail_at=2), progress=lambda _: None)
    if tamper_manifest:
        path = output / 'asr_resume.json'
        manifest = json.loads(path.read_text('utf-8'))
        manifest['caches'][0]['key'] = '../foreign-cache'
        path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setenv('YANHE_ASR_BASE_URL', 'https://new-service.example.org/v1')
    options = ASROptions(backend='openai-compatible', model='another-asr', chunk_seconds=15,
        context_seconds=1 if change_profile else 0)
    model = FakeModel()
    result = transcribe(tone, output, options, model_factory=lambda _: model, progress=lambda _: None)
    assert result.quality.transcription_status == 'completed'
    assert model.calls == (3 if change_profile or tamper_manifest else 1)
    chunks = json.loads((output / 'asr_chunks.json').read_text('utf-8'))
    expected = ['openai-compatible'] * 3 if change_profile or tamper_manifest else ['gemini', 'openai-compatible', 'gemini']
    assert [item['provider'] for item in chunks] == expected
    assert result.quality.asr_provider == ' + '.join(sorted(set(expected)))


def test_offset_and_external_audio_must_be_explicit(tone, tmp_path):
    with pytest.raises(MediaError, match='偏移|audio-offset'):
        run_transcription(tone, tmp_path / 'out', audio_source=tone)
    result = run_transcription(tone, tmp_path / 'offset', audio_source=tone, audio_offset=2)
    assert result.quality.alignment == 'user_offset'
    assert result.segments[0].start == 2.5
    assert result.quality.processed_intervals[0].start_seconds == 2
    assert result.segments[-1].end <= 32


def test_negative_offset_clips_to_original_video(tone, tmp_path):
    result = run_transcription(tone, tmp_path / 'out', audio_source=tone, audio_offset=-2)
    assert result.segments[0].start == 0
    assert result.segments[0].end == 1
    assert result.quality.processed_intervals[-1].end_seconds == 30


def test_silence_does_not_load_model_or_claim_no_events(tmp_path):
    path = tmp_path / 'silence.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x00' * 16000 * 20)
    def forbidden(_):
        pytest.fail('Silent audio must not invoke ASR')
    result = transcribe(path, tmp_path / 'out', ASROptions(model='not-downloaded'), model_factory=forbidden, progress=lambda _: None)
    assert result.quality.silence_intervals[0].end_seconds == 20
    assert result.quality.no_transcript_intervals[0].end_seconds == 20
    assert not result.segments
    report = attach_media(analyze([], MockProvider(), path, tmp_path / 'out'), result)
    assert report.detection_status == 'inconclusive'
    assert report.analysis_status == 'failed'


def test_model_failure_is_recorded_and_retryable(tone, tmp_path):
    def unavailable(_):
        raise MediaError('模型暂不可用')
    result = transcribe(tone, tmp_path / 'out', ASROptions(backend='faster-whisper', model='bad', chunk_seconds=15),
                        model_factory=unavailable, progress=lambda _: None)
    assert result.quality.transcription_status == 'failed'
    assert result.quality.failed_chunk_ids == [0, 1, 2]
    assert not result.segments
    assert '模型暂不可用' in ' '.join(result.quality.warnings)


def test_no_audio_stream_fails_with_report(tmp_path):
    path = tmp_path / 'no-audio.mp4'
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
              '-i', 'color=c=black:s=64x64:r=1', '-t', '1', '-c:v', 'libx264', path])
    assert not probe_media(path).audio_streams
    code = main(['analyze', '--video', str(path), '--provider', 'mock', '--output', str(tmp_path / 'out')])
    assert code == 2
    report = Report.model_validate_json((tmp_path / 'out/events.json').read_text('utf-8'))
    assert report.analysis_status == 'failed'
    assert report.detection_status == 'inconclusive'
    assert '没有音轨' in ' '.join(report.warnings)


def test_corrupt_media_fails_without_success_claim(tmp_path):
    path = tmp_path / 'corrupt.mp4'
    path.write_bytes(b'not a movie')
    assert main(['analyze', '--video', str(path), '--provider', 'mock', '--output', str(tmp_path / 'out')]) == 2
    report = json.loads((tmp_path / 'out/events.json').read_text('utf-8'))
    assert report['detection_status'] == 'inconclusive'
    assert not report['events']


def test_multiple_tracks_default_and_global_stream_selection(tone, tmp_path):
    path = tmp_path / 'tracks.mkv'
    run_tool([ffmpeg_executable(), '-hide_banner', '-loglevel', 'error', '-y', '-i', tone,
        '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono', '-t', '2', '-map', '0:a', '-map', '1:a',
        '-c:a', 'pcm_s16le', '-disposition:a:0', '0', '-disposition:a:1', 'default', path])
    info = probe_media(path)
    assert len(info.audio_streams) == 2
    assert select_audio(info).index == 1
    assert select_audio(info, 0).index == 0
    with pytest.raises(MediaError):
        select_audio(info, 50)


def test_full_cli_report_and_clip_from_decoded_audio(monkeypatch, tone, tmp_path):
    import yanhe_sentinel.asr as asr
    monkeypatch.setattr(asr, 'load_model', lambda _: FakeModel())
    out = tmp_path / 'out'
    code = main(['analyze', '--audio', str(tone), '--asr-provider', 'faster-whisper', '--asr-chunk-seconds', '15', '--asr-context-seconds', '0',
                 '--provider', 'mock', '--export-clips', '--output', str(out)])
    assert code == 0
    report = Report.model_validate_json((out / 'events.json').read_text('utf-8'))
    assert report.media.transcription_status == 'completed'
    assert report.coverage_status == 'complete_for_provided_media'
    assert len(report.events) == 3
    assert report.media.input_path == str(tone)
    for event in report.events:
        assert Path(event.audio_clip_path).is_file()
        assert event.listen_end_seconds <= report.media_duration_seconds
        assert event.sources[0].path == str((out / 'transcript.srt').resolve())


def test_missing_api_preserves_transcript_and_failure_report(monkeypatch, tone, tmp_path):
    monkeypatch.delenv('YANHE_API_KEY', raising=False)
    monkeypatch.setattr('yanhe_sentinel.asr.load_model', lambda _: FakeModel())
    out = tmp_path / 'out'
    assert main(['analyze', '--audio', str(tone), '--asr-provider', 'faster-whisper', '--output', str(out)]) == 2
    report = Report.model_validate_json((out / 'events.json').read_text('utf-8'))
    assert report.media.transcription_status == 'completed'
    assert report.analysis_status == 'failed'
    assert report.detection_status == 'inconclusive'
    assert (out / 'transcript.srt').read_text('utf-8').strip()
    assert not report.analyzed_segment_ids


def test_transcribe_only_requires_no_event_provider(monkeypatch, tone, tmp_path):
    monkeypatch.setattr('yanhe_sentinel.asr.load_model', lambda _: FakeModel())
    monkeypatch.setattr('yanhe_sentinel.cli.OpenAICompatibleProvider', lambda: pytest.fail('Must not call event provider'))
    out = tmp_path / 'out'
    assert main(['transcribe', '--audio', str(tone), '--asr-provider', 'faster-whisper', '--output', str(out)]) == 0
    assert (out / 'transcript.srt').exists()
    assert not (out / 'events.json').exists()


def test_ffprobe_metadata_path(monkeypatch, tone):
    fake = {'format': {'start_time': '10', 'duration': '32'}, 'streams': [
        {'index': 4, 'codec_type': 'audio', 'codec_name': 'pcm_s16le', 'channels': 1,
         'sample_rate': '16000', 'start_time': '12', 'duration': '30', 'disposition': {'default': 1}}]}
    monkeypatch.setattr('yanhe_sentinel.media_tools.run_tool', lambda *args, **kw: json.dumps(fake).encode())
    info = probe_media(tone, ffprobe=str(tone))
    assert info.probe_engine == 'ffprobe'
    assert info.audio_streams[0].start_seconds == 2
    assert info.audio_streams[0].index == 4


def test_partial_asr_cannot_be_completed_analysis(tone, tmp_path):
    result = run_transcription(tone, tmp_path / 'out', FakeModel(fail_at=2))
    report = attach_media(analyze(result.segments, MockProvider(), tone, tmp_path / 'out'), result)
    assert report.analysis_status == 'partial'
    assert report.coverage_status == 'partial'


def test_corrupt_asr_chunk_is_recomputed(tone, tmp_path):
    run_transcription(tone, tmp_path / 'out')
    chunks = sorted((tmp_path / 'out/.cache/asr').glob('*/00001.json'))
    assert len(chunks) == 1
    chunks[0].write_text('corrupt', encoding='utf-8')
    model = FakeModel()
    assert run_transcription(tone, tmp_path / 'out', model).quality.transcription_status == 'completed'
    assert model.calls == 1
