"""Rebuild the hud_health.xml entry point from the CURRENT game file.

Panorama has no mod script loader: our JS runs only if a layout the game loads includes it. We take
the game's own hud_health layout, decompile it, and add our <styles>/<scripts> includes, so after a
patch the HP bar stays Valve's latest version instead of a stale copy.

usage: python make_layout.py [--vrf path\\to\\Source2Viewer-CLI.exe]
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

import vpk

ROOT = os.path.join(os.path.dirname(__file__), "..")
GAME_PAK = r"C:\Program Files (x86)\Steam\steamapps\common\Deadlock\game\citadel\pak01_dir.vpk"
VRF = os.path.join(ROOT, "..", "tools", "vrf-cli-20.1", "Source2Viewer-CLI.exe")
HOST = "panorama/layout/hud_health.vxml_c"
OUT = os.path.join(ROOT, "mod_src", "panorama", "layout", "hud_health.xml")

STYLE_INCLUDE = '\t\t<include src="s2r://panorama/styles/recommender.vcss" />\n'
SCRIPTS = ('\t<scripts>\n'
           '\t\t<include src="s2r://panorama/scripts/recommender_data.vjs" />\n'
           '\t\t<include src="s2r://panorama/scripts/ql_feat_recommender.vjs" />\n'
           '\t</scripts>\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vrf", default=VRF)
    ap.add_argument("--pak", default=GAME_PAK)
    a = ap.parse_args()

    entries = vpk.read_entries(a.pak)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "hud_health.vxml_c")
        with open(src, "wb") as f:
            f.write(vpk.read_file(a.pak, entries[HOST]))
        dst = os.path.join(tmp, "hud_health.xml")
        subprocess.run([a.vrf, "-i", src, "-o", dst, "-d"], check=True, capture_output=True)
        xml = open(dst, encoding="utf-8").read()

    xml = re.sub(r"^<!--.*?-->\s*", "", xml)                       # drop the decompiler banner
    if "recommender" in xml:
        sys.exit("game layout already references recommender files — refusing to double-inject")
    xml, n1 = re.subn(r"(\t*</styles>)", STYLE_INCLUDE.replace("\\", "\\\\") + r"\1", xml, count=1)
    xml, n2 = re.subn(r"(\t*</styles>\n)", r"\1" + SCRIPTS, xml, count=1)
    if not (n1 and n2):
        sys.exit("could not find </styles> in the game layout — its structure changed, inject by hand")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
