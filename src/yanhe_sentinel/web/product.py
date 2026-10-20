"""Local preferences and study records. Secrets never enter persisted preferences."""
import json
from pathlib import Path
import threading
import time
import uuid

import httpx
from pydantic import Field

from ..cloud_asr import safe_base_url
from ..detection import digest
from ..schema import StrictModel
from .jobs import atomic_json
from .models import ModelSettings

PUBLIC_FIELDS = set(ModelSettings.model_fields) - {'asr_api_key', 'event_api_key', 'vision_api_key'}


def merge_settings(original, changes):
    if set(changes) - set(ModelSettings.model_fields):
        raise ValueError('设置中包含未知字段')
    values = original.model_dump()
    for kind in ('asr', 'event', 'vision'):
        identity = [kind + '_base_url', kind + '_provider', kind + '_deployment']
        if any(key in changes and changes[key] != values.get(key) for key in identity):
            values[kind + '_api_key'] = ''
    for key, value in changes.items():
        if not (key.endswith('_api_key') and value == ''):
            values[key] = value
    return ModelSettings.model_validate(values)


class Preferences:
    def __init__(self, root):
        self.file = Path(root) / 'preferences.json'
        self.lock = threading.RLock()

    def load(self):
        defaults = ModelSettings.from_environment()
        with self.lock:
            try:
                values = json.loads(self.file.read_text('utf-8'))
                if not isinstance(values, dict) or set(values) - PUBLIC_FIELDS:
                    return defaults
                return merge_settings(defaults, values)
            except (OSError, ValueError, TypeError):
                return defaults

    def save(self, settings, enabled):
        with self.lock:
            if enabled:
                atomic_json(self.file, settings.model_dump(include=PUBLIC_FIELDS))
            else:
                self.file.unlink(missing_ok=True)


def test_connection(settings, kind):
    """Non-generative capability check; no audio, transcript or prompts are sent."""
    if kind not in ('asr', 'event'):
        raise ValueError('未知服务类型')
    provider = getattr(settings, kind + '_provider')
    base = getattr(settings, kind + '_base_url')
    model = getattr(settings, kind + '_model')
    key = getattr(settings, kind + '_api_key').get_secret_value()
    if provider == 'gemini':
        base = base or 'https://generativelanguage.googleapis.com/v1beta'
        url = safe_base_url(base) + '/models'
        headers = {'x-goog-api-key': key} if key else {}
    else:
        base = base or ('https://api.groq.com/openai/v1' if provider == 'groq' else '')
        if not base:
            return {'status': 'incomplete', 'model_found': None}
        url = safe_base_url(base) + '/models'
        headers = {'Authorization': 'Bearer ' + key} if key else {}
    if getattr(settings, kind + '_deployment') == 'cloud' and not key:
        return {'status': 'incomplete', 'model_found': None}
    try:
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            response = client.get(url, headers=headers)
        if response.status_code in (401, 403):
            status = 'auth_error'
        elif response.status_code == 429:
            status = 'rate_limited'
        elif response.status_code in (404, 405):
            status = 'unsupported'
        elif 300 <= response.status_code < 400:
            status = 'redirect'
        elif response.status_code >= 400:
            status = 'unavailable'
        else:
            try:
                data = response.json()
                entries = data.get('data', data.get('models', []))
                names = {str(item.get('id', item.get('name', ''))).removeprefix('models/') for item in entries if isinstance(item, dict)}
                if not names:
                    return {'status': 'unsupported', 'model_found': None}
                found = model in names if model else None
                return {'status': 'available' if found is not False else 'model_missing', 'model_found': found}
            except (ValueError, TypeError, AttributeError):
                status = 'unsupported'
        return {'status': status, 'model_found': None}
    except httpx.HTTPError:
        return {'status': 'unreachable', 'model_found': None}


class StudyProgress(StrictModel):
    position: float = Field(default=0, ge=0, le=604800)
    speed: float = Field(default=1, ge=.5, le=3)


class BookmarkInput(StrictModel):
    segment_id: int | None = None
    position: float | None = Field(default=None, ge=0, le=604800)
    note: str = Field(default='', max_length=2000)


class NoteInput(StrictModel):
    text: str = Field(max_length=20000)


class RetrySelection(StrictModel):
    job_ids: list[str] = Field(min_length=1, max_length=100)


class StudyRecords:
    def __init__(self, study):
        self.study, self.lock = study, threading.RLock()

    def _file(self, job_id):
        self.study.jobs.get(job_id)
        return self.study.jobs.root / job_id / 'study-records.json'

    def read(self, job_id):
        with self.lock:
            path = self._file(job_id)
            if path.exists():
                data = json.loads(path.read_text('utf-8'))
            else:
                data = {'position': 0, 'speed': 1, 'note': '', 'bookmarks': [], 'updated_at': None}
            _, segments = self.study.source(job_id)
            lookup = {s.id: s for s in segments}
            source_hash = digest([s.model_dump() for s in segments])
            data['progress_stale'] = bool(data.get('progress_source_hash') and data['progress_source_hash'] != source_hash)
            for bookmark in data['bookmarks']:
                source = lookup.get(bookmark['segment_id'])
                bookmark['stale'] = (source is None or source.text != bookmark['quote'] or source.start != bookmark['position']) if bookmark['segment_id'] is not None else bookmark.get('source_hash') != source_hash
            return data

    def update(self, job_id, **values):
        with self.lock:
            data = self.read(job_id)
            if 'position' in values:
                _, segments = self.study.source(job_id)
                values['progress_source_hash'] = digest([s.model_dump() for s in segments])
            data.update(values, updated_at=time.time())
            atomic_json(self._file(job_id), data)
            return self.read(job_id)

    def bookmark(self, job_id, body):
        _, segments = self.study.source(job_id)
        lookup = {s.id: s for s in segments}
        segment = lookup.get(body.segment_id) if body.segment_id is not None else None
        if body.segment_id is not None and not segment:
            raise ValueError('引用的文字稿片段不存在')
        if segment is None and body.position is None:
            raise ValueError('请选择原文片段或播放位置')
        with self.lock:
            data = self.read(job_id)
            if len(data['bookmarks']) >= 500:
                raise ValueError('此课次书签已达上限')
            bookmark = {'id': uuid.uuid4().hex, 'segment_id': segment.id if segment else None,
                        'position': segment.start if segment else body.position,
                        'quote': segment.text if segment else '', 'note': body.note,
                        'source_hash': digest([s.model_dump() for s in segments])}
            data['bookmarks'].append(bookmark)
            return self.update(job_id, bookmarks=data['bookmarks'])

    def remove(self, job_id, bookmark_id):
        with self.lock:
            data = self.read(job_id)
            return self.update(job_id, bookmarks=[item for item in data['bookmarks'] if item['id'] != bookmark_id])
