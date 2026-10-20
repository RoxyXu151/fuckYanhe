from .schema import Segment, Interval


def chunks(segments: list[Segment], window=360, overlap=60, max_chars=12000):
    """Every cue is retained; overlap by time and at least one neighboring cue."""
    if window <= overlap or overlap < 0 or max_chars < 1:
        raise ValueError('分块参数无效')
    ordered = sorted(segments, key=lambda s: (s.start if s.start is not None else s.id, s.id))
    i = 0
    while i < len(ordered):
        j, size = i, 0
        while j < len(ordered):
            cue = ordered[j]
            if j > i and (size + len(cue.text) > max_chars or (
                cue.start is not None and cue.start - ordered[i].start >= window
            )):
                break
            size += len(cue.text)
            j += 1
        yield ordered[i:j]
        if j == len(ordered):
            break
        next_i = j - 1
        if ordered[j - 1].end is not None:
            threshold = ordered[j - 1].end - overlap
            while next_i > i + 1 and ordered[next_i - 1].end >= threshold:
                next_i -= 1
        i = max(i + 1, next_i)


def intervals(segments: list[Segment]) -> list[Interval]:
    merged = []
    for s in sorted((s for s in segments if s.start is not None), key=lambda s: s.start):
        if merged and s.start <= merged[-1].end_seconds:
            merged[-1].end_seconds = max(merged[-1].end_seconds, s.end)
        else:
            merged.append(Interval(start_seconds=s.start, end_seconds=s.end))
    return merged
