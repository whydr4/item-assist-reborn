"""Every API query the build needs, in one place. collect.py warms the cache; build.py reads it."""
import time

from api import get

# Rank ladder used for shrinkage: PRO estimates lean on HIGH, HIGH on ALL.
# Badge = tier*10 + subtier; 9x = Phantom, 10x = Ascendant, 11x = Eternus.
LEVELS = {                      # (min badge, max badge)
    "all": (None, None), "high": (90, None), "pro": (100, None),
    "r_low": (None, 39), "r_mid": (40, 69), "r_upper": (70, 89),
}
VS_LEVELS = ("all", "high")     # per-enemy splits are too thin at PRO
# Rank bands the player picks in the mod; each is a shrinkage ladder ending at that band's matches.
# Real pro-vs-all item lifts differ by ~2.2 pp (research/rank_consistency.py), so a player should get
# tables from their own rank, not the pros'. Shares of matches since 2026-09-29: 34/30/20/16%.
BANDS = {
    "low": ("all", "r_low"),            # Initiate - Alchemist
    "mid": ("all", "r_mid"),            # Arcanist - Emissary
    "high": ("all", "r_upper"),         # Archon - Oracle
    "pro": ("all", "high", "pro"),      # Phantom+ (leaning on Ascendant+)
}
BAND_NAMES = {"low": "Initiate-Alchemist", "mid": "Arcanist-Emissary", "high": "Archon-Oracle", "pro": "Phantom+"}
DEFAULT_BAND = "mid"            # the median player is a Ritualist


def active_heroes():
    return [h for h in get("/v1/assets/heroes", cached=False)
            if h.get("player_selectable") and not h.get("disabled") and not h.get("in_development")]


def assets_items():
    return get("/v1/assets/items", cached=False)


def last_big_patch():
    """Unix time of the latest big gameplay patch (data before it describes a different game)."""
    days = get("/v1/patches/big-days", cached=False)
    return int(time.mktime(time.strptime(days[0][:19], "%Y-%m-%dT%H:%M:%S"))) - time.timezone


class Q:
    def __init__(self, since):
        self.since = since

    def _f(self, level):
        lo, hi = LEVELS[level]
        return {"min_unix_timestamp": self.since, "min_average_badge": lo, "max_average_badge": hi}

    def hero_stats(self, level):
        return get("/v1/analytics/hero-stats", **self._f(level))

    def flow(self, hero, level):
        return get("/v1/analytics/item-flow-stats", hero_ids=hero, min_matches=1, **self._f(level))

    def items(self, hero, level):
        return get("/v1/analytics/item-stats", hero_ids=hero, min_matches=1, **self._f(level))

    def timing(self, hero, level):
        return get("/v1/analytics/item-stats", hero_ids=hero, bucket="game_time_min", min_matches=1, **self._f(level))

    def vs(self, hero, enemy, level):
        return get("/v1/analytics/item-stats", hero_ids=hero, enemy_hero_ids=enemy, min_matches=1, **self._f(level))

    def ability_order(self, hero, level):
        return get("/v1/analytics/ability-order-stats", hero_id=hero, **self._f(level))

    def all_calls(self, hero_ids):
        """(fn, args) for every query, cheapest first so a partial run is still useful."""
        calls = [(self.hero_stats, (lv,)) for lv in LEVELS]
        for h in hero_ids:
            calls += [(self.flow, (h, lv)) for lv in LEVELS]
            calls += [(self.items, (h, lv)) for lv in LEVELS]
            calls += [(self.timing, (h, lv)) for lv in ("all", "high")]
            calls += [(self.ability_order, (h, "high"))]
        for h in hero_ids:
            calls += [(self.vs, (h, e, lv)) for e in hero_ids if e != h for lv in VS_LEVELS]
        return calls
