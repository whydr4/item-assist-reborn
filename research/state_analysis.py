"""Real-match checks for state-conditioned recommendations.

1. Does the best item change with the player's state at 9:00 (own net worth vs the enemy team's
   average: behind < -10%, ahead > +10%)? For every hero x tier x item bought 9-20 min we take the
   item's win-rate lift over its tier-mates within each state and test the behind-vs-ahead
   difference against sampling noise: sd(z) ~ 1 means no real state effect.
2. Win probability added (dW, Jalovaara 2024): a win-probability model w(time, team net-worth
   diff) is fit from the same matches; each purchase is scored by w 6 min later minus w just before
   it. Selection ("bought because already winning") cancels out of dW but not out of win rate.

usage: python state_analysis.py data/matches/<file>.jsonl
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import queries  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
STATE_T = 540            # 9:00 sample
WINDOW = (540, 1200)     # purchases between 9 and 20 min
REL = 0.10               # +-10% vs enemy average net worth
MIN_N = 40


def load(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def nw_at(p, t):
    if t in p["ts"]:
        return p["nw"][p["ts"].index(t)]
    return None


def team_diff_series(m):
    """{time: (team0 NW - team1 NW) / mean team NW} over the recorded samples."""
    by_t = defaultdict(lambda: [0, 0])
    for p in m["p"]:
        for t, v in zip(p["ts"], p["nw"]):
            if v is not None:
                by_t[t][p["t"]] += v
    return {t: (a - b) / max((a + b) / 2, 1) for t, (a, b) in by_t.items() if a and b}


def fit_winprob(matches, times):
    """Empirical w(t, d) from binned team-0 diff; returns lookup function for team 0 POV."""
    tables = {}
    for t in times:
        xs, ys = [], []
        for m in matches:
            d = m["_diff"].get(t)
            if d is not None and m["win"] in (0, 1):
                xs.append(d)
                ys.append(1.0 if m["win"] == 0 else 0.0)
        if len(xs) < 200:
            continue
        xs, ys = np.array(xs), np.array(ys)
        edges = np.quantile(xs, np.linspace(0, 1, 21)[1:-1])
        b = np.digitize(xs, edges)
        wr = np.array([(ys[b == k].sum() + 1) / ((b == k).sum() + 2) for k in range(20)])
        wr = np.maximum.accumulate(wr)              # monotone in the lead
        tables[t] = (edges, wr)

    def w(t, d):
        tt = min(tables, key=lambda x: abs(x - t)) if tables else None
        if tt is None:
            return 0.5
        e, wr = tables[tt]
        return wr[np.digitize([d], e)[0]]
    return w, sorted(tables)


def main():
    path = sys.argv[1]
    matches = [m for m in load(path) if m.get("win") in (0, 1) and len(m["p"]) == 12]
    items = {it["id"]: it for it in queries.assets_items() if it.get("type") == "upgrade" and it.get("shopable")}
    print(f"{len(matches)} matches")

    for m in matches:
        m["_diff"] = team_diff_series(m)
    times = sorted({t for m in matches for t in m["_diff"]})
    w, wt = fit_winprob(matches, times)

    states = defaultdict(int)
    rows = []        # (hero, item, tier, state, won, dW)
    for m in matches:
        for p in m["p"]:
            mine = nw_at(p, STATE_T)
            enemy = [nw_at(q, STATE_T) for q in m["p"] if q["t"] != p["t"]]
            if mine is None or None in enemy or not enemy:
                continue
            rel = mine / (sum(enemy) / len(enemy)) - 1
            s = -1 if rel < -REL else (1 if rel > REL else 0)
            won = m["win"] == p["t"]
            states[(s, won)] += 1
            sign = 1 if p["t"] == 0 else -1
            seen = set()
            for iid, t_buy, _sold, _imb in p["it"]:
                if iid not in items or iid in seen or not (WINDOW[0] <= t_buy < WINDOW[1]):
                    continue
                seen.add(iid)
                before = max([t for t in wt if t <= t_buy], default=None)
                after = min([t for t in wt if t >= t_buy + 360], default=None)
                dw = None
                if before is not None and after is not None and before in m["_diff"] and after in m["_diff"]:
                    w0 = w(before, m["_diff"][before])
                    w1 = w(after, m["_diff"][after])
                    dw = sign * (w1 - w0)
                rows.append((p["h"], iid, items[iid]["item_tier"], s, won, dw))

    print("win rate by state at 9:00:", {
        {-1: "behind", 0: "even", 1: "ahead"}[s]: f"{states[(s, True)] / max(states[(s, True)] + states[(s, False)], 1) * 100:.0f}% "
        f"of {states[(s, True)] + states[(s, False)]}" for s in (-1, 0, 1)})
    print(f"{len(rows)} purchases 9-20 min")

    # ---- 1. state interaction
    cell = defaultdict(lambda: [0, 0])           # (hero, tier, item, state) -> wins, n
    for h, i, t, s, won, _ in rows:
        c = cell[(h, t, i, s)]
        c[0] += won
        c[1] += 1
    base = defaultdict(lambda: [0, 0])
    for (h, t, i, s), (wn, n) in cell.items():
        b = base[(h, t, s)]
        b[0] += wn
        b[1] += n
    zs, diffs, per_item = [], [], defaultdict(list)
    for (h, t, i, s), (wn, n) in cell.items():
        if s != -1:
            continue
        wa, na = cell.get((h, t, i, 1), (0, 0))
        if n < MIN_N or na < MIN_N:
            continue
        lb = wn / n - base[(h, t, -1)][0] / base[(h, t, -1)][1]
        la = wa / na - base[(h, t, 1)][0] / base[(h, t, 1)][1]
        se = np.sqrt(0.25 / n + 0.25 / na)
        zs.append((lb - la) / se)
        diffs.append(lb - la)
        per_item[i].append((lb - la, 1 / se ** 2))
    zs, diffs = np.array(zs), np.array(diffs)
    if len(zs):
        se2 = np.mean(diffs ** 2 / np.maximum(zs ** 2, 1e-9))
        print(f"\n[1] behind-vs-ahead item lift: {len(zs)} hero x item cells, sd(z) = {zs.std():.2f} "
              f"(1.0 = noise), |z|>3: {np.mean(np.abs(zs) > 3) * 100:.1f}%, "
              f"real interaction sd ~ {np.sqrt(max(diffs.var() - se2, 0)) * 100:.1f} pp")
        pooled = []
        for i, lst in per_item.items():
            wsum = sum(x[1] for x in lst)
            est = sum(x[0] * x[1] for x in lst) / wsum
            pooled.append((est, est * np.sqrt(wsum), items[i]["name"], len(lst)))
        pooled.sort()
        print("    items relatively better when BEHIND (pooled over heroes, z):")
        for est, z, nm, k in pooled[::-1][:8]:
            print(f"      {nm:24} {est * 100:+.1f} pp  z={z:+.1f}  heroes={k}")
        print("    items relatively better when AHEAD:")
        for est, z, nm, k in pooled[:8]:
            print(f"      {nm:24} {est * 100:+.1f} pp  z={z:+.1f}  heroes={k}")

    # ---- 2. dW vs win rate
    agg = defaultdict(lambda: [0, 0, 0.0, 0])     # item -> wins, n, dW sum, dW n
    for h, i, t, s, won, dw in rows:
        a = agg[i]
        a[0] += won
        a[1] += 1
        if dw is not None:
            a[2] += dw
            a[3] += 1
    tier_wr = defaultdict(lambda: [0, 0])
    tier_dw = defaultdict(lambda: [0.0, 0])
    for i, (wn, n, ds, dn) in agg.items():
        t = items[i]["item_tier"]
        tier_wr[t][0] += wn
        tier_wr[t][1] += n
        tier_dw[t][0] += ds
        tier_dw[t][1] += dn
    table = []
    for i, (wn, n, ds, dn) in agg.items():
        if n < 300 or dn < 300:
            continue
        t = items[i]["item_tier"]
        table.append((items[i]["name"], t, n, wn / n - tier_wr[t][0] / tier_wr[t][1], ds / dn - tier_dw[t][0] / tier_dw[t][1]))
    if table:
        wr = np.array([r[3] for r in table])
        dw = np.array([r[4] for r in table])
        from scipy.stats import spearmanr
        print(f"\n[2] {len(table)} items: rank corr(win-rate lift, dW lift) = {spearmanr(wr, dw)[0]:.2f}")
        table.sort(key=lambda r: r[3] - r[4] * 2)
        print("    biggest 'bought when already winning' gap (WR lift >> dW lift):")
        for nm, t, n, a, b in table[::-1][:8]:
            print(f"      T{t} {nm:24} WR {a * 100:+.1f} pp   dW {b * 100:+.2f} pp   n={n}")
        print("    undervalued by WR (dW lift >> WR lift):")
        for nm, t, n, a, b in table[:8]:
            print(f"      T{t} {nm:24} WR {a * 100:+.1f} pp   dW {b * 100:+.2f} pp   n={n}")


if __name__ == "__main__":
    main()
