from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit
import uuid
import zipfile

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, SecretStr, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from ..media_tools import MediaError
from ..portal import PortalError, YanheClient
from ..schema import Report
from .auth import BrowserLogin
from .jobs import JobRunner, atomic_json
from .models import BatchRequest, JobRequest, LibraryUpdate, ModelSettings
from .library import CourseLibrary
from .learning import StudyRequest, StudyRunner


@dataclass
class Session:
    settings: ModelSettings = field(default_factory=ModelSettings.from_environment)
    portal: object = None
    user: dict | None = None
    login_status: dict = field(default_factory=lambda: {'state': 'disconnected', 'message': '尚未连接延河课堂'})
    login_stop: object = None


class TokenInput(BaseModel):
    token: SecretStr


class ReviewInput(BaseModel):
    reviewed: bool


def create_app(data_dir=None, *, client_factory=YanheClient, login_manager=None, runner=None, testing=False):
    root = Path(data_dir or 'runs/webui').resolve()
    root.mkdir(parents=True, exist_ok=True)
    uploads = root / 'uploads'
    uploads.mkdir(exist_ok=True)
    jobs = runner or JobRunner(root / 'jobs')
    study = StudyRunner(jobs)
    library = CourseLibrary(root / 'library.json')
    login = login_manager or BrowserLogin(client_factory=client_factory)
    sessions, lock = {}, threading.RLock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        login.close()
        study.close()
        jobs.close()

    app = FastAPI(title='延河课堂重要事项雷达', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs, app.state.login, app.state.sessions = jobs, login, sessions
    app.state.study = study
    app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32), session_cookie='yanhe_workspace',
                       same_site='strict', https_only=False, max_age=43200)

    @app.middleware('http')
    async def local_guard(request, call_next):
        hostname = request.url.hostname
        if hostname not in ({'localhost', '127.0.0.1', '::1', 'testserver'} if testing else {'localhost', '127.0.0.1', '::1'}):
            return JSONResponse({'error': '此工作台只允许本机访问。'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
            return JSONResponse({'error': '请求来源与当前工作台不一致。'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
        return response

    def session_for(request, mutation=False):
        with lock:
            sid = request.session.get('sid')
            if sid not in sessions:
                sid = secrets.token_urlsafe(24)
                request.session['sid'] = sid
                request.session['csrf'] = secrets.token_urlsafe(24)
                sessions[sid] = Session()
            if mutation and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), request.session['csrf']):
                raise PortalError('页面会话已更新，请刷新后再试。', 'csrf', 403)
            return sessions[sid]

    def portal_for(request, mutation=False):
        session = session_for(request, mutation)
        if not session.portal:
            raise PortalError('请先连接延河课堂。', 'auth', 401)
        return session.portal

    @app.exception_handler(PortalError)
    async def platform_error(request, exc):
        if exc.kind == 'auth':
            sid = request.session.get('sid')
            session = sessions.get(sid)
            if session:
                session.portal, session.user = None, None
                session.login_status = {'state': 'disconnected', 'message': str(exc)}
        return JSONResponse({'error': str(exc), 'kind': exc.kind}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = ['.'.join(str(p) for p in item['loc'][1:]) for item in exc.errors()]
        return JSONResponse({'error': '输入参数不正确，请检查：' + '、'.join(fields)}, status_code=400)

    @app.exception_handler(ValueError)
    async def input_error(request, exc):
        # Pydantic error strings can contain input values: never echo them.
        message = '设置格式不正确，请检查地址、模型与参数。' if isinstance(exc, ValidationError) else str(exc)
        return JSONResponse({'error': message}, status_code=400)

    @app.exception_handler(MediaError)
    async def media_error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=400)

    @app.get('/api/state')
    def state(request: Request):
        session = session_for(request)
        return {'csrf': request.session['csrf'], 'auth': {
            'connected': bool(session.portal), 'display_name': (session.user or {}).get('display_name', ''),
            **session.login_status}, 'settings': session.settings.public(), 'jobs': jobs.list(),
            'library': library.snapshot(jobs.list()), 'features': {'study': True}}

    @app.post('/api/auth/browser')
    def browser_login(request: Request):
        session = session_for(request, True)
        login.start(session)
        return session.login_status

    @app.get('/api/auth')
    def auth_status(request: Request):
        session = session_for(request)
        return {'connected': bool(session.portal), 'display_name': (session.user or {}).get('display_name', ''), **session.login_status}

    @app.post('/api/auth/token')
    def import_token(body: TokenInput, request: Request):
        session = session_for(request, True)
        client = client_factory(body.token.get_secret_value())
        user = client.verify()
        if session.login_stop:
            session.login_stop.set()
        session.portal, session.user = client, user
        session.login_status = {'state': 'connected', 'message': '已连接延河课堂'}
        return {'connected': True, 'display_name': user['display_name']}

    @app.delete('/api/auth')
    def logout(request: Request):
        session = session_for(request, True)
        if session.user:
            jobs.cancel_owner(session.user['owner_key'])
        if session.login_stop:
            session.login_stop.set()
        session.portal, session.user = None, None
        session.login_status = {'state': 'disconnected', 'message': '已退出登录'}
        return {'ok': True}

    @app.put('/api/settings')
    def save_settings(body: dict, request: Request):
        session = session_for(request, True)
        allowed = set(ModelSettings.model_fields)
        if set(body) - allowed:
            raise ValueError('设置中包含未知字段')
        values = session.settings.model_dump()
        # Never forward an old service's secret to a newly selected endpoint.
        for kind in ('asr', 'event', 'vision'):
            identity = [kind + '_base_url', kind + '_provider', kind + '_deployment']
            if any(key in body and body[key] != values.get(key) for key in identity):
                values[kind + '_api_key'] = ''
        for key, value in body.items():
            if key.endswith('_api_key') and value == '':
                continue  # Empty password fields retain existing in-memory keys.
            values[key] = value
        session.settings = ModelSettings.model_validate(values)
        return session.settings.public()

    @app.delete('/api/settings/secrets')
    def clear_secrets(request: Request):
        session = session_for(request, True)
        values = session.settings.model_dump()
        for key in ('asr_api_key', 'event_api_key', 'vision_api_key'):
            values[key] = ''
        session.settings = ModelSettings.model_validate(values)
        return session.settings.public()

    @app.get('/api/courses')
    def courses(request: Request, page: int = 1, query: str = '', mine: bool = True):
        if not 1 <= page <= 10000:
            raise ValueError('页码不正确')
        data = portal_for(request).courses(page, query, mine)
        for course in data['items']:
            library.remember(course)
        return data

    @app.get('/api/courses/{course_id}/sessions')
    def recordings(course_id: str, request: Request):
        data = portal_for(request).sessions(course_id)
        library.remember(data['course'], data['items'])
        return data

    @app.get('/api/library')
    def saved_courses(request: Request):
        session_for(request)
        return {'items': library.snapshot(jobs.list())}

    @app.patch('/api/library/{course_id}')
    def update_saved_course(course_id: str, body: LibraryUpdate, request: Request):
        session_for(request, True)
        library.update(course_id, body.model_dump(exclude_none=True), jobs.list())
        return {'items': library.snapshot(jobs.list())}

    @app.post('/api/batches')
    def submit_batch(body: BatchRequest, request: Request):
        session = session_for(request, True)
        client = portal_for(request)
        session.settings.ready(vision=body.vision)
        prepared, errors, skipped, reused = [], [], [], []
        existing = jobs.list()
        for course_id in dict.fromkeys(body.course_ids):
            try:
                data = client.sessions(course_id)
                library.remember(data['course'], data['items'])
            except PortalError as exc:
                if exc.kind == 'auth':
                    raise
                errors.append({'course_id': course_id, 'message': '无法读取此课程，请检查权限或重新连接后重试。'})
                continue
            except ValueError:
                errors.append({'course_id': course_id, 'message': '无法读取此课程，请检查权限或重新连接后重试。'})
                continue
            for recording in data['items']:
                signal = body.signal
                if signal == 'auto':
                    signal = 'screen' if 'screen' in recording['signals'] else 'camera'
                if signal not in recording['signals']:
                    errors.append({'course_id': course_id, 'message': f'{recording["title"]} 没有所选录像信号'})
                    continue
                values = {k: recording[k] for k in ('course_id', 'session_id', 'video_id')}
                values.update(signal=signal, audio_source=body.audio_source, audio_offset=body.audio_offset,
                    vision=body.vision, export_clips=body.export_clips)
                matching = [j for j in existing if j['owner'] == client.user['owner_key']
                    and all(j['request'].get(k) == values[k] for k in ('course_id', 'session_id', 'video_id'))]
                if any(j['status'] in ('queued', 'running') for j in matching):
                    reused.append({'course_id': course_id, 'video_id': recording['video_id']})
                    continue
                if body.skip_completed and any(j['status'] == 'completed' for j in matching):
                    skipped.append({'course_id': course_id, 'video_id': recording['video_id']})
                    continue
                prepared.append((JobRequest(**values, force=not body.skip_completed), recording['title']))
        if len(prepared) > 100:
            raise ValueError('一次最多处理 100 个课次，请减少所选课程')
        queued = [jobs.submit(item, session.settings, client=client, title=title) for item, title in prepared]
        return {'jobs': queued, 'skipped': skipped, 'reused': reused, 'errors': errors}

    @app.post('/api/uploads')
    async def upload(request: Request, file: UploadFile = File(...)):
        session_for(request, True)
        name = (file.filename or 'input').replace('\\', '/').rsplit('/', 1)[-1]
        extension = Path(name).suffix.lower()
        if extension not in ('.mp4', '.mkv', '.mov', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.srt', '.vtt', '.txt'):
            raise ValueError('请选择视频、音频或字幕文件')
        upload_id = uuid.uuid4().hex
        destination = uploads / (upload_id + extension)
        total = 0
        with destination.open('wb') as target:
            while block := await file.read(2 * 1024**2):
                total += len(block)
                if total > 8 * 1024**3:
                    raise ValueError('文件超过 8 GiB，请先截取需要的课次')
                target.write(block)
        if total == 0:
            raise ValueError('文件为空')
        atomic_json(uploads / (upload_id + '.json'), {'id': upload_id, 'name': name[:180],
            'file': destination.name, 'size': total})
        return {'id': upload_id, 'name': name, 'size': total}

    def uploaded(upload_id):
        if not re.fullmatch(r'[a-f0-9]{32}', upload_id):
            raise ValueError('上传记录不存在')
        metadata_file = uploads / (upload_id + '.json')
        if not metadata_file.is_file():
            raise ValueError('上传记录不存在，请重新选择文件')
        metadata = json.loads(metadata_file.read_text('utf-8'))
        path = (uploads / metadata['file']).resolve()
        if path.parent != uploads or not path.is_file():
            raise ValueError('上传文件已不可用')
        return path, metadata

    @app.post('/api/jobs')
    def submit(body: JobRequest, request: Request):
        session = session_for(request, True)
        if body.upload_id:
            path, metadata = uploaded(body.upload_id)
            return jobs.submit(body, session.settings, local_path=path, title=metadata['name'])
        client = portal_for(request)
        recording = client.recording(body.course_id, body.session_id, body.video_id)
        return jobs.submit(body, session.settings, client=client, title=recording.title)

    @app.post('/api/demo')
    def demo(request: Request):
        session = session_for(request, True)
        return jobs.submit(JobRequest(upload_id='demo'), session.settings, demo=True, title='合成课堂演示 · 四个重要事项')

    @app.get('/api/jobs')
    def job_list(request: Request):
        session_for(request)
        return {'items': jobs.list()}

    @app.get('/api/jobs/{job_id}')
    def job_status(job_id: str, request: Request):
        session_for(request)
        return jobs.get(job_id)

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(job_id: str, request: Request):
        session_for(request, True)
        return jobs.cancel(job_id)

    @app.post('/api/jobs/{job_id}/retry')
    def retry(job_id: str, request: Request):
        session = session_for(request, True)
        job = jobs.get(job_id)
        local = None
        if job['source'] == 'local':
            try:
                local, _ = uploaded(job['request']['upload_id'])
            except ValueError:
                pass
        return jobs.retry(job_id, session.settings, client=session.portal if job['source'] == 'yanhe' else None, local_path=local)

    def file_url(job_id, value):
        if not value:
            return None
        try:
            relative = Path(value).resolve().relative_to((jobs.root / job_id).resolve())
        except ValueError:
            return None
        return '/api/jobs/' + job_id + '/files/' + relative.as_posix()

    @app.get('/api/jobs/{job_id}/report')
    def report(job_id: str, request: Request):
        session_for(request)
        job = jobs.get(job_id)
        directory = jobs.root / job_id
        file = directory / 'analysis/events.json'
        if not file.is_file():
            raise ValueError('报告尚未生成')
        data = Report.model_validate_json(file.read_text('utf-8')).model_dump(mode='json')
        links = {'video': file_url(job_id, directory / job['media_file']) if job.get('media_file') else None,
                 'report': file_url(job_id, directory / 'analysis/report.md'),
                 'events': file_url(job_id, file), 'quality': file_url(job_id, directory / 'analysis/quality.json'),
                 'subtitles': file_url(job_id, directory / 'analysis/transcript.srt') if (directory / 'analysis/transcript.srt').exists() else None}
        event_links = {event['event_id']: {'clip': file_url(job_id, event.get('audio_clip_path')),
            'frames': [file_url(job_id, source['path']) for source in event['sources'] if source['kind'] == 'frame']}
            for event in data['events']}
        reviews_file = directory / 'reviews.json'
        reviews = json.loads(reviews_file.read_text('utf-8')) if reviews_file.exists() else {}
        return {'report': data, 'links': links, 'event_links': event_links, 'reviews': reviews}

    @app.get('/api/jobs/{job_id}/learning')
    def learning(job_id: str, request: Request):
        session_for(request)
        job = jobs.get(job_id)
        data = study.snapshot(job_id)
        directory = jobs.root / job_id
        data['video'] = file_url(job_id, directory / job['media_file']) if job.get('media_file') and Path(job['media_file']).suffix.lower() not in ('.srt', '.vtt', '.txt') else None
        data['export'] = file_url(job_id, directory / 'analysis/learning.md') if data.get('chapters') else None
        return data

    @app.get('/api/jobs/{job_id}/transcript')
    def transcript_page(job_id: str, request: Request, offset: int = Query(0, ge=0),
                        limit: int = Query(80, ge=1, le=200), q: str = Query('', max_length=500),
                        chapter: int | None = Query(None, ge=0)):
        session_for(request)
        _, segments = study.source(job_id)
        if chapter is not None:
            chapters = study.snapshot(job_id)['chapters']
            if chapter >= len(chapters):
                raise ValueError('章节不存在或需要重新生成')
            selected = chapters[chapter]
            positions = {s.id: i for i, s in enumerate(segments)}
            segments = segments[positions[selected['start_id']]:positions[selected['end_id']]+1]
        if q.strip():
            segments = [s for s in segments if q.strip().casefold() in s.text.casefold()]
        return {'items': [s.model_dump() for s in segments[offset:offset+limit]],
                'total': len(segments), 'offset': offset, 'limit': limit}

    @app.post('/api/jobs/{job_id}/learning')
    def generate_learning(job_id: str, body: StudyRequest, request: Request):
        session = session_for(request, True)
        return study.start(job_id, session.settings, body)

    @app.post('/api/jobs/{job_id}/learning/cancel')
    def cancel_learning(job_id: str, request: Request):
        session_for(request, True)
        return study.cancel(job_id)

    @app.put('/api/jobs/{job_id}/reviews/{event_id}')
    def review(job_id: str, event_id: str, body: ReviewInput, request: Request):
        session_for(request, True)
        jobs.get(job_id)
        directory = jobs.root / job_id
        data = Report.model_validate_json((directory / 'analysis/events.json').read_text('utf-8'))
        if event_id not in {e.event_id for e in data.events}:
            raise ValueError('事项不存在')
        file = directory / 'reviews.json'
        reviews = json.loads(file.read_text('utf-8')) if file.exists() else {}
        reviews[event_id] = body.reviewed
        atomic_json(file, reviews)
        return {'reviewed': body.reviewed}

    @app.get('/api/jobs/{job_id}/files/{relative:path}')
    def artifact(job_id: str, relative: str, request: Request):
        session_for(request)
        jobs.get(job_id)
        directory = (jobs.root / job_id).resolve()
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or any(part.startswith('.') for part in Path(relative).parts):
            raise PortalError('文件不可访问。', 'file', 403)
        if not path.is_file() or path.suffix.lower() not in ('.mp4', '.mkv', '.mov', '.webm', '.mp3', '.wav', '.aac', '.m4a', '.flac', '.ogg', '.srt', '.vtt', '.txt', '.md', '.json', '.jpg', '.png'):
            raise PortalError('文件不存在或不能通过工作台访问。', 'file', 404)
        if path.name in ('job.json', 'reviews.json'):
            raise PortalError('此文件不作为报告附件提供。', 'file', 403)
        return FileResponse(path)

    @app.get('/api/jobs/{job_id}/export')
    def export_bundle(job_id: str, request: Request):
        session_for(request)
        jobs.get(job_id)
        directory = jobs.root / job_id
        analysis = directory / 'analysis'
        if not (analysis / 'events.json').exists():
            raise ValueError('报告尚未生成')
        destination = directory / 'report-bundle.zip'
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in analysis.rglob('*'):
                relative = path.relative_to(analysis)
                if path.is_file() and not any(p.startswith('.') for p in relative.parts) and path.suffix.lower() in ('.json', '.md', '.srt', '.jpg', '.png', '.wav'):
                    archive.write(path, 'analysis/' + relative.as_posix())
        return FileResponse(destination, filename='课堂重要事项报告.zip')

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def homepage():
        return FileResponse(static / 'index.html')

    return app


def serve(port=8765, data_dir=None, open_browser=True):
    import uvicorn
    import httpx
    import webbrowser
    address = f'http://127.0.0.1:{port}'
    try:
        response = httpx.get(address, timeout=1)
        if response.status_code == 200 and '延河课堂 · 重要事项雷达' in response.text:
            print(f'工作台已在运行：{address}', flush=True)
            if open_browser:
                webbrowser.open(address)
            return
    except httpx.HTTPError:
        pass
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(address)).start()
    print(f'延河课堂工作台：http://127.0.0.1:{port}', flush=True)
    uvicorn.run(create_app(data_dir), host='127.0.0.1', port=port, access_log=False, log_level='warning')
