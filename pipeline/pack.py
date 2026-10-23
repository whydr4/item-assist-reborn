"""Compile the mod with CS2's resourcecompiler (Workshop Tools DLC) and pack it into a VPK.

Deadlock has no public tools; CS2's resourcecompiler produces Panorama resources (_c) Deadlock loads.
It compiles files placed under <cs2>/content/csgo_addons/<addon>/ into <cs2>/game/csgo_addons/<addon>/.

usage: python pack.py [--pak 30] [--install]
  --install  copy dist/pakNN_dir.vpk into Deadlock's addons (refuses to overwrite a different mod)
"""
import argparse
import os
import shutil
import subprocess
import sys

import vpk

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive"
DEADLOCK_ADDONS = r"C:\Program Files (x86)\Steam\steamapps\common\Deadlock\game\citadel\addons"
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


def compiled_name(rel):
    stem, ext = os.path.splitext(rel)
    return stem + {".xml": ".vxml_c", ".js": ".vjs_c", ".css": ".vcss_c"}[ext]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pak", type=int, default=30)
    ap.add_argument("--cs2", default=CS2)
    ap.add_argument("--install", action="store_true")
    a = ap.parse_args()

    rc = os.path.join(a.cs2, "game", "bin", "win64", "resourcecompiler.exe")
    if not os.path.exists(rc):
        sys.exit(f"resourcecompiler not found: {rc}\nInstall the free 'Counter-Strike 2 Workshop Tools' DLC in Steam.")
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
        r = subprocess.run([rc, "-i", dst, "-nop4", "-f"], capture_output=True, text=True, errors="replace")
        out = os.path.join(game, *compiled_name(rel).split("/"))
        if r.returncode != 0 or not os.path.exists(out):
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
        target = os.path.join(DEADLOCK_ADDONS, os.path.basename(out))
        if os.path.exists(target) and MARKER not in vpk.read_entries(target):
            sys.exit(f"{target} is another mod — pick a free --pak number")
        shutil.copyfile(out, target)
        print(f"installed -> {target}")


if __name__ == "__main__":
    main()
