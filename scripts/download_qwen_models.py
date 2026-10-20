"""Download official Qwen model weights into this deployment directory."""
import json
import argparse
import os
from pathlib import Path

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--directory', type=Path, default=root / 'models')
parser.add_argument('--with-events', action='store_true')
args = parser.parse_args()
directory = args.directory
os.environ.setdefault('MODELSCOPE_DOWNLOAD_PARALLELS', '8')
os.environ.setdefault('MODELSCOPE_PARALLEL_DOWNLOAD_THRESHOLD_MB', '64')
from modelscope import snapshot_download
names = ['Qwen3-ASR-1.7B', 'Qwen3-ForcedAligner-0.6B']
if args.with_events:
    names.append('Qwen3-4B')
for name in names:
    target = directory / name
    path = snapshot_download('Qwen/' + name, local_dir=str(target))
    print(json.dumps({'model': name, 'directory': str(path)}), flush=True)
