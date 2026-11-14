"""Chart doctor: check a guitar or bass chart against its separated stem, and fix it.

Custom charts are sometimes made with an auto-charter and never played: a riff of three notes
charted as two frets alternating, the same note jumping between frets, Hard copied from Expert.
The stems this program splits out make that visible. The doctor page (/doctor in review.py)
shows the stem's pitch under the chart's notes, finds the chart's repeated riffs, and lets you
re-fret a riff once and apply it to every repeat, and add, remove and change any note. Saving
rewrites only that instrument's notes in notes.mid, after keeping the original as
notes.mid.orig, and adds your name to song.ini's charter credits.

A note, here and on the page: {"tick", "len", "lanes", "force", "tap", "lens"}. lanes are
0-4 for green to orange and 5 (OPEN) for an open note; force is "H" (forced HOPO), "S"
(forced strum) or absent; tap is read only; lens gives each fret's own sustain where a chord's
sustains differ.

  load(song_dir)                -> the chart's tempo map and guitar / bass notes, for the page
  pitch(song_dir, instrument)   -> the stem's pitch every 10 ms (cached)
  save(song_dir, instrument, expert_notes, rebuild_lower, credit)
  restore(song_dir)             -> put notes.mid.orig (and the song.ini credits) back

  python chartdoctor.py "path/to/song folder"     prints the chart's notes and riff summary
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np

import chartfile
import stemsplit

INSTRUMENTS = {"guitar": ("PART GUITAR", "guitar.ogg"), "bass": ("PART BASS", "rhythm.ogg")}
GEM_BASE = chartfile.GEM_BASE  # {difficulty: green note}; green..orange is base..base+4
FORCE = {"H": 5, "S": 6}  # base+5 forces a HOPO, base+6 a strum
OPEN = 5  # lane number of an open note: base-1 in notes.mid, with an [ENHANCED_OPENS] event
PS_SYSEX = (0x50, 0x53, 0, 0)  # Phase Shift sysex: difficulty (0xFF = all), type (1 open, 4 tap), on/off
BACKUP = "notes.mid.orig"
SIDECAR = "chartdoctor.json"  # what the doctor changed in song.ini, so restore can undo it
HOP = 0.01  # pitch frames
SR = 16000
CACHE_VERSION = 2
# Lower difficulties made from Expert: (shortest gap between notes in seconds, largest chord,
# Expert lane -> this difficulty's lane). Contour is kept; orange folds down on Medium, etc.
REDUCE = {2: (0.2, 2, [0, 1, 2, 3, 4, OPEN]), 1: (0.3, 2, [0, 1, 2, 2, 3, OPEN]),
          0: (0.6, 1, [0, 0, 1, 1, 2, OPEN])}


def _midi(song_dir):
    import mido
    path = Path(song_dir) / "notes.mid"
    if not path.exists():
        raise ValueError("this song has no notes.mid (.chart songs aren't supported here yet)")
    return mido.MidiFile(str(path))


def _absolute(track):
    t, out = 0, []
    for msg in track:
        t += msg.time
        out.append((t, msg))
    return out


def tempo_map(mid):
    """[(tick, microseconds per beat)] and [(tick, numerator, denominator)] from the first track."""
    tempos, sigs = [], []
    for tick, msg in _absolute(mid.tracks[0]):
        if msg.type == "set_tempo":
            tempos.append((tick, msg.tempo))
        elif msg.type == "time_signature":
            sigs.append((tick, msg.numerator, msg.denominator))
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0, 500000))
    if not sigs or sigs[0][0] > 0:
        sigs.insert(0, (0, 4, 4))
    return tempos, sigs


def seconds(tick, tempos, tpb):
    s, last_tick, last_tempo = 0.0, 0, tempos[0][1]
    for t, tempo in tempos:
        if t > tick:
            break
        s += (t - last_tick) / tpb * last_tempo / 1e6
        last_tick, last_tempo = t, tempo
    return s + (tick - last_tick) / tpb * last_tempo / 1e6


def _is_open_event(msg):
    return msg.is_meta and getattr(msg, "text", "").strip().strip("[]") == "ENHANCED_OPENS"


def parse_track(track):
    """{difficulty: [note]} for a 5-fret track: gems, opens (either way of marking them),
    forced HOPOs / strums and taps."""
    events = _absolute(track)
    enhanced = any(_is_open_event(msg) for _, msg in events)
    ranges, sysex_on, held = {}, {}, {}
    gems = {d: {} for d in GEM_BASE}  # difficulty -> tick -> {lane: length}
    for tick, msg in events:
        if msg.type == "sysex" and tuple(msg.data[:4]) == PS_SYSEX and len(msg.data) >= 7:
            kind = {1: "open", 4: "tap"}.get(msg.data[5])
            for d in (range(4) if msg.data[4] == 0xFF else [msg.data[4]]):
                if kind and msg.data[6]:
                    sysex_on[(kind, d)] = tick
                elif kind and (kind, d) in sysex_on:
                    ranges.setdefault((kind, d), []).append((sysex_on.pop((kind, d)), tick))
            continue
        if msg.type not in ("note_on", "note_off"):
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            held[msg.note] = tick
            continue
        if msg.note not in held:
            continue
        start, p = held.pop(msg.note), msg.note
        if p == chartfile.TAP:
            ranges.setdefault("tap", []).append((start, tick))
        for d, base in GEM_BASE.items():
            if base <= p <= base + 4 or (enhanced and p == base - 1):
                lane = OPEN if p == base - 1 else p - base
                g = gems[d].setdefault(start, {})
                g[lane] = max(g.get(lane, 0), tick - start)
            for flag, off in FORCE.items():
                if p == base + off:
                    ranges.setdefault((flag, d), []).append((start, tick))

    def inside(key, t):
        return any(a <= t < max(b, a + 1) for a, b in ranges.get(key, ()))

    out = {}
    for d in GEM_BASE:
        notes = []
        for t in sorted(gems[d]):
            lanes = gems[d][t]
            if list(lanes) == [0] and inside(("open", d), t):  # Phase Shift's way: a green note made open
                lanes = {OPEN: lanes[0]}
            n = {"tick": t, "len": max(lanes.values()), "lanes": sorted(lanes)}
            if len(set(lanes.values())) > 1:
                n["lens"] = {str(l): v for l, v in lanes.items()}
            if inside(("H", d), t):
                n["force"] = "H"
            elif inside(("S", d), t):
                n["force"] = "S"
            if inside("tap", t) or inside(("tap", d), t):
                n["tap"] = True
            notes.append(n)
        out[d] = notes
    return out


def gems(track, difficulty):
    """[[tick, length, [lanes]]] for one difficulty (open notes are lane 5)."""
    return [[n["tick"], n["len"], n["lanes"]] for n in parse_track(track)[difficulty]]


def song_info(song_dir):
    ini = chartfile.ini_values(Path(song_dir) / "song.ini")
    return {k: ini.get(k, "") for k in ("name", "artist", "charter", "frets", "delay",
                                         "hopo_frequency", "eighthnote_hopo", "sustain_cutoff_threshold")}


def load(song_dir):
    song_dir = Path(song_dir)
    mid = _midi(song_dir)
    tempos, sigs = tempo_map(mid)
    end = max(sum(m.time for m in tr) for tr in mid.tracks)
    by_name = {tr.name: tr for tr in mid.tracks}
    instruments = {}
    for inst, (track_name, stem) in INSTRUMENTS.items():
        tr = by_name.get(track_name)
        if tr is None:
            continue
        diffs = parse_track(tr)
        if not diffs[3]:
            continue
        instruments[inst] = {"track": track_name, "stem": stem if (song_dir / stem).exists() else None,
                             "difficulties": diffs}
    info, tpb = song_info(song_dir), mid.ticks_per_beat
    cutoff = chartfile._int(info, "sustain_cutoff_threshold")
    return {"dir": str(song_dir), "info": info, "tpb": tpb,
            # a note is a natural HOPO this close after the one before; shorter sustains don't hold
            "hopo_threshold": chartfile._hopo_thresholds(tpb, info)[1],
            "sustain_cutoff": cutoff if cutoff is not None else tpb // 3,
            "tempos": tempos, "time_signatures": sigs, "end_tick": end,
            "duration": seconds(end, tempos, mid.ticks_per_beat), "instruments": instruments,
            "audio": sorted(p.name for p in song_dir.glob("*.ogg")),
            "has_backup": (song_dir / BACKUP).exists()}


def find_songs(root, limit=5000):
    """Song folders under root with a notes.mid: [{path, artist, name, guitar, bass}]."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        names = {f.lower() for f in filenames}
        if "notes.mid" not in names:
            continue
        info = song_info(dirpath)
        out.append({"path": str(Path(dirpath).resolve()), "artist": info["artist"],
                    "name": info["name"] or Path(dirpath).name,
                    "stems": [i for i, (_, stem) in INSTRUMENTS.items() if stem in names],
                    "edited": BACKUP.lower() in names})
        if len(out) >= limit:
            break
    return sorted(out, key=lambda s: (s["artist"].lower(), s["name"].lower()))


# ---------------------------------------------------------------- pitch

def _cache_path(cache_dir, stem_path):
    st = stem_path.stat()
    key = hashlib.sha1(f"{stem_path.resolve()}|{st.st_size}|{int(st.st_mtime)}|{CACHE_VERSION}".encode()).hexdigest()
    return Path(cache_dir) / f"{key[:20]}.json"


def cached_pitch(song_dir, instrument, cache_dir):
    stem_path = Path(song_dir) / INSTRUMENTS[instrument][1]
    if not stem_path.exists():
        raise ValueError(f"no {stem_path.name} in this song folder")
    path = _cache_path(cache_dir, stem_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pitch(song_dir, instrument, cache_dir, log=print):
    """The stem's pitch: {"hop": seconds, "midi": [...], "conf": [...], "level": [...]} (cached).

    midi is 0 where nothing is pitched; conf is CREPE's periodicity (0-1); level is loudness
    in dB below the loudest frame, so quiet bleed can be told apart from the part itself."""
    found = cached_pitch(song_dir, instrument, cache_dir)
    if found:
        return found
    import librosa
    import scipy.signal
    import torch
    import torchcrepe
    stem_path = Path(song_dir) / INSTRUMENTS[instrument][1]
    log(f"Reading {stem_path.name}")
    data, sr = stemsplit.read_audio(stem_path)
    x = scipy.signal.resample_poly(data.mean(axis=1), SR, sr).astype(np.float32)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = "full" if dev == "cuda" else "tiny"
    hop = int(SR * HOP)
    fmin = 33 if instrument == "bass" else 60  # CREPE goes no lower than 32.7 Hz (C1)
    f0s, pers = [], []
    chunk = SR * 30  # bounded memory for long songs; chunks are whole frames apart
    for i, a in enumerate(range(0, len(x), chunk)):
        log(f"Tracking pitch ({a // SR}s of {len(x) // SR}s, {dev})")
        seg = torch.from_numpy(x[a:a + chunk])[None]
        f0, per = torchcrepe.predict(seg, SR, hop_length=hop, fmin=fmin, fmax=1500, model=model,
                                     batch_size=1024, device=dev, return_periodicity=True)
        n = (min(chunk, len(x) - a) + hop - 1) // hop
        f0s.append(f0[0, :n].cpu().numpy())
        pers.append(per[0, :n].cpu().numpy())
    f0, per = np.concatenate(f0s), np.concatenate(pers)
    per = np.clip(np.nan_to_num(per, nan=0.0, posinf=0.0, neginf=0.0), 0, 1)  # silent stretches give -inf
    f0 = np.nan_to_num(f0, nan=0.0, posinf=0.0, neginf=0.0)
    rms = librosa.feature.rms(y=x, frame_length=1024, hop_length=hop, center=True)[0][:len(f0)]
    db = 20 * np.log10(rms + 1e-9)
    db = np.maximum(db - db.max(), -80)
    midi = 69 + 12 * np.log2(np.maximum(f0, 1) / 440)
    result = {"hop": HOP, "model": model,
              "midi": np.round(np.where(per > 0.1, midi, 0), 2).tolist(),
              "conf": np.round(per, 2).tolist(), "level": np.round(db, 1).tolist()}
    path = _cache_path(cache_dir, stem_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)
    return result


# ---------------------------------------------------------------- writing

def _check_notes(notes):
    out, seen = [], set()
    for n in notes:
        try:
            tick, length = int(n["tick"]), int(n["len"])
            lanes = sorted({int(l) for l in n["lanes"]})
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"bad note {n!r}")
        bad = (tick < 0 or length < 0 or not lanes or not all(0 <= l <= OPEN for l in lanes)
               or (OPEN in lanes and len(lanes) > 1) or n.get("force") not in (None, "H", "S"))
        if bad or tick in seen:
            raise ValueError(f"bad note {n!r}")
        seen.add(tick)
        note = {"tick": tick, "len": length, "lanes": lanes}
        if n.get("force"):
            note["force"] = n["force"]
        lens = n.get("lens")
        if isinstance(lens, dict) and set(lens) == {str(l) for l in lanes}:
            note["lens"] = {k: max(0, int(v)) for k, v in lens.items()}
        out.append(note)
    return sorted(out, key=lambda n: n["tick"])


def reduce(expert, difficulty, tempos, tpb):
    """A lower difficulty from Expert notes: fewer notes when they come fast, smaller chords,
    fewer frets. Notes on the beat are kept over the off-beat ones around them."""
    gap, max_chord, lane_map = REDUCE[difficulty]
    out, last = [], -1e9
    times = [seconds(n["tick"], tempos, tpb) for n in expert]
    for i, n in enumerate(expert):
        tick, t = n["tick"], times[i]
        if t - last < gap:
            continue
        if tick % tpb and i + 1 < len(expert):  # off the beat: give way to a nearby note on it
            nt = expert[i + 1]["tick"]
            if nt % tpb == 0 and times[i + 1] - t < gap and times[i + 1] - last >= gap:
                continue
        mapped = sorted({lane_map[l] for l in n["lanes"]})
        if len(mapped) > max_chord:
            mapped = [mapped[0], mapped[-1]] if max_chord == 2 else [mapped[0]]
        out.append({"tick": tick, "len": n["len"], "lanes": mapped})
        last = t
    return out


def _rewrite_track(track, model):
    """The track with every difficulty's gems, opens and HOPO / strum markers written from model;
    everything else in it (star power, solos, taps, events) is kept as it was."""
    import mido
    drop = set()
    for base in GEM_BASE.values():
        drop.update(range(base - 1, base + 7))
    events = _absolute(track)
    end = events[-1][0] if events else 0

    def keep(msg):
        if msg.type == "end_of_track" or _is_open_event(msg):
            return False
        if msg.type in ("note_on", "note_off") and msg.note in drop:
            return False
        return not (msg.type == "sysex" and tuple(msg.data[:4]) == PS_SYSEX and len(msg.data) >= 7 and msg.data[5] == 1)
    kept = [(t, msg) for t, msg in events if keep(msg)]
    added = []
    if any(OPEN in n["lanes"] for notes in model.values() for n in notes):
        added.append((0, 0, mido.MetaMessage("text", text="[ENHANCED_OPENS]")))
    for d, notes in model.items():
        base = GEM_BASE[d]
        for lane in list(range(5)) + [OPEN]:
            pitch = base - 1 if lane == OPEN else base + lane
            mine = [(n["tick"], n.get("lens", {}).get(str(lane), n["len"])) for n in notes if lane in n["lanes"]]
            for (t, length), nxt in zip(mine, [m[0] for m in mine[1:]] + [None]):
                off = t + max(length, 1)
                if nxt is not None:
                    off = min(off, nxt)  # a sustain may not run into the next note on that fret
                added.append((t, 2, mido.Message("note_on", note=pitch, velocity=100)))
                added.append((off, 1, mido.Message("note_off", note=pitch, velocity=0)))
        for n in notes:
            if n.get("force"):
                pitch = base + FORCE[n["force"]]
                added.append((n["tick"], 2, mido.Message("note_on", note=pitch, velocity=100)))
                added.append((n["tick"] + 1, 1, mido.Message("note_off", note=pitch, velocity=0)))

    def rank(msg):  # at one tick: meta events, then note-offs, then everything else
        if msg.is_meta:
            return 0
        if msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            return 1
        return 2
    order = [(t, rank(msg), i, msg) for i, (t, msg) in enumerate(kept)]
    order += [(t, o, len(order) + i, msg) for i, (t, o, msg) in enumerate(added)]
    order.sort(key=lambda e: e[:3])
    out, now = mido.MidiTrack(), 0
    for t, _, _, msg in order:
        out.append(msg.copy(time=t - now))
        now = t
    out.append(mido.MetaMessage("end_of_track", time=max(0, end - now)))
    return out


def save(song_dir, instrument, expert, rebuild_lower=False, credit=None):
    """Replace one instrument's Expert notes (and optionally rebuild Hard/Medium/Easy from them).

    Everything else in notes.mid stays as it was: other instruments, star power, solos, taps,
    events, and the other difficulties unless they're rebuilt. The first save keeps
    notes.mid.orig. credit (your charter name) is added to song.ini's charter and frets."""
    song_dir = Path(song_dir)
    if instrument not in INSTRUMENTS:
        raise ValueError("unknown instrument")
    path = song_dir / "notes.mid"
    mid = _midi(song_dir)
    track_name = INSTRUMENTS[instrument][0]
    index = next((i for i, tr in enumerate(mid.tracks) if tr.name == track_name), None)
    if index is None:
        raise ValueError(f"notes.mid has no {track_name} track")
    tempos, _ = tempo_map(mid)
    model = parse_track(mid.tracks[index])
    model[3] = _check_notes(expert)
    if rebuild_lower:
        model.update({d: reduce(model[3], d, tempos, mid.ticks_per_beat) for d in (2, 1, 0)})
    mid.tracks[index] = _rewrite_track(mid.tracks[index], model)
    backup = song_dir / BACKUP
    if not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name("notes.mid.tmp")
    mid.save(str(tmp))
    os.replace(tmp, path)
    return {"counts": {d: len(n) for d, n in model.items()}, "credited": add_credit(song_dir, credit)}


def add_credit(song_dir, name):
    """Add name to song.ini's charter (and frets, where present). Returns the charter value."""
    ini = Path(song_dir) / "song.ini"
    if not (name or "").strip() or not ini.exists():
        return None
    lines, fmt = chartfile.read_ini_lines(ini)
    new, changed = chartfile.credit_lines(lines, name, ("charter", "frets"))
    if changed:
        side = Path(song_dir) / SIDECAR
        try:
            record = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        if "song_ini" not in record:  # the credits as they were before the doctor's first change
            before = chartfile.ini_values(ini)
            record["song_ini"] = {k: before.get(k) for k in ("charter", "frets")}
            side.write_text(json.dumps(record, indent=1), encoding="utf-8")
        chartfile.write_ini_lines(ini, new, fmt)
    return chartfile.ini_values(ini).get("charter")


def restore(song_dir):
    song_dir = Path(song_dir)
    backup = song_dir / BACKUP
    if not backup.exists():
        raise ValueError("there's no original chart to go back to")
    os.replace(backup, song_dir / "notes.mid")
    side = song_dir / SIDECAR
    try:
        record = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    ini = song_dir / "song.ini"
    if record.get("song_ini") and ini.exists():
        lines, fmt = chartfile.read_ini_lines(ini)
        chartfile.write_ini_lines(ini, chartfile.set_ini_values(lines, record["song_ini"]), fmt)
    side.unlink()


def main():
    song = load(sys.argv[1])
    print(song["info"])
    for inst, d in song["instruments"].items():
        notes = d["difficulties"][3]
        lanes = {}
        for n in notes:
            key = "".join("GRYBOP"[l] for l in n["lanes"])
            lanes[key] = lanes.get(key, 0) + 1
        same = [(n["tick"], n["lanes"]) for n in d["difficulties"][2]] == [(n["tick"], n["lanes"]) for n in notes]
        print(f"{inst}: {len(notes)} Expert notes {sorted(lanes.items(), key=lambda kv: -kv[1])}"
              f"{'  (Hard is a copy of Expert)' if same else ''}")


if __name__ == "__main__":
    main()
