#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
mkdir -p logs results
gpu="${YANHE_GPU:-0}"
if [[ ! "$gpu" =~ ^[0-9]+$ ]]; then echo 'Choose one GPU index'; exit 1; fi
if [[ ! -f service.key ]]; then
  .venv/bin/python -c 'import secrets; from pathlib import Path; Path("service.key").write_text(secrets.token_urlsafe(48))'
fi
chmod 600 service.key
for kind in ${YANHE_SERVICES:-asr}; do
  if [[ "$kind" != asr && "$kind" != event ]]; then echo 'Unknown service'; exit 1; fi
  if [[ -f "$kind.pid" ]] && kill -0 "$(cat "$kind.pid")" 2>/dev/null; then
    echo "$kind is already running"; continue
  fi
  port=18765
  if [[ "$kind" == event ]]; then port=18766; fi
  nohup env CUDA_VISIBLE_DEVICES="$gpu" OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    .venv/bin/python -u -m uvicorn "qwen_${kind}_service:app" --host 127.0.0.1 --port "$port" \
    >"logs/$kind.log" 2>&1 </dev/null &
  echo "$!" >"$kind.pid"
  echo "$kind launched on loopback port $port, GPU $gpu"
done
