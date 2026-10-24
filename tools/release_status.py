#!/usr/bin/env python3
"""Print a compact release status for operators and deployment checks."""
from __future__ import annotations
import argparse, json, subprocess, urllib.request
from pathlib import Path

def cmd(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]); ap.add_argument('--url'); ns=ap.parse_args()
    root=ns.root.resolve(); result={}
    try: result['commit']=cmd('git','-C',str(root),'rev-parse','HEAD')
    except Exception: result['commit']='unknown'
    catalog=root/'catalog.json'
    try:
        data=json.loads(catalog.read_text(encoding='utf-8')); media=data.get('media',{}) if isinstance(data,dict) else {}
        result['catalog']='ok' if isinstance(data,dict) and isinstance(data.get('products'),list) else 'invalid'
        result['teaser']=media.get('teaser','')
    except Exception as exc: result['catalog']=f'error: {exc}'
    if ns.url:
        try:
            req=urllib.request.Request(ns.url.rstrip('/')+'/health')
            with urllib.request.urlopen(req,timeout=10) as response: result['health']=response.status
        except Exception as exc: result['health']=f'error: {exc}'
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('catalog')=='ok' and (not ns.url or result.get('health')==200) else 1
if __name__=='__main__': raise SystemExit(main())
