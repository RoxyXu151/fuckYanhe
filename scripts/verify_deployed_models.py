"""Verify deployed model files against the official download manifests."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--models', nargs='+', default=['Qwen3-ASR-1.7B', 'Qwen3-ForcedAligner-0.6B'])
args = parser.parse_args()
root = Path(__file__).resolve().parent / 'models'
for model in args.models:
    folder = root / model
    folder.resolve().relative_to(root.resolve())
    manifest = json.loads((folder/'official-files.json').read_text('utf-8'))
    total = 0
    for item in manifest:
        if item['Type'] != 'blob' or not item['Path'].endswith(('.json', '.txt', '.model', '.safetensors')):
            continue
        path = folder/item['Path']
        path.resolve().relative_to(folder.resolve())
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for piece in iter(lambda: source.read(8*1024*1024), b''):
                digest.update(piece)
        if path.stat().st_size != item['Size'] or digest.hexdigest() != item['Sha256']:
            raise RuntimeError('Model file failed the official size/SHA256 check')
        total += item['Size']
    print(json.dumps({'verified': model, 'bytes': total}), flush=True)
