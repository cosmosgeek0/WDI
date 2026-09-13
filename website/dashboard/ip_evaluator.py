"""Publish an independent minute-scale IP evidence index; no network clients."""
import argparse
import fcntl
import json
import os
import time
from pathlib import Path
from dashboard.fleet_test import FleetTestView
from dashboard.ip_evidence import EvidenceIndex
from dashboard.ip_index import INDEX_PATH,Publisher
from dashboard.server import history_snapshot,cost_snapshot,running_cost


def run(evidence,publisher,once=False):
    next_evaluation=0
    while True:
        start=time.monotonic();full=start>=next_evaluation
        if full:next_evaluation=start+60
        try:
            if full:
                publisher.refresh_rates(evidence)
                evidence.refresh()
                if evidence.error:raise RuntimeError(evidence.error)
                result=publisher.publish(evidence)
                print(json.dumps(dict(generated_ms=result['generated_ms'],ips=result['all_ips'],summary=result['summary'],metric_errors=result['metric_errors'],elapsed_seconds=round(time.monotonic()-start,3))),flush=True)
            # Refresh after expensive work too: native recording continues
            # while the minute evaluation reads its retained history snapshot.
            publisher.refresh_rates(evidence)
            publisher.refresh_observations(evidence)
        except Exception as exc:
            print(json.dumps(dict(error=type(exc).__name__,observed_ms=int(time.time()*1000))),flush=True)
            if once:raise
        if once:break
        time.sleep(max(1,min(next_evaluation-time.monotonic(),15-(time.monotonic()-start))))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path('/home/salad/.hg1'))
    parser.add_argument('--index',type=Path,default=INDEX_PATH)
    parser.add_argument('--once',action='store_true');args=parser.parse_args()
    os.umask(0o027);args.index.parent.mkdir(parents=True,exist_ok=True)
    lock=args.index.with_suffix('.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    root=args.data_dir;fleet=FleetTestView(root/'fleet-tests');caches={}
    def history(service,selected):
        return history_snapshot(root,dict(service=[service],test=[selected],hours=['all'],summary=['1']),fleet,caches.setdefault(service,{}),lambda path:json.loads(path.read_text()))
    evidence=EvidenceIndex(root,history,lambda s,a,b:cost_snapshot(root,s,a,b),running_cost,
        honeygain=lambda selected:fleet.snapshot(archive=None if selected=='current' else selected,include_observations=True,honeygain_only=True))
    publisher=Publisher(root,args.index)
    run(evidence,publisher,args.once)

if __name__=='__main__':main()
