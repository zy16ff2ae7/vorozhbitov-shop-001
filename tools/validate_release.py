#!/usr/bin/env python3
"""Validate catalog and media files before a release is published."""
from __future__ import annotations
import argparse, json, shutil, subprocess
from pathlib import Path

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--catalog', type=Path, default=None)
    ns = ap.parse_args()
    root = ns.root.resolve()
    catalog_path = (ns.catalog or (root / 'catalog.json')).resolve()
    errors = []
    try:
        loaded = json.loads(catalog_path.read_text(encoding='utf-8'))
        catalog = loaded if isinstance(loaded, dict) else {}
        if not isinstance(loaded, dict):
            errors.append('catalog root must be a JSON object')
    except Exception as exc:
        errors.append(f'catalog unreadable: {exc}')
        catalog = {}
    if not isinstance(catalog.get('categories'), list) or not isinstance(catalog.get('products'), list):
        errors.append('catalog must contain categories and products arrays')
    media = catalog.get('media', {}) if isinstance(catalog, dict) else {}
    if not isinstance(media, dict):
        errors.append('catalog media must be an object')
        media = {}
    checked = []
    for key, value in media.items():
        if key.endswith(('_title', '_caption')) or not isinstance(value, str) or not value or '://' in value:
            continue
        raw_path = value.split('?', 1)[0]
        if Path(raw_path).is_absolute():
            errors.append(f"media.{key} absolute path is not allowed")
            continue
        relative = raw_path.lstrip('/')
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            errors.append(f'media.{key} path escapes release root')
            continue
        path = candidate if candidate.exists() else (root / 'miniapp' / relative).resolve()
        if not path.is_relative_to(root):
            errors.append(f'media.{key} path escapes release root')
            continue
        if not path.is_file():
            errors.append(f'media.{key} missing: {path}')
            continue
        item = {'key': key, 'path': str(path.relative_to(root)), 'bytes': path.stat().st_size}
        if path.suffix.lower() in {'.mp4', '.mov', '.m4v', '.webm'}:
            ffprobe = shutil.which('ffprobe')
            if ffprobe:
                proc = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', str(path)], text=True, capture_output=True)
                if proc.returncode:
                    errors.append(f'media.{key} ffprobe failed: {proc.stderr.strip()}')
                else:
                    try: item['duration_s'] = round(float(proc.stdout.strip()), 3)
                    except ValueError: errors.append(f'media.{key} invalid duration')
        checked.append(item)
    result = {'ok': not errors, 'catalog': str(catalog_path), 'media': checked, 'errors': errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1

if __name__ == '__main__':
    raise SystemExit(main())
