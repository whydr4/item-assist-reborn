"""Per-match report from the mod's "[IAR] {json}" console lines.

Start Deadlock with the launch option -condebug: the console then goes to game/citadel/console.log.
The report shows, for every match: what the panel advised and which score part drove each pick,
what was bought and where it stood in the advice at that moment, skill hints vs. actual level-ups,
the imbue popup outcomes, and script errors.

usage: python log_report.py [console.log] [--all] [--out report.md]
  default: the last game session in the default console.log; --all = every session in the file
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.join(os.path.dirname(__file__), "..")
CONSOLE = r"C:\Program Files (x86)\Steam\steamapps\common\Deadlock\game\citadel\console.log"
# recs "top" row: [id, score, then what each term adds to the score: pick, value, matchup, synergy, counter,
# timing, tier bias; affordable, late, imbue slot]
PARTS = {2: "pick", 3: "value", 4: "matchup", 5: "synergy", 6: "counter", 7: "timing", 8: "tier"}


def load_names():
    t = open(os.path.join(ROOT, "data", "recommender_data.js"), encoding="utf-8").read()
    d, _ = json.JSONDecoder().raw_decode(t, t.index("{"))
    return {int(k): v["n"] for k, v in d["items"].items()}, d.get("heroes", {}), d["items"]


def parse(path):
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            i = ln.find("[IAR] ")
            if i < 0:
                continue
            try:
                events.append(json.loads(ln[i + 6:].strip()))
            except ValueError:
                pass
    return events


def clock(t):
    t = int(t or 0)
    return f"{t // 60}:{t % 60:02d}"


def split_matches(events):
    """A match starts at a 'match' or 'hero' event; events before the first one form a prelude."""
    matches, cur = [], {"events": [], "hero": None}
    for e in events:
        if e["ev"] in ("match", "hero") and cur["events"] and any(x["ev"] in ("recs", "buy") for x in cur["events"]):
            matches.append(cur)
            cur = {"events": [], "hero": cur["hero"]}
        if e["ev"] in ("hero", "match") and e.get("hero"):
            cur["hero"] = e.get("hero")
        cur["events"].append(e)
    if cur["events"]:
        matches.append(cur)
    return [m for m in matches if any(x["ev"] in ("recs", "buy", "skill_up", "imbue") for x in m["events"])]


def driver(row):
    """Score part that adds the most to this item (the logged parts are already weighted and sum to the score)."""
    parts = {name: row[i] for i, name in PARTS.items() if i < len(row)}
    return max(parts, key=lambda k: abs(parts[k] or 0))


def report_match(m, names, heroes, items, out):
    ev = m["events"]
    hero = m["hero"]
    nm = lambda i: names.get(i, str(i)) if isinstance(i, int) else str(i)
    recs = [e for e in ev if e["ev"] == "recs"]
    last = recs[-1] if recs else {}
    out.append(f"## {heroes.get(str(hero), hero)} — band {last.get('band', '?')}, mode {last.get('mode', '?')}")
    en = next((e["enemies"] for e in reversed(ev) if e["ev"] == "enemies"), [])
    lane = next((e["lane"] for e in reversed(ev) if e["ev"] == "lane"), [])
    out.append(f"enemies: {', '.join(heroes.get(str(x), str(x)) for x in en) or '-'}; lane: {', '.join(heroes.get(str(x), str(x)) for x in lane) or '-'}")

    # purchases vs advice
    buys = [e for e in ev if e["ev"] == "buy"]
    out.append("\n### Покупки и место в совете")
    out.append("| время | предмет | место в списке | по оценке | что советовал #1–#3 в тот момент |")
    out.append("|---|---|---|---|---|")
    rec_at = None
    ranks, score_ranks = [], []
    # A component bought in the same few seconds as the upgrade it goes into (Duration Extender with
    # Superior Duration) is part of that one decision, not a choice of its own: shown, not counted.
    buy_ev = [e for e in ev if e["ev"] == "buy"]
    def component_of(e):
        for f in buy_ev:
            comp = (items.get(str(f["item"])) or {}).get("comp") or []
            if f is not e and abs(f["t"] - e["t"]) <= 3 and any(str(c) == str(e["item"]) for c in comp):
                return f["item"]
        return None
    for e in ev:
        if e["ev"] == "recs":
            rec_at = e
        if e["ev"] != "buy":
            continue
        parent = component_of(e)
        if parent:
            out.append(f"| {clock(e['t'])} | {nm(e['item'])} | компонент {nm(parent)} | - | - |")
            continue
        r = e.get("rank", -1)
        ranks.append(r)
        top3 = ", ".join(f"{nm(row[0])} ({driver(row)})" for row in (rec_at or {}).get("top", [])[:3]) if rec_at else "-"
        place = f"#{r + 1} из {e.get('of', '?')}" if r is not None and r >= 0 else "не в списке"
        bs = e.get("byScore")
        by_score = f"#{bs + 1}" if isinstance(bs, int) and bs >= 0 else "-"
        if isinstance(bs, int):
            score_ranks.append(bs)
        out.append(f"| {clock(e['t'])} | {nm(e['item'])} | {place} | {by_score} | {top3} |")
    if ranks:
        n = len(ranks)
        out.append(f"\nСовпадение с советом: в топ-1 {sum(r == 0 for r in ranks)}/{n}, в топ-3 {sum(0 <= r < 3 for r in ranks)}/{n}, "
                   f"в топ-8 {sum(0 <= r < 8 for r in ranks)}/{n}, не советовалось {sum(r < 0 for r in ranks)}/{n}.")
    if score_ranks:
        m = len(score_ranks)
        out.append(f"По оценке (без учёта, хватает ли душ): в топ-1 {sum(r == 0 for r in score_ranks)}/{m}, "
                   f"в топ-3 {sum(0 <= r < 3 for r in score_ranks)}/{m}, в топ-8 {sum(0 <= r < 8 for r in score_ranks)}/{m}.")
    sells = [e for e in ev if e["ev"] == "sell"]
    if sells:
        out.append("Продано: " + ", ".join(f"{nm(e['item'])} ({clock(e['t'])})" for e in sells))

    # advice timeline
    out.append("\n### Что советовал мод (при каждом изменении списка)")
    out.append("| время | души | фаза | топ-5 (главная причина) |")
    out.append("|---|---|---|---|")
    for e in recs:
        top = "; ".join(f"{nm(row[0])}{'' if row[9] else '*'} [{driver(row)}]" for row in e.get("top", [])[:5])
        out.append(f"| {clock(e['t'])} | {e.get('souls')} | {e.get('phase')} | {top} |")
    out.append("(* = пока не по карману; причина = самая большая составляющая оценки)")

    # skills
    # Not counted: level-ups with no hint (the order in the data ran out) and bursts of several slots in
    # one tick (the HUD re-reading all levels at a map change or the post-match screen).
    raw = [e for e in ev if e["ev"] == "skill_up"]
    per_tick = Counter(e["hb"] for e in raw)
    ups = [e for e in raw if e.get("hinted") and per_tick[e["hb"]] == 1]
    if raw:
        fol = sum(e.get("followed", 0) for e in ups)
        skipped = len(raw) - len(ups)
        out.append(f"\n### Прокачка: следовал подсказке {fol}/{len(ups)}" + (f" (не учтено {skipped}: без подсказки или пачкой)" if skipped else ""))
        out.append(", ".join(f"{clock(e['t'])} A{e['slot']}" + ("" if e.get("followed") else f" (советовал A{e.get('hinted') or '-'})") for e in ups))

    # imbue
    imb = [e for e in ev if e["ev"] in ("imbue", "imbue_panel", "imbue_missed")]
    if imb:
        out.append("\n### Окно выбора умения (imbue)")
        for e in imb:
            if e["ev"] == "imbue_panel":
                out.append(f"- {clock(e['t'])} окно найдено (магазин {'открыт' if e.get('shopOpen') else 'закрыт'})")
                continue
            if e["ev"] == "imbue_missed":
                shown = ", ".join(f"{c.get('text')!r} ({c.get('root')}, {'видно' if c.get('vis') else 'скрыто'})" for c in e.get("cands", []))
                out.append(f"- {clock(e['t'])} {nm(e.get('item'))}: окно за 3 с не появилось — "
                           + (f"в менеджере окон: {shown}" if shown else "в менеджере окон ни одного окна выбора (игра его не показала)"))
                continue
            share = f", {round(e['share'] * 100)}% сильных, n={e.get('n')}" if e.get("share") is not None else ""
            out.append(f"- {clock(e['t'])} {nm(e.get('item')) if e.get('item') else repr(e.get('text'))}: "
                       f"слот {e.get('slot') or '-'}{share} → **{e.get('result')}** "
                       f"(магазин {'открыт' if e.get('shopOpen') else 'закрыт'}, кнопок {e.get('kids')})")

    errs = [e for e in ev if e["ev"] == "error"]
    if errs:
        out.append("\n### Ошибки скрипта")
        for where, c in Counter((e["where"], e["msg"]) for e in errs).most_common():
            out.append(f"- {where[0]}: {where[1]} ×{c}")
    eb = [e for e in ev if e["ev"] == "enemy_buy"]
    if eb:
        out.append(f"\nПокупки врагов замечено: {len(eb)} (из них с данными для контры: {sum(e.get('counters', 0) for e in eb)})")
    out.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default=CONSOLE)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    names, heroes, items = load_names()
    events = parse(a.log)
    if not events:
        sys.exit(f"no [IAR] lines in {a.log} (is the game started with -condebug and log on in settings.json?)")
    starts = [i for i, e in enumerate(events) if e["ev"] == "start"]
    if not a.all and starts:
        events = events[starts[-1]:]
    out = [f"# Item Assist Reborn — лог ({len(events)} событий)\n"]
    st = next((e for e in events if e["ev"] == "start"), None)
    if st:
        out.append(f"данные: клиент {st.get('client')}, собраны {st.get('data')}, матчи с {st.get('since')}; настройки {st.get('cfg')}\n")
    for m in split_matches(events):
        report_match(m, names, heroes, items, out)
    text = "\n".join(out)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
