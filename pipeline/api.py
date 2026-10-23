"""Thin deadlock-api client: disk cache, shared rate limiter, retries on 429/503."""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.deadlock-api.com"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "cache")
RATE_PER_MIN = 170          # analytics limit is 200/min per IP; leave headroom
USER_AGENT = "deadlock-item-assistant-pipeline"

_lock = threading.Lock()
_next_slot = [0.0]


def _wait_slot():
    with _lock:
        now = time.time()
        slot = max(now, _next_slot[0])
        _next_slot[0] = slot + 60.0 / RATE_PER_MIN
    if slot > now:
        time.sleep(slot - now)


def _cache_path(url):
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")


def get(path, cached=True, **params):
    """GET API + path with params; returns parsed JSON. Cached forever on disk by full URL."""
    params = {k: v for k, v in params.items() if v is not None}
    url = API + path + ("?" + urllib.parse.urlencode(sorted(params.items())) if params else "")
    cp = _cache_path(url)
    if cached and os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            return json.load(f)
    for attempt in range(8):
        _wait_slot()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 7:
                time.sleep(float(e.headers.get("Retry-After") or 0) or 5 * (attempt + 1))
                continue
            raise RuntimeError(f"{e.code} {url}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 7:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"{url}: {e}") from None
    if cached:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, cp)
    return data
