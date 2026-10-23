"""Download all stats the build needs into data/cache (resumable: cached calls are skipped).

usage: python collect.py [--since UNIX] [--workers N] [--heroes 1,2,3]
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import queries

ROOT = os.path.join(os.path.dirname(__file__), "..")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, help="min match start time (default: last big patch)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--heroes", help="comma-separated hero ids (default: all active)")
    a = ap.parse_args()

    since = a.since or queries.last_big_patch()
    heroes = queries.active_heroes()
    ids = [int(x) for x in a.heroes.split(",")] if a.heroes else sorted(h["id"] for h in heroes)
    queries.assets_items()
    run = {"since": since, "hero_ids": ids, "started": int(time.time())}
    with open(os.path.join(ROOT, "data", "run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f)

    q = queries.Q(since)
    calls = q.all_calls(ids)
    print(f"since {time.strftime('%Y-%m-%d', time.gmtime(since))}, {len(ids)} heroes, {len(calls)} queries", flush=True)
    done = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fn, *args): (fn.__name__, args) for fn, args in calls}
        for fut in as_completed(futs):
            try:
                fut.result()
                done += 1
            except Exception as e:
                failed += 1
                print(f"FAIL {futs[fut]}: {e}", flush=True)
            if (done + failed) % 200 == 0:
                print(f"{done + failed}/{len(calls)} ({failed} failed) {time.time() - t0:.0f}s", flush=True)
    print(f"done: {done} ok, {failed} failed, {time.time() - t0:.0f}s", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
