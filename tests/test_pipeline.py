import json
from pathlib import Path
from importlib.resources import files

import httpx
import pytest

from yanhe_sentinel.cli import main
from yanhe_sentinel.detection import analyze, verify
from yanhe_sentinel.inputs import read_subtitles
from yanhe_sentinel.providers import MockProvider, OpenAICompatibleProvider, ProviderError
from yanhe_sentinel.schema import Extraction, Report, Segment
from yanhe_sentinel.timeline import chunks

GOLD = json.loads((Path(__file__).parent / 'fixtures/gold.json').read_text('utf-8'))


def cue(text='第三题留给大家自己做。', id=1, start=0, end=4):
    return Segment(id=id, start=start, end=end, text=text)


def result(quote, ids=None):
    return Extraction.model_validate({'events': [dict(type='homework', polarity='affirmed',
        tense='future', status='actionable', summary='完成练习', action='完成练习',
        due_text=None, segment_ids=ids or [1], quote=quote)]})


class Fake:
    name = 'test-transport'
    identity = 'test'

    def extract(self, segments, lecture_date=None):
        return Extraction(events=[])


@pytest.mark.parametrize('case', GOLD, ids=[str(i) for i in range(len(GOLD))])
def test_synthetic_contract(case, tmp_path):
    report = analyze([cue(case['text'])], MockProvider(), Path('test.srt'), tmp_path)
    assert len(report.events) == len(case['events'])
    for actual, expected in zip(report.events, case['events']):
        assert (actual.type, actual.status, actual.polarity, actual.tense) == (
            expected['type'], expected['status'], expected['polarity'], expected['tense'])
        assert actual.sources[0].quote == case['text']
        assert actual.start_seconds == 0
        if actual.status != 'actionable':
            assert actual.action is None
    assert report.simulated
    Report.model_validate_json(report.model_dump_json())


@pytest.mark.parametrize('suffix,text,expected', [
    ('.srt', '7\n00:00:01,125 --> 00:00:02,500\n第一行\n第二行\n', 1.125),
    ('.vtt', 'WEBVTT\n\nNOTE note\nignore\n\ncue-a\n01:01.200 --> 01:03.000 align:start\n测试\n', 61.2),
    ('.txt', '测试\n第二句', None),
])
def test_parser(suffix, text, expected, tmp_path):
    path = tmp_path / ('test' + suffix)
    path.write_text(text, encoding='utf-8-sig')
    cues = read_subtitles(path)
    assert cues[0].start == expected
    assert cues[0].id == 1


@pytest.mark.parametrize('text', ['损坏的字幕', '1\n00:00:09,000 --> 00:00:02,000\n测试',
    '1\n00:99:01,000 --> 00:99:02,000\n测试', '1\n00:00:01,000 --> 00:00:02,000\n',
    '1\n00:00:01,000 --> 00:00:02,000\n\ufffd'])
def test_malformed_not_silently_skipped(text, tmp_path):
    path = tmp_path / 'bad.srt'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        read_subtitles(path)


def test_overlaps_and_order(tmp_path):
    path = tmp_path / 'overlap.srt'
    path.write_text('1\n00:00:02,000 --> 00:00:05,000\nA\n\n2\n00:00:01,000 --> 00:00:04,000\nB', encoding='utf-8')
    report = analyze(read_subtitles(path), Fake(), path, tmp_path)
    assert report.analyzed_intervals[0].start_seconds == 1
    assert report.analyzed_intervals[0].end_seconds == 5
    assert report.coverage_status == 'unknown'


def test_full_coverage_and_boundary():
    cues = [cue(str(i), i, i * 10, i * 10 + 3) for i in range(100)]
    windows = list(chunks(cues))
    assert {s.id for c in windows for s in c} == set(range(100))
    assert any({35, 36} <= {s.id for s in c} for c in windows)
    assert any({71, 72} <= {s.id for s in c} for c in windows)


def test_cross_boundary_evidence_and_dedup(tmp_path):
    cues = [cue('讲课', i, i * 10, i * 10 + 5) for i in range(80)]
    cues[35].text, cues[36].text = '第三章后面的习题', '下周五前交。'

    class Cross(Fake):
        def extract(self, segments, lecture_date=None):
            if {35, 36} <= {s.id for s in segments}:
                return result('第三章后面的习题\n下周五前交。', [35, 36])
            return Extraction(events=[])

    report = analyze(cues, Cross(), Path('test.srt'), tmp_path)
    assert len(report.events) == 1
    assert report.events[0].start_seconds == 350
    assert report.events[0].end_seconds == 365


@pytest.mark.parametrize('quote,ids', [('编造的原话', [1]), ('真实原话', [99]), ('真实原话', [1, 1]), ('真实原话', [1, 2])])
def test_hallucination_rejected(quote, ids):
    with pytest.raises(ProviderError):
        verify(result(quote, ids), [cue('真实原话'), cue('无关', 2)])


def test_evidence_failure_is_not_no_event(tmp_path):
    class Bad(Fake):
        def extract(self, *args):
            return result('不存在')
    report = analyze([cue()], Bad(), Path('test.srt'), tmp_path)
    assert report.analysis_status == 'failed'
    assert report.detection_status == 'inconclusive'
    assert not report.events


def test_partial_failure(tmp_path):
    class Partial(Fake):
        def extract(self, segments, lecture_date=None):
            if segments[0].id != 0:
                raise ProviderError('模拟失败')
            return Extraction(events=[])
    report = analyze([cue('讲课', i, i * 400, i * 400 + 5) for i in range(3)], Partial(), Path('a.srt'), tmp_path)
    assert report.analysis_status == 'partial'
    assert report.detection_status == 'inconclusive'
    assert report.analyzed_segment_ids == [0]


def test_no_events_and_empty(tmp_path):
    report = analyze([cue('矩阵乘法')], Fake(), Path('a.srt'), tmp_path)
    assert report.detection_status == 'no_event_detected'
    empty = analyze([], Fake(), Path('b.srt'), tmp_path)
    assert empty.analysis_status == 'failed'
    assert empty.detection_status == 'inconclusive'


def test_cache_resume_and_idempotency(tmp_path):
    class Count(MockProvider):
        calls = 0
        def extract(self, *args):
            self.calls += 1
            return super().extract(*args)
    provider = Count()
    first = analyze([cue()], provider, Path('a.srt'), tmp_path)
    second = analyze([cue()], provider, Path('a.srt'), tmp_path)
    assert provider.calls == 1
    assert first.events == second.events


def test_retry_only_calls_model_for_failed_analysis_windows(tmp_path):
    class Recovering(Fake):
        def __init__(self):
            self.calls = []
            self.fail = True
        def extract(self, segments, lecture_date=None):
            self.calls.append(segments[0].id)
            if self.fail and segments[0].id == 1:
                raise ProviderError('temporary failure')
            return Extraction(events=[])
    provider = Recovering()
    cues = [cue('讲课', i, i * 400, i * 400 + 5) for i in range(3)]
    first = analyze(cues, provider, Path('a.srt'), tmp_path)
    assert first.failed_chunk_ids == [1]
    provider.fail = False
    provider.calls.clear()
    second = analyze(cues, provider, Path('a.srt'), tmp_path)
    assert second.analysis_status == 'completed'
    assert provider.calls == [1]


def test_corrupt_cache_recovered(tmp_path):
    analyze([cue()], MockProvider(), Path('a.srt'), tmp_path)
    next((tmp_path / '.cache').glob('*.json')).write_text('bad', encoding='utf-8')
    assert analyze([cue()], MockProvider(), Path('a.srt'), tmp_path).analysis_status == 'completed'


def test_cancellation_marks_prior_for_review(tmp_path):
    report = analyze([cue('周四有个小测试。'), cue('之前说要测验，现在取消。', 2, 10, 15)], MockProvider(), Path('a.srt'), tmp_path)
    assert [e.status for e in report.events] == ['needs_review', 'canceled']


def test_no_key_and_no_secret_logs(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv('YANHE_API_KEY', raising=False)
    assert main(['analyze', '--subtitle', 'missing.srt', '--provider', 'openai-compatible', '--output', str(tmp_path)]) == 2
    assert '处理失败' in capsys.readouterr().out


def configured(monkeypatch):
    monkeypatch.setenv('YANHE_API_KEY', 'test-secret')
    monkeypatch.setenv('YANHE_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('YANHE_MODEL', 'test-model')


def test_transport_schema_retry(monkeypatch):
    configured(monkeypatch)
    count = []
    def post(self, url, **kwargs):
        count.append(kwargs)
        content = 'bad json' if len(count) == 1 else '{"events":[]}'
        return httpx.Response(200, request=httpx.Request('POST', url), json={'choices': [{'message': {'content': content}}]})
    monkeypatch.setattr(httpx.Client, 'post', post)
    monkeypatch.setattr('yanhe_sentinel.providers.time.sleep', lambda _: None)
    assert not OpenAICompatibleProvider().extract([cue()]).events
    assert len(count) == 2
    assert '第三题留给大家自己做。' in count[0]['json']['messages'][1]['content']


def test_auth_error_redacted(monkeypatch):
    configured(monkeypatch)
    def post(self, url, **kwargs):
        return httpx.Response(401, request=httpx.Request('POST', url), text='test-secret')
    monkeypatch.setattr(httpx.Client, 'post', post)
    with pytest.raises(ProviderError) as error:
        OpenAICompatibleProvider().extract([cue()])
    assert 'test-secret' not in str(error.value)


@pytest.mark.parametrize('url', ['http://remote.invalid/v1', 'https://user:pass@example.invalid/v1', 'https://example.invalid/v1?token=secret'])
def test_unsafe_endpoint_rejected(monkeypatch, url):
    configured(monkeypatch)
    monkeypatch.setenv('YANHE_BASE_URL', url)
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider()


def test_cli_demo(tmp_path):
    assert main(['demo', '--output', str(tmp_path)]) == 0
    report = Report.model_validate_json((tmp_path / 'events.json').read_text('utf-8'))
    assert len(report.events) == 4
    assert {e.type for e in report.events} == {'attendance', 'homework', 'exam', 'in_class_task'}
    assert '模拟演示' in (tmp_path / 'report.md').read_text('utf-8')
    assert (tmp_path / 'quality.json').exists()
    assert (tmp_path / 'events.schema.json').exists()


def test_txt_does_not_invent_time(tmp_path):
    report = analyze([cue(start=None, end=None)], MockProvider(), Path('a.txt'), tmp_path)
    assert report.events[0].start_seconds is None
    assert report.analyzed_intervals == []


def test_discontinuous_quote_rejected():
    with pytest.raises(ProviderError):
        verify(result('作业\n交。', [1, 3]), [cue('作业'), cue('不用', 2), cue('交。', 3)])


def test_vtt_missing_separator_is_failure(tmp_path):
    path = tmp_path / 'bad.vtt'
    path.write_text('WEBVTT\n00:01.000 --> 00:03.000\n内容', encoding='utf-8')
    with pytest.raises(ValueError):
        read_subtitles(path)


def test_missing_provider_configuration_writes_failure(monkeypatch, tmp_path):
    monkeypatch.delenv('YANHE_API_KEY', raising=False)
    path = tmp_path / 'input.txt'
    path.write_text('老师开始点名。', encoding='utf-8')
    assert main(['analyze', '--subtitle', str(path), '--provider', 'openai-compatible', '--output', str(tmp_path)]) == 2
    report = Report.model_validate_json((tmp_path / 'events.json').read_text('utf-8'))
    assert report.analysis_status == 'failed'
    assert report.detection_status == 'inconclusive'
    assert not report.analyzed_segment_ids
