"""Do items really work differently for strong players, or is the pro-vs-all gap sampling noise?

For every hero x phase x item with enough pro games, compare the same-tier, net-worth-adjusted lift
measured on Ascendant+ matches with the lift on all ranks:  z = (lift_pro - lift_all) / SE.
If ranks did not matter, z would be ~N(0,1) (sd ~ 1). An sd well above 1 means real rank effects;
the excess variance tells how big they are.
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import queries  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.join(os.path.dirname(__file__), "..")


def tier_lifts(flow, item_tier):
    acc = defaultdict(lambda: [0.0, 0])
    for nd in flow["nodes"]:
        t = item_tier.get(nd["item_id"])
        if t is not None and nd.get("adjusted_win_rate") is not None:
            a = acc[(nd["column"], t)]
            a[0] += nd["adjusted_win_rate"] * nd["matches"]
            a[1] += nd["matches"]
    out = {}
    for nd in flow["nodes"]:
        t = item_tier.get(nd["item_id"])
        s, n = acc.get((nd["column"], t), (0, 0))
        if n and nd.get("adjusted_win_rate") is not None:
            out[(nd["column"], nd["item_id"])] = (nd["adjusted_win_rate"] - s / n, nd["matches"])
    return out


def main():
    run = json.load(open(os.path.join(ROOT, "data", "run.json"), encoding="utf-8"))
    q = queries.Q(run["since"])
    item_tier = {it["id"]: it["item_tier"] for it in queries.assets_items()
                 if it.get("type") == "upgrade" and it.get("shopable")}
    rows = []
    for hid in run["hero_ids"]:
        a = tier_lifts(q.flow(hid, "all"), item_tier)
        for lv in ("high", "pro"):
            b = tier_lifts(q.flow(hid, lv), item_tier)
            for k, (lb, nb) in b.items():
                if k in a and nb >= 150:
                    la, na = a[k]
                    se = np.sqrt(0.25 / nb + 0.25 / na)
                    rows.append((lv, k[0], (lb - la) / se, lb - la, lb, la))
    for lv in ("high", "pro"):
        z = np.array([r[2] for r in rows if r[0] == lv])
        diff = np.array([r[3] for r in rows if r[0] == lv])
        lb = np.array([r[4] for r in rows if r[0] == lv])
        la = np.array([r[5] for r in rows if r[0] == lv])
        excess = max(0.0, diff.var() - np.mean([0.25 / 150]) * 0)  # raw spread, reported below
        se2 = np.mean(diff ** 2 / np.maximum(z ** 2, 1e-9))
        true_sd = np.sqrt(max(diff.var() - se2, 0))
        print(f"{lv:5} vs all: {len(z)} hero-phase-items, sd(z) = {z.std():.2f} "
              f"(1.0 = pure noise), |z|>3: {np.mean(np.abs(z) > 3) * 100:.1f}% (noise: 0.3%)")
        print(f"       lift sd: {lv} {lb.std() * 100:.2f} pp, all {la.std() * 100:.2f} pp, "
              f"corr {np.corrcoef(lb, la)[0, 1]:.2f}, real rank-effect sd ~ {true_sd * 100:.2f} pp")
        for ph in range(4):
            zz = np.array([r[2] for r in rows if r[0] == lv and r[1] == ph])
            if len(zz):
                print(f"       phase {ph}: n={len(zz)}, sd(z)={zz.std():.2f}")


if __name__ == "__main__":
    main()
