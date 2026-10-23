"""Service contract tests, with GPU inference explicitly replaced."""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('qwen_service', Path(__file__).parents[1] / 'scripts/qwen_asr_service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def test_service_rejects_unauthorized_audio_without_inference(monkeypatch):
    monkeypatch.setattr(service, 'service_key', 'test-secret')
    monkeypatch.setattr(service, 'infer', lambda *_: pytest.fail('Unauthorized audio must not invoke GPU'))
    response = TestClient(service.app).post('/v1/audio/transcriptions',
        data={'model': service.MODEL}, files={'file': ('audio.wav', b'audio')})
    assert response.status_code == 401


def test_service_accepts_workstation_transcription_contract(monkeypatch):
    monkeypatch.setattr(service, 'service_key', 'test-secret')
    calls = []
    def infer(raw, language):
        calls.append((raw, language))
        return {'text': '测试', 'segments': [{'start': 0, 'end': 1, 'text': '测试'}]}
    monkeypatch.setattr(service, 'infer', infer)
    response = TestClient(service.app).post('/v1/audio/transcriptions',
        headers={'Authorization': 'Bearer test-secret'}, data={'model': service.MODEL,
            'response_format': 'verbose_json', 'language': 'zh', 'timestamp_granularities[]': 'segment'},
        files={'file': ('audio.wav', b'audio', 'audio/wav')})
    assert response.status_code == 200
    assert calls == [(b'audio', 'Chinese')]
    assert response.json()['segments'][0]['end'] == 1


@pytest.mark.parametrize('start,end', [(0, float('nan')), (2, 1), (0, 100)])
def test_service_rejects_invalid_alignment_instead_of_inventing_times(start, end):
    with pytest.raises(ValueError):
        service.timed_segments([{'word': '考试', 'start': start, 'end': end}], 10)


def test_service_groups_aligned_characters_and_preserves_original_bounds():
    words = [{'word': '考', 'start': 1, 'end': 1}, {'word': '试', 'start': 1, 'end': 1.5},
        {'word': '作业', 'start': 8, 'end': 9}]
    result = service.timed_segments(words, 10)
    assert result == [{'id': 0, 'start': 1, 'end': 1.5, 'text': '考试'},
        {'id': 1, 'start': 8, 'end': 9, 'text': '作业'}]


def test_zero_length_boundary_characters_are_enclosed_without_new_timestamps():
    words = [{'word': '这', 'start': 1, 'end': 1}, {'word': '次作业', 'start': 2, 'end': 3},
        {'word': '呀', 'start': 8, 'end': 8}]
    assert service.timed_segments(words, 10) == [
        {'id': 0, 'start': 1, 'end': 8, 'text': '这次作业呀'}]
    with pytest.raises(ValueError):
        service.timed_segments([{'word': '呀', 'start': 8, 'end': 8}], 10)


def test_punctuation_only_hypothesis_does_not_require_fabricated_alignment(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    monkeypatch.setattr(service, 'recognizer', SimpleNamespace(transcribe=lambda **kw: [
        SimpleNamespace(text='……。', time_stamps=None)]))
    assert service.aligned_window(np.ones(16000), 16000, 'Chinese') == []


def test_bad_window_is_split_and_real_timestamps_receive_only_audio_offsets(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    calls = []
    def transcribe(audio, **kwargs):
        duration = len(audio[0]) / audio[1]
        calls.append(duration)
        stamps = None if duration > 22 else [SimpleNamespace(text='作业', start_time=1., end_time=2.)]
        return [SimpleNamespace(text='作业', time_stamps=stamps)]
    monkeypatch.setattr(service, 'recognizer', SimpleNamespace(transcribe=transcribe))
    diagnostics = {'split_windows': 0, 'punctuation_windows': 0}
    result = service.aligned_window(np.ones(60 * 16000), 16000, 'Chinese', diagnostics=diagnostics)
    assert len(calls) == 5 and diagnostics['split_windows'] == 1
    assert [w['start'] for w in result] == [1., 20.5, 40.5]
    assert all(w['end'] - w['start'] == 1 for w in result)


def test_unrecoverable_speech_is_still_failure_not_successful_empty_transcript(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    monkeypatch.setattr(service, 'recognizer', SimpleNamespace(transcribe=lambda **kw: [
        SimpleNamespace(text='考试', time_stamps=None)]))
    with pytest.raises(ValueError, match='short-window repair'):
        service.aligned_window(np.ones(60 * 16000), 16000, 'Chinese')
