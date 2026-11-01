"""Compile the mod with CS2's resourcecompiler (Workshop Tools DLC) and pack it into a VPK.

Deadlock has no public tools; CS2's resourcecompiler produces Panorama resources (_c) Deadlock loads.
It compiles files placed under <cs2>/content/csgo_addons/<addon>/ into <cs2>/game/csgo_addons/<addon>/.

usage: python pack.py [--pak 30] [--install]
  settings.json (repo root) holds the panel defaults baked into the build
  --install  copy dist/pakNN_dir.vpk into Deadlock's addons (refuses to overwrite a different mod)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

import vpk
from paths import game_dir

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CS2 = game_dir("Counter-Strike Global Offensive")
DEADLOCK_ADDONS = os.path.join(game_dir("Deadlock"), "game", "citadel", "addons")
ADDON = "item_assist_reborn"
MARKER = "panorama/scripts/ql_feat_recommender.vjs_c"   # identifies our VPK when overwriting

# source (relative to ROOT) -> path inside the addon's panorama tree
SOURCES = {
    "mod_src/panorama/layout/hud_health.xml": "panorama/layout/hud_health.xml",
    "mod_src/panorama/scripts/ql_feat_recommender.js": "panorama/scripts/ql_feat_recommender.js",
    "data/recommender_data.js": "panorama/scripts/recommender_data.js",
    "mod_src/panorama/styles/recommender.css": "panorama/styles/recommender.css",
}
PREBUILT = os.path.join(ROOT, "mod_src", "prebuilt")
SETTINGS = os.path.join(ROOT, "settings.json")   # player's lasting panel choices


def wine_path(path):
    return "Z:" + os.path.abspath(path).replace("/", "\\")


def compiled_name(rel):
    stem, ext = os.path.splitext(rel)
    return stem + {".xml": ".vxml_c", ".js": ".vjs_c", ".css": ".vcss_c"}[ext]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pak", type=int, default=30)
    ap.add_argument("--cs2", default=CS2)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--addons", default=DEADLOCK_ADDONS, help="Deadlock addons directory")
    ap.add_argument("--wine", default="wine", help="Wine executable on Linux")
    ap.add_argument("--wine-prefix", default=os.environ.get("WINEPREFIX") or os.path.join(ROOT, "build", "wine"))
    a = ap.parse_args()

    a.cs2 = os.path.abspath(a.cs2)
    rc = os.path.join(a.cs2, "game", "bin", "win64", "resourcecompiler.exe")
    if not os.path.exists(rc):
        sys.exit(f"resourcecompiler not found: {rc}\nInstall the free 'Counter-Strike 2 Workshop Tools' DLC in Steam.")
    if not 1 <= a.pak <= 99:
        sys.exit("--pak must be between 1 and 99")
    if os.name != "nt" and not shutil.which(a.wine):
        sys.exit("Wine not found; install Wine or pass --wine")
    if os.name != "nt" and not os.path.isfile(os.path.join(a.cs2, "game", "bin", "win64", "tier0.dll")):
        sys.exit("Windows CS2 libraries missing. Enable Proton for CS2 in Steam and install Workshop Tools.")
    env = os.environ.copy()
    if os.name != "nt":
        os.makedirs(a.wine_prefix, exist_ok=True)
        env.update(WINEPREFIX=os.path.abspath(a.wine_prefix), WINEDEBUG="-all")
    content = os.path.join(a.cs2, "content", "csgo_addons", ADDON)
    game = os.path.join(a.cs2, "game", "csgo_addons", ADDON)

    files = {}
    for src, rel in SOURCES.items():
        s = os.path.join(ROOT, src)
        if not os.path.exists(s):
            sys.exit(f"missing {s} (run build.py / make_layout.py first)")
        dst = os.path.join(content, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(s, dst)
        if src == "data/recommender_data.js":
            # Panorama in Deadlock has no storage the panel could save to, so the player's lasting
            # choices (settings.json) ride along with the data as QOL_REC_DATA.userDefaults
            with open(SETTINGS, encoding="utf-8") as f:
                prefs = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
            with open(dst, "a", encoding="utf-8") as f:
                f.write("QOL_REC_DATA.userDefaults=" + json.dumps(prefs, separators=(",", ":")) + ";\n")
        out = os.path.join(game, *compiled_name(rel).split("/"))
        # Remove stale output: a previous successful build must never hide a new failure.
        if os.path.exists(out):
            os.remove(out)
        args = [rc, "-i", dst, "-nop4", "-f"]
        if os.name != "nt":
            args = [a.wine, rc, "-game", wine_path(os.path.join(a.cs2, "game", "csgo")),
                    "-i", wine_path(dst), "-nop4", "-f"]
        r = subprocess.run(args, cwd=os.path.dirname(rc), env=env,
                           capture_output=True, text=True, errors="replace")
        # Wine may return nonzero after RC has explicitly reported a successful compile.
        success = r.returncode == 0 or bool(re.search(r"OK:\s+1 compiled, 0 failed", r.stdout))
        if not success or not os.path.exists(out):
            print(r.stdout[-3000:], r.stderr[-2000:])
            sys.exit(f"compile failed: {rel}")
        with open(out, "rb") as f:
            files[compiled_name(rel)] = f.read()
        print(f"compiled {rel}")

    for dirpath, _, names in os.walk(PREBUILT):
        for n in names:
            p = os.path.join(dirpath, n)
            with open(p, "rb") as f:
                files[os.path.relpath(p, PREBUILT).replace("\\", "/")] = f.read()

    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", f"pak{a.pak:02d}_dir.vpk")
    vpk.write(out, files)
    print(f"wrote {out} ({os.path.getsize(out) / 1e6:.2f} MB, {len(files)} files)")

    if a.install:
        os.makedirs(a.addons, exist_ok=True)
        target = os.path.join(a.addons, os.path.basename(out))
        if os.path.exists(target) and MARKER not in vpk.read_entries(target):
            sys.exit(f"{target} is another mod — pick a free --pak number")
        shutil.copyfile(out, target)
        print(f"installed -> {target}")


if __name__ == "__main__":
    main()
