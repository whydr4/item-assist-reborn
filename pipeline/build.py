"""Turn the cached API stats into the mod's recommender_data.js.

All win-rate signals are LIFTS (item win rate minus the right baseline), shrunk along the rank
ladder ALL -> HIGH -> PRO: each level's observation pulls the running estimate toward itself in
proportion to its sample size, so pro data wins wherever pros played enough games and broader
data fills the gaps.

usage: python build.py [--out path]
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict

import queries

ROOT = os.path.join(os.path.dirname(__file__), "..")
GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Deadlock\game\citadel"
LADDER = ("all", "high", "pro")
K_WR = 150          # pseudo-matches pulling a win-rate lift toward the broader level
K_PICK = 40         # pseudo-games pulling a pick rate toward the broader level
PHASE_END_MIN = (9, 20, 30, 999)   # item-flow columns for normal mode
TIMING_BIN_MIN = 5
TIMING_BINS = 10    # 0-5, 5-10, ... 45+
LATE_LIFT = 0.02    # "too late" = two bins in a row this much worse than same-tier buys
# Size budget: per-hero tables only cover items strong players actually buy on that hero.
RELEVANT_PICK = 0.02
VS_MIN_LIFT, VS_PER_ENEMY = 0.02, 12
NEXT_TOP, SYN_TOP = 6, 6

sys.stdout.reconfigure(encoding="utf-8")


def ladder_shrink(obs, k, prior=0.0):
    """obs: {level: (value, n)}. Sequentially update prior with each level's value weighted by n."""
    est = prior
    for lv in LADDER:
        if lv in obs:
            v, n = obs[lv]
            if n > 0:
                est = (n * v + k * est) / (n + k)
    return est


RELIABILITY_PICK = 0.05


def reliability(pick):
    """How much to trust an item's win-rate lift given how often strong players buy it. Niche items
    (Frenzy on Abrams, ~0% pick) show +5..8% lifts that are selection, not value: they get bought only
    in situations that are already won. Mirrored in ql_feat_recommender.js."""
    return pick / (pick + RELIABILITY_PICK)


def r3(x):
    return round(x, 3)


def load_old():
    t = open(os.path.join(ROOT, "data", "recommender_data_6635_original.js"), encoding="utf-8").read()
    dec = json.JSONDecoder()
    data, end = dec.raw_decode(t, t.index("{"))
    for m in re.finditer(r"QOL_REC_DATA\.(\w+)=", t[end:]):
        data[m.group(1)], _ = dec.raw_decode(t, end + m.end())
    return data


def client_version():
    try:
        m = re.search(r"ClientVersion=(\d+)", open(os.path.join(GAME, "steam.inf")).read())
        return int(m.group(1))
    except OSError:
        return None


# ---------------------------------------------------------------- per-hero signals
def phase_values(q, hero, item_tier):
    """From item-flow nodes: per phase, net-worth-adjusted lift and pick rate per item.

    Lift is measured against items of the SAME tier bought in the same phase: even after the
    net-worth adjustment every T4 looks +5..9% vs the phase average (survivorship of games long
    enough to afford it), and the real in-shop choice is between items of one tier anyway."""
    lift_obs = defaultdict(dict)     # (col, item) -> {lv: (lift, n)}
    pick_obs = defaultdict(dict)
    for lv in LADDER:
        f = q.flow(hero, lv)
        reached = f["summary_reached_per_column"]
        acc = defaultdict(lambda: [0.0, 0])
        for nd in f["nodes"]:
            t = item_tier.get(nd["item_id"])
            if t is not None and nd.get("adjusted_win_rate") is not None:
                a = acc[(nd["column"], t)]
                a[0] += nd["adjusted_win_rate"] * nd["matches"]
                a[1] += nd["matches"]
        base = {k: s / n for k, (s, n) in acc.items() if n}
        for nd in f["nodes"]:
            c, i, n = nd["column"], nd["item_id"], nd["matches"]
            b = base.get((c, item_tier.get(i)))
            if nd.get("adjusted_win_rate") is not None and b is not None:
                lift_obs[(c, i)][lv] = (nd["adjusted_win_rate"] - b, n)
            if reached[c]:
                pick_obs[(c, i)][lv] = (n / reached[c], reached[c])
    phases = [dict() for _ in range(4)]
    for key in set(lift_obs) | set(pick_obs):
        c, i = key
        phases[c][i] = (ladder_shrink(lift_obs.get(key, {}), K_WR), ladder_shrink(pick_obs.get(key, {}), K_PICK))
    return phases


def overall_pick(q, hero, hero_matches):
    obs = defaultdict(dict)
    for lv in LADDER:
        hm = hero_matches[lv].get(hero, 0)
        if not hm:
            continue
        for r in q.items(hero, lv):
            obs[r["item_id"]][lv] = (min(1.0, r["matches"] / hm), hm)
    return {i: ladder_shrink(o, K_PICK) for i, o in obs.items()}


def transitions(q, hero):
    """From item-flow edges: after owning A, how often B comes next and B's lift vs A's other follow-ups."""
    lift_obs = defaultdict(dict)
    share_obs = defaultdict(dict)
    for lv in LADDER:
        f = q.flow(hero, lv)
        node_n = defaultdict(int)
        for nd in f["nodes"]:
            if nd["column"] < 3:
                node_n[nd["item_id"]] += nd["matches"]
        pair = defaultdict(lambda: [0, 0])
        out = defaultdict(lambda: [0, 0])
        for e in f["edges"]:
            p = pair[(e["from_item_id"], e["to_item_id"])]
            p[0] += e["wins"]
            p[1] += e["matches"]
            o = out[e["from_item_id"]]
            o[0] += e["wins"]
            o[1] += e["matches"]
        for (a, b), (w, n) in pair.items():
            if a == b or not n:
                continue
            ow, on = out[a]
            lift_obs[(a, b)][lv] = (w / n - ow / on, n)
            if node_n[a]:
                share_obs[(a, b)][lv] = (min(1.0, n / node_n[a]), node_n[a])
    res = defaultdict(list)
    for key in set(lift_obs) | set(share_obs):
        a, b = key
        res[a].append((b, ladder_shrink(lift_obs.get(key, {}), K_WR), ladder_shrink(share_obs.get(key, {}), K_PICK)))
    return res


def timing(q, hero, item_tier):
    """Lift by buy time vs items of the SAME tier bought in the same minutes (same-tier = similar wealth)."""
    obs = defaultdict(dict)    # (item, bin) -> {lv: (lift, n)}
    for lv in ("all", "high"):
        rows = q.timing(hero, lv)
        agg = defaultdict(lambda: [0, 0])     # (item, bin)
        base = defaultdict(lambda: [0, 0])    # (tier, bin)
        for r in rows:
            b = min(TIMING_BINS - 1, r["bucket"] // TIMING_BIN_MIN)
            t = item_tier.get(r["item_id"])
            if t is None:
                continue
            a = agg[(r["item_id"], b)]
            a[0] += r["wins"]
            a[1] += r["matches"]
            s = base[(t, b)]
            s[0] += r["wins"]
            s[1] += r["matches"]
        for (i, b), (w, n) in agg.items():
            bw, bn = base[(item_tier[i], b)]
            if n and bn:
                obs[(i, b)][lv] = (w / n - bw / bn, n)
    out = {}
    for i in {i for i, _ in obs}:
        ns = [sum(n for _, n in obs.get((i, b), {}).values()) for b in range(TIMING_BINS)]
        if sum(ns) < 300:
            continue
        lifts = [ladder_shrink(obs[(i, b)], K_WR) if (i, b) in obs else None for b in range(TIMING_BINS)]
        out[i] = (lifts, ns)
    return out


def good_until(lifts, ns):
    """Minute after which buying the item stops paying off: the start of the first run of two
    negative bins (with data) that follows the item's best bin. None if it never turns."""
    pts = [(b, l) for b, (l, n) in enumerate(zip(lifts, ns)) if l is not None and n >= 50]
    if len(pts) < 3:
        return None
    best = max(range(len(pts)), key=lambda k: pts[k][1])
    for k in range(best + 1, len(pts) - 1):
        if pts[k][1] < -LATE_LIFT and pts[k + 1][1] < -LATE_LIFT:
            return pts[k][0] * TIMING_BIN_MIN
    return None


def matchups(q, hero, enemies, relevant):
    """vs[enemy][item]: item's lift when this enemy is in the match, beyond the item's normal lift.
    Only items the hero actually buys, only clear effects, top VS_PER_ENEMY per enemy."""
    over = {}
    for lv in queries.VS_LEVELS:
        rows = q.items(hero, lv)
        tw, tn = sum(r["wins"] for r in rows), sum(r["matches"] for r in rows)
        over[lv] = {r["item_id"]: r["wins"] / r["matches"] - tw / tn for r in rows if r["matches"]}
    res = {}
    for e in enemies:
        obs = defaultdict(dict)
        for lv in queries.VS_LEVELS:
            rows = q.vs(hero, e, lv)
            tw, tn = sum(r["wins"] for r in rows), sum(r["matches"] for r in rows)
            if not tn:
                continue
            for r in rows:
                i = r["item_id"]
                if r["matches"] and i in over[lv]:
                    obs[i][lv] = (r["wins"] / r["matches"] - tw / tn - over[lv][i], r["matches"])
        lifts = [(i, ladder_shrink(o, K_WR)) for i, o in obs.items() if i in relevant]
        lifts = sorted((x for x in lifts if abs(x[1]) >= VS_MIN_LIFT), key=lambda x: -abs(x[1]))[:VS_PER_ENEMY]
        if lifts:
            res[str(e)] = {str(i): r3(v) for i, v in lifts}
    return res


def ability_order(q, hero, slot_of):
    rows = [r for r in q.ability_order(hero, "high") if all(a in slot_of for a in r["abilities"])]
    if not rows:
        return None
    longest = max(len(r["abilities"]) for r in rows)
    full = [r for r in rows if len(r["abilities"]) >= longest - 1]
    best = max(full, key=lambda r: r["matches"])
    return [slot_of[a] for a in best["abilities"]]


# ---------------------------------------------------------------- static metadata
def panorama_index():
    p = os.path.join(ROOT, "data", "game_panorama_images.txt")
    return [ln.strip() for ln in open(p, encoding="utf-8") if ln.strip()]


def find_icon(index, folder, url):
    """Map an assets-API image url to the game's own s2r texture path, verified against the VPK index."""
    if not url:
        return None
    stem = os.path.splitext(url.rsplit("/", 1)[-1])[0]
    for cand in (f"{stem}_psd.vtex_c", f"{stem}.vtex_c", f"{stem}_png.vtex_c"):
        hits = [p for p in index if p.startswith(folder) and p.endswith("/" + cand)]
        if hits:
            return "s2r://" + min(hits, key=len)[:-2]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "recommender_data.js"))
    ap.add_argument("--heroes", help="comma-separated hero ids to build (default: all collected); matchups still use all")
    a = ap.parse_args()

    run = json.load(open(os.path.join(ROOT, "data", "run.json"), encoding="utf-8"))
    q = queries.Q(run["since"])
    old = load_old()
    index = panorama_index()

    heroes_a = {h["id"]: h for h in queries.active_heroes() if h["id"] in run["hero_ids"]}
    assets = queries.assets_items()
    by_class = {it["class_name"]: it for it in assets}
    shop = {str(it["id"]): it for it in assets if it.get("type") == "upgrade" and it.get("shopable") and not it.get("disabled")}

    items = {}
    for iid, it in shop.items():
        o = old["items"].get(iid)
        if o:
            items[iid] = dict(o, c=it.get("cost", o["c"]), t=it.get("item_tier", o["t"]))
        else:
            ic = find_icon(index, "panorama/images/items/", it.get("shop_image"))
            items[iid] = {"n": it["name"], "c": it["cost"], "t": it["item_tier"], "s": it.get("item_slot_type"), "ic": ic or ""}
    item_tier = {int(k): v["t"] for k, v in items.items()}

    hero_matches = {lv: {r["hero_id"]: r["matches"] for r in q.hero_stats(lv)} for lv in LADDER}

    out = {"meta": {}, "heroes": {}, "heroIcon": {}, "items": items, "rec": {}, "recp": {}, "next": {},
           "tm": {}, "vs": {}, "syn": {}, "ctr": old.get("ctr", {}), "imbue": {}}
    extra = {"byName": {}, "abilityOrder": {}, "abilityIcon": {}, "tips": {}}
    report = []
    hero_ids = sorted(heroes_a)
    build_ids = [int(x) for x in a.heroes.split(",")] if a.heroes else hero_ids
    for n_done, hid in enumerate(build_ids, 1):
        h, key = heroes_a[hid], str(hid)
        out["heroes"][key] = h["name"]
        extra["byName"][h["class_name"]] = hid
        out["heroIcon"][key] = find_icon(index, "panorama/images/heroes/", (h.get("images") or {}).get("icon_image_small")) \
            or old.get("heroIcon", {}).get(key, "")
        slot_of, icons = {}, {}
        for s in range(1, 5):
            ab = by_class.get((h.get("items") or {}).get(f"signature{s}", ""))
            if ab:
                slot_of[ab["id"]] = s
                icons[str(s)] = find_icon(index, "panorama/images/hud/abilities/", ab.get("image")) \
                    or old.get("abilityIcon", {}).get(key, {}).get(str(s), "")
        extra["abilityIcon"][key] = icons
        order = ability_order(q, hid, slot_of)
        if order:
            extra["abilityOrder"][key] = order
        if key in old.get("imbue", {}):
            out["imbue"][key] = old["imbue"][key]
        if key in old.get("tips", {}):
            extra["tips"][key] = old["tips"][key]

        phases = phase_values(q, hid, item_tier)
        pick = overall_pick(q, hid, hero_matches)
        rec = []
        for i in set(pick) | {i for ph in phases for i in ph}:
            if str(i) not in items:
                continue
            ws = [(ph[i][0], ph[i][1]) for ph in phases if i in ph]
            wsum = sum(p for _, p in ws)
            val = 0.5 + (sum(l * reliability(p) * p for l, p in ws) / wsum if wsum else 0.0)
            rec.append([i, round(val, 4), r3(pick.get(i, 0.0))])
        rec.sort(key=lambda r: -r[1])
        out["rec"][key] = rec
        out["recp"][key] = [
            [[i, round(l * 1000), round(p * 1000)] for i, (l, p) in sorted(ph.items(), key=lambda kv: -kv[1][1])
             if str(i) in items and p >= 0.01][:30]
            for ph in phases]

        relevant = {i for i, p in pick.items() if p >= RELEVANT_PICK and str(i) in items}
        nxt, syn = {}, {}
        for a_id, lst in transitions(q, hid).items():
            if a_id not in relevant:
                continue
            lst = [x for x in lst if x[0] in relevant]
            top = sorted(lst, key=lambda x: -x[2])[:NEXT_TOP]
            if top:
                nxt[str(a_id)] = [[b, round(l * 1000), round(s * 1000)] for b, l, s in top]
            good = sorted(((b, l) for b, l, s in lst if s >= 0.05 and l >= 0.02), key=lambda x: -x[1])[:SYN_TOP]
            if good:
                syn[str(a_id)] = {str(b): r3(l) for b, l in good}
        out["next"][key], out["syn"][key] = nxt, syn

        tm = {}
        for i, (lifts, ns) in timing(q, hid, item_tier).items():
            if i in relevant:
                tm[str(i)] = [good_until(lifts, ns), [None if l is None else round(l * 1000) for l in lifts]]
        out["tm"][key] = tm
        out["vs"][key] = matchups(q, hid, [e for e in hero_ids if e != hid], relevant)

        names = lambda lst: ", ".join(f"{items[str(r[0])]['n']} ({(r[1] - 0.5) * 100:+.1f}%, pick {r[2] * 100:.0f}%)" for r in lst)
        report.append(f"{h['name']} [pro {hero_matches['pro'].get(hid, 0)} / high {hero_matches['high'].get(hid, 0)} games]\n"
                      f"  top value: {names(rec[:6])}\n"
                      f"  top pick : {names(sorted(rec, key=lambda r: -r[2])[:6])}")
        print(f"[{n_done}/{len(build_ids)}] {h['name']}", flush=True)

    out["meta"] = {
        "client_version": client_version(),
        "generated": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
        "since": time.strftime("%Y-%m-%d", time.gmtime(run["since"])),
        "badge_ladder": queries.LEVELS,
        "phase_end_min": list(PHASE_END_MIN[:3]),
        "timing_bin_min": TIMING_BIN_MIN,
        "note": ("rec[hero]=[item, 0.5+net-worth-adjusted lift, pro pick rate]; recp[hero][phase]=[item, lift*1000, pick*1000]; "
                 "next[hero][owned]=[item, lift*1000, share*1000]; tm[hero][item]=[good_until_min, lift*1000 per bin]; "
                 "vs[hero][enemy][item]=lift; syn from next; ctr/imbue/tips carried over from 6635 data"),
    }
    js = "// generated by pipeline/build.py — do not edit\nvar QOL_REC_DATA = " + json.dumps(out, separators=(",", ":")) + ";\n"
    js += "".join(f"QOL_REC_DATA.{k}={json.dumps(v, separators=(',', ':'))};\n" for k, v in extra.items())
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(js)
    with open(os.path.join(ROOT, "data", "build_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print(f"wrote {a.out} ({len(js) / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
