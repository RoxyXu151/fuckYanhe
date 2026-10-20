import pytest
from yanhe_sentinel.detection import verify
from yanhe_sentinel.providers import OpenAICompatibleProvider, ProviderError, restore_exact_quote_boundaries
from yanhe_sentinel.schema import Extraction, Segment


def extraction(ids, quote):
    return Extraction.model_validate({'events': [{'type': 'homework', 'polarity': 'affirmed',
        'tense': 'current', 'status': 'actionable', 'summary': '作业要求', 'action': '提交',
        'due_text': None, 'segment_ids': ids, 'quote': quote}]})


def test_only_restore_missing_newlines_from_exact_contiguous_complete_cues():
    segments = [Segment(id=4, start=0, end=1, text='这次作业。'),
        Segment(id=5, start=1, end=2, text='明天提交。')]
    result = restore_exact_quote_boundaries(extraction([4, 5], '这次作业。明天提交。'), segments)
    assert result.events[0].quote == '这次作业。\n明天提交。'
    assert result.events[0].segment_ids == [4, 5]
    verify(result, segments)


@pytest.mark.parametrize('ids,quote', [([4, 6], '这次作业。明天提交。'),
    ([4, 5], '这次作业明天提交。'), ([4, 5], '这次作业。'),
    ([5, 4], '明天提交。这次作业。'), ([4, 4], '这次作业。这次作业。')])
def test_never_repair_missing_changed_reordered_or_misattributed_evidence(ids, quote):
    segments = [Segment(id=4, start=0, end=1, text='这次作业。'),
        Segment(id=5, start=1, end=2, text='补充说明。'),
        Segment(id=6, start=2, end=3, text='明天提交。')]
    result = restore_exact_quote_boundaries(extraction(ids, quote), segments)
    assert result.events[0].quote == quote
    with pytest.raises(ProviderError):
        verify(result, segments)


def test_qwen_repairs_citation_with_feedback_and_preserves_strict_validation(monkeypatch):
    provider = OpenAICompatibleProvider(key='test', base='http://127.0.0.1:18766/v1', model='qwen3-4b')
    segments = [Segment(id=4, start=0, end=1, text='这次作业。')]
    calls = []
    def complete(messages, **_):
        calls.append(list(messages))
        return extraction([4], '伪造原句' if len(calls) == 1 else '这次作业。')
    monkeypatch.setattr(provider, 'complete', complete)
    result = provider.extract(segments)
    verify(result, segments)
    assert len(calls) == 2
    assert '证据校验失败' in calls[1][-1]['content']


def test_qwen_cannot_turn_invalid_candidates_into_successful_no_events(monkeypatch):
    provider = OpenAICompatibleProvider(key='test', base='http://127.0.0.1:18766/v1', model='qwen3-4b')
    segments = [Segment(id=4, start=0, end=1, text='这次作业。')]
    calls = []
    def complete(messages, **_):
        calls.append(list(messages))
        return extraction([4], '伪造原句') if len(calls) == 1 else Extraction(events=[])
    monkeypatch.setattr(provider, 'complete', complete)
    with pytest.raises(ProviderError, match='丢失先前候选通知'):
        provider.extract(segments)
    assert len(calls) == 3
