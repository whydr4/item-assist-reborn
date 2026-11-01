"""Discover Steam games on Windows and Linux, including additional library folders."""
import os
import re
from pathlib import Path


def steam_roots():
    roots = [Path.home() / ".local/share/Steam", Path.home() / ".steam/steam",
             Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]) / "Steam")
    roots.append(Path(r"C:\Program Files (x86)\Steam"))
    libraries = []
    for root in roots:
        if root not in libraries and root.exists():
            libraries.append(root)
        manifest = root / "steamapps/libraryfolders.vdf"
        if manifest.is_file():
            for value in re.findall(r'"path"\s+"([^"\n]+)"', manifest.read_text(errors="replace")):
                library = Path(value.replace("\\\\", "\\"))
                if library not in libraries:
                    libraries.append(library)
    return libraries


def game_dir(name):
    for root in steam_roots():
        candidate = root / "steamapps/common" / name
        if candidate.is_dir():
            return str(candidate.resolve())
    return str(Path(r"C:\Program Files (x86)\Steam\steamapps\common") / name)
