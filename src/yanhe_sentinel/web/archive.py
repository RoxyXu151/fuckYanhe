"""Opt-in remote archive over an authenticated loopback SSH tunnel."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from ..cloud_asr import safe_base_url
from .jobs import atomic_json


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def archive_path(root, job_id, relative):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('Invalid archive identifier')
    if not relative or '\\' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Invalid archive path')
    directory = (Path(root) / job_id).resolve()
    path = (directory / relative).resolve()
    if not directory.is_relative_to(Path(root).resolve()) or not path.is_relative_to(directory):
        raise ValueError('Invalid archive path')
    return path


def create_archive_app(root, key, *, reserve_bytes=32 * 1024**3):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if len(key) < 32:
        raise ValueError('A dedicated archive credential is required')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def guard(request, call_next):
        from fastapi.responses import JSONResponse
        supplied = request.headers.get('authorization', '')
        if not secrets.compare_digest(supplied, 'Bearer ' + key):
            return JSONResponse({'error': 'Authentication required'}, status_code=401)
        return await call_next(request)

    def locate(job_id, relative):
        try:
            return archive_path(root, job_id, relative)
        except ValueError:
            raise HTTPException(400, 'Invalid archive path') from None

    @app.get('/health')
    def health():
        return {'ready': True, 'free_bytes': shutil.disk_usage(root).free}

    @app.get('/metadata/{job_id}/{relative:path}')
    def metadata(job_id: str, relative: str):
        path = locate(job_id, relative)
        if not path.is_file():
            raise HTTPException(404)
        return {'size': path.stat().st_size, 'sha256': file_digest(path)}

    @app.put('/files/{job_id}/{relative:path}')
    async def upload(job_id: str, relative: str, request: Request):
        path = locate(job_id, relative)
        expected = request.headers.get('x-content-sha256', '')
        if not re.fullmatch('[a-f0-9]{64}', expected):
            raise HTTPException(400, 'Missing content checksum')
        try:
            size = int(request.headers.get('content-length', '-1'))
        except ValueError:
            raise HTTPException(400) from None
        if not 0 <= size <= 8 * 1024**3:
            raise HTTPException(413)
        if shutil.disk_usage(root).free < reserve_bytes + size:
            raise HTTPException(507, 'Archive reserve would be exceeded')
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.pending')
        digest, written = hashlib.sha256(), 0
        try:
            with pending.open('wb') as output:
                async for block in request.stream():
                    written += len(block)
                    if written > size:
                        raise HTTPException(413)
                    output.write(block)
                    digest.update(block)
            if written != size or digest.hexdigest() != expected:
                raise HTTPException(422, 'Content verification failed')
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
        return {'size': written, 'sha256': digest.hexdigest()}

    @app.get('/files/{job_id}/{relative:path}')
    def download(job_id: str, relative: str):
        path = locate(job_id, relative)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    return app


class RemoteArchive:
    def __init__(self, root, base_url, key, *, transport=None):
        self.root, self.base, self.key, self.transport = Path(root).resolve(), safe_base_url(base_url), key, transport

    @classmethod
    def from_environment(cls, root):
        base = os.environ.get('YANHE_ARCHIVE_BASE_URL', '')
        if not base:
            return None
        key = os.environ.get('YANHE_ARCHIVE_API_KEY', '')
        if len(key) < 32:
            raise ValueError('Remote archive credential is missing')
        return cls(root, base, key)

    def client(self):
        return httpx.Client(base_url=self.base + '/', headers={'Authorization': 'Bearer ' + self.key},
                            timeout=httpx.Timeout(120, connect=10), follow_redirects=False,
                            trust_env=False, transport=self.transport)

    def index(self, job_id):
        path = archive_path(self.root, job_id, 'archive-index.json')
        return json.loads(path.read_text('utf8')).get('files', {}) if path.exists() else {}

    def available(self, job_id, relative):
        return relative in self.index(job_id)

    def sync(self, job_id, *, evict=True):
        directory = archive_path(self.root, job_id, 'job.json').parent
        index, verified = self.index(job_id), []
        with self.client() as client:
            for path in sorted(directory.rglob('*')):
                if not path.is_file() or path.is_symlink() or path.name == 'archive-index.json' or path.suffix in ('.pending', '.part'):
                    continue
                relative = path.relative_to(directory).as_posix()
                expected = {'size': path.stat().st_size, 'sha256': file_digest(path)}
                remote = client.get(f'metadata/{job_id}/{relative}')
                if remote.status_code != 200 or remote.json() != expected:
                    with path.open('rb') as source:
                        response = client.put(f'files/{job_id}/{relative}', content=source,
                            headers={'X-Content-SHA256': expected['sha256'], 'Content-Length': str(expected['size'])})
                    response.raise_for_status()
                    if response.json() != expected:
                        raise ValueError('Archive checksum mismatch')
                index[relative] = expected
                verified.append((path, expected))
        atomic_json(directory / 'archive-index.json', {'files': index, 'archived_at': time.time()})
        if evict:
            for path, expected in verified:
                # Reports, transcripts, review state and small inference caches remain local.
                large = path.suffix.lower() in ('.mp4', '.mkv', '.mov', '.webm', '.mp3', '.wav', '.aac', '.m4a', '.flac', '.ogg', '.ts', '.f32')
                if large and path.stat().st_size == expected['size'] and file_digest(path) == expected['sha256']:
                    path.unlink()
        return {'files': len(index), 'bytes': sum(item['size'] for item in index.values())}

    def restore(self, job_id):
        with self.client() as client:
            for relative, expected in self.index(job_id).items():
                path = archive_path(self.root, job_id, relative)
                if path.exists():
                    continue
                if shutil.disk_usage(self.root).free < expected['size'] + 4 * 1024**3:
                    raise ValueError('本机临时空间不足，请释放空间后继续')
                path.parent.mkdir(parents=True, exist_ok=True)
                pending = path.with_suffix(path.suffix + '.pending')
                try:
                    with client.stream('GET', f'files/{job_id}/{relative}') as response:
                        response.raise_for_status()
                        with pending.open('wb') as output:
                            for block in response.iter_bytes(1024 * 1024):
                                output.write(block)
                    if pending.stat().st_size != expected['size'] or file_digest(pending) != expected['sha256']:
                        raise ValueError('Archive download checksum mismatch')
                    pending.replace(path)
                finally:
                    pending.unlink(missing_ok=True)
