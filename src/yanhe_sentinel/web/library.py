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
                course_jobs.setdefault(str(course_id), []).append(job)
        result = []
        for course_id in entries.keys() | course_jobs.keys():
            entry = entries.get(course_id, {})
            history = sorted(course_jobs.get(course_id, []), key=lambda j: j['updated_at'], reverse=True)
            sessions = {(str(s['session_id']), str(s['video_id'])): {
                **{k: v for k, v in s.items() if k not in
                ('job_ids', 'latest_job_id', 'completed_job_id', 'completed_job_ids', 'status')},
                'course_id': course_id, 'session_id': str(s['session_id']), 'video_id': str(s['video_id'])}
                for s in entry.get('sessions', [])}
            for job in history:
                request = job['request']
                key = (str(request['session_id']), str(request['video_id']))
                session = sessions.setdefault(key, {'course_id': course_id, 'session_id': key[0],
                    'video_id': key[1], 'title': job['title'], 'started_at': request.get('lecture_date') or '',
                    'duration': None, 'signals': [request.get('signal', 'screen')]})
                session.setdefault('job_ids', []).append(job['id'])
                session.setdefault('latest_job_id', job['id'])
                session.setdefault('status', job['status'])
                if job['status'] == 'completed':
                    session.setdefault('completed_job_id', job['id'])
                    session.setdefault('completed_job_ids', []).append(job['id'])
            recordings = sorted(sessions.values(), key=lambda s: (s.get('started_at', ''), s['session_id']), reverse=True)
            completed = sum(bool(s.get('completed_job_id')) for s in recordings)
            active = sum(j['status'] in ('queued', 'running') for j in history)
            report_jobs = {s.get('completed_job_id') or s.get('latest_job_id') for s in recordings}
            result.append({'id': course_id, 'title': entry.get('title') or f'课程 {course_id}',
                'professors': entry.get('professors', []), 'school_year': entry.get('school_year', ''),
                'semester': entry.get('semester', ''), 'archived': entry.get('archived', False),
                'starred': entry.get('starred', False), 'notes': entry.get('notes', ''),
                'total_sessions': len(recordings), 'completed_sessions': completed,
                'pending_sessions': len(recordings)-completed, 'active_jobs': active,
                'event_count': sum(j.get('event_count', 0) for j in history if j['id'] in report_jobs),
                'updated_at': max([entry.get('updated_at', 0)] + [j['updated_at'] for j in history]),
                'sessions': recordings, 'job_ids': [j['id'] for j in history]})
        return sorted(result, key=lambda c: (c['starred'], c['updated_at']), reverse=True)


def course_report(course, load_report):
    """Build one live report per official course, selecting one result per recording.

    Completed results survive failed retries. Stored job reports remain untouched;
    they are evidence for the canonical course report, not separate course reports.
    """
    lectures = []
    for session in reversed(course['sessions']):
        completed_ids = session.get('completed_job_ids', [session.get('completed_job_id')])
        candidates = list(dict.fromkeys(filter(None, [*completed_ids,
                                                     *session.get('job_ids', [])])))
        selected = None
        unreadable = False
        for job_id in candidates:
            try:
                selected = {'job_id': job_id, **load_report(job_id)}
                break
            except (OSError, ValueError, KeyError):
                unreadable = True
        complete = bool(selected and selected['job_id'] in completed_ids
                        and selected['report']['analysis_status'] == 'completed')
        lectures.append({'session_id': str(session['session_id']), 'video_id': str(session['video_id']),
            'title': session['title'], 'started_at': session.get('started_at', ''),
            'latest_job_id': session.get('latest_job_id'), 'status': session.get('status', 'not_started'),
            'report_status': 'completed' if complete else 'partial' if selected else 'unavailable',
            'unreadable_result': unreadable, 'result': selected})
    completed = sum(lecture['report_status'] == 'completed' for lecture in lectures)
    available = sum(lecture['result'] is not None for lecture in lectures)
    return {'id': 'course-' + course['id'], 'course_id': course['id'], 'title': course['title'],
        'professors': course['professors'], 'school_year': course['school_year'],
        'semester': course['semester'], 'updated_at': course['updated_at'],
        'status': 'completed' if lectures and completed == len(lectures) else 'partial' if available
                  else 'running' if course['active_jobs'] else 'queued',
        'total_sessions': len(lectures), 'completed_sessions': completed,
        'available_sessions': available,
        'event_count': sum(len(lecture['result']['report']['events']) for lecture in lectures if lecture['result']),
        'lectures': lectures}


def course_report_markdown(data):
    """Export the same canonical report, including gaps and source timestamps."""
    lines = [f"# {data['title']}", '', f"课程 / Course ID: {data['course_id']}",
             f"完整课次 / Completed recordings: {data['completed_sessions']} / {data['total_sessions']}",
             '', '模型结果需核对，未检出不代表课堂上未发生。 / Verify findings against the recording.', '']
    for lecture in data['lectures']:
        lines += [f"## {lecture['title']}", '', str(lecture['started_at']),
                  f"状态 / Status: {lecture['report_status']}", '']
        result = lecture['result']
        if not result:
            lines += ['报告尚未生成或无法读取。 / Report unavailable.', '']
            continue
        report = result['report']
        lines += [f"来源任务 / Source job: {result['job_id']}", '']
        if report['simulated']:
            lines += ['> 合成演示，非真实课程结果。 / Synthetic demo, not a real course result.', '']
        lines += [f"字幕覆盖 / Subtitle coverage: {len(report['analyzed_segment_ids'])} / {report['total_segments']}", '']
        for warning in report['warnings']:
            lines += [f'> {warning}', '']
        for event in report['events']:
            seconds = event.get('start_seconds')
            stamp = '时间未知 / Unknown time' if seconds is None else (
                f'{int(seconds)//3600:02}:{int(seconds)//60%60:02}:{int(seconds)%60:02}')
            lines += [f"### [{stamp}] {event['summary']}", '',
                      f"{event['type']} · {event['status']}", '',
                      '核验 / Reviewed: ' + ('yes' if result['reviews'].get(event['event_id']) else 'no'), '']
            if event.get('action'):
                lines += [event['action'], '']
            if event.get('due_text'):
                lines += [f"截止原文 / Deadline quote: {event['due_text']}", '']
            for source in event['sources']:
                lines += ['> ' + source.get('quote', '').replace('\n', '\n> '), '']
        if not report['events']:
            lines += ['所提供内容中未检出事项；请查看覆盖范围。 / No findings in the provided content; check coverage.', '']
    return '\n'.join(lines)
