from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal['attendance', 'homework', 'exam', 'grading', 'schedule',
                    'in_class_task', 'resource', 'other_action']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Segment(StrictModel):
    id: int
    start: float | None
    end: float | None
    text: str


class Candidate(StrictModel):
    type: EventType
    polarity: Literal['affirmed', 'negated', 'uncertain']
    tense: Literal['past', 'current', 'future', 'unspecified']
    status: Literal['actionable', 'informational', 'canceled', 'needs_review']
    summary: str = Field(min_length=1)
    action: str | None
    due_text: str | None
    segment_ids: list[int] = Field(min_length=1)
    quote: str = Field(min_length=1)


class Extraction(StrictModel):
    events: list[Candidate]


class Source(StrictModel):
    kind: Literal['subtitle', 'frame'] = 'subtitle'
    path: str
    segment_ids: list[int]
    quote: str
    quote_verified: bool = True

    @model_validator(mode='after')
    def visual_quote_is_unverified(self):
        if self.kind == 'frame' and self.quote_verified:
            raise ValueError('画面识别文字不能标记为已自动核验')
        return self


class Event(StrictModel):
    event_id: str
    type: EventType
    severity: Literal['critical', 'high', 'medium']
    polarity: Literal['affirmed', 'negated', 'uncertain']
    tense: Literal['past', 'current', 'future', 'unspecified']
    status: Literal['actionable', 'informational', 'canceled', 'needs_review']
    summary: str
    action: str | None
    due_at: str | None = None
    due_text: str | None
    start_seconds: float | None
    end_seconds: float | None
    sources: list[Source]
    confidence: float | None = None
    needs_review: bool = True
    review_reason: str
    evidence_status: Literal['confirmed_by_transcript', 'visual_needs_review'] = 'confirmed_by_transcript'
    listen_start_seconds: float | None
    listen_end_seconds: float | None
    audio_clip_path: str | None = None


class Interval(StrictModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)

    @model_validator(mode='after')
    def ordered(self):
        if self.end_seconds < self.start_seconds:
            raise ValueError('invalid interval')
        return self


class Report(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    lecture_id: str
    lecture_date: date | None
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    provider: str
    simulated: bool
    analysis_status: Literal['completed', 'partial', 'failed']
    coverage_status: Literal['complete_for_provided_media', 'partial', 'unknown']
    detection_status: Literal['events_detected', 'no_event_detected', 'inconclusive']
    media_duration_seconds: float | None = None
    provided_subtitle_end_seconds: float | None
    analyzed_intervals: list[Interval]
    total_segments: int
    analyzed_segment_ids: list[int]
    failed_chunk_ids: list[int]
    warnings: list[str]
    events: list[Event]
    media: 'MediaQuality | None' = None
    visual: 'VisualQuality | None' = None


class AudioStream(StrictModel):
    index: int
    codec: str
    channels: int
    sample_rate: int
    start_seconds: float = 0
    duration_seconds: float | None = None
    default: bool = False
    language: str | None = None


class MediaInfo(StrictModel):
    path: str
    duration_seconds: float = Field(gt=0)
    audio_streams: list[AudioStream]
    probe_engine: str
    video_end_seconds: float | None = None


class MediaQuality(StrictModel):
    input_path: str
    audio_source_path: str
    audio_stream_index: int
    media_duration_seconds: float = Field(gt=0)
    decoded_audio_seconds: float = Field(ge=0)
    audio_offset_seconds: float
    alignment: Literal['same_media', 'user_offset', 'uncalibrated']
    transcription_status: Literal['completed', 'partial', 'failed']
    asr_model: str
    asr_device: str
    asr_compute_type: str
    asr_language: str
    asr_provider: str = 'faster-whisper'
    timestamp_quality: Literal['asr_estimate', 'generative_estimate'] = 'asr_estimate'
    total_chunks: int
    completed_chunk_ids: list[int]
    failed_chunk_ids: list[int]
    processed_intervals: list[Interval]
    silence_intervals: list[Interval]
    no_transcript_intervals: list[Interval]
    low_quality_segment_ids: list[int]
    warnings: list[str]


class VisualQuality(StrictModel):
    input_path: str
    provider: str
    model: str
    sampling_interval_seconds: float
    requested_frame_seconds: list[float]
    analyzed_frame_seconds: list[float]
    failed_frame_seconds: list[float]
    warnings: list[str]


Report.model_rebuild()
