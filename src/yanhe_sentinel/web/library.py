"""Local course metadata. Never persist portal credentials or media URLs here."""
import json
from pathlib import Path
import re
import threading
import time

from .jobs import atomic_json


class CourseLibrary:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            self.entries = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            self.entries = {}

    def remember(self, course, sessions=None):
        identifier = str(course['id'])
        if not re.fullmatch(r'\d+', identifier):
            raise ValueError('课程编号不正确')
        with self.lock:
            previous = self.entries.get(identifier, {})
            entry = {**previous, **{k: course[k] for k in
                ('title', 'professors', 'school_year', 'semester', 'college') if k in course},
                'id': identifier, 'updated_at': time.time()}
            if sessions is not None:
                entry['sessions'] = [{k: s[k] for k in ('course_id', 'session_id', 'video_id',
                    'title', 'started_at', 'ended_at', 'duration', 'signals') if k in s} for s in sessions]
            self.entries[identifier] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, self.entries)

    def update(self, course_id, values, jobs):
        if not any(c['id'] == course_id for c in self.snapshot(jobs)):
            raise ValueError('课程尚未保存，请先读取课程')
        with self.lock:
            self.entries.setdefault(course_id, {'id': course_id}).update(values)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, self.entries)

    def snapshot(self, jobs):
        with self.lock:
            entries = json.loads(json.dumps(self.entries))
        course_jobs = {}
        for job in jobs:
            course_id = job.get('request', {}).get('course_id')
            if job.get('source') == 'yanhe' and course_id:
                course_jobs.setdefault(course_id, []).append(job)
        result = []
        for course_id in entries.keys() | course_jobs.keys():
            entry = entries.get(course_id, {})
            history = sorted(course_jobs.get(course_id, []), key=lambda j: j['updated_at'], reverse=True)
            sessions = {(s['session_id'], s['video_id']): dict(s) for s in entry.get('sessions', [])}
            for job in history:
                request = job['request']
                key = (request['session_id'], request['video_id'])
                session = sessions.setdefault(key, {'course_id': course_id, 'session_id': key[0],
                    'video_id': key[1], 'title': job['title'], 'started_at': request.get('lecture_date') or '',
                    'duration': None, 'signals': [request.get('signal', 'screen')]})
                session.setdefault('job_ids', []).append(job['id'])
                session.setdefault('latest_job_id', job['id'])
                session.setdefault('status', job['status'])
                if job['status'] == 'completed':
                    session.setdefault('completed_job_id', job['id'])
            recordings = sorted(sessions.values(), key=lambda s: (s.get('started_at', ''), s['session_id']), reverse=True)
            completed = sum(bool(s.get('completed_job_id')) for s in recordings)
            active = sum(j['status'] in ('queued', 'running') for j in history)
            latest_jobs = {s.get('latest_job_id') for s in recordings}
            result.append({'id': course_id, 'title': entry.get('title') or f'课程 {course_id}',
                'professors': entry.get('professors', []), 'school_year': entry.get('school_year', ''),
                'semester': entry.get('semester', ''), 'archived': entry.get('archived', False),
                'starred': entry.get('starred', False), 'notes': entry.get('notes', ''),
                'total_sessions': len(recordings), 'completed_sessions': completed,
                'pending_sessions': len(recordings)-completed, 'active_jobs': active,
                'event_count': sum(j.get('event_count', 0) for j in history if j['id'] in latest_jobs),
                'updated_at': max([entry.get('updated_at', 0)] + [j['updated_at'] for j in history]),
                'sessions': recordings, 'job_ids': [j['id'] for j in history]})
        return sorted(result, key=lambda c: (c['starred'], c['updated_at']), reverse=True)
