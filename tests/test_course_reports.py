import json
from pathlib import Path

from fastapi.testclient import TestClient

from yanhe_sentinel.web.app import create_app
from yanhe_sentinel.web.library import CourseLibrary
from test_webui import bootstrap


class ReportJobs:
    def __init__(self, root, items):
        self.root, self.items = Path(root), items

    def list(self):
        return self.items

    def get(self, job_id):
        return next(item for item in self.items if item['id'] == job_id)

    def close(self):
        pass


def job(identifier, session='200', *, status='completed', updated=1, course='100'):
    return {'id': identifier, 'source': 'yanhe', 'status': status, 'stage': status,
        'title': '合成课次 ' + str(session), 'event_count': 1, 'media_file': None,
        'updated_at': updated, 'created_at': updated,
        'request': {'course_id': course, 'session_id': session, 'video_id': '300',
                    'lecture_date': '2026-01-01'}}


def write_report(root, item, summary, *, partial=False):
    directory = root / item['id'] / 'analysis'
    directory.mkdir(parents=True)
    report = {'schema_version': '1.0', 'lecture_id': item['id'], 'lecture_date': None,
        'provider': 'synthetic-fixture', 'simulated': True,
        'analysis_status': 'partial' if partial else 'completed',
        'coverage_status': 'partial' if partial else 'complete_for_provided_media',
        'detection_status': 'events_detected', 'provided_subtitle_end_seconds': 20,
        'analyzed_intervals': [{'start_seconds': 0, 'end_seconds': 20}],
        'total_segments': 1, 'analyzed_segment_ids': [1], 'failed_chunk_ids': [],
        'warnings': ['合成测试，需人工核验'], 'events': [{'event_id': 'same-id-in-each-recording',
            'type': 'homework', 'severity': 'high', 'polarity': 'affirmed', 'tense': 'future',
            'status': 'actionable', 'summary': summary, 'action': '提交作业', 'due_text': '下周五',
            'start_seconds': 12, 'end_seconds': 20, 'listen_start_seconds': 10,
            'listen_end_seconds': 20, 'sources': [{'path': 'synthetic.srt', 'segment_ids': [1],
                'quote': '下周五提交作业'}], 'review_reason': '核对原文'}]}
    (directory/'events.json').write_text(json.dumps(report, ensure_ascii=False), 'utf-8')


def test_course_report_selects_one_result_per_recording_and_exports_offline(tmp_path):
    items = [job('a'*32, course=100), job('b'*32, status='partial', updated=3),
             job('c'*32, '201', updated=2)]
    runner = ReportJobs(tmp_path/'jobs', items)
    library = CourseLibrary(tmp_path/'workspace/library.json')
    library.remember({'id': '100', 'title': '合成课程', 'professors': []})
    write_report(runner.root, items[0], '第一课次的完整结果')
    write_report(runner.root, items[1], '失败重试中的重复结果', partial=True)
    write_report(runner.root, items[2], '第二课次的完整结果')
    with TestClient(create_app(tmp_path/'workspace', runner=runner, testing=True)) as client:
        bootstrap(client)  # No campus login or model configuration is needed.
        courses = client.get('/api/library').json()['items']
        assert len(courses) == 1 and courses[0]['event_count'] == 2
        report = client.get('/api/library/100/report').json()
        assert report['id'] == 'course-100' and report['status'] == 'completed'
        assert report['total_sessions'] == 2 and report['completed_sessions'] == 2
        assert report['event_count'] == 2
        assert {lecture['result']['job_id'] for lecture in report['lectures']} == {'a'*32, 'c'*32}
        first = next(l for l in report['lectures'] if l['session_id'] == '200')
        assert first['latest_job_id'] == 'b'*32 and first['result']['report']['events'][0]['start_seconds'] == 12
        assert first['result']['event_links'] and first['result']['reviews'] == {}
        exported = client.get('/api/library/100/report?format=markdown')
        assert exported.status_code == 200 and 'course-100.md' in exported.headers['content-disposition']
        assert exported.text.count('第一课次的完整结果') == 1
        assert exported.text.count('第二课次的完整结果') == 1
        assert '失败重试中的重复结果' not in exported.text and '00:00:12' in exported.text
        assert 'Synthetic demo' in exported.text and 'Reviewed: no' in exported.text
        source = runner.root/items[0]['id']/'analysis/events.json'
        original = json.loads(source.read_text('utf-8'))
        original['events'][0]['start_seconds'] = None
        source.write_text(json.dumps(original, ensure_ascii=False), 'utf-8')
        assert 'Unknown time' in client.get('/api/library/100/report?format=markdown').text
        assert client.get('/api/library/999/report').status_code == 400
        assert client.get('/api/library/100/report?format=html').status_code == 400
    assert all((runner.root/item['id']/'analysis/events.json').is_file() for item in items)


def test_course_report_exposes_missing_sessions_and_falls_back_from_corrupt_result(tmp_path):
    items = [job('a'*32), job('b'*32, updated=2), job('c'*32, '201', status='failed', updated=3)]
    runner = ReportJobs(tmp_path/'jobs', items)
    library = CourseLibrary(tmp_path/'workspace/library.json')
    library.remember({'id': '100', 'title': '合成课程'}, [
        {'session_id': '202', 'video_id': '300', 'title': '尚未处理的课次'}])
    write_report(runner.root, items[0], '保留的完整结果')
    path = runner.root/items[1]['id']/'analysis'
    path.mkdir(parents=True)
    (path/'events.json').write_text('corrupt', 'utf-8')
    with TestClient(create_app(tmp_path/'workspace', runner=runner, testing=True)) as client:
        data = client.get('/api/library/100/report').json()
        assert data['status'] == 'partial' and data['total_sessions'] == 3
        assert data['completed_sessions'] == 1 and data['available_sessions'] == 1
        assert sum(l['result'] is None for l in data['lectures']) == 2
        selected = next(l for l in data['lectures'] if l['result'])
        assert selected['unreadable_result'] and selected['result']['job_id'] == 'a'*32
        assert 'Report unavailable' in client.get('/api/library/100/report?format=markdown').text


def test_same_title_in_different_course_ids_stays_separate_and_snapshot_is_stable(tmp_path):
    library = CourseLibrary(tmp_path/'library.json')
    for course in ['100', '101']:
        library.remember({'id': course, 'title': '同名合成课程'})
    items = [job('first', course='100'), job('second', course='101')]
    first = library.snapshot(items)
    second = library.snapshot(items)
    assert len(first) == 2 and first == second
    assert all(len(c['sessions'][0]['job_ids']) == 1 for c in second)
