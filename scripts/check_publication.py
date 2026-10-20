"""Check tracked publication content without printing any matched secret."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

FORBIDDEN_DIRS = {'runs', 'input', 'models', '.venv', '__pycache__', '.cache', '.pytest_cache'}
FORBIDDEN_SUFFIXES = {'.key', '.pem', '.pfx', '.log', '.zip', '.pyc', '.safetensors', '.gguf',
    '.pt', '.pth', '.mp4', '.mkv', '.mov', '.mp3', '.wav', '.aac', '.m4a', '.flac', '.cookies'}
PATTERNS = {
    'credential': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9_-]{24,})'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'personal absolute path': re.compile(r'(?:[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+/)'),
    'private network address': re.compile(r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
    'JWT': re.compile(r'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'),
}


def inspect(root, files):
    issues = []
    for relative in files:
        path = root/relative
        if not path.is_file():
            continue
        if path.is_symlink():
            issues.append((relative, 0, 'symlink is not allowed in publication'))
            continue
        if any(p in FORBIDDEN_DIRS for p in Path(relative).parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            issues.append((relative, 0, 'runtime, credentials, media or model artifact'))
            continue
        if path.name.startswith('.env') and path.name != '.env.example':
            issues.append((relative, 0, 'private environment file'))
            continue
        if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.ico'}:
            continue  # Images need separate human review; use synthetic screenshots only.
        try:
            content = path.read_text('utf-8')
        except (UnicodeError, OSError):
            issues.append((relative, 0, 'unreviewed binary or unreadable file'))
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for category, pattern in PATTERNS.items():
                if pattern.search(line):
                    issues.append((relative, number, category))
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True)
    if result.returncode == 0:
        files = [p for p in result.stdout.decode('utf-8').split('\0') if p]
        if not files:
            print('No tracked files to inspect. Stage the intended public source first.')
            return 1
    else:
        files = [str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '.git' not in p.parts]
    issues = inspect(root, files)
    for path, line, kind in issues:
        print(f'{path}:{line}: {kind}')
    print(f'Checked {len(files)} files; {len(issues)} issues. Matched values are never printed.')
    return 1 if issues else 0


if __name__ == '__main__':
    sys.exit(main())
