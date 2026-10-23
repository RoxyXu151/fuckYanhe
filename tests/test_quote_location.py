import pytest
from yanhe_sentinel.providers import restore_unique_quote_location, ProviderError, OpenAICompatibleProvider
from yanhe_sentinel.detection import verify
from yanhe_sentinel.schema import Extraction, Segment


def event(quote, ids):
    return Extraction.model_validate({'events': [dict(type='homework', polarity='affirmed',
        tense='future', status='actionable', summary='作业', action='提交',
        due_text=None, segment_ids=ids, quote=quote)]})


def test_unique_literal_quote_repairs_wrong_ids_and_missing_newline():
    cues = [Segment(id=41, start=10, end=15, text='这次作业完成第三题。'),
            Segment(id=42, start=15, end=20, text='下周一提交到课程群。')]
    result = restore_unique_quote_location(event('这次作业完成第三题。下周一提交到课程群。', [1, 1]), cues)
    verify(result, cues)
    assert result.events[0].segment_ids == [41, 42]
    assert result.events[0].quote == '这次作业完成第三题。\n下周一提交到课程群。'


@pytest.mark.parametrize('quote,texts', [
    ('这次作业完成第三题。', ['这次作业完成第三题。', '这次作业完成第三题。']),
    ('这次作业完成第四题。', ['这次作业完成第三题。']),
    ('这次作业不需要提交。', ['这次作业需要提交。']),
])
def test_ambiguous_changed_number_and_negation_are_not_repaired(quote, texts):
    cues = [Segment(id=i + 40, start=i * 5, end=i * 5 + 5, text=t) for i,t in enumerate(texts)]
    result = restore_unique_quote_location(event(quote, [999]), cues)
    with pytest.raises(ProviderError):
        verify(result, cues)


@pytest.mark.parametrize('recover', [True, False])
def test_failed_window_is_split_without_losing_candidates(monkeypatch, recover):
    import json
    provider = OpenAICompatibleProvider(key='test', base='http://localhost:18766/v1', model='qwen3-4b')
    cues = [Segment(id=i + 40, start=i * 5, end=i * 5 + 5, text=f'第{i}条作业明天提交。') for i in range(8)]
    sizes = []
    def complete(messages, **kwargs):
        # Every extract call appends its actual chunk after the fixed few-shot examples.
        inputs = [json.loads(m['content']) for m in messages if m['role']=='user' and m['content'].startswith('{')]
        rows = inputs[-1]['segments']
        sizes.append(len(rows))
        if len(rows) == 8:
            return event('编造的并非字幕中的作业证据', [999])
        return event(rows[0]['text'], [rows[0]['id']]) if recover else Extraction(events=[])
    monkeypatch.setattr(provider, 'complete', complete)
    if recover:
        result = provider.extract(cues)
        verify(result, cues)
        assert len(result.events) == 2
    else:
        with pytest.raises(ProviderError, match='未找回候选'):
            provider.extract(cues)
    assert sizes == [8,8,8,5,5]
