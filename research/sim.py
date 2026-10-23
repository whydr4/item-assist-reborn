"""Synthetic stress test for the recommender's estimators.

A toy match model with KNOWN item effects generates purchase/outcome data shaped like what the
pipeline sees (per phase x item wins/matches, net worth at buy, rank levels). We then run the same
estimators the pipeline uses, let each recommendation mode pick an item, and score it against the
truth:

    captured = (true effect of pick - mean effect of the options) / (best effect - mean effect)

100% = always the truly best item, 0% = no better than a random affordable item, <0 = harmful.

Model, per player-game (one hero):
  rank r in {0,1,2} (all / high / pro lobbies), skill z ~ N(mu_r, 1), team edge d ~ N(0, 1)
  lane lead L = a*d + b*(z - mu_r) + noise  ->  state s = behind / even / ahead
  4 phases; phase p offers tiers TIERS[p], player buys BUYS[p] items it doesn't own:
      utility_i = habit_i + q(z) * eff_i(s, z) + lam_i * L + Gumbel   (good players exploit truth)
  net worth at buy = base_p * exp(c*L + noise); long phases are only reached by closer games
  win ~ Bernoulli(sigmoid(k_d*d + k_l*L + sum eff_i(s, z) of bought items))
  eff_i(s, z) = beta_i + gamma_i * s + rho_i * (z - mu_pro)        (state- and skill-dependent)

usage: python sim.py [--reps 12] [--out ../docs/sim_results.md]
"""
import argparse
import sys
from dataclasses import dataclass, field, replace

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

N_ITEMS_PER_TIER = 8
TIERS = [(1,), (2, 3), (3, 4), (4,)]
BUYS = [3, 3, 2, 2]
NW_BASE = [3000, 12000, 25000, 40000]
RANK_MU = [-1.0, 0.0, 1.0]           # skill centre of all / high / pro lobbies
RANK_SHARE = [0.70, 0.22, 0.08]
STATES = (-1, 0, 1)
STATE_NAMES = {-1: "behind", 0: "even", 1: "ahead"}
K_WR, K_PICK, K_STATE = 150, 40, 300
NW_BUCKETS = 5


@dataclass
class Scenario:
    name: str
    desc: str
    n_games: int = 60000            # player-games of this hero in the data window
    beta_sd: float = 0.12           # spread of true item effects (logit)
    gamma_sd: float = 0.03          # state-dependence of effects
    comeback_items: int = 0         # items that are strong when behind, weak when ahead
    comeback_size: float = 0.35
    rho_sd: float = 0.0             # skill-dependence (items good for pros, bad for low ranks)
    luxury_items: int = 3           # zero-effect items bought preferentially when ahead
    luxury_lambda: float = 1.2
    habit_sd: float = 0.8           # meta habits unrelated to truth
    habit_anti: float = 0.0         # habits anti-correlated with truth (community misvalues items)
    q0: float = 2.0                 # how much players follow true effects
    q1: float = 1.5                 # ... more so the better they are
    state_noise: float = 0.0        # P(mod misreads the player's state)
    drift: float = 0.0              # post-patch change of true effects (sd, logit)
    post_patch_share: float = 1.0   # share of the data window played after the patch
    user_rank: int = 2              # rank of the mod's user (2 = pro, 0 = low)
    extra: dict = field(default_factory=dict)


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def make_world(sc, rng):
    n = N_ITEMS_PER_TIER * 4
    tier = np.repeat([1, 2, 3, 4], N_ITEMS_PER_TIER)
    beta = rng.normal(0, sc.beta_sd, n)
    gamma = rng.normal(0, sc.gamma_sd, n)
    rho = rng.normal(0, sc.rho_sd, n) if sc.rho_sd else np.zeros(n)
    lam = np.zeros(n)
    if sc.comeback_items:
        idx = rng.choice(n, sc.comeback_items, replace=False)
        gamma[idx] = -sc.comeback_size
    if sc.luxury_items:
        cand = np.where(tier >= 3)[0]
        idx = rng.choice(cand, sc.luxury_items, replace=False)
        beta[idx] = 0.0
        gamma[idx] = 0.0
        lam[idx] = sc.luxury_lambda
    habit = rng.normal(0, sc.habit_sd, n) - sc.habit_anti * beta / max(sc.beta_sd, 1e-9)
    return dict(n=n, tier=tier, beta=beta, gamma=gamma, rho=rho, lam=lam, habit=habit)


def effects(w, s, z):
    """True effect (logit) of every item for players in state s (array) with skill z (array)."""
    return w["beta"][None, :] + w["gamma"][None, :] * s[:, None] + w["rho"][None, :] * (z[:, None] - RANK_MU[2])


def simulate(sc, w, rng, n_games, truth_shift=None):
    rank = rng.choice(3, n_games, p=RANK_SHARE)
    z = rng.normal(np.array(RANK_MU)[rank], 1.0)
    d = rng.normal(0, 1, n_games)
    lead = 0.8 * d + 0.4 * (z - np.array(RANK_MU)[rank]) + rng.normal(0, 0.6, n_games)
    s = np.where(lead < -0.6, -1, np.where(lead > 0.6, 1, 0))
    wt = dict(w)
    if truth_shift is not None:
        wt["beta"] = w["beta"] + truth_shift
    eff = effects(wt, s, z)                                # (games, items)
    q = sc.q0 + sc.q1 * (z - RANK_MU[1])
    owned = np.zeros((n_games, w["n"]), bool)
    bought_phase = np.full((n_games, w["n"]), -1)
    # longer phases are reached less often in stomps (survivorship)
    reach = np.ones((n_games, 4), bool)
    reach[:, 3] = rng.random(n_games) < sigmoid(1.2 - 1.3 * np.abs(lead))
    reach[:, 2] = reach[:, 3] | (rng.random(n_games) < sigmoid(2.5 - 1.0 * np.abs(lead)))
    nw = np.zeros((n_games, 4))
    for p in range(4):
        nw[:, p] = NW_BASE[p] * np.exp(0.25 * lead + rng.normal(0, 0.12, n_games))
        allowed = np.isin(w["tier"], TIERS[p])[None, :] & ~owned & reach[:, [p]]
        util = w["habit"][None, :] + q[:, None] * eff + w["lam"][None, :] * lead[:, None] + rng.gumbel(size=eff.shape)
        util[~allowed] = -np.inf
        top = np.argsort(-util, axis=1)[:, :BUYS[p]]
        rows = np.arange(n_games)[:, None]
        ok = np.isfinite(util[rows, top])
        owned[rows, top] |= ok
        bp = bought_phase[rows, top]
        bought_phase[rows, top] = np.where(ok & (bp < 0), p, bp)
    logit = 1.0 * d + 0.5 * lead + (eff * owned).sum(1)
    win = rng.random(n_games) < sigmoid(logit)
    return dict(rank=rank, z=z, s=s, lead=lead, nw=nw, bought_phase=bought_phase, win=win, reach=reach)


# ------------------------------------------------------------------ estimators (mirror build.py)
def phase_tables(g, w, mask):
    """Per phase: matches/wins per item, NW-adjusted win rate, reached games."""
    out = []
    for p in range(4):
        bp = (g["bought_phase"] == p) & mask[:, None]
        n = bp.sum(0).astype(float)
        wins = (bp & g["win"][:, None]).sum(0).astype(float)
        reached = float((g["reach"][:, p] & mask).sum())
        # API-style adjusted win rate: item WR per NW bucket, reweighted to the phase NW mix
        nwp = g["nw"][:, p]
        rows = bp.any(1)
        if rows.sum() > NW_BUCKETS * 5:
            edges = np.quantile(nwp[rows], np.linspace(0, 1, NW_BUCKETS + 1)[1:-1])
            b = np.digitize(nwp, edges)
            mix = np.array([(bp[b == k]).sum() for k in range(NW_BUCKETS)], float)
            mix /= max(mix.sum(), 1)
            raw = (wins + 1) / (n + 2)
            adj = np.zeros(w["n"])
            for k in range(NW_BUCKETS):
                nk = bp[b == k].sum(0)
                wk = (bp[b == k] & g["win"][b == k][:, None]).sum(0)
                adj += mix[k] * (wk + 5 * raw) / (nk + 5)
        else:
            adj = (wins + 1) / (n + 2)
        out.append(dict(n=n, wins=wins, adj=adj, reached=reached))
    return out


def tier_lift(tab, w):
    lift = np.zeros(w["n"])
    for t in range(1, 5):
        m = (w["tier"] == t) & (tab["n"] > 0)
        if m.any():
            base = (tab["adj"][m] * tab["n"][m]).sum() / tab["n"][m].sum()
            lift[m] = tab["adj"][m] - base
    return lift


def ladder(obs, k):
    est = 0.0 if np.ndim(obs[0][0]) == 0 else np.zeros_like(obs[0][0])
    for v, n in obs:
        est = (n * v + k * est) / (n + k)
    return est


def estimate(g, w, state_mask=None, k_wr=None, user_rank=2):
    """-> per phase: lift (same-tier, NW-adjusted, ladder-shrunk), pick (pro-leaning), old winner-build-rate.
    The ladder climbs only up to the user's own rank (a low-rank user gets all-rank data)."""
    levels = [np.ones_like(g["win"]), g["rank"] >= 1, g["rank"] >= 2][:user_rank + 1]
    res = []
    tabs = [phase_tables(g, w, lv if state_mask is None else lv & state_mask) for lv in levels]
    for p in range(4):
        lift = ladder([(tier_lift(t[p], w), t[p]["n"]) for t in tabs], k_wr or K_WR)
        pick = ladder([(t[p]["n"] / max(t[p]["reached"], 1), t[p]["reached"]) for t in tabs], K_PICK)
        winners = g["win"] & (g["reach"][:, p]) & (state_mask if state_mask is not None else True)
        old = ((g["bought_phase"] == p) & winners[:, None]).sum(0) / max(winners.sum(), 1)
        raw_lift = tabs[0][p]["wins"] / np.maximum(tabs[0][p]["n"], 1) - g["win"].mean()
        res.append(dict(lift=lift, pick=pick, old=old, raw=raw_lift, n=tabs[-1][p]["n"],
                        lift_all=tier_lift(tabs[0][p], w), pick_all=tabs[0][p]["n"] / max(tabs[0][p]["reached"], 1)))
    return res


def reliability(pick):
    return pick / (pick + 0.05)


# ------------------------------------------------------------------ evaluation
MODES = ["old (winners' build rate)", "raw win rate", "popular (pro pick)", "value", "value w/o reliability",
         "blend", "blend + state", "value + state", "value, all ranks pooled"]
TOP_K = 3      # the mod shows a list; score the top 3 it puts in front of the player


def scores(est, est_state, p):
    e = est[p]
    val = e["lift"] * reliability(e["pick"])
    out = {
        MODES[0]: e["old"],
        MODES[1]: e["raw"] * (e["n"] > 30),
        MODES[2]: e["pick"],
        MODES[3]: val,
        MODES[4]: e["lift"],
        MODES[5]: e["pick"] + 4.0 * val,
        MODES[8]: e["lift_all"] * reliability(e["pick_all"]),
    }
    if est_state is not None:
        es = est_state[p]
        # state table shrunk toward the unconditional one by its own sample size
        lift_s = (es["n"] * es["lift"] + K_STATE * e["lift"]) / (es["n"] + K_STATE)
        pick_s = (es["n"] * es["pick"] + K_STATE * e["pick"]) / (es["n"] + K_STATE)
        out[MODES[6]] = pick_s + 4.0 * lift_s * reliability(pick_s)
        out[MODES[7]] = lift_s
    return out


def evaluate(sc, w, g, rng, truth_shift=None):
    k = sc.extra.get("k_wr")
    ur = sc.user_rank if sc.extra.get("ladder_to_user_rank") else 2
    est = estimate(g, w, k_wr=k, user_rank=ur)
    est_by_state = {s: estimate(g, w, g["s"] == s, k_wr=k, user_rank=ur) for s in STATES}
    wt = dict(w)
    if truth_shift is not None:
        wt["beta"] = w["beta"] + truth_shift
    zuser = RANK_MU[sc.user_rank]
    res = {m: {s: [] for s in STATES} for m in MODES}
    for s_true in STATES:
        # the mod reads the state; with prob state_noise it reads a wrong one
        for p in range(4):
            options = np.where(np.isin(w["tier"], TIERS[p]))[0]
            truth = effects(wt, np.array([s_true]), np.array([zuser]))[0][options]
            best, mean = np.sort(truth)[-TOP_K:].mean(), truth.mean()
            for s_read, prob in state_read_probs(s_true, sc.state_noise):
                sc_map = scores(est, est_by_state[s_read], p)
                for m, sv in sc_map.items():
                    gain = truth[np.argsort(-sv[options])[:TOP_K]].mean()
                    res[m][s_true].append((prob, (gain - mean) / max(best - mean, 1e-9)))
    out = {}
    for m in MODES:
        per_state = {s: sum(p * v for p, v in res[m][s]) / sum(p for p, _ in res[m][s]) for s in STATES}
        out[m] = per_state
    return out


def state_read_probs(s_true, noise):
    if noise <= 0:
        return [(s_true, 1.0)]
    others = [s for s in STATES if s != s_true]
    return [(s_true, 1 - noise)] + [(s, noise / 2) for s in others]


def run(sc, reps, seed=0):
    agg = {m: {s: [] for s in STATES} for m in MODES}
    for r in range(reps):
        rng = np.random.default_rng(seed + 1000 * r)
        w = make_world(sc, rng)
        shift = rng.normal(0, sc.drift, w["n"]) if sc.drift else None
        if shift is not None and sc.post_patch_share < 1:
            n_post = int(sc.n_games * sc.post_patch_share)
            g_pre = simulate(sc, w, rng, sc.n_games - n_post)
            g_post = simulate(sc, w, rng, n_post, truth_shift=shift)
            g = {k: np.concatenate([g_pre[k], g_post[k]]) for k in g_pre}
        else:
            g = simulate(sc, w, rng, sc.n_games, truth_shift=shift)
        ev = evaluate(sc, w, g, rng, truth_shift=shift)
        for m in MODES:
            for s in STATES:
                agg[m][s].append(ev[m][s])
    return {m: {s: (np.mean(v), np.std(v)) for s, v in d.items()} for m, d in agg.items()}


BASE = Scenario("base", "умеренные смещения, 60k игр героя, пользователь — про")
SCENARIOS = [
    BASE,
    replace(BASE, name="luxury", desc="сильный эффект отбора: 3 бесполезных T3/T4 покупают, когда уже выигрывают", luxury_lambda=3.0, luxury_items=5),
    replace(BASE, name="rare_hero", desc="редкий герой: 4k игр в окне", n_games=4000),
    replace(BASE, name="very_rare", desc="очень редкий герой: 1k игр", n_games=1000),
    replace(BASE, name="comeback", desc="6 предметов сильны при отставании и слабы при преимуществе", comeback_items=6),
    replace(BASE, name="comeback+misread", desc="то же, но мод путает состояние в 30% случаев", comeback_items=6, state_noise=0.3),
    replace(BASE, name="low_rank_user", desc="эффект зависит от скилла, пользователь низкого ранга, данные про", rho_sd=0.08, user_rank=0),
    replace(BASE, name="patch_drift", desc="патч изменил силу предметов, после него 20% окна", drift=0.12, post_patch_share=0.2),
    replace(BASE, name="patch_fresh", desc="тот же патч, но окно только после него (как в пайплайне)", drift=0.12, post_patch_share=1.0),
    replace(BASE, name="meta_wrong", desc="сообщество систематически недооценивает сильные предметы", habit_anti=0.8, q0=0.5, q1=0.5),
    replace(BASE, name="flat_items", desc="предметы почти равны по силе (сильный баланс)", beta_sd=0.03),
    # rank effects sized like the real data (pro-vs-all lift sd ~2.2 pp ~ 0.09 logit)
    replace(BASE, name="rank_real_pro_k150", desc="реальный размер разницы по рангам, пользователь — про, K=150 (сейчас)", rho_sd=0.055),
    replace(BASE, name="rank_real_pro_k500", desc="то же, K=500 (эмпирический Байес)", rho_sd=0.055, extra={"k_wr": 500}),
    replace(BASE, name="rank_real_low_k500", desc="реальный размер разницы, пользователь низкого ранга, лестница до его ранга", rho_sd=0.055,
            user_rank=0, extra={"k_wr": 500, "ladder_to_user_rank": True}),
    replace(BASE, name="rank_real_low_pro_data", desc="то же, но пользователю низкого ранга дают про-таблицы (как сейчас)", rho_sd=0.055,
            user_rank=0, extra={"k_wr": 500}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=12)
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--out")
    a = ap.parse_args()
    lines = []
    sel = [s for s in SCENARIOS if not a.only or s.name in a.only.split(",")]
    for sc in sel:
        r = run(sc, a.reps)
        lines.append(f"\n### {sc.name}: {sc.desc}\n")
        lines.append("| режим | отстаёт | наравне | впереди | среднее |")
        lines.append("|---|---|---|---|---|")
        for m in MODES:
            ps = r[m]
            avg = np.mean([ps[s][0] for s in STATES])
            lines.append(f"| {m} | " + " | ".join(f"{100 * ps[s][0]:.0f}% ±{100 * ps[s][1]:.0f}" for s in STATES) + f" | **{100 * avg:.0f}%** |")
        print("\n".join(lines[-(len(MODES) + 3):]), flush=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
