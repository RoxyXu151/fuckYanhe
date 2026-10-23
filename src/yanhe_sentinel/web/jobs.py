import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from ..detection import digest
from ..download import DownloadCanceled, VODDownloader
from ..media_tools import MediaError
from ..portal import PortalError
from ..schema import Report

TERMINAL = {'completed', 'partial', 'failed', 'canceled', 'interrupted'}
REPAIR_VERSION = 'local-transport-recovery-2'
MAX_REPAIR_ATTEMPTS = 2


def atomic_json(path, value):
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class JobRunner:
    def __init__(self, root, *, downloader_factory=VODDownloader, download_workers=None, analysis_workers=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        from .archive import RemoteArchive
        self.archive = RemoteArchive.from_environment(self.root)
        self.downloader_factory = downloader_factory
        self.jobs, self.payloads, self.cancels = {}, {}, {}
        self.lock, self.queue, self.stopping = threading.RLock(), queue.Queue(), threading.Event()
        self.download_workers = max(1, min(4, int(download_workers or os.environ.get('YANHE_DOWNLOAD_WORKERS', '2'))))
        self.analysis_workers = max(1, min(2, int(analysis_workers or os.environ.get('YANHE_ANALYSIS_WORKERS', '1'))))
        self.analysis_queue = queue.Queue(maxsize=3)
        self.repair_context = None
        self.repair_stop = threading.Event()
        for file in self.root.glob('*/job.json'):
            try:
                job = json.loads(file.read_text('utf-8'))
                if not re.fullmatch(r'[a-f0-9]{32}', job['id']) or file.parent.name != job['id']:
                    continue
                if job['status'] not in TERMINAL:
                    job.update(status='interrupted', resume_requested=not job.get('cancel_requested', False),
                               message='队列已恢复；已有媒体可继续分析，未下载课程等待重新登录。')
                    atomic_json(file, job)
                self.jobs[job['id']] = job
            except (OSError, ValueError, KeyError):
                continue
        self.workers = [threading.Thread(target=self._loop, args=('download',), daemon=True,
                         name=f'yanhe-download-{i}') for i in range(self.download_workers)]
        self.workers += [threading.Thread(target=self._loop, args=('analysis',), daemon=True,
                         name=f'yanhe-analysis-{i}') for i in range(self.analysis_workers)]
        self.workers.append(threading.Thread(target=self._repair_loop, daemon=True, name='yanhe-failed-piece-repair'))
        self.worker = self.workers[0]
        for worker in self.workers:
            worker.start()

    def _update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values, updated_at=time.time())
            atomic_json(self.root / job_id / 'job.json', self.jobs[job_id])

    def list(self):
        with self.lock:
            return sorted((dict(j) for j in self.jobs.values()), key=lambda j: j['created_at'], reverse=True)

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError('任务不存在')
            return dict(self.jobs[job_id])

    def submit(self, request, settings, *, client=None, local_path=None, title='', demo=False):
        if not demo:
            settings.ready(subtitles=bool(local_path and local_path.suffix.lower() in ('.srt', '.vtt', '.txt')), vision=request.vision)
        owner = client.user['owner_key'] if client else 'local'
        fingerprint = digest([request.model_dump(exclude={'force'}), settings.public() if not demo else 'demo', owner])
        with self.lock:
            if not request.force:
                existing = next((job for job in self.jobs.values() if job['fingerprint'] == fingerprint
                    and job['status'] in ('queued', 'running', 'completed')), None)
                if existing:
                    return dict(existing)
            job_id = uuid.uuid4().hex
            directory = self.root / job_id
            directory.mkdir()
            job = {'id': job_id, 'fingerprint': fingerprint, 'owner': owner, 'title': title or '课堂录播',
                'source': 'demo' if demo else 'local' if local_path else 'yanhe', 'status': 'queued',
                'stage': 'queued', 'message': '等待处理', 'current': 0, 'total': 0, 'cancel_requested': False,
                'request': request.model_dump(), 'model_config': settings.public(), 'logs': [],
                'created_at': time.time(), 'updated_at': time.time(), 'media_file': None, 'resume_requested': True}
            self.jobs[job_id], self.cancels[job_id] = job, threading.Event()
            self.payloads[job_id] = (request, settings.model_copy(deep=True), client, local_path, demo)
            atomic_json(directory / 'job.json', job)
            self.queue.put(job_id)
            return dict(job)

    def retry(self, job_id, settings, *, client=None, local_path=None):
        with self.lock:
            return self._retry(job_id, settings, client=client, local_path=local_path)

    def _retry(self, job_id, settings, *, client=None, local_path=None, prepared_repair=False):
        from .models import JobRequest
        job = self.get(job_id)
        if job['status'] not in TERMINAL:
            raise ValueError('任务正在处理，无需重复启动')
        request = JobRequest.model_validate(job['request'])
        demo = job['source'] == 'demo'
        if not demo:
            settings.ready(subtitles=bool(local_path and local_path.suffix.lower() in ('.srt', '.vtt', '.txt')), vision=request.vision)
        if job['source'] == 'yanhe' and (not client or client.user['owner_key'] != job['owner']):
            raise ValueError('请登录创建此任务的延河课堂账户后再继续')
        if job['source'] == 'local' and not local_path:
            existing = self.root / job_id / (job['media_file'] or 'missing')
            if not existing.is_file():
                raise ValueError('原始本地文件已不可用，请重新上传')
            local_path = existing
        with self.lock:
            self.cancels[job_id] = threading.Event()
            self.payloads[job_id] = (request, settings.model_copy(deep=True), client, local_path, demo)
            self._update(job_id, status='queued', stage='queued', message='已排队，将复用成功缓存',
                         cancel_requested=False, resume_requested=True, current=0, total=0, model_config=settings.public(),
                         fingerprint=digest([request.model_dump(exclude={'force'}), settings.public() if not demo else 'demo', job['owner']]))
            if prepared_repair and self._prepared(job_id):
                try:
                    self.analysis_queue.put_nowait(job_id)
                    self._update(job_id, stage='waiting_analysis')
                except queue.Full:
                    self.queue.put(job_id)
            else:
                self.queue.put(job_id)
        return self.get(job_id)

    def cancel(self, job_id):
        job = self.get(job_id)
        if job['status'] not in TERMINAL:
            self.cancels[job_id].set()
            self._update(job_id, cancel_requested=True, resume_requested=False, message='正在停止，已完成的缓存会保留')
        return self.get(job_id)

    def cancel_owner(self, owner):
        with self.lock:
            if self.repair_context and self.repair_context[1] and self.repair_context[1].user['owner_key'] == owner:
                self.repair_context = None
        for job in self.list():
            if job['owner'] == owner and job['status'] not in TERMINAL:
                self.cancel(job['id'])

    def _prepared(self, job_id):
        directory = self.root / job_id
        data = self.get(job_id).get('prepared')
        if not data:
            return None
        for key in ('selected', 'audio'):
            if not data.get(key):
                continue
            path = (directory / data[key]).resolve()
            if not path.is_relative_to(directory) or not path.is_file() or not path.stat().st_size:
                return None
        return data if data.get('selected') else None

    def resume_pending(self, settings, *, client=None):
        """Rebuild volatile payloads from the durable queue; never persist credentials."""
        from .models import JobRequest
        with self.lock:
            if client:
                self.repair_context = (settings.model_copy(deep=True), client)
            for job in sorted(self.jobs.values(), key=lambda j: j['created_at']):
                job_id = job['id']
                if not job.get('resume_requested') or job_id in self.payloads or job['status'] != 'interrupted':
                    continue
                prepared = self._prepared(job_id)
                if job['source'] == 'yanhe' and not prepared:
                    if not client or client.user['owner_key'] != job['owner']:
                        continue
                elif job['source'] == 'yanhe' and client and client.user['owner_key'] != job['owner']:
                    continue
                request = JobRequest.model_validate(job['request'])
                local = None
                if job['source'] == 'local' and not prepared:
                    candidate = self.root / job_id / (job.get('media_file') or 'missing')
                    if not candidate.is_file():
                        continue
                    local = candidate
                try:
                    settings.ready(subtitles=bool(prepared and Path(prepared['selected']).suffix.lower() in ('.srt', '.vtt', '.txt')),
                                   vision=request.vision)
                except ValueError:
                    continue
                self.cancels[job_id] = threading.Event()
                self.payloads[job_id] = (request, settings.model_copy(deep=True), client, local, job['source'] == 'demo')
                self._update(job_id, status='queued', stage='queued', cancel_requested=False,
                             message='已恢复队列，将复用已下载媒体及成功片段缓存')
                self.queue.put(job_id)

    def schedule_repairs(self, settings, *, client=None):
        """Bounded retries for missing ASR pieces, never paid APIs or successful jobs."""
        if settings.asr_deployment != 'self-hosted' or 'qwen3-asr' not in settings.asr_model.lower():
            return 0
        scheduled = 0
        with self.lock:
            for job in list(self.jobs.values()):
                if job['status'] not in ('partial', 'failed') or job.get('cancel_requested'):
                    continue
                if job.get('model_config', {}).get('asr_model') != settings.asr_model:
                    continue
                attempts = job.get('repair_attempts', 0) if job.get('repair_version') == REPAIR_VERSION else 0
                if attempts >= MAX_REPAIR_ATTEMPTS:
                    continue
                try:
                    progress = json.loads((self.root / job['id'] / 'analysis/asr_progress.json').read_text('utf-8'))
                    if not progress.get('failed_chunks'):
                        continue
                    self._retry(job['id'], settings, client=client, prepared_repair=True)
                except (OSError, ValueError, PortalError, MediaError):
                    continue
                self._update(job['id'], repair_version=REPAIR_VERSION, repair_attempts=attempts + 1,
                    message=f'自动补齐失败转写片段（{attempts + 1}/{MAX_REPAIR_ATTEMPTS}），成功缓存保留')
                scheduled += 1
        return scheduled

    def _repair_loop(self):
        while not self.repair_stop.wait(15):
            with self.lock:
                context = self.repair_context
            if context:
                self.schedule_repairs(context[0], client=context[1])

    def _loop(self, phase='download'):
        work = self.queue if phase == 'download' else self.analysis_queue
        while not self.stopping.is_set():
            try:
                job_id = work.get(timeout=.3)
            except queue.Empty:
                continue
            handed_off = False
            try:
                if self.cancels[job_id].is_set() or self.stopping.is_set():
                    raise DownloadCanceled()
                # Preserve the lightweight runner seam used by integrations.
                if phase == 'download' and job_id in self.payloads:
                    self._prepare(job_id)
                    self._update(job_id, status='queued', stage='waiting_analysis',
                                 message='下载已就绪，等待分析；其他课程可并行下载', current=0, total=0)
                    while not self.stopping.is_set():
                        if self.cancels[job_id].is_set():
                            raise DownloadCanceled()
                        try:
                            self.analysis_queue.put(job_id, timeout=.2)
                            handed_off = True
                            break
                        except queue.Full:
                            pass
                    if not handed_off:
                        raise DownloadCanceled()
                    continue
                self._update(job_id, status='running', stage='analyzing', message='开始分析已下载媒体')
                self._execute(job_id)
            except DownloadCanceled:
                if self.stopping.is_set() and self.get(job_id).get('resume_requested'):
                    self._update(job_id, status='interrupted', message='服务停止，重启后自动恢复队列')
                else:
                    self._update(job_id, status='canceled', stage='canceled', resume_requested=False,
                                 message='已停止；可继续处理并复用缓存')
            except (PortalError, MediaError, ValueError) as exc:
                self._update(job_id, status='failed', stage='failed', message=str(exc))
            except Exception:
                self._update(job_id, status='failed', stage='failed', message='处理未完成，请检查模型设置和文件后重试；成功缓存已保留。')
            finally:
                if not handed_off:
                    if not self.stopping.is_set():
                        self._update(job_id, resume_requested=False)
                    if self.archive and self.get(job_id)['source'] != 'demo' and not self.stopping.is_set():
                        self._archive(job_id)
                    with self.lock:
                        self.payloads.pop(job_id, None)
                work.task_done()

    def _archive(self, job_id):
        previous = self.get(job_id)
        self._update(job_id, status='running', stage='archiving', message='正在校验并归档到远程存储…')
        while not self.stopping.is_set():
            try:
                saved = self.archive.sync(job_id)
                self._update(job_id, status=previous['status'], stage=previous['stage'],
                             message=previous['message'], remote_archived=True, archive_bytes=saved['bytes'])
                self.archive.sync(job_id, evict=False)
                return
            except Exception:
                if self.cancels[job_id].is_set():
                    self._update(job_id, status=previous['status'], stage=previous['stage'],
                                 message='归档已停止，本机文件仍保留；可稍后重试归档。')
                    return
                self._update(job_id, status='running', stage='archiving',
                             message='远程归档暂不可用，已保留本机文件；队列暂停并稍后重试。')
                self.stopping.wait(20)

    def _prepare(self, job_id):
        request, settings, client, local_path, demo = self.payloads[job_id]
        directory = self.root / job_id
        if demo or self._prepared(job_id):
            return
        self._update(job_id, status='running', stage='preparing', message='准备下载课次…')
        if shutil.disk_usage(directory).free < 6 * 1024**3:
            raise MediaError('本机临时空间不足，请释放空间后继续')
        if self.archive:
            self.archive.restore(job_id)
        cancel = self.cancels[job_id]
        media = directory / 'media'
        media.mkdir(exist_ok=True)
        audio_path = None
        if client:
            recording = client.recording(request.course_id, request.session_id, request.video_id)
            source_url = recording.screen_url if request.signal == 'screen' else recording.main_url
            if not source_url:
                raise ValueError('所选课次没有此录像信号，请改选摄像头或屏幕')
            downloader = self.downloader_factory(client, cancel=cancel,
                progress=lambda **data: self._update(job_id, **data))
            selected = downloader.video(source_url, media / 'lecture.mp4')
            if request.audio_source == 'microphone':
                audio_path = downloader.direct(client.microphone_url(recording), media / 'microphone.aac')
            elif request.audio_source == 'camera' and request.signal != 'camera':
                if not recording.main_url:
                    raise ValueError('该课次没有摄像头音源')
                audio_path = downloader.video(recording.main_url, media / 'camera.mp4')
            lecture_date = request.lecture_date or recording.started_at[:10]
        else:
            selected = media / ('source' + local_path.suffix.lower())
            if local_path.resolve() != selected.resolve():
                shutil.copyfile(local_path, selected)
            lecture_date = request.lecture_date
        self._update(job_id, media_file=str(selected.relative_to(directory)).replace('\\', '/'))
        prepared = {'selected': str(selected.relative_to(directory)).replace('\\', '/'),
                    'audio': str(audio_path.relative_to(directory)).replace('\\', '/') if audio_path else None,
                    'lecture_date': lecture_date}
        self._update(job_id, prepared=prepared, video_deleted=False)

    def _execute(self, job_id):
        request, settings, client, local_path, demo = self.payloads[job_id]
        directory = self.root / job_id
        output = directory / 'analysis'
        output.mkdir(exist_ok=True)
        cancel = self.cancels[job_id]
        if demo:
            command = [sys.executable, '-m', 'yanhe_sentinel', 'demo', '--output', str(output)]
        else:
            prepared = self._prepared(job_id)
            if not prepared:
                raise MediaError('已下载媒体不可用，请继续任务以重新获取媒体')
            selected = directory / prepared['selected']
            audio_path = directory / prepared['audio'] if prepared.get('audio') else None
            lecture_date = prepared.get('lecture_date')
            subtitle = selected.suffix.lower() in ('.srt', '.vtt', '.txt')
            if subtitle and request.vision:
                raise ValueError('字幕文件不包含画面，请关闭画面辅助或上传视频')
            command = [sys.executable, '-m', 'yanhe_sentinel', 'analyze', '--subtitle' if subtitle else '--video',
                str(selected), '--provider', settings.event_provider, '--output', str(output)]
            if not subtitle:
                command += ['--asr-provider', settings.asr_provider]
                if settings.asr_model:
                    command += ['--asr-model', settings.asr_model]
                if request.export_clips:
                    command.append('--export-clips')
                if audio_path:
                    command += ['--audio-source', str(audio_path), '--audio-offset', str(request.audio_offset)]
            if lecture_date:
                from datetime import date
                date.fromisoformat(lecture_date)
                command += ['--lecture-date', lecture_date]
            if request.vision:
                command += ['--vision', '--vision-provider', 'openai-compatible',
                            '--vision-interval', str(request.vision_interval), '--vision-max-frames', str(request.vision_max_frames)]
            if request.force:
                command.append('--no-cache')
        secrets = [getattr(settings, key).get_secret_value() for key in ('asr_api_key', 'event_api_key', 'vision_api_key')]
        if client:
            secrets.append(client._token)
        code = self._command(job_id, command, settings.environment(), secrets)
        if cancel.is_set():
            raise DownloadCanceled()
        report_file = output / 'events.json'
        if not report_file.is_file():
            raise ValueError('未生成报告，请检查模型设置后重试')
        report = Report.model_validate_json(report_file.read_text('utf-8'))
        status = report.analysis_status
        status = 'completed' if code == 0 and status == 'completed' else 'partial' if status == 'partial' or report.events else 'failed'
        self._update(job_id, status=status, stage=status, current=0, total=0,
            message=f'发现 {len(report.events)} 个需留意的事项' if report.events else
                    '分析完成，未检出事项；请查看覆盖范围' if status == 'completed' else '分析未完整完成，请查看质量提示后继续',
            event_count=len(report.events), detection_status=report.detection_status, simulated=report.simulated)

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _command(self, job_id, command, environment, secrets):
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        lines = queue.Queue()
        def read():
            for line in process.stdout:
                lines.put(line)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            while process.poll() is None or reader.is_alive() or not lines.empty():
                if self.cancels[job_id].is_set():
                    self._terminate(process)
                    raise DownloadCanceled()
                try:
                    line = lines.get(timeout=.2).strip()
                except queue.Empty:
                    continue
                if not line:
                    continue
                for secret in secrets:
                    if secret:
                        line = line.replace(secret, '[已隐藏]')
                line = re.sub(r'https?://\S+', '[服务地址]', line)[:500]
                job = self.get(job_id)
                values = {'logs': (job['logs'] + [line])[-40:], 'message': line}
                match = re.search(r'(?:转写分块|复用转写分块)\s*(\d+)/(\d+)', line)
                if match:
                    values.update(stage='transcribing', current=int(match[1]), total=int(match[2]))
                elif '分析字幕' in line:
                    values.update(stage='analyzing', current=0, total=0)
                elif '画面辅助' in line:
                    values.update(stage='vision', current=0, total=0)
                self._update(job_id, **values)
            return process.wait()
        finally:
            self._terminate(process)
            process.stdout.close()

    def close(self):
        self.stopping.set()
        self.repair_stop.set()
        for cancel in self.cancels.values():
            cancel.set()
        for worker in self.workers:
            worker.join(timeout=15)
