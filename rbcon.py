"""Read Rock Band CON packages (Xbox 360 STFS) and turn single-mix ones into song folders.

Only packages whose audio (.mogg) is unencrypted are used. Encrypted audio is never
decrypted: those packages are reported and skipped. Packages whose instrument tracks
already contain audio are skipped too, since they already have separate stems.
"""

import io
import re
import struct
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

CON_MAGIC = (b"CON ", b"LIVE")
UNENCRYPTED_MOGG = 0x0A

# Rock Band "rank" values at which each difficulty tier starts (tiers 1..7 -> ini 0..6).
RANK_TIERS = {
    "drum": [124, 151, 178, 242, 345, 448], "guitar": [139, 176, 221, 267, 333, 409],
    "bass": [135, 181, 228, 293, 364, 436], "vocals": [132, 175, 218, 279, 353, 427],
    "keys": [153, 211, 269, 327, 385, 443], "real_keys": [153, 211, 269, 327, 385, 443],
    "real_guitar": [150, 205, 264, 323, 382, 442], "real_bass": [150, 208, 267, 325, 384, 442],
    "band": [163, 215, 243, 267, 292, 345],
}
INSTRUMENT_TRACKS = ("drum", "bass", "guitar", "vocals", "keys")
# Below this level (relative to the backing track) an instrument track counts as silent.
SILENT_DB = -50.0


def looks_like_con(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) in CON_MAGIC
    except OSError:
        return False


# ---------------------------------------------------------------- STFS container

class Package:
    """Minimal read-only Xbox 360 STFS reader (enough for Rock Band CON files)."""

    def __init__(self, path):
        self.data = Path(path).read_bytes()
        d = self.data
        if d[:4] not in CON_MAGIC:
            raise ValueError("not a CON/LIVE package")
        header_size = struct.unpack(">I", d[0x340:0x344])[0]
        vd = d[0x379:0x379 + 0x24]
        self.sex = (~vd[2]) & 1
        self.first = (header_size + 0xFFF) & 0xF000
        count = struct.unpack("<H", vd[3:5])[0]
        block = int.from_bytes(vd[5:8], "little")
        self.entries = []
        for _ in range(count):
            raw = self._block(block)
            for i in range(0, 0x1000, 0x40):
                e = raw[i:i + 0x40]
                flags = e[0x28]
                if not flags & 0x3F:
                    continue
                self.entries.append({
                    "name": e[:flags & 0x3F].decode("latin1"), "dir": bool(flags & 0x80),
                    "consecutive": bool(flags & 0x40), "blocks": int.from_bytes(e[0x29:0x2C], "little"),
                    "start": int.from_bytes(e[0x2F:0x32], "little"),
                    "parent": struct.unpack(">h", e[0x32:0x34])[0], "size": struct.unpack(">I", e[0x34:0x38])[0],
                })
            block += 1
        self.files = {self._path(i): e for i, e in enumerate(self.entries) if not e["dir"]}

    def _backing(self, b):
        r = (((b + 0xAA) // 0xAA) << self.sex) + b
        if b < 0xAA:
            return r
        if b < 0x70E4:
            return r + (((b + 0x70E4) // 0x70E4) << self.sex)
        return (1 << self.sex) + r + (((b + 0x70E4) // 0x70E4) << self.sex)

    def _block(self, b):
        off = (self._backing(b) << 12) + self.first
        return self.data[off:off + 0x1000]

    def _next(self, b):
        table = self._backing((b // 0xAA) * 0xAA) - (1 << self.sex)
        off = (table << 12) + self.first + (b % 0xAA) * 0x18
        return int.from_bytes(self.data[off + 0x15:off + 0x18], "big")

    def _path(self, i):
        e = self.entries[i]
        return e["name"] if e["parent"] == -1 else self._path(e["parent"]) + "/" + e["name"]

    def read(self, path):
        e = self.files[path]
        out, b = bytearray(), e["start"]
        for _ in range(e["blocks"]):
            out += self._block(b)
            b = b + 1 if e["consecutive"] else self._next(b)
        return bytes(out[:e["size"]])


# ---------------------------------------------------------------- songs.dta

def parse_dta(text):
    """Parse DTA s-expressions into nested Python lists (symbols and strings become str)."""
    tokens, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == ";":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "([{":
            tokens.append("("); i += 1
        elif c in ")]}":
            tokens.append(")"); i += 1
        elif c in "\"'":
            j = text.find(c, i + 1)
            j = n if j < 0 else j
            tokens.append(("str", text[i + 1:j])); i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()[]{};\"'":
                j += 1
            tokens.append(("atom", text[i:j])); i = j
    stack = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
        else:
            kind, v = t
            if kind == "atom":
                try:
                    v = int(v)
                except ValueError:
                    try:
                        v = float(v)
                    except ValueError:
                        pass
            stack[-1].append(v)
    while len(stack) > 1:
        done = stack.pop()
        stack[-1].append(done)
    return stack[0]


def find(node, key):
    """Values of the first child list (key ...) of node, or None."""
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child[1:]
    return None


def first(node, key, default=None):
    v = find(node, key)
    return v[0] if v else default


def dta_songs(dta):
    """Song entries in a songs.dta (unwrapping the extra outer list some tools write)."""
    songs = [n for n in dta if isinstance(n, list) and n and isinstance(n[0], str) and find(n, "song")]
    if not songs and len(dta) == 1 and isinstance(dta[0], list):
        return dta_songs(dta[0])
    return songs


def _channels(v):
    if isinstance(v, list):
        return [int(x) for x in v if isinstance(x, (int, float))]
    return [int(v)] if isinstance(v, (int, float)) else []


# ---------------------------------------------------------------- inspection

def _mogg_ogg(mogg):
    return mogg[struct.unpack("<I", mogg[4:8])[0]:]


def inspect(path):
    """Classify a CON package. Returns a dict with "status":
    convertible | encrypted | multitrack | pack | unsupported (with "reason")."""
    try:
        pkg = Package(path)
        raw_dta = pkg.read("songs/songs.dta")
    except Exception as e:
        return {"status": "unsupported", "reason": f"could not read package ({e})"}
    text = raw_dta.decode("utf-8" if b"'encoding' 'utf8'" in raw_dta or b"(encoding utf8)" in raw_dta else "latin1",
                          errors="replace")
    songs = dta_songs(parse_dta(text))
    if len(songs) != 1:
        return {"status": "pack" if len(songs) > 1 else "unsupported",
                "reason": f"contains {len(songs)} songs" if songs else "no songs in songs.dta"}
    song = songs[0]
    info = {"name": str(first(song, "name", "")), "artist": str(first(song, "artist", "")), "id": str(song[0])}
    song_node = find(song, "song") or []
    base = str(first(song_node, "name", ""))
    mogg_path = base + ".mogg"
    if mogg_path not in pkg.files:
        return {**info, "status": "unsupported", "reason": "audio file not found"}
    mogg = pkg.read(mogg_path)
    if mogg[0] != UNENCRYPTED_MOGG:
        return {**info, "status": "encrypted", "reason": "audio is encrypted"}
    tracks = {t[0]: _channels(t[1]) for t in (first(song_node, "tracks") or []) if isinstance(t, list) and len(t) > 1}
    crowd = _channels(first(song_node, "crowd_channels") or [])
    with tempfile.TemporaryDirectory() as tmp:
        ogg = Path(tmp) / "a.ogg"
        ogg.write_bytes(_mogg_ogg(mogg))
        with sf.SoundFile(str(ogg)) as f:
            nch, frames, sr = f.channels, f.frames, f.samplerate
            power = np.zeros(nch)
            for k in range(12):  # sample 12 one-second slices across the song
                f.seek(int(frames * (k + 0.5) / 12))
                block = f.read(sr, dtype="float32", always_2d=True)
                power += np.square(block, dtype=np.float64).mean(axis=0) if len(block) else 0
    inst = sorted({c for name, chans in tracks.items() if name in INSTRUMENT_TRACKS for c in chans if c < nch})
    backing = [c for c in range(nch) if c not in inst and c not in crowd]
    if not backing:
        return {**info, "status": "multitrack", "reason": "no backing track"}
    db = 10 * np.log10(power + 1e-20)
    ref = db[backing].max()
    if inst and db[inst].max() > ref + SILENT_DB:
        return {**info, "status": "multitrack", "reason": "already has separate instrument tracks"}
    return {**info, "status": "convertible", "reason": "single mix in the backing track"}


# ---------------------------------------------------------------- conversion

def _tier(ranks, part):
    r = ranks.get(part, 0)
    return -1 if not r else sum(r >= t for t in RANK_TIERS[part])


def song_ini(song, raw_text):
    ranks = {}
    for item in find(song, "rank") or []:
        if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], (int, float)):
            ranks[str(item[0])] = int(item[1])
    song_node = find(song, "song") or []
    preview = find(song, "preview") or []
    author = re.search(r";\s*Song authored by\s*(.+)", raw_text)
    vocal_parts = first(song_node, "vocal_parts", 1)
    ini = {
        "name": first(song, "name"), "artist": first(song, "artist"), "album": first(song, "album_name"),
        "track": first(song, "album_track_number"), "year": first(song, "year_released"),
        "genre": first(song, "genre"), "charter": first(song, "author") or (author.group(1).strip() if author else None),
        "song_length": first(song, "song_length"),
        "preview_start_time": preview[0] if preview else None, "preview_end_time": preview[1] if len(preview) > 1 else None,
        "vocal_gender": first(song, "vocal_gender"), "pro_drums": "True", "five_lane_drums": "False",
        "star_power_note": 116, "multiplier_note": 116,
        "hopo_frequency": first(song_node, "hopo_threshold"),
        "diff_band": _tier(ranks, "band"), "diff_guitar": _tier(ranks, "guitar"), "diff_bass": _tier(ranks, "bass"),
        "diff_drums": _tier(ranks, "drum"), "diff_drums_real": _tier(ranks, "drum"),
        "diff_keys": _tier(ranks, "keys"), "diff_keys_real": _tier(ranks, "real_keys"),
        "diff_guitar_real": _tier(ranks, "real_guitar"), "diff_bass_real": _tier(ranks, "real_bass"),
        "diff_vocals": _tier(ranks, "vocals"),
        "diff_vocals_harm": _tier(ranks, "vocals") if isinstance(vocal_parts, int) and vocal_parts > 1 else -1,
        "loading_phrase": "Converted from a Rock Band CON by yarg-stem-separator",
    }
    return "[song]\n" + "".join(f"{k} = {v}\n" for k, v in ini.items() if v is not None)


def png_xbox_to_png(raw):
    """Decode Rock Band album art (.png_xbox: 32-byte header + byte-swapped DXT1/DXT5) to PNG bytes."""
    from PIL import Image
    width, height = struct.unpack("<HH", raw[7:11])
    fourcc = b"DXT1" if raw[1] == 0x04 else b"DXT5"
    block = 8 if fourcc == b"DXT1" else 16
    size = max(1, width // 4) * max(1, height // 4) * block
    body = bytearray(raw[32:32 + size])
    body[0::2], body[1::2] = body[1::2], body[0::2]  # Xbox stores 16-bit words big-endian
    dds = (b"DDS " + struct.pack("<7I", 124, 0x1007 | 0x80000, height, width, size, 0, 1) + b"\0" * 44
           + struct.pack("<2I", 32, 0x4) + fourcc + b"\0" * 20 + struct.pack("<I", 0x1000) + b"\0" * 16)
    img = Image.open(io.BytesIO(dds + bytes(body)))
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def safe_folder_name(text):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip().rstrip(".")
    return name[:120] or "Converted song"


def extract(path, dest):
    """Write song.ogg (backing mix), notes.mid, song.ini and album.png (if decodable) into dest.

    Returns the folder name to use in the library (\"Artist - Title\")."""
    import stemsplit  # local import: stemsplit imports this module lazily too
    info = inspect(path)
    if info["status"] != "convertible":
        raise ValueError(f"{Path(path).name}: {info['reason']}")
    pkg = Package(path)
    raw_dta = pkg.read("songs/songs.dta")
    text = raw_dta.decode("utf-8" if b"utf8" in raw_dta else "latin1", errors="replace")
    song = dta_songs(parse_dta(text))[0]
    song_node = find(song, "song") or []
    base = str(first(song_node, "name"))
    tracks = {t[0]: _channels(t[1]) for t in (first(song_node, "tracks") or []) if isinstance(t, list) and len(t) > 1}
    crowd = _channels(first(song_node, "crowd_channels") or [])
    vols = [float(v) for v in (first(song_node, "vols") or []) if isinstance(v, (int, float))]
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        ogg = Path(tmp) / "a.ogg"
        ogg.write_bytes(_mogg_ogg(pkg.read(base + ".mogg")))
        with sf.SoundFile(str(ogg)) as f:
            nch, sr = f.channels, f.samplerate
            inst = {c for name, chans in tracks.items() if name in INSTRUMENT_TRACKS for c in chans}
            backing = [c for c in range(nch) if c not in inst and c not in crowd]
            gain = np.array([10 ** ((vols[c] if c < len(vols) else 0.0) / 20) for c in backing], dtype=np.float32)
            parts = [blk[:, backing] * gain for blk in f.blocks(blocksize=sr * 10, dtype="float32", always_2d=True)]
    audio = np.concatenate(parts)
    if audio.shape[1] > 2:  # several backing pairs: fold into stereo
        audio = np.stack([audio[:, 0::2].sum(axis=1), audio[:, 1::2].sum(axis=1)], axis=1)
    stemsplit.write_ogg(dest / "song.ogg", audio, sr)
    (dest / "notes.mid").write_bytes(pkg.read(base + ".mid"))
    (dest / "song.ini").write_text(song_ini(song, text), encoding="utf-8")
    art = next((p for p in pkg.files if p.endswith("_keep.png_xbox")), None)
    if art:
        try:
            (dest / "album.png").write_bytes(png_xbox_to_png(pkg.read(art)))
        except Exception:
            pass  # album art is optional
    return safe_folder_name(f"{first(song, 'artist', '')} - {first(song, 'name', '')}")
