#!/usr/bin/env python3
"""Small production smoke test for the public storefront."""
from __future__ import annotations
import argparse, json, sys, urllib.request

def get(url: str, headers=None):
    req=urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.headers, r.read()

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('base_url'); ns=ap.parse_args()
    base=ns.base_url.rstrip('/'); checks=[]
    status,_,body=get(base+'/health'); checks.append(('health', status==200 and json.loads(body).get('status')=='ok'))
    status,h,body=get(base+'/api/catalog'); data=json.loads(body); checks.append(('catalog', status==200 and isinstance(data.get('products'),list)))
    media=data.get('media',{}); teaser=media.get('teaser','')
    if teaser:
        status,h,body=get(base+'/'+teaser.lstrip('/'), {'Range':'bytes=0-1'}); checks.append(('video-range', status==206 and h.get('Accept-Ranges')=='bytes' and len(body)==2))
    for name,ok in checks: print(f'{name}: {"OK" if ok else "FAIL"}')
    return 0 if all(ok for _,ok in checks) else 1
if __name__=='__main__': raise SystemExit(main())
