"""Dedicated loopback OpenAI-compatible Qwen3-4B inference service."""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parent
MODEL = 'Qwen3-4B'
LOCK = threading.Lock()
tokenizer = model = None
service_key = ''


@asynccontextmanager
async def lifespan(app):
    global tokenizer, model, service_key
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    if not os.environ.get('CUDA_VISIBLE_DEVICES', '').isdigit():
        raise RuntimeError('Select one physical GPU')
    service_key = (ROOT / 'service.key').read_text().strip()
    if len(service_key) < 32:
        raise RuntimeError('Missing service credential')
    torch.set_num_threads(4)
    folder = ROOT / 'models' / MODEL
    tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(folder, torch_dtype=torch.bfloat16,
        device_map='cuda:0', attn_implementation='sdpa', local_files_only=True).eval()
    print('READY: ' + MODEL, flush=True)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


class Completion(BaseModel):
    model: str
    messages: list[dict[str, str]] = Field(min_length=1, max_length=20)
    temperature: float = 0
    response_format: dict = Field(default_factory=dict)


@app.get('/health')
def health():
    return {'ready': model is not None, 'model': MODEL,
        'physical_gpu': os.environ.get('CUDA_VISIBLE_DEVICES')}


def complete(request):
    import torch
    with LOCK, torch.inference_mode():
        prompt = tokenizer.apply_chat_template(request.messages, tokenize=False,
            add_generation_prompt=True, enable_thinking=False)
        inputs = tokenizer(prompt, return_tensors='pt').to(model.device)
        if inputs.input_ids.shape[1] > 16000:
            raise ValueError('Input is too long')
        started = time.monotonic()
        output = model.generate(**inputs, max_new_tokens=4096, do_sample=False,
            pad_token_id=tokenizer.eos_token_id)
        generated = output[0, inputs.input_ids.shape[1]:]
        content = tokenizer.decode(generated, skip_special_tokens=True).strip()
        # Accept a complete fenced JSON answer, but never repair invented fields.
        if content.startswith('```json') and content.endswith('```'):
            content = content[7:-3].strip()
        json.loads(content)
        reason = 'length' if len(generated) >= 4096 else 'stop'
        print(json.dumps({'input_tokens': inputs.input_ids.shape[1],
            'output_tokens': len(generated), 'seconds': round(time.monotonic()-started, 2)}), flush=True)
        return {'model': MODEL, 'choices': [{'index': 0, 'finish_reason': reason,
            'message': {'role': 'assistant', 'content': content}}]}


@app.post('/v1/chat/completions')
async def completions(request: Completion, authorization: str | None = Header(None)):
    if not secrets.compare_digest(authorization or '', 'Bearer ' + service_key):
        raise HTTPException(401, 'Invalid service credential')
    if request.model != MODEL:
        raise HTTPException(400, 'Use the deployed model')
    try:
        return await run_in_threadpool(complete, request)
    except (ValueError, RuntimeError):
        raise HTTPException(422, 'Model output invalid or inference failed') from None
