"""Loopback-only Qwen ASR endpoint for the workstation's SSH tunnel.

Install qwen-asr==0.0.6, torch, fastapi, uvicorn and python-multipart remotely.
CUDA_VISIBLE_DEVICES selects one physical server GPU; no workstation model is loaded.
"""
from contextlib import asynccontextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parent
MODEL = 'Qwen3-ASR-1.7B+ForcedAligner-0.6B'
LOCK = threading.Lock()
recognizer = None
service_key = ''


def timed_segments(words, duration):
    """Group original aligner spans; never synthesize timing for missing words."""
    segments, group = [], []
    def flush(final=False):
        if not group:
            return
        start, end = min(w['start'] for w in group), max(w['end'] for w in group)
        if end <= start:
            # CJK alignment can give individual characters zero-length spans.
            # Retain their original points and enclose with neighboring speech.
            if not final or not segments:
                return
            previous = segments.pop()
            group.insert(0, {'word': previous['text'], 'start': previous['start'], 'end': previous['end']})
            start, end = min(w['start'] for w in group), max(w['end'] for w in group)
        text = ''
        for word in group:
            value = word['word']
            if text and text[-1].isascii() and text[-1].isalnum() and value[0].isascii() and value[0].isalnum():
                text += ' '
            text += value
        segments.append({'id': len(segments), 'start': start, 'end': end, 'text': text})
        group.clear()
    for word in words:
        start, end = word['start'], word['end']
        if not word['word'] or not all(math.isfinite(t) for t in (start, end)) or not 0 <= start <= end <= duration + .1:
            raise ValueError('Invalid forced alignment time span')
        if group and (end - group[0]['start'] > 7 or start - group[-1]['end'] > .8 or sum(len(w['word']) for w in group) >= 60):
            flush()
        group.append(word)
    flush(final=True)
    if group:
        raise ValueError('Aligned speech has no positive time span')
    return segments


@asynccontextmanager
async def lifespan(app):
    global recognizer, service_key
    if not os.environ.get('CUDA_VISIBLE_DEVICES', '').isdigit():
        raise RuntimeError('Select exactly one physical GPU with CUDA_VISIBLE_DEVICES')
    service_key = (ROOT / 'service.key').read_text().strip()
    if len(service_key) < 32:
        raise RuntimeError('Missing service credential')
    import torch
    from qwen_asr import Qwen3ASRModel
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable')
    torch.set_num_threads(4)
    recognizer = Qwen3ASRModel.from_pretrained(
        str(ROOT / 'models/Qwen3-ASR-1.7B'), dtype=torch.bfloat16,
        device_map='cuda:0', attn_implementation='sdpa', max_inference_batch_size=1,
        max_new_tokens=4096, forced_aligner=str(ROOT / 'models/Qwen3-ForcedAligner-0.6B'),
        forced_aligner_kwargs={'dtype': torch.bfloat16, 'device_map': 'cuda:0', 'attn_implementation': 'sdpa'})
    print('READY: Qwen3 ASR + ForcedAligner; physical GPU ' + os.environ['CUDA_VISIBLE_DEVICES'], flush=True)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get('/health')
def health():
    return {'ready': recognizer is not None, 'model': MODEL, 'physical_gpu': os.environ.get('CUDA_VISIBLE_DEVICES')}


def infer(raw, language):
    import numpy as np
    import soundfile as sf
    samples, rate = sf.read(io.BytesIO(raw), dtype='float32')
    if rate != 16000 or samples.ndim != 1 or not np.isfinite(samples).all():
        raise ValueError('Expected finite mono 16kHz audio')
    duration = len(samples) / rate
    if not 0 < duration <= 330:
        raise ValueError('Expected at most 330 seconds of audio')
    identity = hashlib.sha256(raw + MODEL.encode() + language.encode()).hexdigest()
    cache = ROOT / 'results' / (identity + '.json')
    with LOCK:
        if cache.exists():
            return json.loads(cache.read_text())
        started = time.monotonic()
        words, texts = [], []
        # Small overlapping windows keep memory and latency predictable.
        for begin in range(0, math.ceil(duration), 60):
            end = min(duration, begin + 60)
            read_begin, read_end = max(0, begin - 1), min(duration, end + 1)
            wave = samples[round(read_begin * rate):round(read_end * rate)]
            if np.max(np.abs(wave), initial=0) <= .0001:
                continue
            output = recognizer.transcribe(audio=(wave, rate), language=language,
                return_time_stamps=True)[0]
            texts.append(output.text)
            if output.text.strip() and not output.time_stamps:
                raise ValueError('Speech returned without forced alignment')
            for item in output.time_stamps or []:
                start, stop = read_begin + float(item.start_time), read_begin + float(item.end_time)
                if begin <= (start + stop) / 2 < end:
                    words.append({'word': item.text, 'start': start, 'end': stop})
        segments = timed_segments(words, duration)
        if any(t.strip() for t in texts) and not segments:
            raise ValueError('No valid aligned speech segments')
        result = {'task': 'transcribe', 'language': 'zh' if language == 'Chinese' else language,
            'duration': duration, 'text': '\n'.join(s['text'] for s in segments),
            'segments': segments, 'words': words, 'model': MODEL,
            'timestamp_source': 'Qwen3-ForcedAligner-0.6B', 'physical_gpu': os.environ.get('CUDA_VISIBLE_DEVICES')}
        cache.parent.mkdir(exist_ok=True)
        temporary = cache.with_suffix('.pending')
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        temporary.replace(cache)
        print(json.dumps({'audio_seconds': round(duration, 3), 'segments': len(segments),
            'processing_seconds': round(time.monotonic() - started, 2)}), flush=True)
        return result


@app.post('/v1/audio/transcriptions')
async def transcriptions(file: UploadFile = File(...), model: str = Form(...),
        response_format: str = Form('verbose_json'), language: str = Form('zh'),
        authorization: str | None = Header(None)):
    if not secrets.compare_digest(authorization or '', 'Bearer ' + service_key):
        raise HTTPException(401, 'Invalid service credential')
    if model != MODEL or response_format != 'verbose_json':
        raise HTTPException(400, 'Use the deployed model and verbose_json')
    if language not in ('zh', 'Chinese', 'auto'):
        raise HTTPException(400, 'This deployment is configured for Chinese classroom audio')
    raw = await file.read(24 * 1024 * 1024 + 1)
    if len(raw) > 24 * 1024 * 1024:
        raise HTTPException(413, 'Audio chunk is too large')
    try:
        return await run_in_threadpool(infer, raw, 'Chinese')
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'detail': str(exc)[:400]}), flush=True)
        # Do not return source audio, text, credentials or library exception bodies.
        raise HTTPException(422, 'Transcription/alignment failed; check the service status') from None
