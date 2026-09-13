#!/usr/bin/env python3
"""Upload only the validated anonymous document to its fixed public route."""
import argparse
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from validate_flow import MAX_BYTES, validate
from export_flow import atomic_write

ENDPOINT = 'https://cosmosgeek.org/api/public-flow.json'


class NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        if fp: fp.close()
        raise ValueError('redirect_blocked')
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def unique(pairs):
    out = {}
    for k,v in pairs:
        if k in out: raise ValueError('duplicate_key')
        out[k] = v
    return out


def reject_constant(value):
    raise ValueError('nonfinite')


def read_payload(path, now):
    with Path(path).open('rb') as f: raw = f.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES: raise ValueError('payload_too_large')
    v=json.loads(raw,object_pairs_hook=unique,parse_constant=reject_constant)
    if not validate(v,now) or now-v['generated_ms']>300000: raise ValueError('invalid_payload')
    return v,json.dumps(v,separators=(',',':'),allow_nan=False).encode()


def publish_once(path,token_path,state):
    now=int(time.time()*1000)
    v,body=read_payload(path,now)
    with Path(token_path).open('rb') as f: raw=f.read(1025)
    token=raw.decode('ascii').strip('\r\n')
    if len(raw)>1024 or not 32<=len(token)<=1000 or any(not 33<=ord(c)<=126 for c in token): raise ValueError('invalid_token')
    request=Request(ENDPOINT,data=body,method='POST',headers={'Content-Type':'application/json','Authorization':'Bearer '+token,'User-Agent':'CosmosGeek-Public-Flow/1.0'})
    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=10) as response:
        if response.getcode()!=200: raise ValueError('http_rejected')
    # This is the exact in-memory snapshot sent, independent of later atomic exports.
    atomic_write(Path(state)/'last-published.json',v)
    return v['generated_ms']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True);p.add_argument('--token-file',required=True);p.add_argument('--state',required=True);p.add_argument('--once',action='store_true')
    a=p.parse_args();last=None;last_attempt=0
    while True:
        try:
            st=Path(a.input).stat();sig=(st.st_ino,st.st_size,st.st_mtime_ns)
            if sig!=last and time.monotonic()-last_attempt>=10:
                last_attempt=time.monotonic()
                generated=publish_once(a.input,a.token_file,a.state)
                last=sig;print(json.dumps({'published_ms':generated}),flush=True)
        except (ValueError,OSError) as e:
            if isinstance(e,HTTPError): e.close()
            print('{"publish":"unavailable"}',flush=True)
            if a.once:return 1
            time.sleep(20)
        if a.once:return 0
        time.sleep(2)


if __name__=='__main__':raise SystemExit(main())
