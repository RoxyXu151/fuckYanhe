"""Fetch official ModelScope weights with small resumable ranges and SHA256 checks."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import threading
import time

import requests

STEP = 1024 * 1024
LOCAL = threading.local()


def session():
    if not hasattr(LOCAL, 'session'):
        LOCAL.session = requests.Session()
        LOCAL.session.trust_env = False
    return LOCAL.session


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for part in iter(lambda: source.read(8 * 1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def get(url, **kwargs):
    for attempt in range(12):
        try:
            response = session().get(url, timeout=(15, 30), **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == 11:
                raise RuntimeError('Public model download failed after 12 attempts') from None
            time.sleep(min(10, 1 + attempt))


def model(name, root):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / 'official-files.json'
    if manifest.is_file():
        files = json.loads(manifest.read_text('utf-8'))
    else:
        files = get(f'https://modelscope.cn/api/v1/models/Qwen/{name}/repo/files',
            params={'Recursive': 'true', 'Revision': 'master'}).json()['Data']['Files']
        manifest.write_text(json.dumps(files), encoding='utf-8')
    for meta in files:
        filename = meta['Path']
        if meta['Type'] != 'blob' or not filename.endswith(('.json', '.txt', '.safetensors', '.model')):
            continue
        target = folder / filename
        target.resolve().relative_to(folder.resolve())
        size, checksum = meta['Size'], meta['Sha256']
        if target.is_file() and target.stat().st_size == size and sha(target) == checksum:
            print(f'VERIFIED {name}/{filename}', flush=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        parts = folder / '.ranges-1m' / filename
        parts.mkdir(parents=True, exist_ok=True)
        # Preserve complete ranges already fetched by ModelScope's large-range client.
        for old in (folder, folder / '._____temp'):
            if not old.is_dir():
                continue
            for candidate in old.glob(filename + '*'):
                match = re.fullmatch(re.escape(filename) + r'_(\d+)_(\d+)', candidate.name)
                begin = int(match[1]) if match else 0
                if candidate.name != filename and not match:
                    continue
                if begin % STEP:
                    continue
                with candidate.open('rb') as source:
                    position = begin
                    while piece := source.read(STEP):
                        expected = min(STEP, size-position)
                        if expected <= 0 or len(piece) != expected:
                            break
                        saved = parts / str(position)
                        if not saved.exists():
                            saved.write_bytes(piece)
                        position += len(piece)
        previous_ranges = folder / '.ranges' / filename
        if previous_ranges.is_dir():
            for candidate in previous_ranges.iterdir():
                if not candidate.name.isdigit():
                    continue
                begin = int(candidate.name)
                with candidate.open('rb') as source:
                    position = begin
                    while piece := source.read(STEP):
                        if len(piece) != min(STEP, size-position):
                            break
                        saved = parts / str(position)
                        if not saved.exists():
                            saved.write_bytes(piece)
                        position += len(piece)
        url = f'https://modelscope.cn/api/v1/models/Qwen/{name}/repo'
        def fetch(begin):
            end = min(size, begin+STEP)-1
            saved = parts / str(begin)
            if saved.is_file() and saved.stat().st_size == end-begin+1:
                return
            for attempt in range(4):
                response = get(url, params={'Revision': 'master', 'FilePath': filename},
                    headers={'Range': f'bytes={begin}-{end}'})
                data = response.content
                if response.status_code == 206 and response.headers.get('Content-Range', '').startswith(f'bytes {begin}-{end}/') and len(data) == end-begin+1:
                    temporary = saved.with_suffix('.pending')
                    temporary.write_bytes(data)
                    temporary.replace(saved)
                    return
                if size <= STEP and response.status_code == 200 and len(data) == size:
                    saved.write_bytes(data)
                    return
            raise RuntimeError('Official server returned an invalid byte range')
        begins = list(range(0, size, STEP))
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(fetch, begin) for begin in begins]
            for count, future in enumerate(as_completed(futures), 1):
                future.result()
                if count % 128 == 0 or count == len(begins):
                    print(f'RANGES {name}/{filename}: {count}/{len(begins)}', flush=True)
        temporary = target.with_suffix(target.suffix + '.assembled')
        with temporary.open('wb') as output:
            for begin in begins:
                with (parts / str(begin)).open('rb') as source:
                    output.write(source.read())
        if temporary.stat().st_size != size or sha(temporary) != checksum:
            raise RuntimeError('Official SHA256 did not match; model was not accepted')
        temporary.replace(target)
        print(f'VERIFIED {name}/{filename}', flush=True)
    print('MODEL COMPLETE: ' + name, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--models', nargs='+', default=['Qwen3-ASR-1.7B', 'Qwen3-ForcedAligner-0.6B'])
    args = parser.parse_args()
    for name in args.models:
        if name not in ('Qwen3-ASR-1.7B', 'Qwen3-ForcedAligner-0.6B', 'Qwen3-4B'):
            raise ValueError('Choose an official deployment model')
        model(name, args.directory)
