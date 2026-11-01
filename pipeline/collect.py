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
    ap.add_argument("--until", type=int, help="snapshot end timestamp (default: collection start; reused on resume)")
    ap.add_argument("--resume", action="store_true", help="resume the saved snapshot")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--heroes", help="comma-separated hero ids (default: all active)")
    a = ap.parse_args()

    previous = None
    run_path = os.path.join(ROOT, "data", "run.json")
    if a.resume:
        with open(run_path, encoding="utf-8") as f:
            previous = json.load(f)
    since = a.since or (previous["since"] if previous else queries.last_big_patch())
    heroes = queries.active_heroes()
    ids = [int(x) for x in a.heroes.split(",")] if a.heroes else sorted(h["id"] for h in heroes)
    assets = queries.assets_items()
    until = a.until or (previous.get("until") if previous else None) or int(time.time())
    if since >= until:
        sys.exit("--since must be earlier than --until")
    if a.workers < 1:
        sys.exit("--workers must be positive")
    if previous and not a.heroes:
        ids = previous["hero_ids"]
    snapshot = os.path.join(ROOT, "data", "cache", f"assets-{until}.json")
    os.makedirs(os.path.dirname(snapshot), exist_ok=True)
    if not os.path.exists(snapshot):
        with open(snapshot + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"heroes": heroes, "items": assets, "fetched": int(time.time())}, f)
        os.replace(snapshot + ".tmp", snapshot)
    run = {"since": since, "until": until, "hero_ids": ids, "started": int(time.time()), "assets_file": os.path.basename(snapshot), "complete": False}
    with open(os.path.join(ROOT, "data", "run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f)

    q = queries.Q(since, until)
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
            if (done + failed) % 50 == 0:
                print(f"{done + failed}/{len(calls)} ({failed} failed) {time.time() - t0:.0f}s", flush=True)
    run.update(complete=failed == 0, collected=done, failed=failed, finished=int(time.time()))
    with open(os.path.join(ROOT, "data", "run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f)
    print(f"done: {done} ok, {failed} failed, {time.time() - t0:.0f}s", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
