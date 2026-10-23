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
N_PERM = 30


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
    rows = []        # (player, hero, item, tier, state, won, dW, match)
    players = []     # (hero, state) per player-game
    for mi, m in enumerate(matches):
        for p in m["p"]:
            mine = nw_at(p, STATE_T)
            enemy = [nw_at(q, STATE_T) for q in m["p"] if q["t"] != p["t"]]
            if mine is None or None in enemy or not enemy:
                continue
            rel = mine / (sum(enemy) / len(enemy)) - 1
            s = -1 if rel < -REL else (1 if rel > REL else 0)
            won = m["win"] == p["t"]
            states[(s, won)] += 1
            pid = len(players)
            players.append((p["h"], s))
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
                rows.append((pid, p["h"], iid, items[iid]["item_tier"], s, won, dw, mi))

    print("win rate by state at 9:00:", {
        {-1: "behind", 0: "even", 1: "ahead"}[s]: f"{states[(s, True)] / max(states[(s, True)] + states[(s, False)], 1) * 100:.0f}% "
        f"of {states[(s, True)] + states[(s, False)]}" for s in (-1, 0, 1)})
    print(f"{len(rows)} purchases 9-20 min")

    # ---- 1. state interaction, with a permutation null (states shuffled among players of a hero)
    obs_sd, zs, diffs, per_item = interaction(rows, [s for _, s in players])
    rng = np.random.default_rng(0)
    by_hero = defaultdict(list)
    for pid, (h, _) in enumerate(players):
        by_hero[h].append(pid)
    null = []
    for _ in range(N_PERM):
        st = [s for _, s in players]
        for pids in by_hero.values():
            perm = rng.permutation([st[p] for p in pids])
            for p, s in zip(pids, perm):
                st[p] = s
        null.append(interaction(rows, st)[0])
    null = np.array(null)
    print(f"\n[1] behind-vs-ahead item lift (tier baseline without the item itself): {len(zs)} hero x item cells")
    print(f"    sd(z) observed {obs_sd:.3f}; shuffled states {null.mean():.3f} +- {null.std():.3f} "
          f"-> p = {(np.sum(null >= obs_sd) + 1) / (N_PERM + 1):.3f}")
    if len(zs):
        excess = max(diffs.var() * (1 - (null.mean() / obs_sd) ** 2), 0)
        print(f"    real interaction sd ~ {np.sqrt(excess) * 100:.1f} pp (excess over shuffled)")
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

    # ---- 2. dW vs win rate, both as within-hero lifts over tier-mates; split-half reliability
    from scipy.stats import spearmanr
    full = item_metrics(rows, items)
    half = rng.random(len(matches)) < 0.5
    a = item_metrics([r for r in rows if half[r[7]]], items)
    b = item_metrics([r for r in rows if not half[r[7]]], items)
    common = [i for i in full if i in a and i in b]
    rel_wr = spearmanr([a[i][0] for i in common], [b[i][0] for i in common])[0]
    rel_dw = spearmanr([a[i][1] for i in common], [b[i][1] for i in common])[0]
    r_xy = spearmanr([full[i][0] for i in common], [full[i][1] for i in common])[0]
    print(f"\n[2] {len(common)} items with n>=300 per half")
    print(f"    split-half reliability: win-rate lift {rel_wr:.2f}, dW lift {rel_dw:.2f}")
    print(f"    corr(win-rate lift, dW lift) = {r_xy:.2f}; corrected for noise = "
          f"{r_xy / np.sqrt(max(rel_wr, 1e-3) * max(rel_dw, 1e-3)):.2f}")
    table = sorted(((items[i]["name"], items[i]["item_tier"], full[i][2], full[i][0], full[i][1]) for i in common),
                   key=lambda r: r[3] - r[4] * 2)
    print("    biggest 'bought when already winning' gap (WR lift >> dW lift):")
    for nm, t, n, x, y in table[::-1][:8]:
        print(f"      T{t} {nm:24} WR {x * 100:+.1f} pp   dW {y * 100:+.2f} pp   n={n}")
    print("    undervalued by WR (dW lift >> WR lift):")
    for nm, t, n, x, y in table[:8]:
        print(f"      T{t} {nm:24} WR {x * 100:+.1f} pp   dW {y * 100:+.2f} pp   n={n}")


def interaction(rows, state_of):
    """sd(z) of (lift when behind - lift when ahead) over hero x item cells; lift = item win rate
    minus its tier-mates' (same hero, same state) win rate, the item itself left out."""
    cell = defaultdict(lambda: [0, 0])
    for pid, h, i, t, _s, won, _dw, _m in rows:
        c = cell[(h, t, i, state_of[pid])]
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
        bb, ba = base[(h, t, -1)], base[(h, t, 1)]
        if n < MIN_N or na < MIN_N or bb[1] - n < MIN_N or ba[1] - na < MIN_N:
            continue
        lb = wn / n - (bb[0] - wn) / (bb[1] - n)
        la = wa / na - (ba[0] - wa) / (ba[1] - na)
        se = np.sqrt(0.25 / n + 0.25 / (bb[1] - n) + 0.25 / na + 0.25 / (ba[1] - na))
        zs.append((lb - la) / se)
        diffs.append(lb - la)
        per_item[i].append((lb - la, 1 / se ** 2))
    zs = np.array(zs)
    return (zs.std() if len(zs) else 0.0), zs, np.array(diffs), per_item


def item_metrics(rows, items, min_n=300):
    """item -> (win-rate lift, dW lift, n): within-hero lift over same-tier items (item left out),
    averaged over heroes weighted by purchases."""
    c = defaultdict(lambda: [0, 0, 0.0, 0])           # (hero, item) -> wins, n, dW sum, dW n
    for _pid, h, i, t, _s, won, dw, _m in rows:
        x = c[(h, i)]
        x[0] += won
        x[1] += 1
        if dw is not None:
            x[2] += dw
            x[3] += 1
    tier = defaultdict(lambda: [0, 0, 0.0, 0])        # (hero, tier) totals
    for (h, i), x in c.items():
        y = tier[(h, items[i]["item_tier"])]
        for k in range(4):
            y[k] += x[k]
    acc = defaultdict(lambda: [0.0, 0.0, 0])
    for (h, i), (wn, n, ds, dn) in c.items():
        tw, tn, tds, tdn = tier[(h, items[i]["item_tier"])]
        if tn - n < 30 or tdn - dn < 30 or not dn:
            continue
        a = acc[i]
        a[0] += n * (wn / n - (tw - wn) / (tn - n))
        a[1] += n * (ds / dn - (tds - ds) / (tdn - dn))
        a[2] += n
    return {i: (s1 / n, s2 / n, n) for i, (s1, s2, n) in acc.items() if n >= min_n}


if __name__ == "__main__":
    main()
