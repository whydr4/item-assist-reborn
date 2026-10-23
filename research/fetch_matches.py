"""Download raw high-rank matches and keep only what the state-conditioned analysis needs.

Per player: hero, team, lane, won, net worth / deaths / damage taken every sample, purchases with
time (sold time, imbue target). Written as one JSON line per match to data/matches/<since>.jsonl.

usage: python fetch_matches.py --n 5000 [--badge 90] [--workers 8]
"""
import argparse
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import api  # noqa: E402  (shared disk cache + rate limiter for the id listing)

ROOT = os.path.join(os.path.dirname(__file__), "..")
PER_MATCH_RATE = 9.0      # S3-backed per-match metadata: 100 req / 10 s per IP
_lock = threading.Lock()
_next = [0.0]


def _slot():
    with _lock:
        now = time.time()
        s = max(now, _next[0])
        _next[0] = s + 1.0 / PER_MATCH_RATE
    if s > now:
        time.sleep(s - now)


def fetch(mid):
    for attempt in range(5):
        _slot()
        try:
            req = urllib.request.Request(f"{api.API}/v1/matches/{mid}/metadata", headers={"User-Agent": api.USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(float(e.headers.get("Retry-After") or 0) or 3 * (attempt + 1))
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            time.sleep(3 * (attempt + 1))     # dropped connections, timeouts, truncated JSON
    return None


def compact(meta):
    info = meta.get("match_info", meta)
    out = {"id": info["match_id"], "dur": info.get("duration_s"), "win": info.get("winning_team"),
           "badge": [info.get("average_badge_team0"), info.get("average_badge_team1")], "p": []}
    for p in info.get("players", []):
        st = p.get("stats") or []
        out["p"].append({
            "h": p["hero_id"], "t": p["team"], "lane": p.get("assigned_lane"), "slot": p.get("player_slot"),
            "won": p.get("player_match_outcome") == 0 if "player_match_outcome" in p else None,
            "ts": [s.get("time_stamp_s") for s in st],
            "nw": [s.get("net_worth") for s in st],
            "de": [s.get("deaths") for s in st],
            "dt": [s.get("player_damage_taken") for s in st],
            "it": [[i["item_id"], i["game_time_s"], i.get("sold_time_s", 0), i.get("imbued_ability_id", 0)]
                   for i in p.get("items", []) if i.get("game_time_s", 0) > 0],
            "kills": [[d.get("game_time_s"), d.get("killer_player_slot")] for d in (p.get("death_details") or [])],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--badge", type=int, default=90)
    ap.add_argument("--since", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    run = json.load(open(os.path.join(ROOT, "data", "run.json"), encoding="utf-8"))
    since = a.since or run["since"]

    ids, last = [], None
    while len(ids) < a.n:
        page = api.get("/v1/matches/metadata", cached=False, include_info="true", game_mode="normal",
                       min_average_badge=a.badge, min_unix_timestamp=since, order_by="match_id",
                       order_direction="desc", limit=min(10000, a.n - len(ids)),
                       max_match_id=(last - 1) if last else None)
        if not page:
            break
        ids += [m["match_id"] for m in page]
        last = min(m["match_id"] for m in page)
    print(f"{len(ids)} match ids (badge>={a.badge})", flush=True)

    out_dir = os.path.join(ROOT, "data", "matches")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"badge{a.badge}_since{since}.jsonl")
    have, good = set(), []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    have.add(json.loads(ln)["id"])
                    good.append(ln if ln.endswith("\n") else ln + "\n")
                except ValueError:
                    pass          # line cut off by an interrupted run
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(good)
    todo = [m for m in ids if m not in have]
    t0, done = time.time(), 0
    with open(path, "a", encoding="utf-8") as f, ThreadPoolExecutor(a.workers) as ex:
        for meta in ex.map(fetch, todo):
            if meta:
                f.write(json.dumps(compact(meta), separators=(",", ":")) + "\n")
            done += 1
            if done % 500 == 0:
                print(f"{done}/{len(todo)} {time.time() - t0:.0f}s", flush=True)
    print(f"done: {len(have) + done} matches in {path}", flush=True)


if __name__ == "__main__":
    main()
