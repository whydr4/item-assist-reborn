"""Minimal VPK v2 reader (multi-archive) and single-file writer."""
import os
import struct
import zlib


def _cstr(f):
    out = bytearray()
    while True:
        c = f.read(1)
        if not c or c == b"\x00":
            return out.decode("utf-8", "replace")
        out += c


def read_entries(dir_path):
    """-> {path: (archive_index, offset, length, preload_bytes)}; offsets are absolute for embedded data."""
    entries = {}
    with open(dir_path, "rb") as f:
        sig, ver, tree_size = struct.unpack("<III", f.read(12))
        if sig != 0x55AA1234:
            raise ValueError(f"not a VPK: {dir_path}")
        header = 12
        if ver == 2:
            f.read(16)
            header = 28
        data_start = header + tree_size
        while ext := _cstr(f):
            while path := _cstr(f):
                while name := _cstr(f):
                    _crc, preload, arch, off, length, _term = struct.unpack("<IHHIIH", f.read(18))
                    pre = f.read(preload)
                    full = (path.strip() + "/" if path.strip() else "") + name + "." + ext
                    entries[full] = (arch, data_start + off if arch == 0x7FFF else off, length, pre)
    return entries


def read_file(dir_path, entry):
    arch, off, length, pre = entry
    src = dir_path if arch == 0x7FFF else dir_path.replace("_dir.vpk", f"_{arch:03d}.vpk")
    with open(src, "rb") as f:
        f.seek(off)
        return pre + f.read(length)


def write(out_path, files):
    """files: {"panorama/scripts/x.vjs_c": bytes}. Writes a self-contained v2 _dir.vpk."""
    tree = {}
    for p, data in files.items():
        d, base = os.path.split(p.replace("\\", "/"))
        name, ext = base.rsplit(".", 1)
        tree.setdefault(ext, {}).setdefault(d or " ", []).append((name, data))
    tree_bytes, blob = bytearray(), bytearray()
    for ext, dirs in tree.items():
        tree_bytes += ext.encode() + b"\x00"
        for d, names in dirs.items():
            tree_bytes += d.encode() + b"\x00"
            for name, data in names:
                tree_bytes += name.encode() + b"\x00"
                tree_bytes += struct.pack("<IHHIIH", zlib.crc32(data) & 0xFFFFFFFF, 0, 0x7FFF, len(blob), len(data), 0xFFFF)
                blob += data
            tree_bytes += b"\x00"
        tree_bytes += b"\x00"
    tree_bytes += b"\x00"
    header = struct.pack("<IIIIIII", 0x55AA1234, 2, len(tree_bytes), len(blob), 0, 0, 0)
    with open(out_path, "wb") as f:
        f.write(header + tree_bytes + blob)
