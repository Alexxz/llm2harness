"""Prepare the pinned public CPU training environment; never train an adapter."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    python = ROOT / 'work/training-venv/bin/python'
    if not python.is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(python.parent.parent)], check=True, timeout=120)
    check = subprocess.run([str(python), '-I', '-c', "from importlib.metadata import version; assert all(version(n)==v for n,v in [('torch','2.6.0+cpu'),('transformers','4.51.3'),('peft','0.15.2'),('accelerate','1.6.0'),('safetensors','0.5.3'),('jsonschema','4.23.0')])"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if check.returncode:
        subprocess.run([str(python), '-m', 'pip', 'install', 'torch==2.6.0', '--index-url', 'https://download.pytorch.org/whl/cpu'], check=True, timeout=1800)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-training.txt')], check=True, timeout=1800)
    profile = json.loads((ROOT / 'resources/models.json').read_text())['training']
    cache = ROOT / profile['cache_directory']
    env = os.environ.copy()
    env.update(HF_HOME=str(ROOT / 'work/hf-home'), HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_DISABLE_XET='1')
    script = """from huggingface_hub import snapshot_download
import sys
kwargs = dict(revision=sys.argv[2], cache_dir=sys.argv[3], token=False, allow_patterns=['*.json','*.safetensors','merges.txt'])
try:
    snapshot_download(sys.argv[1], local_files_only=True, **kwargs)
except Exception:
    snapshot_download(sys.argv[1], **kwargs)
"""
    subprocess.run([str(python), '-I', '-c', script, profile['upstream'], profile['revision'], str(cache)], env=env, check=True, timeout=1800)
    weights = cache / 'models--Qwen--Qwen2.5-0.5B-Instruct/snapshots' / profile['revision'] / 'model.safetensors'
    with weights.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if weights.stat().st_size != profile['bytes'] or digest != profile['sha256']:
        raise ValueError('Веса обучения не соответствуют закреплённому профилю; файлы сохранены.')


if __name__ == '__main__':
    main()
