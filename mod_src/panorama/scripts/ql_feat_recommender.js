// ql_feat_recommender.js — Deadlock in-shop item recommender (runtime)
// Loaded via the hud_health override. When the hero shop is open it shows a
// floating panel at the hero's edge with the recommended items to buy (hero +
// phase auto-detected). A ⚙ gear opens a config menu (knobs). No network.
(function () {
    "use strict";

    // ---- CLOCK (1s tick; drives coach timing) -------------------------------
    var _hb = 0;
    function heartbeat() { _hb++; $.Schedule(1.0, heartbeat); }
    heartbeat();

    if (typeof QOL_REC_DATA === "undefined") return;
    var DATA = QOL_REC_DATA;

    // Reverse lookup: item display-name (lowercased) -> id. Owned items are read from
    // the shop BY NAME; synergy (syn) is keyed by item id, so we map owned names -> ids.
    var _nameToId = null;
    function nameToId(name) {
        if (!_nameToId) {
            _nameToId = {};
            if (DATA.items) for (var id in DATA.items) if (DATA.items.hasOwnProperty(id)) {
                var n = DATA.items[id] && DATA.items[id].n; if (n) _nameToId[String(n).toLowerCase()] = id;
            }
        }
        return name ? _nameToId[String(name).toLowerCase()] : undefined;
    }

    // Component coverage: buying an upgrade (e.g. Capacitor) folds in its component items
    // (Tesla Bullets) — Deadlock stops offering the component, but our feed/scan only logs the
    // PARENT's name, so the component never enters `owned` and keeps showing as a rec. Walk each
    // owned item's meta.comp (direct component ids) transitively and flag every id it covers, so
    // computeRecs can strike them. IDs only — kept out of the owned name-map so effectiveCost's
    // component discount (which needs a free-standing owned component) is unaffected.
    function markCovered(id, out) {
        var meta = DATA.items && DATA.items[String(id)];
        var comp = meta && meta.comp; if (!comp) return;
        for (var i = 0; i < comp.length; i++) {
            var cid = String(comp[i]);
            if (!out[cid]) { out[cid] = 1; markCovered(cid, out); }   // recurse: components of components
        }
    }
    function coveredComponents(owned) {
        var out = {};
        for (var nm in owned) if (owned.hasOwnProperty(nm)) { var oid = nameToId(nm); if (oid) markCovered(oid, out); }
        return out;
    }

    // Per-hero item tip (why buy this on this hero), summarized from real player build notes and
    // baked by 13_export.py as DATA.tips[hero_id][item_id]. Returns "" when no tip exists (older
    // data or an item nobody annotated) so the hover UI simply shows nothing.
    function tipFor(hero, itemId) {
        var t = DATA.tips && DATA.tips[String(hero)];
        return (t && t[String(itemId)]) || "";
    }

    // ---- CONFIG (knobs, persisted) -----------------------------------------
    var CFG = { count: 8, refreshSec: 3, opacity: 90, side: "left", mode: "popular", showPct: true, min: false, holding: true, holdCount: 3, coach: false, gubami: false, skills: false, buy: true, counters: true, tips: false };
    var GUBAMI_ICON = "s2r://panorama/images/gubami/gubami_coach.vtex";   // bundled custom portrait (mod asset)
    function loadCfg() {
        var s = null;
        try { s = $.persistentStorage.getItem("qlrec_cfg"); } catch (e) {  }
        try { if (s) { var o = JSON.parse(s); for (var k in o) if (CFG.hasOwnProperty(k)) CFG[k] = o[k]; } } catch (e) {}
        if (CFG.mode === "balanced" || CFG.mode === "smart") CFG.mode = "popular";
    }
    function saveCfg() {
        try {
            $.persistentStorage.setItem("qlrec_cfg", JSON.stringify(CFG));
            var chk = $.persistentStorage.getItem("qlrec_cfg");
        } catch (e) {  }
    }
    loadCfg();

    function phaseIndex(tSec) { return tSec < 600 ? 0 : (tSec < 1500 ? 1 : 2); }
    // Item-flow phases baked by pipeline/build.py (0-9 / 9-20 / 20-30 / 30+ min): recp[hero][phase] =
    // [item, lift*1000, pick*1000]. Lift is net-worth-adjusted (rich players winning anyway is factored
    // out) and pick is how often strong players buy it in that phase. Indexed lazily per hero.
    function flowPhase(tSec) {
        var ends = (DATA.meta && DATA.meta.phase_end_min) || [9, 20, 30], m = (tSec || 0) / 60;
        for (var i = 0; i < ends.length; i++) if (m < ends[i]) return i;
        return ends.length;
    }
    var _phaseIdx = {};
    function phaseStats(hero, ph) {
        var key = hero + ":" + ph;
        if (_phaseIdx[key]) return _phaseIdx[key];
        var out = {}, rows = DATA.recp && DATA.recp[String(hero)] && DATA.recp[String(hero)][ph];
        if (rows) for (var i = 0; i < rows.length; i++) out[String(rows[i][0])] = [rows[i][1] / 1000, rows[i][2] / 1000];
        _phaseIdx[key] = out;
        return out;
    }
    // Buy-timing: tm[hero][item] = [goodUntilMin|null, lift*1000 per timing bin]. Past goodUntil the
    // item stops paying off compared with same-tier items bought at that minute -> penalize + flag.
    function timingInfo(hero, id, tSec) {
        var t = DATA.tm && DATA.tm[String(hero)] && DATA.tm[String(hero)][id];
        if (!t) return null;
        var bin = Math.min(t[1].length - 1, Math.floor((tSec || 0) / 60 / ((DATA.meta && DATA.meta.timing_bin_min) || 5)));
        var lift = t[1][bin];
        return { until: t[0], lift: lift == null ? 0 : lift / 1000, late: t[0] != null && (tSec || 0) / 60 >= t[0] };
    }

    // ---- HERO AUTO-DETECT (from the shop's own header labels) ---------------
    var _heroDisplays = null, _diagDumped = false;
    function heroDisplaysSorted() {
        if (_heroDisplays) return _heroDisplays;
        _heroDisplays = [];
        if (DATA.heroes) for (var id in DATA.heroes) if (DATA.heroes.hasOwnProperty(id)) _heroDisplays.push([DATA.heroes[id], id]);
        _heroDisplays.sort(function (a, b) { return b[0].length - a[0].length; });
        return _heroDisplays;
    }
    function scanTextForDisplay(txt) {
        if (!txt) return null;
        var s = String(txt).toLowerCase(), d = heroDisplaysSorted();   // labels are UPPERCASE (INFERNUS)
        for (var i = 0; i < d.length; i++) if (s.indexOf(d[i][0].toLowerCase()) !== -1) return parseInt(d[i][1], 10);
        return null;
    }
    function deepLabelScan(p, depth, diag) {
        if (!p || depth > 30) return null;
        var t = null; try { t = p.text; } catch (e) {}
        if (t && (t.indexOf("Recommended") !== -1 || t.indexOf(" Build") !== -1 || t.indexOf("’s ") !== -1 || t.indexOf("'s ") !== -1)) {
            var id = scanTextForDisplay(t); if (id) return id;
        }
        var kids = (p.Children ? p.Children() : []) || [];
        for (var i = 0; i < kids.length; i++) { var r = deepLabelScan(kids[i], depth + 1, diag); if (r) return r; }
        return null;
    }
    function getLocalHeroId(shop) {
        if (!shop) return null;
        var hdr = shop.FindChildTraverse && shop.FindChildTraverse("HeroFavoritesHeaderLabel");
        if (hdr) { var hid = scanTextForDisplay(hdr.text); if (hid) return hid; }
        var id = deepLabelScan(shop, 0, false);
        if (id) return id;
        if (!_diagDumped) { _diagDumped = true;  deepLabelScan(shop, 0, true); }
        return null;
    }
    // Enemy comp: top bar groups players by team (CitadelHudTopBarTeam); each player's
    // details has <Label id="HeroName" text="{s:hero_name}">. Read the enemy team's
    // hero names -> ids. (Lane isn't exposed in the HUD, so we use the whole comp.)
    // hero token -> id: display name ("Infernus") OR internal ("hero_inferno", via byName).
    function resolveHeroToken(txt) {
        if (!txt) return null;
        var id = scanTextForDisplay(txt); if (id) return id;
        var s = String(txt).trim().toLowerCase();
        if (DATA.byName) {
            if (DATA.byName[s]) return DATA.byName[s];
            for (var k in DATA.byName) if (DATA.byName.hasOwnProperty(k) && s.indexOf(k) !== -1) return DATA.byName[k];
        }
        return null;
    }
    // QoLLock reads per-player hero from a hidden label class PlayerHeroHidden (reliable;
    // HeroName is its fallback). Read those within a panel.
    function heroesInPanel(t) {
        var out = [], cls = ["PlayerHeroHidden", "HeroName"];
        for (var c = 0; c < cls.length; c++) {
            var labels = (t.FindChildrenWithClassTraverse ? t.FindChildrenWithClassTraverse(cls[c]) : []) || [];
            for (var i = 0; i < labels.length; i++) { var id = resolveHeroToken(labels[i].text); if (id) out.push(id); }
            if (out.length) return out;
        }
        return out;
    }
    function scanEnemies(root, myHeroId) {
        if (!root) return [];
        var en = [];
        // A: two top-bar team panels; the team without my hero = enemies
        var teams = [];
        (function findTeams(p, d) {
            if (!p || d > 30 || teams.length >= 2) return;
            try { if (p.paneltype === "CitadelHudTopBarTeam") { teams.push(p); return; } } catch (e) {}
            var k = (p.Children ? p.Children() : []) || [];
            for (var i = 0; i < k.length; i++) findTeams(k[i], d + 1);
        })(root, 0);
        if (teams.length === 2) {
            var a = heroesInPanel(teams[0]), b = heroesInPanel(teams[1]);
            if (a.length || b.length) en = (a.indexOf(myHeroId) !== -1) ? b : ((b.indexOf(myHeroId) !== -1) ? a : b);
        }
        // B fallback: pair each PlayerHeroHidden with its entry's TeamRelationship label
        if (!en.length) {
            var all = (root.FindChildrenWithClassTraverse ? root.FindChildrenWithClassTraverse("PlayerHeroHidden") : []) || [];
            for (var i = 0; i < all.length; i++) {
                var id = resolveHeroToken(all[i].text); if (!id || id === myHeroId) continue;
                var entry = all[i]; for (var u = 0; u < 6 && entry && entry.GetParent; u++) entry = entry.GetParent();
                var rl = entry && entry.FindChildrenWithClassTraverse ? (entry.FindChildrenWithClassTraverse("TeamRelationship") || []) : [];
                var rel = ""; try { if (rl.length) rel = String(rl[0].text || "").toLowerCase(); } catch (e) {}
                if (rel.indexOf("enem") !== -1 || rel.indexOf("opp") !== -1) en.push(id);
            }
        }
        var seen = {}, out = [];
        for (var j = 0; j < en.length; j++) { if (en[j] && !seen[en[j]]) { seen[en[j]] = 1; out.push(en[j]); } }
        return out;
    }
    // Matchup weight decays after the laning phase (matchup matters most early).
    function enemyWeight(t) { return t < 600 ? 1.0 : (t < 1200 ? 1.0 - 0.6 * (t - 600) / 600 : 0.4); }
    function clamp01(x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
    // "Affluence" 0..1 = how flush/late you are. Ranking by winner build-rate alone
    // floats cheap T1 staples to the top forever (95% of winners buy them), so a 60k-souls
    // late-game player still gets a T1 at #1. As affluence rises we bias toward higher tiers
    // (you've presumably bought the staples and each slot should be high-impact); early/poor
    // leaves T1s on top. Souls OR game time drives it. Real fix is owned detection (task #1);
    // this is the souls/phase proxy for "you're past the cheap-staples stage".
    function affluence(souls, t) {
        var bySouls = souls > 0 ? clamp01((souls - 3000) / 9000) : 0;   // 3k->0 .. 12k+->1
        var byTime = clamp01(((t || 0) - 600) / 1200);                  // 10min->0 .. 30min->1
        return Math.max(bySouls, byTime);
    }
    var TIER_W = 0.28;   // strength of the affluence tier-bias
    // Diagnostic (throttled, few runs): when we have a hero but no enemies resolved,
    // dump what the top-bar scan actually sees so a match run confirms the structure.
    var _enDiagRuns = 0, _enDiagHb = -99;
    function enemyDiag(root) {
        if (!root || _enDiagRuns >= 3 || (_hb - _enDiagHb) < 6) return;
        _enDiagHb = _hb; _enDiagRuns++;
        function inv(cls) {
            var l = (root.FindChildrenWithClassTraverse ? root.FindChildrenWithClassTraverse(cls) : []) || [];
            var txt = [];
            for (var i = 0; i < l.length && i < 14; i++) { var t = ""; try { t = String(l[i].text || "").substr(0, 16); } catch (e) {} txt.push(t); }
            return l.length + " [" + txt.join("|") + "]";
        }
        var teams = 0, players = 0;
        (function c(p, d) {
            if (!p || d > 34) return;
            try { if (p.paneltype === "CitadelHudTopBarTeam") teams++; if (p.paneltype === "CitadelHudTopBarPlayer") players++; } catch (e) {}
            var k = (p.Children ? p.Children() : []) || []; for (var i = 0; i < k.length; i++) c(k[i], d + 1);
        })(root, 0);
        // per-team resolution (confirms grouping + case fix)
        var tp = [];
        (function ft(p, d) { if (!p || d > 30 || tp.length >= 2) return; try { if (p.paneltype === "CitadelHudTopBarTeam") { tp.push(p); return; } } catch (e) {} var k = (p.Children ? p.Children() : []) || []; for (var i = 0; i < k.length; i++) ft(k[i], d + 1); })(root, 0);
        for (var ti = 0; ti < tp.length; ti++) {
            var lb = (tp[ti].FindChildrenWithClassTraverse ? tp[ti].FindChildrenWithClassTraverse("HeroName") : []) || [];
            var nm = []; for (var i = 0; i < lb.length && i < 8; i++) { var id = null; try { id = resolveHeroToken(lb[i].text); } catch (e) {} nm.push((lb[i].text || "?") + "->" + (id || "?")); }
        }
    }

    // === LIVE ENEMY-PURCHASE FEED (drives data-driven counters) ===
    // The game's shop layout (citadel_hud_hero_shop.xml) has a NATIVE, READABLE purchase feed
    // (verified in-match) that QoLLock's "recent purchases" also reads — NOT the top-bar
    // mod_universal icons (those have native-set/unreadable src). Structure per entry:
    //   RecentPurchasesContainer > .recentPurchase >
    //     .recentTimePurchased  (time text; dedup key with buyer+item),
    //     CitadelHeroImage#RecentPurchaseHeroImage (portrait; heroid NOT exposed),
    //     .recentModPurchaseName (item NAME text), .recentModTierCost (tier),
    //     .recentModPurchaserHero (BUYER hero display name — readable),
    //   + game-set state classes: team isTeam1/2Purchase, cat isWeapon/Armor/TechPurchase,
    //     tier isTier1..4Purchase. It's a LIVE feed (populates shop-closed) and a rolling ~50 cap
    //     newest-first (old entries scroll off), so we ACCUMULATE (dedup buyer|item|time).
    // We collect the ENEMY team's item ids (entry team class != local team) into _enemyItems and
    // feed them to computeRecs' counter term. See [[reference-deadlock-recent-purchases-feed]].
    var _enemyItems = {};   // enemy item id(str) -> 1, accumulated this match (reset on hero change)
    var _rpSeen = {};       // dedup: "buyer|item|time" -> 1
    var _rpFound = false, _rpEnemySig = "";
    var _recentContCache = null;   // cached RecentPurchasesContainer handle (self-heals via IsValid)
    function _rpText(p, cls) {
        var l = (p && p.FindChildrenWithClassTraverse) ? p.FindChildrenWithClassTraverse(cls) : [];
        return (l && l.length) ? String(l[0].text || "").trim() : "";
    }
    function localTeamNum(root) {
        if (!root) return 0;
        if (root.BHasClass && root.BHasClass("localPlayerTeam1")) return 1;
        if (root.BHasClass && root.BHasClass("localPlayerTeam2")) return 2;
        if (findByAny(root, "localPlayerTeam1", 0)) return 1;
        if (findByAny(root, "localPlayerTeam2", 0)) return 2;
        return 0;   // spectator / unknown -> can't tell friend from foe, skip
    }
    function collectRecent(root, shop) {
        if (!root) return;   // always run: owned-tracking is independent of the Counters toggle
        // Cache the feed container: this fn runs every tick (shop open AND closed), so re-traversing the whole
        // HUD each time to locate it added directly to the per-second hitch.
        var cont = valid(_recentContCache) ? _recentContCache
            : (root.FindChildTraverse && root.FindChildTraverse("RecentPurchasesContainer"));
        _recentContCache = valid(cont) ? cont : null;
        if (!cont || !cont.FindChildrenWithClassTraverse) return;
        var lt = localTeamNum(root);
        if (!lt) return;   // no local team -> can't classify friend/foe
        var entries = cont.FindChildrenWithClassTraverse("recentPurchase") || [];
        if (!_rpFound && entries.length) { _rpFound = true;  }
        var enemyAdded = 0, ownedAdded = 0;
        for (var i = 0; i < entries.length; i++) {
            var e = entries[i];
            var item = _rpText(e, "recentModPurchaseName"); if (!item) continue;
            var buyer = _rpText(e, "recentModPurchaserHero");
            var key = buyer + "|" + item + "|" + _rpText(e, "recentTimePurchased");
            // The feed is newest-first with a rolling cap: the first entry we've ALREADY seen means
            // every entry below it is older and also seen -> stop. Steady state (no new buys) scans ~1
            // entry instead of ~50, removing the per-second FindChildrenWithClassTraverse storm that
            // was the 1-second stutter.
            if (_rpSeen[key]) break; _rpSeen[key] = 1;   // one purchase event = buyer+item+time
            var entryTeam = (e.BHasClass && e.BHasClass("isTeam1Purchase")) ? 1 : ((e.BHasClass && e.BHasClass("isTeam2Purchase")) ? 2 : 0);
            if (!entryTeam) continue;
            if (entryTeam !== lt) {                                   // ENEMY team -> counter input
                var id = nameToId(item);
                if (id && !_enemyItems[String(id)]) { _enemyItems[String(id)] = 1; enemyAdded++; }
            } else if (_heroId && resolveHeroToken(buyer) === _heroId) {   // OUR hero's own buy -> owned
                // Tab-INDEPENDENT owned detection: the feed logs every one of our purchases with its
                // item NAME, so we no longer depend on having opened the item's shop tab (the flaky
                // mod_view scan). We union into the SAME _ownedAccum the tab scan uses (accumulate-only,
                // reset on hero change). Limitation (same as the tab scan today): it's a purchase STREAM,
                // so sold items stay struck until hero change; consumed components read as owned like now.
                var lc = item.toLowerCase();
                if (!_ownedAccum[lc]) { _ownedAccum[lc] = 1; ownedAdded++; }
            }
        }
        if (enemyAdded && CFG.counters) {
            var names = []; for (var k in _enemyItems) if (_enemyItems.hasOwnProperty(k)) names.push((DATA.items[k] && DATA.items[k].n) || k);
            var sig = names.slice().sort().join(",");
            if (sig !== _rpEnemySig) { _rpEnemySig = sig;  }
        }
        if ((enemyAdded || ownedAdded) && shop && valid(_float)) refreshFloat(root, shop);   // strike/re-rank now
    }

    function getGameTimeSec(root) {
        try { if (typeof Game.GetGameTime === "function") return Math.floor(Game.GetGameTime()); } catch (e) {}
        var g = root && root.FindChildTraverse && (root.FindChildTraverse("HudGameTime") || root.FindChildTraverse("GameTime"));
        return g && g.text ? parseClock(g.text) : 0;
    }
    var _goldLabelCache = null;
    function getSouls(root) {
        // hudCurGoldLabel = {i:hud_cur_gold} = current spendable souls (from
        // hud_gold_and_ap_container.xml). Bound labels return their rendered number.
        // Cache the label handle — getSouls runs every second (coachCheck when shop closed, refreshFloat
        // when open) and FindChildTraverse is a full-tree walk. The handle self-heals via valid() when
        // the HUD rebuilds on a new match (same pattern as _shopCache / _recentContCache).
        if (!root || !root.FindChildTraverse) return 0;
        var l = valid(_goldLabelCache) ? _goldLabelCache : root.FindChildTraverse("hudCurGoldLabel");
        _goldLabelCache = valid(l) ? l : null;
        if (l && l.text) { var n = parseInt(String(l.text).replace(/[^0-9]/g, ""), 10); if (!isNaN(n)) return n; }
        return 0;   // hideout shows ∞ -> NaN -> 0 (no affordability filter, fine)
    }
    // Owned items: each shop entry is CitadelShopMod.mod_view with a .modName label and a
    // .purchasedIcon checkmark shown when owned (citadel_shop_mod_view.xml). Collect owned item
    // NAMES (our recs are keyed by name too) so we can strike them. The modName text + purchasedIcon
    // only resolve for the ACTIVE category tab, so a single scan sees only that tab's owned items;
    // we ACCUMULATE across scans (union; reset on hero change) and never remove per-panel (a
    // readable-but-unclassed copy isn't proof of a sale, so removing false-drops owned items). If an
    // owned item is in a tab you haven't opened, it won't be detected until you visit that tab — the
    // Help tip tells the player to open the shop's Search tab (shows every item) to catch them all.
    var _ownedAccum = {}, _ownedDiagN = 0;
    function getOwnedNames(shop) {
        if (!shop || !shop.FindChildrenWithClassTraverse) return _ownedAccum;
        var entries = shop.FindChildrenWithClassTraverse("mod_view") || [];
        var nOwn = 0, nComp = 0, nVis = 0;
        for (var i = 0; i < entries.length; i++) {
            var e = entries[i], own = false, isComp = false, vis = false;
            if (!e.FindChildrenWithClassTraverse) continue;
            try { if (e.BHasClass) { own = e.BHasClass("owned"); isComp = e.BHasClass("usedAsComponent"); } } catch (x) {}
            if (!own && !isComp) {   // FALLBACK: visible checkmark (current tab only)
                var pics = e.FindChildrenWithClassTraverse("purchasedIcon") || [];
                for (var j = 0; j < pics.length; j++) { try { if (pics[j].visible) { vis = true; break; } } catch (x) {} }
            }
            if (own) nOwn++; if (isComp) nComp++; if (vis) nVis++;
            if (!(own || isComp || vis)) continue;               // accumulate-only: never remove here
            var nms = e.FindChildrenWithClassTraverse("modName") || [];
            for (var k = 0; k < nms.length; k++) { var t = ""; try { t = String(nms[k].text || "").replace(/^\s+|\s+$/g, ""); } catch (x) {} if (t) _ownedAccum[t.toLowerCase()] = 1; }
        }
        if (_ownedDiagN < 8) { _ownedDiagN++;  }
        return _ownedAccum;
    }

    function parseClock(txt) { var m = String(txt).match(/(\d+):(\d+)/); return m ? (+m[1]) * 60 + (+m[2]) : 0; }
    // Component itemization: an item built from components you ALREADY OWN costs only
    // the difference (e.g. Sharpshooter 3200 with owned Long Range + High-Velocity Rounds
    // costs 800). meta.comp = component item ids; subtract each owned component's cost.
    function effectiveCost(meta, owned) {
        var c = meta.c;
        if (meta.comp) for (var i = 0; i < meta.comp.length; i++) {
            var cm = DATA.items[String(meta.comp[i])];
            if (cm && cm.n && owned[String(cm.n).toLowerCase()]) c -= (cm.c || 0);
        }
        return c < 0 ? 0 : c;
    }

    // ---- CLICK-TO-BUY -------------------------------------------------------
    // Buy a recommended item by activating the game's OWN shop panel for it: find the
    // CitadelShopMod (.mod_view) whose .modName matches the item, then dispatch the same
    // "Activated" event a real left-click sends -> native buy (QoLLock uses this dispatch).
    // All four category lists (Weapon/Armor/Tech/All) are persistent siblings, so the panel
    // is findable regardless of the active tab; prefer a visible copy (active tab) if present.
    function findModView(shop, name) {
        if (!shop || !shop.FindChildrenWithClassTraverse) return null;
        var entries = shop.FindChildrenWithClassTraverse("mod_view") || [];
        var target = String(name).replace(/^\s+|\s+$/g, "").toLowerCase(), vis = null, any = null;
        for (var i = 0; i < entries.length; i++) {
            var e = entries[i]; if (!e.FindChildrenWithClassTraverse) continue;
            var nms = e.FindChildrenWithClassTraverse("modName") || [];
            for (var k = 0; k < nms.length; k++) {
                var t = ""; try { t = String(nms[k].text || "").replace(/^\s+|\s+$/g, "").toLowerCase(); } catch (x) {}
                if (t && t === target) { any = any || e; try { if (e.visible) vis = e; } catch (x) {} break; }
            }
        }
        return vis || any;
    }
    // Category nav buttons in citadel_hud_hero_shop.xml (activating one switches the shop tab,
    // which binds that category's modName labels so findModView can see the item).
    var _navFor = { weapon: "WeaponNav", vitality: "ArmorNav", spirit: "TechNav" };
    function _activateBuy(mv, name) {
        var v = "?"; try { v = mv.visible; } catch (e) {}
        try { $.DispatchEvent("Activated", mv, "mouse"); } catch (e) {  }
        $.Schedule(0.4, function () { if (valid(_float)) refreshFloat(hudRoot(), null); });   // re-scan owned -> strike the bought item
    }
    function buyItem(it) {
        if (!it) return;
        var root = hudRoot(), shop = shopIfOpen(root);
        if (!shop) {  return; }
        var mv = findModView(shop, it.name);
        if (mv) { _activateBuy(mv, it.name); return; }
        // Not rendered in the active tab: its modName text only binds when its category tab is
        // shown. Switch to that tab (activate the nav button), then retry the find + buy once.
        var navId = _navFor[it.slot] || "SearchNav";
        var nav = shop.FindChildTraverse(navId);
        if (!nav) {  return; }
        try { $.DispatchEvent("Activated", nav, "mouse"); } catch (e) {}
        $.Schedule(0.15, function () {
            var s = shopIfOpen(hudRoot()); if (!s) return;
            var mv2 = findModView(s, it.name);
            if (mv2) _activateBuy(mv2, it.name);
        });
    }

    // ---- SCORING ------------------------------------------------------------
    // rec[hero] is a flat, value-sorted candidate list (highest win-value first,
    // tier-covered). Show the best AFFORDABLE unowned items first, then the cheapest
    // upcoming ones (dimmed) so it's never empty and reads as "buy now / save toward".
    // Ranking modes (coach-style): "popular" = winner build-rate (robust consensus);
    // "value" = model win-value (impact, but noisier); "balanced" = popularity primary
    // with a win-value tilt. Matchup lift is added as a situational adjustment in all.
    function modeScore(val, pop, matchup, cost) {
        if (CFG.mode === "value") return (val - 0.5) + 0.4 * matchup;      // val is ~0.45-0.57
        // Efficiency: win-value impact PER soul (scaled to the pop/value range so the
        // situational lifts stay comparable). Favours cost-efficient picks over raw impact.
        if (CFG.mode === "efficiency") return (val - 0.5) / Math.max(cost || 1, 1) * 4000 + 0.4 * matchup;
        return pop + 0.4 * matchup;                                        // popular (default)
    }
    var SYN_WEIGHT = 0.8;   // build-so-far synergy strength (comparable to popularity term)
    // Enemy-item counters: DATA.ctr[enemyItemId][myItemId] = duration-stratified winner build-rate
    // lift when the enemy team owns enemyItemId (baked in 13_export.py from real match data — no
    // hand-authored counter rules). Runtime accumulates the enemy team's purchases from the live
    // recent-purchases feed (_enemyItems) and adds CTR_WEIGHT * sum of lifts to each candidate, so
    // items that winners itemize into vs THEIR build float up. Lifts are ~0.04-0.07 (like vs/syn).
    var CTR = DATA.ctr || {};
    var CTR_WEIGHT = 1.0;
    var TIMING_WEIGHT = 1.5;   // buy-time lift (~±0.03) -> comparable to the matchup/synergy terms
    var _lastCand = [];     // full score-sorted candidate list from the last computeRecs (for Soul-holding)
    // Soul holding: preview the best items in the item TIER you should be saving toward.
    // Target = max of a souls-based tier (<1600->T2, <3200->T3, 6400+->T4) and a time-based
    // tier (<20min->T2, <30min->T3, 30min+->T4), so late game always points at T4 even right
    // after a buy leaves you poor. Always shows that tier's best picks (affordable or not —
    // the ⏳/opacity marks what you can't yet buy).
    function holdTier(souls, t) {
        var sTier = souls < 1600 ? 2 : (souls < 3200 ? 3 : 4);
        var tSec = t || 0, tTier = tSec < 1200 ? 2 : (tSec < 1800 ? 3 : 4);
        return sTier > tTier ? sTier : tTier;
    }
    function computeHolding(souls, t) {
        var empty = { tier: 0, items: [] };
        if (!souls || souls <= 0) return empty;
        var tt = holdTier(souls, t);
        var out = [];
        for (var i = 0; i < _lastCand.length && out.length < CFG.holdCount; i++) {
            var c = _lastCand[i];
            if (c.tier === tt) out.push(c);   // best-first items of the target tier
        }
        return { tier: tt, items: out };
    }
    function computeRecs(hero, souls, owned, enemies, tSec) {
        var list = DATA.rec[String(hero)]; if (!list) return [];
        var vsH = (DATA.vs && DATA.vs[String(hero)]) || {};
        var synH = (DATA.syn && DATA.syn[String(hero)]) || null;
        var imbH = (DATA.imbue && DATA.imbue[String(hero)]) || null;   // item -> ability slot
        // enemy-item counters: the ids the enemy team has bought (from the live feed), whose ctr
        // table entry exists — so the inner loop only iterates enemy items that can lift something.
        var ctrOn = CFG.counters, ctrX = [];
        if (ctrOn) for (var ex in _enemyItems) if (_enemyItems.hasOwnProperty(ex) && CTR[ex]) ctrX.push(ex);
        var w = (enemies && enemies.length) ? enemyWeight(tSec || 0) : 0;
        var ownedCount = 0; for (var o in owned) if (owned.hasOwnProperty(o)) ownedCount++;
        var aff01 = affluence(souls, tSec);   // flush/late -> bias toward higher tiers
        // Owned item ids that drive synergy (map owned names -> ids present in syn table).
        var synDrivers = [];
        if (synH) for (var on in owned) if (owned.hasOwnProperty(on)) { var oid = nameToId(on); if (oid && synH[oid]) synDrivers.push(oid); }
        var covered = coveredComponents(owned);   // component ids folded into an owned upgrade -> strike them too
        var phS = phaseStats(hero, flowPhase(tSec));
        var cand = [];
        for (var k = 0; k < list.length; k++) {
            var id = String(list[k][0]), meta = DATA.items[id];
            if (!meta || owned[meta.n.toLowerCase()] || covered[id]) continue;   // strike owned + owned-component (case-insensitive)
            var adj = 0;                                      // matchup: sum lifts over enemy comp
            for (var e = 0; w && e < enemies.length; e++) { var vE = vsH[String(enemies[e])]; if (vE && vE[id] != null) adj += vE[id]; }
            var syn = 0;                                       // synergy: sum co-build lifts over owned
            for (var d = 0; d < synDrivers.length; d++) { var sD = synH[synDrivers[d]]; if (sD && sD[id] != null) syn += sD[id]; }
            var ctr = 0;                                       // counter: sum lifts over enemy team's purchases
            if (ctrOn) for (var xi = 0; xi < ctrX.length; xi++) { var cX = CTR[ctrX[xi]]; if (cX && cX[id] != null) ctr += cX[id]; }
            var val = list[k][1], pop = (list[k].length > 2 ? list[k][2] : 0);
            var ph = phS[id];                                   // this phase's lift/pick beats the all-game average
            if (ph) { val = 0.5 + ph[0] * ph[1] / (ph[1] + 0.05); pop = ph[1]; }   // lift trusted by adoption (build.py reliability)
            var tm = timingInfo(hero, id, tSec);
            var eff = effectiveCost(meta, owned);   // discount for owned components
            // no flush/late tier-bias in Efficiency mode — it already accounts for cost. With phase data
            // the tier shift over the game is already in pick rates, so the affluence proxy is halved.
            var tierBias = (CFG.mode === "efficiency") ? 0 : (ph ? 0.5 : 1) * TIER_W * aff01 * (meta.t - 2);
            var timeAdj = tm ? TIMING_WEIGHT * tm.lift : 0;
            cand.push({ id: id, name: meta.n, cost: eff, fullCost: meta.c, tier: meta.t, slot: meta.s, icon: meta.ic || "", aff: true, syn: syn, ctr: ctr, pop: pop, late: !!(tm && tm.late), until: tm ? tm.until : null, imb: (imbH ? (imbH[id] || 0) : 0), score: modeScore(val, pop, w * adj, eff) + SYN_WEIGHT * syn + CTR_WEIGHT * ctr + tierBias + timeAdj });
        }
        cand.sort(function (a, b) { return b.score - a.score; });   // re-rank by mode + matchup
        _lastCand = cand;                                            // for Soul-holding preview
        if (souls > 0) {
            var aff = [], soon = [];
            for (var i = 0; i < cand.length; i++) { if (cand[i].cost <= souls) aff.push(cand[i]); else soon.push(cand[i]); }
            soon.sort(function (a, b) { return a.cost - b.cost; });   // cheapest upcoming
            for (var s = 0; s < soon.length; s++) soon[s].aff = false;
            return aff.concat(soon);                          // affordable (value-sorted) then save-toward
        }
        // souls unknown (hideout): cap tier by owned count as a build-progress proxy
        var cap = ownedCount <= 1 ? 2 : (ownedCount <= 4 ? 3 : 4);
        return cand.filter(function (x) { return x.tier <= cap; });
    }

    // ---- UI helpers ---------------------------------------------------------
    function css(p, o) { if (p && p.style) { for (var k in o) if (o.hasOwnProperty(k)) { try { p.style[k] = o[k]; } catch (e) {} } try { if ((p.paneltype === "Label" || o.fontFamily) && !o.fontWeight) p.style.fontWeight = "semi-bold"; } catch (e) {} } }   // labels default to semi-bold weight
    function valid(p) { try { return p && p.IsValid && p.IsValid(); } catch (e) { return false; } }
    // ---- UX: hover-brighten + click sound (matches the game's main-menu buttons) ------
    var SND_CLICK = "UI.MainMenu.Activate";   // main-menu button activate SFX (citadel_base_styles.css)
    var SND_HOVER = "UI.MainMenu.Hover";      // main-menu button hover SFX
    function playClick() { try { $.DispatchEvent("PlaySoundEffect", SND_CLICK); } catch (e) {} }
    function playHover() { try { $.DispatchEvent("PlaySoundEffect", SND_HOVER); } catch (e) {} }
    function withClick(fn) { return function (e) { playClick(); if (fn) fn(e); }; }   // wrap a click handler so it plays the SFX first
    // Brighten a panel's background between baseBg (rest) and hi (hover) + play the hover SFX.
    function hoverFx(panel, baseBg, hi) {
        if (!hi) hi = (baseBg && (baseBg.indexOf("3C9E6A") >= 0 || baseBg.indexOf("5FE69E") >= 0)) ? "#57C489ee" : "#FFFFFF28";   // brighter green for "on", else white overlay
        panel.SetPanelEvent("onmouseover", function () { playHover(); css(panel, { backgroundColor: hi }); });
        panel.SetPanelEvent("onmouseout", function () { css(panel, { backgroundColor: baseBg }); });
    }
    // Row hover: brighten (like hoverFx) AND drive the shared side tip panel (see showTip/hideTip).
    // The tip panel is a SIBLING of the main float (not a child of any row), so it never touches row
    // layout. `it` carries the item; the tip text is looked up per current hero.
    function hoverFxTip(panel, baseBg, hi, it) {
        panel.SetPanelEvent("onmouseover", function () { playHover(); css(panel, { backgroundColor: hi }); showTip(it); });
        panel.SetPanelEvent("onmouseout", function () { css(panel, { backgroundColor: baseBg }); hideTip(); });
    }
    function mkBtn(parent, text, onClick, style) {
        var b = $.CreatePanel("Panel", parent, "");
        css(b, { padding: "3px 9px", marginRight: "4px", marginBottom: "4px", backgroundColor: PAL.btn, borderRadius: "3px", flowChildren: "right" });
        css(b, style || {});
        var l = $.CreatePanel("Label", b, ""); l.text = text; css(l, { color: PAL.btnText, fontSize: "15px", fontFamily: PAL.font });
        hoverFx(b, (style && style.backgroundColor) || PAL.btn);
        var cb = withClick(onClick);
        b.SetPanelEvent("onactivate", cb); b.SetPanelEvent("onmouseactivate", cb);
        return b;
    }
    // Deadlock asset icons (referenced by their in-game s2r path).
    var ICON_GEAR = "s2r://panorama/images/icons/icon_gear.vsvg";
    var ICON_INFO = "s2r://panorama/images/control_icons/info_icon.vsvg";
    var ICON_SOUL = "s2r://panorama/images/hud/icons/icon_soul.vsvg";
    var ICON_X = "s2r://panorama/images/control_icons/24px/x_close.vsvg";
    // Icon button using a real Deadlock .vsvg (tinted via wash-color). round -> circular,
    // bare -> no background/border (just the clickable icon).
    function mkIconBtn(parent, imgUrl, onClick, sz, round, bare) {
        sz = sz || 26;
        var b = $.CreatePanel("Panel", parent, "");
        css(b, { width: sz + "px", height: sz + "px", marginRight: "8px", verticalAlign: "center" });
        if (!bare) { css(b, { backgroundColor: PAL.btn, borderRadius: (round ? (sz / 2) : 3) + "px" }); if (round) css(b, { border: "1px solid " + PAL.border }); }
        var ic = $.CreatePanel("Panel", b, "");
        css(ic, { width: (bare ? "100%" : "62%"), height: (bare ? "100%" : "62%"), horizontalAlign: "center", verticalAlign: "center", backgroundImage: 'url("' + imgUrl + '")', backgroundSize: "contain", backgroundRepeat: "no-repeat", washColor: bare ? PAL.text : PAL.btnText });   // bare -> dark on paper; on a dark-gray btn -> light
        hoverFx(b, bare ? "#00000000" : PAL.btn, bare ? "#FFFFFF1A" : "#FFFFFF28");   // bare has no rest-bg -> faint hover tint
        var cb = withClick(onClick);
        b.SetPanelEvent("onactivate", cb); b.SetPanelEvent("onmouseactivate", cb);
        return b;
    }
    // Round text button (used for the modal close ✕).
    function mkRoundBtn(parent, text, onClick, sz) {
        sz = sz || 26;
        var b = $.CreatePanel("Panel", parent, "");
        css(b, { width: sz + "px", height: sz + "px", borderRadius: (sz / 2) + "px", backgroundColor: PAL.btn, border: "1px solid " + PAL.border, verticalAlign: "center" });
        var l = $.CreatePanel("Label", b, ""); l.text = text; css(l, { horizontalAlign: "center", verticalAlign: "center", color: PAL.btnText, fontSize: "15px", fontFamily: PAL.font });
        hoverFx(b, PAL.btn);
        var cb = withClick(onClick);
        b.SetPanelEvent("onactivate", cb); b.SetPanelEvent("onmouseactivate", cb);
        return b;
    }

    // ---- FLOATING PANEL + CONFIG MENU --------------------------------------
    // Design tokens lifted from the game's own CSS (pak01 citadel_base_styles /
    // shop styles) so the panel reads as native Deadlock UI.
    var PAL = {
        font: "VALVEOracle, Reaver, sans-serif",   // @define oracle -> QoLLock's most-used font (real family, resolves inline)
        text: "#26291E",            // dark warm gray (slightly lifted off pure black), reads dark on the paper bg
        dim: "#34382B",             // slightly lighter for secondary labels
        faint: "#4C5041",           // dark gray for ranks / section subheads
        btnText: "#FFEFD7",         // offWhite — kept light for button labels/icons (buttons unchanged)
        gold: "#FFED79",            // goldColor (souls)
        accent: "#5FE69E",          // brandGreen — the settings-menu toggle green (was blue)
        enemy: "#FF410D",           // colorEnemy
        panelBg: "#0d1110f5",       // offBlack-ish
        border: "#5FE69E38",        // brandGreen hairline (subtle, ties to the accent)
        btn: "#2C2F28E6",           // dark gray — unselected buttons (readable on the paper bg)
        btnOn: "#3C9E6Acc",         // darker green for text-buttons (offWhite stays readable on it)
        switchOn: "#5FE69E"         // bright brandGreen for switch tracks (matches settings toggle)
    };
    // Deadlock's own shop backdrop texture (single url layer — Panorama's inline style parser
    // rejects a multi-layer gradient()+url() string). It's a dark texture, so the panel's dark
    // backgroundColor + this image read fine behind the content.
    var SHOP_BG = 'url("s2r://panorama/images/shop/catalog/catalog_shop_generic_bg_psd.vtex")';
    var FLOAT_ID = "RecFloat";
    var _float = null, _title = null, _body = null, _cfgBox = null, _cfgShown = false, _helpShown = false;
    var _gearBtn = null, _rightGrp = null, _rule = null;
    // Shared hover-tip panel: a SIBLING of _float (same parent), docked to _float's top and offset to
    // the side, so it lives OUTSIDE the main panel and can never affect row flow. Row hover fills it
    // and shows it; mouseout hides it. Tracks _float's side + vertical drag via positionTip().
    var _tipPanel = null, _tipName = null, _tipBody = null;
    var TIP_W = 250;      // tip panel width (px)
    function buildTip(parent) {
        if (valid(_tipPanel)) return;
        _tipPanel = $.CreatePanel("Panel", parent, "RecTip");
        css(_tipPanel, {
            verticalAlign: "center", width: TIP_W + "px", flowChildren: "down",
            padding: "10px 12px", backgroundColor: PAL.panelBg, backgroundImage: SHOP_BG,
            backgroundSize: "cover", backgroundRepeat: "no-repeat", borderRadius: "4px",
            border: "1px solid " + PAL.border, boxShadow: "0px 2px 10px 0px #000000b0",
            zIndex: "45", opacity: (CFG.opacity / 100).toString(), visibility: "collapse"
        });
        _tipName = $.CreatePanel("Label", _tipPanel, "");
        css(_tipName, { color: PAL.accent, fontSize: "17px", fontFamily: PAL.font, marginBottom: "4px" });
        _tipBody = $.CreatePanel("Label", _tipPanel, "");
        css(_tipBody, { color: PAL.text, fontSize: "15px", fontFamily: PAL.font, width: "100%", whiteSpace: "normal" });
        positionTip();
    }
    // Dock the tip panel to the main panel's top corner, offset to the free side (right of a left-docked
    // panel, left of a right-docked one). Mirrors _float's verticalAlign/marginTop and drag translateY.
    function positionTip() {
        if (!valid(_tipPanel)) return;
        var FW = 340, GAP = 12, off = 10 + FW + GAP;   // 10 = _float side margin; clear the whole panel width
        var st = { horizontalAlign: CFG.side, verticalAlign: "center", marginTop: "-160px",
                   marginLeft: "0px", marginRight: "0px",
                   transform: "translateY(" + Math.round(_dragY) + "px)",
                   opacity: (CFG.opacity / 100).toString() };
        if (CFG.side === "right") st.marginRight = off + "px"; else st.marginLeft = off + "px";
        css(_tipPanel, st);
    }
    function showTip(it) {
        if (!CFG.tips) { hideTip(); return; }            // item tips are off by default; toggle in settings
        if (!valid(_tipPanel) || !it) return;
        var txt = tipFor(_heroId, it.id);
        if (it.until != null) txt = (txt ? txt + "\n" : "") + "Pays off if bought before minute " + it.until + ".";
        if (!txt) { hideTip(); return; }                 // no tip for this item -> show nothing
        if (valid(_tipName)) _tipName.text = it.name || "";
        if (valid(_tipBody)) _tipBody.text = txt;
        css(_tipPanel, { visibility: "visible" });
    }
    function hideTip() { if (valid(_tipPanel)) css(_tipPanel, { visibility: "collapse" }); }
    // vertical drag state — GameUI.GetCursorPosition is absent in Deadlock, so we track the
    // cursor's Y via a full-screen stack of hover strips (onmouseover works on panels).
    var _dragY = 0, _dragging = false, _dragY0 = 0, _dragCapture = null, _dragStartIdx = -1, _stripPx = 0;
    var _dragGhost = null, _dragEnterSeen = false;
    var STRIPS = 100;
    // Official item-category colors (weaponColor / armorColor / spiritBrightColor).
    var _slotColor = { weapon: "#FFA826", vitality: "#00F794", spirit: "#CE90FF" };
    // Darker category shades for use on the LIGHT parchment coach bubble (the bright ones glare there).
    var _slotColorDark = { weapon: "#B85C00", vitality: "#0A8F52", spirit: "#7A44C9" };
    var _slotColorMid = { weapon: "#DC8213", vitality: "#05C373", spirit: "#A46AE4" };   // halfway bright<->dark — dimmed prices

    function applyCfg() {
        if (valid(_float)) css(_float, { horizontalAlign: CFG.side, opacity: (CFG.opacity / 100).toString() });
        positionTip();   // keep the side tip panel docked on the correct side
        saveCfg();
    }
    // ---- Y-only hold drag ---------------------------------------------------
    // Native drag-drop gives the press-hold gesture, but dragging `displayPanel=_float`
    // moves it freely in X/Y and can fly off-screen. Instead we use an INVISIBLE ghost as
    // the drag visual (panel never flies), and a full-screen stack of drop-target strips:
    // during a native drag, panels fire DragEnter (mouseover is suppressed) so each strip
    // tells us the cursor's row. We move the panel Y-ONLY via translateY, clamped on-screen.
    function makeDragOverlay(dragEnter) {
        var host = (valid(_float) && _float.GetParent) ? _float.GetParent() : hudRoot();
        _dragCapture = $.CreatePanel("Panel", host, "");
        css(_dragCapture, { width: "100%", height: "100%", zIndex: "500", flowChildren: "down" });
        for (var i = 0; i < STRIPS; i++) (function (idx) {
            var s = $.CreatePanel("Panel", _dragCapture, "");
            css(s, { width: "100%", height: (100 / STRIPS) + "%" });
            if (dragEnter) $.RegisterEventHandler("DragEnter", s, function () { dragHover(idx); return true; });
            else s.SetPanelEvent("onmouseover", function () { dragHover(idx); });
        })(i);
        if (!dragEnter) { _dragCapture.SetPanelEvent("onmouseup", dragEnd); _dragCapture.SetPanelEvent("onmouseactivate", dragEnd); }
    }
    function clearDrag() {
        if (valid(_dragCapture)) { try { _dragCapture.DeleteAsync(0); } catch (e) {} } _dragCapture = null;
        if (valid(_dragGhost)) { try { _dragGhost.DeleteAsync(0); } catch (e) {} } _dragGhost = null;
    }
    function onDragStart(panelID, settings) {
        _dragging = true; _dragStartIdx = -1; _dragY0 = _dragY; _stripPx = 0; _dragEnterSeen = false;
        try {   // invisible 1px ghost => the real panel never moves with the cursor
            _dragGhost = $.CreatePanel("Panel", _float, ""); css(_dragGhost, { width: "1px", height: "1px", opacity: "0" });
            settings.displayPanel = _dragGhost; settings.removePositionBeforeDrop = true;
        } catch (e) {}
        makeDragOverlay(true);
        return true;
    }
    function onDragEnd() {
        clearDrag();
        $.Schedule(0.0, function () { _dragging = false; });
        return true;
    }
    function dragHover(idx) {
        if (!_dragging) return;
        _dragEnterSeen = true;
        if (!_stripPx) { try { var h = _dragCapture.actuallayoutheight; if (h > 0) _stripPx = h / STRIPS; } catch (e) {} if (!_stripPx) _stripPx = 10; }
        if (_dragStartIdx < 0) { _dragStartIdx = idx; return; }   // anchor to where the drag began
        var y = _dragY0 + (idx - _dragStartIdx) * _stripPx;
        var lim = _stripPx * STRIPS / 2 - 90;                      // keep the panel on-screen
        if (y > lim) y = lim; if (y < -lim) y = -lim;
        _dragY = y;
        if (valid(_float)) css(_float, { transform: "translateY(" + Math.round(_dragY) + "px)" });
        positionTip();   // tip panel follows the vertical drag
    }
    // fallback for engines without SetDraggable: click-move-click via mouseover strips
    function dragStart() { if (_dragging) return; _dragging = true; _dragStartIdx = -1; _dragY0 = _dragY; _stripPx = 0; makeDragOverlay(false); }
    function dragEnd() {
        if (!_dragging && !valid(_dragCapture)) return;
        var moved = Math.abs(_dragY - _dragY0);
        _dragging = false; clearDrag();
        if (CFG.min && moved < 4) setMinimized(false);
    }
    // ---- minimize: collapse to just "★ Buy Next" (still draggable) ----------
    function setMinimized(min) {
        CFG.min = !!min; saveCfg();
        var vis = min ? "collapse" : "visible";
        if (valid(_gearBtn)) css(_gearBtn, { visibility: vis });
        if (valid(_rightGrp)) css(_rightGrp, { visibility: vis });
        if (valid(_rule)) css(_rule, { visibility: vis });
        if (valid(_body)) css(_body, { visibility: vis });
        if (min) { _cfgShown = false; if (valid(_cfgBox)) css(_cfgBox, { visibility: "collapse" }); }
        if (valid(_float)) css(_float, { width: min ? "150px" : "340px", padding: min ? "8px 12px" : "12px 14px" });
    }
    function ensureFloat(shop) {
        if (valid(_float)) return _float;
        _float = $.CreatePanel("Panel", shop, FLOAT_ID);
        css(_float, {
            horizontalAlign: CFG.side, verticalAlign: "center", marginLeft: "10px", marginRight: "10px",
            marginTop: "-160px",   // base position raised ~15% of screen height
            width: "340px", backgroundColor: PAL.panelBg, backgroundImage: SHOP_BG, backgroundSize: "cover", backgroundRepeat: "no-repeat",
            borderRadius: "4px", border: "1px solid " + PAL.border,
            padding: "12px 14px", flowChildren: "down", zIndex: "40", opacity: (CFG.opacity / 100).toString(),
            boxShadow: "0px 2px 10px 0px #000000b0"
        });
        var titleRow = $.CreatePanel("Panel", _float, ""); css(titleRow, { flowChildren: "right", marginBottom: "8px", width: "100%" });
        // gear (real Deadlock icon) left, then title; souls + [?] help docked right.
        _gearBtn = mkIconBtn(titleRow, ICON_GEAR, toggleConfig, 26);
        _title = $.CreatePanel("Label", titleRow, ""); css(_title, { fontFamily: PAL.font, fontSize: "20px", color: PAL.accent, verticalAlign: "center" });
        _rightGrp = $.CreatePanel("Panel", titleRow, ""); css(_rightGrp, { flowChildren: "right", horizontalAlign: "right", verticalAlign: "center" });
        mkIconBtn(_rightGrp, ICON_INFO, toggleHelp, 22, false, true);   // bare [?] icon (no gray button)
        // hold-drag the title to move the panel; click it (minimized) to expand.
        // Native drag-drop = true press-hold-move-release; hover-strip is a fallback only.
        if (_title.SetDraggable) {
            _title.SetDraggable(true);
            $.RegisterEventHandler("DragStart", _title, onDragStart);
            $.RegisterEventHandler("DragEnd", _title, onDragEnd);
        } else {
            _title.SetPanelEvent("onmousedown", dragStart);
        }
        _title.SetPanelEvent("onmouseactivate", function () { if (CFG.min && !_dragging) setMinimized(false); });
        // hairline divider under the header (native shop uses subtle silvered rules)
        _rule = $.CreatePanel("Panel", _float, ""); css(_rule, { height: "1px", width: "100%", backgroundColor: PAL.border, marginBottom: "8px" });
        _cfgBox = $.CreatePanel("Panel", _float, ""); css(_cfgBox, { flowChildren: "down", marginBottom: "6px", visibility: "collapse", width: "100%" });
        _body = $.CreatePanel("Panel", _float, ""); css(_body, { flowChildren: "down", width: "100%" });
        buildTip(shop);   // sibling side panel for hover tips (created once, tracks _float)
        if (_dragY) css(_float, { transform: "translateY(" + Math.round(_dragY) + "px)" });
        positionTip();
        setMinimized(CFG.min);   // restore minimized state
        return _float;
    }
    function toggleConfig() { _cfgShown = !_cfgShown; renderConfig(); }
    // ---- HELP MODAL (centered window over the shop; native dim-backdrop pattern) ----
    var _helpModal = null;
    function closeHelp() { if (valid(_helpModal)) { try { _helpModal.DeleteAsync(0); } catch (e) {} } _helpModal = null; _helpShown = false; }
    function secHead(box, txt) { var l = $.CreatePanel("Label", box, ""); l.text = txt; css(l, { color: PAL.faint, fontSize: "12px", letterSpacing: "2px", fontFamily: PAL.font, marginTop: "11px", marginBottom: "5px" }); }
    function tip(box, term, meaning) {
        var r = $.CreatePanel("Panel", box, ""); css(r, { flowChildren: "right", width: "100%", marginBottom: "5px" });
        var t = $.CreatePanel("Label", r, ""); t.text = term; css(t, { color: PAL.text, fontSize: "15px", fontFamily: PAL.font, width: "150px" });
        var m = $.CreatePanel("Label", r, ""); m.text = meaning; css(m, { color: PAL.dim, fontSize: "15px", fontFamily: PAL.font, width: "250px" });
    }
    function toggleHelp() {
        if (_helpShown) { closeHelp(); return; }
        _helpShown = true;
        var host = (valid(_float) && _float.GetParent) ? _float.GetParent() : hudRoot();
        _helpModal = $.CreatePanel("Panel", host, "RecHelpModal");
        css(_helpModal, { width: "100%", height: "100%", backgroundColor: "#000000cc", zIndex: "200" });
        var win = $.CreatePanel("Panel", _helpModal, "");
        css(win, { width: "528px", horizontalAlign: "center", verticalAlign: "center", backgroundColor: PAL.panelBg, backgroundImage: SHOP_BG, backgroundSize: "cover", backgroundRepeat: "no-repeat", border: "1px solid " + PAL.border, borderRadius: "5px", padding: "18px 22px", flowChildren: "down", boxShadow: "0px 4px 24px 0px #000000d8" });
        var hrow = $.CreatePanel("Panel", win, ""); css(hrow, { flowChildren: "right", width: "100%", marginBottom: "8px" });
        var ht = $.CreatePanel("Label", hrow, ""); ht.text = "How this works"; css(ht, { color: PAL.accent, fontSize: "23px", fontFamily: PAL.font, verticalAlign: "center" });
        var close = mkIconBtn(hrow, ICON_X, closeHelp, 26, true); css(close, { horizontalAlign: "right", marginRight: "0px" });
        var rl = $.CreatePanel("Panel", win, ""); css(rl, { height: "1px", width: "100%", backgroundColor: PAL.border, marginBottom: "6px" });
        var intro = $.CreatePanel("Label", win, ""); intro.text = "The items to buy next — best first."; css(intro, { color: PAL.dim, fontSize: "15px", fontFamily: PAL.font });
        secHead(win, "THE LIST");
        tip(win, "Click a row", "buys the item now (spends souls; affordable rows only)");
        tip(win, "Gold name", "synergises with what you own");
        tip(win, "Green name", "counters an item the enemy team bought (from real match data)");
        tip(win, "Dimmed name", "can't afford right now");
        secHead(win, "SORT  (settings)");
        tip(win, "Popular", "most-built by winners");
        tip(win, "Win-val", "biggest win-rate impact");
        tip(win, "Effic.", "best win-rate impact per soul");
        var snote = $.CreatePanel("Label", win, "");
        snote.text = "Note: certain heroes benefit differently from the selected modes. Check all per hero to determine the better item recommendation.";
        css(snote, { color: PAL.faint, fontSize: "13px", fontFamily: PAL.font, width: "100%", marginTop: "3px", marginBottom: "2px" });
        secHead(win, "OPTIONS  (settings)");
        tip(win, "Pct", "show each item's build rate");
        tip(win, "Counters", "promotes items that beat the enemy team's live build");
        tip(win, "Soul hold", "previews the best items in the tier");
        tip(win, "", "you're saving toward (souls + game time)");
        tip(win, "Item tips", "hover a row for why it's good on your hero");
        var tnote = $.CreatePanel("Label", win, "");
        tnote.text = "Enable tips for items summarized from various builds and hero abilities (might be innacurate).";
        css(tnote, { color: PAL.faint, fontSize: "13px", fontFamily: PAL.font, width: "100%", marginTop: "3px", marginBottom: "2px" });
        secHead(win, "COACH  (settings)");
        var crow = $.CreatePanel("Panel", win, ""); css(crow, { flowChildren: "right", width: "100%", marginBottom: "4px" });
        var ctxt = $.CreatePanel("Label", crow, ""); ctxt.text = "Your character will remind you to buy items when you have enough souls."; css(ctxt, { color: PAL.dim, fontSize: "15px", fontFamily: PAL.font, width: "380px", verticalAlign: "center" });
        var gb = $.CreatePanel("Panel", crow, ""); css(gb, { padding: "2px 8px", backgroundColor: CFG.gubami ? PAL.btnOn : PAL.btn, borderRadius: "3px", horizontalAlign: "right", verticalAlign: "center" });
        var gl = $.CreatePanel("Label", gb, ""); gl.text = "Gubami Coach"; css(gl, { color: PAL.text, fontSize: "12px", fontFamily: PAL.font });
        hoverFx(gb, CFG.gubami ? PAL.btnOn : PAL.btn);
        var gcb = withClick(toggleGubami);
        gb.SetPanelEvent("onactivate", gcb); gb.SetPanelEvent("onmouseactivate", gcb);
    }
    function toggleGubami() { CFG.gubami = !CFG.gubami; if (CFG.gubami) CFG.coach = true; saveCfg(); closeHelp(); toggleHelp(); if (CFG.gubami) coachPreview(); }   // enabling Gubami turns the coach on, reopens the panel, and previews him right away

    function knobRow(box, label, opts, cur, pick) {
        var row = $.CreatePanel("Panel", box, ""); css(row, { flowChildren: "right", marginBottom: "3px" });
        var l = $.CreatePanel("Label", row, ""); l.text = label; css(l, { width: "70px", color: PAL.dim, fontSize: "14px", fontFamily: PAL.font, verticalAlign: "center" });
        for (var i = 0; i < opts.length; i++) (function (o) {
            mkBtn(row, o.t, function () { pick(o.v); }, { backgroundColor: cur === o.v ? PAL.btnOn : PAL.btn });
        })(opts[i]);
    }
    // sliding on/off switch (track + knob) for boolean settings
    function mkSwitch(parent, on, onClick) {
        var W = 34, H = 18, K = 14;
        var track = $.CreatePanel("Panel", parent, "");
        css(track, { width: W + "px", height: H + "px", borderRadius: (H / 2) + "px", backgroundColor: on ? PAL.btnOn : "#FFFFFF22", verticalAlign: "center" });
        var knob = $.CreatePanel("Panel", track, "");
        css(knob, { width: K + "px", height: K + "px", borderRadius: (K / 2) + "px", backgroundColor: "#FFFFFFEE", verticalAlign: "center", horizontalAlign: on ? "right" : "left", marginLeft: "2px", marginRight: "2px" });
        hoverFx(track, on ? PAL.btnOn : "#FFFFFF22", on ? "#57C489ee" : "#FFFFFF3A");
        var cb = withClick(onClick);
        track.SetPanelEvent("onactivate", cb); track.SetPanelEvent("onmouseactivate", cb);
        return track;
    }
    function switchRow(box, label, value, onChange) {
        var row = $.CreatePanel("Panel", box, ""); css(row, { flowChildren: "right", marginBottom: "4px", width: "100%" });
        var l = $.CreatePanel("Label", row, ""); l.text = label; css(l, { width: "70px", color: PAL.dim, fontSize: "14px", fontFamily: PAL.font, verticalAlign: "center" });
        mkSwitch(row, value, function () { onChange(!value); });
    }
    function renderConfig() {
        if (!valid(_cfgBox)) return;
        _cfgBox.RemoveAndDeleteChildren();
        css(_cfgBox, { visibility: _cfgShown ? "visible" : "collapse" });
        if (!_cfgShown) return;
        var hdr = $.CreatePanel("Label", _cfgBox, ""); hdr.text = "SETTINGS"; css(hdr, { color: PAL.faint, fontSize: "12px", fontFamily: PAL.font, letterSpacing: "2px", marginBottom: "5px" });
        knobRow(_cfgBox, "Sort", [{ t: "Popular", v: "popular" }, { t: "Win-val", v: "value" }, { t: "Effic.", v: "efficiency" }], CFG.mode, function (v) { CFG.mode = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        knobRow(_cfgBox, "Items", [{ t: "5", v: 5 }, { t: "8", v: 8 }, { t: "10", v: 10 }, { t: "15", v: 15 }], CFG.count, function (v) { CFG.count = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        switchRow(_cfgBox, "Pct", CFG.showPct, function (v) { CFG.showPct = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        switchRow(_cfgBox, "Click-buy", CFG.buy, function (v) { CFG.buy = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        switchRow(_cfgBox, "Counters", CFG.counters, function (v) { CFG.counters = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        switchRow(_cfgBox, "Skill order", CFG.skills, function (v) { CFG.skills = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        switchRow(_cfgBox, "Item tips", CFG.tips, function (v) { CFG.tips = v; saveCfg(); if (!v) hideTip(); renderConfig(); });
        var crow = $.CreatePanel("Panel", _cfgBox, ""); css(crow, { flowChildren: "right", marginBottom: "3px" });
        var cl = $.CreatePanel("Label", crow, ""); cl.text = "Coach"; css(cl, { width: "70px", color: PAL.dim, fontSize: "14px", fontFamily: PAL.font, verticalAlign: "center" });
        mkSwitch(crow, CFG.coach, function () { CFG.coach = !CFG.coach; saveCfg(); if (!CFG.coach) hideCoach(); else coachPreview(); renderConfig(); });
        knobRow(_cfgBox, "Refresh", [{ t: "3s", v: 3 }, { t: "5s", v: 5 }, { t: "10s", v: 10 }, { t: "30s", v: 30 }], CFG.refreshSec, function (v) { CFG.refreshSec = v; saveCfg(); renderConfig(); });
        knobRow(_cfgBox, "Opacity", [{ t: "60", v: 60 }, { t: "80", v: 80 }, { t: "100", v: 100 }], CFG.opacity, function (v) { CFG.opacity = v; applyCfg(); renderConfig(); });
        knobRow(_cfgBox, "Side", [{ t: "Left", v: "left" }, { t: "Right", v: "right" }], CFG.side, function (v) { CFG.side = v; applyCfg(); renderConfig(); });
        // --- Soul-holding group, set off by separators above and below ---
        cfgSep();
        switchRow(_cfgBox, "Soul hold", CFG.holding, function (v) { CFG.holding = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        knobRow(_cfgBox, "Next", [{ t: "1", v: 1 }, { t: "2", v: 2 }, { t: "3", v: 3 }, { t: "5", v: 5 }], CFG.holdCount, function (v) { CFG.holdCount = v; saveCfg(); refreshFloat(hudRoot(), null); renderConfig(); });
        cfgSep();
        // Turn Off: collapse to just the draggable "★ Buy Next" title (click title to restore).
        var doHide = function () { _cfgShown = false; renderConfig(); setMinimized(true); };
        mkBtn(_cfgBox, "Turn Off", doHide, { backgroundColor: PAL.btnOn, marginTop: "4px" });
    }
    function cfgSep() { var s = $.CreatePanel("Panel", _cfgBox, ""); css(s, { height: "1px", width: "100%", backgroundColor: PAL.border, marginTop: "4px", marginBottom: "6px" }); }

    var _ownedSig = "";
    // Hash of what's actually on screen. recLoop calls refreshFloat every CFG.refreshSec (3s);
    // computeRecs is a cheap table lookup, but _body.RemoveAndDeleteChildren()+rebuild (icons,
    // hover tips, skill grid) is not — doing it every tick when nothing changed caused a periodic
    // ~3s stutter. So: compute first, hash the visible state, and skip the DOM churn if identical.
    var _renderSig = "";
    function refreshFloat(root, shop) {
        if (!valid(_float)) return;
        if (!shop) shop = shopIfOpen(root);
        if (!shop) return;   // the panel is drawn ONLY while the shop is open — no rec compute or DOM
                             // work when it's closed. The coach (shop-closed nudge) computes items via
                             // computeRecs directly and never touches this panel.
        var hero = _heroId, souls = getSouls(root), t = getGameTimeSec(root);
        var owned = getOwnedNames(shop);
        var sig = Object.keys(owned).sort().join(",");
        if (sig !== _ownedSig) { _ownedSig = sig;  }
        if (valid(_title)) _title.text = "Buy Next";
        if (!valid(_body)) return;
        // --- placeholder states (compute nothing heavy) ---
        if (!hero) {
            if (_renderSig === "@calibrating" && _body.GetChildCount() > 0) return;
            _renderSig = "@calibrating"; _body.RemoveAndDeleteChildren();
            var c = $.CreatePanel("Label", _body, ""); c.text = "calibrating…"; css(c, { color: PAL.dim, fontSize: "16px", fontFamily: PAL.font }); return;
        }
        // (enemy comp still conditions the ranking via matchup lifts; just not shown in-panel)
        var recs = computeRecs(hero, souls, owned, _enemies, t);
        if (!recs.length) {
            if (_renderSig === "@empty" && _body.GetChildCount() > 0) return;
            _renderSig = "@empty"; _body.RemoveAndDeleteChildren();
            var e = $.CreatePanel("Label", _body, ""); e.text = "calibrating…"; css(e, { color: PAL.dim, fontSize: "16px", fontFamily: PAL.font }); return;
        }
        var hold = CFG.holding ? computeHolding(souls, t) : { tier: 0, items: [] };
        var holdIds = {};
        for (var i = 0; i < hold.items.length; i++) holdIds[hold.items[i].id] = 1;
        // --- signature of the VISIBLE result; identical => leave the DOM alone (no stutter) ---
        var parts = ["" + hero, "C" + CFG.count + (CFG.showPct ? 1 : 0) + (CFG.holding ? 1 : 0) + (CFG.skills ? 1 : 0) + (CFG.buy ? 1 : 0) + (CFG.counters ? 1 : 0) + CFG.mode];
        var probe = 0;
        for (var p = 0; p < recs.length && probe < CFG.count; p++) {
            var it = recs[p]; if (holdIds[it.id]) continue;
            parts.push(it.id + (it.aff ? "a" : "_") + (it.imb || 0) + ((it.syn && it.syn > 0.05) ? "s" : "") + ((it.ctr && it.ctr > 0.03) ? "c" : ""));
            probe++;
        }
        parts.push("H" + hold.tier);
        for (var hj = 0; hj < hold.items.length; hj++) parts.push("h" + hold.items[hj].id + (hold.items[hj].aff ? "a" : "_"));
        var rsig = parts.join(",");
        if (rsig === _renderSig && _body.GetChildCount() > 0) return;   // nothing visible changed
        _renderSig = rsig;
        // --- rebuild ---
        _body.RemoveAndDeleteChildren();
        var shown = 0;   // main list, skipping anything shown in the Soul-holding table
        for (var r = 0; r < recs.length && shown < CFG.count; r++) {
            if (holdIds[recs[r].id]) continue;
            rowRender(_body, recs[r], shown); shown++;
        }
        if (hold.items.length) renderHolding(hold);
        if (CFG.skills) renderSkillOrder(hero);
    }
    // Recommended ability leveling order (from ability-order-stats), as a build-order grid:
    // one ROW per ability (A1..A4), columns = upgrade steps left->right; the ability's icon
    // marks the step at which it's leveled, empty cells elsewhere.
    function renderSkillOrder(hero) {
        var order = DATA.abilityOrder && DATA.abilityOrder[String(hero)];
        var icons = DATA.abilityIcon && DATA.abilityIcon[String(hero)];
        if (!order || !order.length || !icons) return;
        var div = $.CreatePanel("Panel", _body, ""); css(div, { height: "1px", width: "100%", backgroundColor: PAL.border, marginTop: "8px", marginBottom: "5px" });
        var hdr = $.CreatePanel("Label", _body, ""); hdr.text = "SKILL ORDER"; css(hdr, { color: PAL.faint, fontSize: "12px", letterSpacing: "1px", fontFamily: PAL.font, marginBottom: "4px" });
        var grid = $.CreatePanel("Panel", _body, ""); css(grid, { flowChildren: "down", width: "100%" });
        for (var slot = 1; slot <= 4; slot++) {
            var ic = icons[String(slot)];
            var row = $.CreatePanel("Panel", grid, ""); css(row, { flowChildren: "right", marginBottom: "2px" });
            for (var i = 0; i < order.length; i++) {
                var cell = $.CreatePanel("Panel", row, ""); css(cell, { width: "16px", height: "16px", marginRight: "1px", borderRadius: "2px" });
                if (order[i] === slot && ic) css(cell, { backgroundImage: 'url("' + ic + '")', backgroundSize: "contain", backgroundRepeat: "no-repeat" });
                else css(cell, { backgroundColor: "#FFFFFF0A" });   // empty step placeholder
            }
        }
    }
    // One rec row: [bar] rank name [A#] .......... price (soul). Discounted price is already
    // baked into it.cost; imbue items show their recommended A1-A4 slot.
    function rowRender(parent, it, idx) {
        var slotCol = _slotColor[it.slot] || PAL.text;
        var rowBg = (idx % 2 ? "#FFFFFF08" : "#00000000");
        var row = $.CreatePanel("Panel", parent, ""); css(row, { flowChildren: "right", width: "100%", padding: "3px 4px", marginBottom: "1px", backgroundColor: rowBg, borderRadius: "2px", opacity: it.aff ? "1.0" : "0.45" });
        hoverFxTip(row, rowBg, "#FFFFFF1C", it);   // brighten on hover + show this item's tip in the side panel
        var bar = $.CreatePanel("Panel", row, ""); css(bar, { width: "3px", height: "18px", backgroundColor: slotCol, borderRadius: "2px", marginRight: "8px", verticalAlign: "center" });
        var rk = $.CreatePanel("Label", row, ""); rk.text = (idx + 1); css(rk, { width: "20px", color: PAL.faint, fontSize: "15px", fontFamily: PAL.font, verticalAlign: "center" });
        if (it.icon) {   // item icon, left of the name
            var im = $.CreatePanel("Panel", row, ""); css(im, { width: "22px", height: "22px", marginRight: "7px", backgroundImage: 'url("' + it.icon + '")', backgroundSize: "contain", backgroundRepeat: "no-repeat", verticalAlign: "center" });
        }
        var isSyn = (it.syn && it.syn > 0.05);      // pairs well with your current build (gold)
        var isCtr = (it.ctr && it.ctr > 0.03);      // counters something the enemy team built (green)
        var nm = $.CreatePanel("Label", row, ""); nm.text = it.name; css(nm, { color: isSyn ? PAL.gold : (isCtr ? PAL.accent : PAL.text), fontSize: "16px", fontFamily: PAL.font, verticalAlign: "center" });
        if (it.imb) {   // imbue item -> show the recommended ability's ICON (same art as SKILL ORDER)
            var abIcons = DATA.abilityIcon && DATA.abilityIcon[String(_heroId)];
            var abIc = abIcons && abIcons[String(it.imb)];
            if (abIc) {   // icon only — no "A#" text fallback (show nothing if an icon is missing)
                var ab = $.CreatePanel("Panel", row, ""); css(ab, { width: "20px", height: "20px", marginLeft: "5px", backgroundImage: 'url("' + abIc + '")', backgroundSize: "contain", backgroundRepeat: "no-repeat", verticalAlign: "center" });
            }
        }
        var rightGrp = $.CreatePanel("Panel", row, ""); css(rightGrp, { flowChildren: "right", horizontalAlign: "right", verticalAlign: "center" });
        if (it.late) {   // past the minute this item pays off for strong players (tm.until)
            var lt = $.CreatePanel("Label", rightGrp, ""); lt.text = "⌛" + it.until + "'";
            css(lt, { color: PAL.enemy, fontSize: "13px", fontFamily: PAL.font, marginRight: "6px", verticalAlign: "center" });
        }
        if (CFG.showPct) {   // pick rate = "% of strong <hero> players who buy this in this phase"
            var pc = $.CreatePanel("Label", rightGrp, ""); pc.text = Math.round((it.pop || 0) * 100) + "%";
            css(pc, { color: PAL.dim, fontSize: "14px", fontFamily: PAL.font, marginRight: "8px", verticalAlign: "center" });
        }
        var priceCol = _slotColorMid[it.slot] || slotCol;   // dimmed (halfway to dark), not as dim as before
        var co = $.CreatePanel("Label", rightGrp, ""); co.text = String(it.cost); css(co, { color: priceCol, fontSize: "16px", fontFamily: PAL.font, verticalAlign: "center" });
        var si = $.CreatePanel("Panel", rightGrp, ""); css(si, { width: "13px", height: "13px", marginLeft: "3px", backgroundImage: 'url("' + ICON_SOUL + '")', backgroundSize: "contain", backgroundRepeat: "no-repeat", washColor: priceCol, verticalAlign: "center" });
        // Click-to-buy: clicking an affordable row activates the game's real shop panel.
        if (CFG.buy && it.aff) {
            var bcb = withClick(function () { buyItem(it); });
            row.SetPanelEvent("onactivate", bcb); row.SetPanelEvent("onmouseactivate", bcb);
        }
    }
    // Soul-holding preview: a small table of the best items in the tier you're saving toward.
    function renderHolding(h) {
        if (!h || !h.items || !h.items.length) return;
        var div = $.CreatePanel("Panel", _body, ""); css(div, { height: "1px", width: "100%", backgroundColor: PAL.border, marginTop: "8px", marginBottom: "5px" });
        var hrow = $.CreatePanel("Panel", _body, ""); css(hrow, { flowChildren: "right", width: "100%", marginBottom: "3px" });
        var hl = $.CreatePanel("Label", hrow, ""); hl.text = "SAVING TOWARD"; css(hl, { color: PAL.faint, fontSize: "12px", letterSpacing: "1px", fontFamily: PAL.font, verticalAlign: "center" });
        var tl = $.CreatePanel("Label", hrow, ""); tl.text = "  T" + h.tier; css(tl, { color: PAL.dim, fontSize: "13px", fontFamily: PAL.font, verticalAlign: "center" });
        for (var i = 0; i < h.items.length; i++) rowRender(_body, h.items[i], i);
    }

    // ---- COACH POPUP (shop CLOSED): hero portrait + speech bubble ----------
    // When you cross a soul threshold and a recommended item is affordable, your hero
    // "says" to buy it. Portrait on the left edge, bubble above with a tail to the portrait.
    var _coachPanel = null, _coachBubble = null, _coachPortrait = null;
    var _coachFired = {}, _coachShownHb = -99, _coachVisible = false, _coachLastFire = -99, _coachGen = 0, _coachForceUntil = -99;
    var COACH_THRESH = [800, 1600, 2400, 3200, 6400];   // 2400 = a discounted-#1 tier
    var COACH_VIS = 8;   // seconds the popup stays up before auto-hide (drives the end-bounce timing)
    // Coach only nudges HIGH-tier buys (never T1/T2), and once you're in the T4 era it drops T3 too.
    // Boundary from analyze_tier_time.py (shard 93, ~1.7M buys): T3 dominates until ~min 28, where
    // T4 first overtakes T3 (40% vs 38%); T4 becomes the outright majority at ~min 33. 1680s = 28 min
    // = the crossover where T4 is the modal buy. (25 min is still a T3 game — 45% T3 vs 28% T4.)
    var COACH_MIN_TIER = 3;      // baseline: skip T1/T2 — the coach is for impactful items only
    var COACH_T4_MIN = 1680;     // past 28 min, only nudge T4 (data-derived crossover)
    function coachMinTier(tSec) { return (tSec || 0) >= COACH_T4_MIN ? 4 : COACH_MIN_TIER; }
    var COACH_BG = "#e9e0c6f7";      // paper/parchment bubble
    var COACH_TEXT = "#2b2416";      // dark ink on paper
    var COACH_SHIFT = "-38px";       // portrait + tail nudged left of centre (~half portrait width)
    // Gubami (rude coach) lines. {n} = the item name (rendered in its category color, icon after).
    // GUBAMI_LINES roll for ANY item; GUBAMI_SLOT_LINES[slot] are only added to the pool when the
    // recommended item is of that category — i.e. exactly "reroll until a generic or matching line".
    var GUBAMI_LINES = [
        "Buy {n}, genius.",
        "{n}. Now. Before you feed again.",
        "Even a bot would've bought {n} by now.",
        "You. {n}. Chop chop.",
        "Hey champ, {n} is the play. Shocking, I know.",
        "Buy {n}. I made a whole AI\nmodel for this, use it.",
        "The face of Deadlock\ncommands you to buy {n}.",
        "Buy {n} or I am adding you to the FILES.",
        "Do not buy {n}.",
        "Buying {n} donates 30% to the Lash-Back fund.",
        "For every {n} I recommend,\nmy AI datacenter destroys 12 gallons of water.",
        "{n} will contribute greatly to your pacifist build",
        "Buying {n} will not get you to Eternus,\nbut its still something.",
        "Buy {n} to continue your losing streak.",
        "Buy {n} or I access your pc files.",
        "Buy {n} dumbass",
        "Go buy {n}",
        "Go get {n} now!"
    ];
    var GUBAMI_SLOT_LINES = {
        weapon:   ["{n} will help you to massacre more ixians."],
        vitality: ["{n}, Eat your greens."],
        spirit:   ["You dont really need {n}."]
    };
    var COACH_LINE_DEFAULT = ["I should buy {n}"];   // polite (non-Gubami) coach
    function ensureCoach(host) {
        if (valid(_coachPanel)) return _coachPanel;
        _coachPanel = $.CreatePanel("Panel", host, "RecCoach");
        css(_coachPanel, { horizontalAlign: CFG.side, verticalAlign: "center", marginLeft: "16px", marginRight: "16px", marginTop: "-100px", flowChildren: "down", zIndex: "1000", overflow: "noclip", visibility: "collapse" });
        // The coach is a passive popup shown during LIVE gameplay (shop closed). It has no
        // interactive elements, so make the whole subtree mouse-transparent — otherwise it would
        // swallow clicks/aim in its screen region for the ~8s it's up. (Source2 Panorama JS props.)
        try { _coachPanel.hittest = false; _coachPanel.hittestchildren = false; } catch (e) {}
        _coachBubble = $.CreatePanel("Panel", _coachPanel, "");
        css(_coachBubble, { horizontalAlign: "center", flowChildren: "down", backgroundColor: COACH_BG, borderRadius: "9px", padding: "9px 13px", boxShadow: "0px 2px 10px 0px #000000b0" });
        // content (text + item icon) is built per-show in showCoach.
        // tail: a rotation-free CSS border triangle pointing DOWN, in the bubble colour, just below
        // the bubble. (Rotated squares mis-position because Panorama ignores transform-origin.)
        // Shifted left with the portrait so it points at it.
        var tail = $.CreatePanel("Panel", _coachPanel, "");
        css(tail, {
            width: "0px", height: "0px", horizontalAlign: "center", marginTop: "-1px",
            borderLeftWidth: "9px", borderLeftColor: "#00000000",
            borderRightWidth: "9px", borderRightColor: "#00000000",
            borderTopWidth: "12px", borderTopColor: COACH_BG, borderBottomWidth: "0px",
            transform: "translateX(" + COACH_SHIFT + ")"
        });
        _coachPortrait = $.CreatePanel("Panel", _coachPanel, ""); css(_coachPortrait, { width: "76px", height: "76px", horizontalAlign: "center", marginTop: "13px", backgroundSize: "contain", backgroundRepeat: "no-repeat", transform: "translateX(" + COACH_SHIFT + ")" });
        return _coachPanel;
    }
    function hideCoach() { _coachVisible = false; if (valid(_coachPanel)) css(_coachPanel, { visibility: "collapse" }); }
    // quick damped rotation bounce (tilt left ~10° then oscillate back to 0, ~3 bounces)
    function coachBounce() {
        if (!valid(_coachPortrait)) return;
        var seq = [-10, 6, -3.5, 2, -1, 0], i = 0;
        css(_coachPortrait, { transitionProperty: "transform", transitionDuration: "0.07s", transitionTimingFunction: "ease-out" });
        (function step() {
            if (!valid(_coachPortrait) || i >= seq.length) return;
            css(_coachPortrait, { transform: "translateX(" + COACH_SHIFT + ") rotateZ(" + seq[i] + "deg)" });
            i++; $.Schedule(0.075, step);
        })();
    }
    // vertical hop: bounce UP twice (bouncy) as a ONE-SHOT finishing flourish near the end
    // of the popup's life (no repeat). gen guards against a stale burst from an earlier show.
    function coachVBounce(gen) {
        if (gen !== _coachGen || !_coachVisible || !valid(_coachPortrait)) return;
        var seq = [-9, 0, -6, 0], i = 0;
        css(_coachPortrait, { transitionProperty: "transform", transitionDuration: "0.13s", transitionTimingFunction: "ease-out" });
        (function step() {
            if (gen !== _coachGen || !valid(_coachPortrait) || i >= seq.length) return;   // single burst, then stop
            css(_coachPortrait, { transform: "translateX(" + COACH_SHIFT + ") translateY(" + seq[i] + "px)" });
            i++; $.Schedule(0.14, step);
        })();
    }
    function showCoach(root, item) {
        ensureCoach(root);
        // Show FIRST so a render hiccup below can never leave the popup collapsed (was the bug: a
        // throw here aborted before visibility was set, so coach-fire logged but nothing appeared).
        css(_coachPanel, { horizontalAlign: CFG.side, visibility: "visible" });
        _coachShownHb = _hb; _coachVisible = true;
        try {
            var hp = CFG.gubami ? GUBAMI_ICON : (DATA.heroIcon && DATA.heroIcon[String(_heroId)]);
            css(_coachPortrait, { backgroundImage: hp ? 'url("' + hp + '")' : "" });
            // template uses {n} for the item name; the icon goes right AFTER the name, then any trailing text.
            // Gubami pool = generic lines + the per-slot lines that match this item's category.
            var tmpls = CFG.gubami ? GUBAMI_LINES.concat(GUBAMI_SLOT_LINES[item.slot] || []) : COACH_LINE_DEFAULT;
            var tmpl = tmpls[Math.floor(Math.random() * tmpls.length)];
            _coachBubble.RemoveAndDeleteChildren();
            // A template may hard-code line breaks with "\n" (for long Gubami lines). Render one ROW
            // per line (bubble flows down); the item name + icon go inline on whichever line holds {n}.
            var nameCol = _slotColorDark[item.slot] || COACH_TEXT;   // darker parchment-tuned category color
            var lines = tmpl.split("\n");
            for (var li = 0; li < lines.length; li++) {
                var line = lines[li];
                var row = $.CreatePanel("Panel", _coachBubble, "");
                css(row, { flowChildren: "right", horizontalAlign: "center" });
                (function (row) {
                    function rLabel(t, col) { var l = $.CreatePanel("Label", row, ""); l.text = t; css(l, { color: col || COACH_TEXT, fontSize: "16px", fontWeight: "bold", fontFamily: PAL.font, verticalAlign: "center" }); return l; }
                    var at = line.indexOf("{n}");
                    if (at < 0) { rLabel(line); return; }                    // this line has no item name
                    var lead = line.substring(0, at), suffix = line.substring(at + 3);
                    if (lead) rLabel(lead);
                    var nameLbl = rLabel(item.name, nameCol); css(nameLbl, { textShadow: "0px 0px 2px #0000004D" });
                    if (item.icon) { var ic = $.CreatePanel("Panel", row, ""); css(ic, { width: "26px", height: "26px", marginLeft: "6px", marginRight: suffix ? "6px" : "0px", backgroundImage: 'url("' + item.icon + '")', backgroundSize: "contain", backgroundRepeat: "no-repeat", verticalAlign: "center" }); }
                    if (suffix) rLabel(suffix);
                })(row);
            }
        } catch (e) {  }
        coachBounce();   // rotational tilt at the START only (no vertical bounce here)
        _coachGen++;
        // vertical bounce ONLY at the END: one burst ~1s before the auto-hide. Scheduled inside
        // the existing visibility window, so it does NOT extend how long the bubble stays up.
        (function (g) { $.Schedule(COACH_VIS - 1.0, function () { coachVBounce(g); }); })(_coachGen);
        // Gubami: always play a random Atlas effort grunt (01-11).
        // NOTE: Panorama PlaySoundEffect is name-only — volume/pitch can't be set from here.
        if (CFG.gubami) {
            var n = 1 + Math.floor(Math.random() * 11);   // 1..11
            var snd = "atlas_effort_melee_small_" + (n < 10 ? "0" + n : n) + "_emote_melee_effort_3d";
            try { $.DispatchEvent("PlaySoundEffect", snd); } catch (e) {  }
        }
    }
    // Immediately show the coach once (bubble + Gubami grunt) when you enable/switch it mid-match,
    // so it gives feedback right away instead of waiting for the next un-fired soul threshold (which,
    // mid/late game, has usually already fired). Shows over the shop too, for the normal window.
    function coachPreview() {
        if (!CFG.coach || !_heroId) return;
        var root = hudRoot();
        var souls = getSouls(root) || 0;
        var tSec = getGameTimeSec(root), minTier = coachMinTier(tSec);
        var recs = computeRecs(_heroId, souls, _ownedAccum, _enemies, tSec) || [];
        var pick = null;   // prefer the #1 affordable item at/above the min tier, else best of tier, else top rec
        for (var r = 0; r < recs.length; r++) { if (recs[r].aff && recs[r].cost <= souls && recs[r].tier >= minTier) { pick = recs[r]; break; } }
        if (!pick) for (var r2 = 0; r2 < recs.length; r2++) { if (recs[r2].tier >= minTier) { pick = recs[r2]; break; } }
        if (!pick && recs.length) pick = recs[0];
        if (!pick) return;   // no recs (hero data missing) -> nothing to say
        _coachForceUntil = _hb + COACH_VIS; _coachLastFire = _hb;
        showCoach(root, pick);
    }
    function coachCheck(root) {
        if (_coachVisible && (_hb - _coachShownHb) > COACH_VIS) hideCoach();   // auto-hide after ~COACH_VIS s
        if (!CFG.coach || !_heroId) return;
        if (_hb - _coachLastFire < 1) return;   // 1s debounce between nudges
        var souls = getSouls(root); if (!souls || souls <= 0) return;
        for (var i = 0; i < COACH_THRESH.length; i++) {
            var T = COACH_THRESH[i];
            if (souls >= T && !_coachFired[T]) {   // crossed a soul tier -> nudge once
                _coachFired[T] = 1; _coachLastFire = _hb;
                var tSec = getGameTimeSec(root), minTier = coachMinTier(tSec);   // high-tier only; T4-only late
                var recs = computeRecs(_heroId, souls, _ownedAccum, _enemies, tSec);
                var pick = null;   // prefer the #1 affordable item at/above the min tier...
                for (var r = 0; r < recs.length; r++) { if (recs[r].aff && recs[r].cost <= souls && recs[r].tier >= minTier) { pick = recs[r]; break; } }
                if (!pick) for (var rf = 0; rf < recs.length; rf++) { if (recs[rf].aff && recs[rf].cost <= souls) { pick = recs[rf]; break; } }   // ...else the best AFFORDABLE, so it still reminds at 800/1600
                // the 2400 tier only applies when the #1 item is discounted (e.g. a 3200 item -> 2400)
                if (T === 2400 && !(pick && pick.fullCost && pick.cost < pick.fullCost)) break;
                if (pick) showCoach(root, pick);
                break;   // one nudge per check (rest fire on later ticks -> honours the debounce)
            }
        }
    }

    // ---- driver -------------------------------------------------------------
    var _rootCache = null;
    function hudRoot() {
        if (valid(_rootCache)) return _rootCache;   // cache: the HUD root is stable; re-find only if it rebuilds (new match)
        var p = $.GetContextPanel();
        while (p && p.GetParent && p.GetParent()) p = p.GetParent();
        _rootCache = p;
        return p;
    }
    function findByAny(p, needle, depth) {
        if (!p || depth > 40) return null;
        try { if (p.id === needle || p.paneltype === needle || (p.BHasClass && p.BHasClass(needle))) return p; } catch (e) {}
        var kids = (p.Children ? p.Children() : []) || [];
        for (var i = 0; i < kids.length; i++) { var hit = findByAny(kids[i], needle, depth + 1); if (hit) return hit; }
        return null;
    }
    // NOTE: reading owned/enemy items from CitadelModsPurchasedPanel is a DEAD END — the
    // CitadelModIcon's ModIconImage src is native-set and not exposed to JS (verified in-game:
    // "no readable src" for all icons). Local owned items come from the shop mod_view name-scan
    // (getOwnedNames), which works. Enemy items have no readable source and stay unavailable.
    var _shopCache = null;
    function shopIfOpen(root) {
        // Cache the shop panel handle: it persists in the tree whether open or closed (open is just the
        // gShopOpen class). Re-traversing the whole HUD every tick to find it is the main per-second cost.
        var shop = valid(_shopCache) ? _shopCache
            : (root && (root.FindChildTraverse("CitadelHudHeroShop") || findByAny(root, "CitadelHudHeroShop", 0)));
        _shopCache = valid(shop) ? shop : null;
        return (shop && shop.BHasClass && shop.BHasClass("gShopOpen")) ? shop : null;
    }
    // Street Brawl detection. AUTHORITATIVE signal = the HUD carries a "gamemode_streetbrawl"
    // class in brawl (the game's own top_bar/shop CSS gates everything off it). Corroborated by
    // the top-bar round banner (id "StretBrawlContainer" — Valve's real typo), which is
    // `visibility: collapse` by default and only `visible` under .gamemode_streetbrawl.
    // NOTE: do NOT use the round scoring display (id "StreetBrawlDisplay") — it's hidden via
    // opacity:0, not visibility, so its `.visible` reads true in EVERY mode (would false-hide
    // the panel in the hideout). Brawl also uses a DRAFT, not the buy-anything shop, so the recs
    // don't apply there anyway. We hide in brawl and restore in the hideout.
    var _brawlOn = false, _brawlClear = 0, _brawlDiag = "";
    function _vis(p) { if (!p) return false; try { return !!p.visible; } catch (e) { return false; } }
    var _brawlContCache = null;
    function detectBrawl(root) {
        if (!root) return false;
        // Brawl detection via the AUTHORITATIVE O(1) signal ONLY: the HUD root carries the
        // "gamemode_streetbrawl" class during Street Brawl (the game's own top_bar/shop CSS gates
        // everything off it). The previous FindChildTraverse("StretBrawlContainer") corroboration was a
        // per-second full-HUD walk that CAUSED the 1s jitter (confirmed 2026-07-21) — removed entirely.
        // BHasClass is O(1), no tree traversal. StreetBrawlDisplay is NOT usable (opacity:0, not
        // visibility -> reads visible in every mode). Brawl uses a draft, not the buy shop, so recs
        // don't apply there anyway: we hide in brawl, restore in the hideout.
        return !!(root.BHasClass && root.BHasClass("gamemode_streetbrawl"));
    }
    // Debounced latch: one hit turns it on; 3 clean ticks (no brawl signal) turns it off. This
    // avoids a mid-round flicker if a banner briefly hides, yet recovers in the hideout.
    function inBrawl(root) {
        if (detectBrawl(root)) { _brawlOn = true; _brawlClear = 0; }
        else if (_brawlOn && ++_brawlClear >= 3) { _brawlOn = false;  }
        return _brawlOn;
    }

    var _heroId = null, _loggedShop = false, _enemies = [], _enemySig = "";
    // Re-detect hero every scan (hideout swap / new match). Also scan the enemy comp;
    // re-scanned each loop so the first-30s lane shuffle settles. Keep last known.
    function scanPlayers(root, shop) {
        if (shop) {
            var h = getLocalHeroId(shop);
            if (h && h !== _heroId) {
                _heroId = h; _ownedAccum = {}; _ownedSig = ""; _ownedDiagN = 0; _coachFired = {};   // new hero/match -> reset owned + coach
                _enemyItems = {}; _rpSeen = {}; _rpEnemySig = "";   // reset accumulated enemy purchases
                if (valid(_float)) refreshFloat(root, shop);
            }
        }
        if (_heroId) {
            var en = scanEnemies(root, _heroId);
            if (!en.length) enemyDiag(root);   // reveal why (hideout=no team, or structure differs)
            if (en.length) {
                var sig = en.slice().sort(function (a, b) { return a - b; }).join(",");
                if (sig !== _enemySig) {
                    _enemySig = sig; _enemies = en;
                    if (valid(_float)) refreshFloat(root, shop);
                }
            }
        }
        return _heroId;
    }
    var _shopWasOpen = false;
    function tick() {
        var root = hudRoot();
        if (inBrawl(root)) {                       // Street Brawl -> keep the recommender fully hidden
            if (valid(_float)) css(_float, { visibility: "collapse" });
            hideCoach(); _shopWasOpen = false; if (_helpShown) closeHelp();
            $.Schedule(1.0, tick); return;
        }
        if (valid(_float)) css(_float, { visibility: "visible" });   // restore after leaving brawl (back in hideout)
        var shop = shopIfOpen(root);
        if (shop) {
            // Enemy-purchase + owned accumulation. SHOP-OPEN ONLY now (was every tick, open or closed):
            // the per-second feed scan while roaming was a stutter source. The recent-purchases feed
            // shows the ~50 newest entries at the moment the shop opens, and we only consult counters/
            // owned in-shop anyway, so periodic shop visits still capture the recent backlog. Own buys
            // happen in-shop, so owned-detection is unaffected.
            collectRecent(root, shop);
            ensureFloat(shop);
            if (!_loggedShop) { _loggedShop = true;  }
            if (!_shopWasOpen) {                  // shop JUST opened -> rescan + refresh immediately
                _shopWasOpen = true;
                scanPlayers(root, shop);
                refreshFloat(root, shop);
            }
            if (_hb >= _coachForceUntil) hideCoach();   // shop-closed only, EXCEPT a brief preview when you (re)enable the coach
        } else { _shopWasOpen = false; coachCheck(root); if (_helpShown) closeHelp(); }   // tips close with the shop
        $.Schedule(1.0, tick);
    }
    function recLoop() {
        if (!_brawlOn) {                           // skip all rec work while Street Brawl is active
            var root = hudRoot(), shop = shopIfOpen(root);
            if (shop) { scanPlayers(root, shop); refreshFloat(root, shop); }
        }
        $.Schedule(CFG.refreshSec, recLoop);
    }

    $.Schedule(1.0, tick);
    $.Schedule(CFG.refreshSec, recLoop);
})();
