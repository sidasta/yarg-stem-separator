"""Split a YARG/Clone Hero song.ogg into instrument stems.

Separated stems are written to a staging folder; the song folder itself is never
modified. Only instruments that are actually charted get a stem, and the new
song.ogg is the residual (original minus every kept stem), so playing all of the
output files together reproduces the original mix.

Usage:
  python stemsplit.py split <song folder>... [--model NAME] [--staging DIR]
  python stemsplit.py bench <multitrack song folder>... [--model NAME]
  python stemsplit.py queue <library folder>... [--retry-failed] [--limit N]
  python stemsplit.py remix <staging folder> [--fill guitar,piano] [--gate-song [DB]]
  python stemsplit.py undo <song folder>...
"""

import argparse
import configparser
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "models"
STAGING_DIR = ROOT / "staging"
DEFAULT_MODEL = "BS-Roformer-SW.ckpt"
MODEL_SR = 44100

# model stem name -> (YARG file name, song.ini difficulty tags, notes.mid track names)
STEM_MAP = {
    "guitar": ("guitar", ["diff_guitar", "diff_guitar_coop", "diff_guitar_real", "diff_guitarghl"],
               ["PART GUITAR", "PART GUITAR COOP", "PART REAL_GUITAR", "PART GUITAR GHL"]),
    "bass": ("rhythm", ["diff_bass", "diff_bass_real", "diff_bassghl", "diff_rhythm"],
             ["PART BASS", "PART RHYTHM", "PART REAL_BASS", "PART BASS GHL"]),
    "drums": ("drums", ["diff_drums", "diff_drums_real"], ["PART DRUMS"]),
    "piano": ("keys", ["diff_keys", "diff_keys_real"], ["PART KEYS", "PART REAL_KEYS_X"]),
    "vocals": ("vocals", ["diff_vocals", "diff_vocals_harm"], ["PART VOCALS", "HARM1"]),
}

# A stem quieter than this (relative to the full mix) is treated as "instrument not present".
SILENCE_DB = -35.0


def ffmpeg_exe():
    exe = Path(sys.executable).parent / "ffmpeg.exe"  # copied into the venv by setup
    if exe.exists():
        return str(exe)
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def read_audio(path):
    """Return (float32 array shaped [frames, channels], sample rate)."""
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return data, sr
    except sf.LibsndfileError:  # e.g. some .opus files; let ffmpeg decode them instead
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "x.wav"
            subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(path), "-c:a", "pcm_f32le", str(wav)], check=True)
            return sf.read(str(wav), dtype="float32", always_2d=True)


def write_ogg(path, data, sr, quality=8):
    """Encode float audio to Ogg Vorbis at the given VBR quality via ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "x.wav"
        sf.write(str(wav), data, sr, subtype="FLOAT")
        subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(wav), "-c:a", "libvorbis",
                        "-q:a", str(quality), str(path)], check=True)


def rms_db(x):
    return 10 * np.log10(np.mean(np.square(x, dtype=np.float64)) + 1e-12)


def fit_length(x, frames, channels):
    """Trim/pad a stem to the original length and channel count."""
    if x.shape[1] != channels:
        x = np.repeat(x.mean(axis=1, keepdims=True), channels, axis=1)
    if len(x) >= frames:
        return x[:frames]
    return np.pad(x, ((0, frames - len(x)), (0, 0)))


def charted_from_ini(song_dir):
    """{model stem: True/False/None} from song.ini difficulties (None = no tag present)."""
    ini = song_dir / "song.ini"
    result = {stem: None for stem in STEM_MAP}
    if not ini.exists():
        return result
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read_string(ini.read_text(encoding="utf-8-sig", errors="replace"))
    section = next((s for s in cp.sections() if s.lower() == "song"), None)
    if section is None:
        return result
    for stem, (_, tags, _) in STEM_MAP.items():
        values = []
        for tag in tags:
            raw = cp[section].get(tag)
            if raw is None:
                continue
            try:
                values.append(int(raw.strip()))
            except ValueError:
                pass
        if values:
            result[stem] = any(v >= 0 for v in values)
    return result


def charted_from_mid(song_dir):
    """{model stem: True/False} from notes.mid track names, or None if there is no notes.mid."""
    mid = song_dir / "notes.mid"
    if not mid.exists():
        return None
    import mido
    try:
        names = {t.name.strip().upper() for t in mido.MidiFile(str(mid), clip=True).tracks}
    except Exception:
        return None
    return {stem: any(n in names for n in tracks) for stem, (_, _, tracks) in STEM_MAP.items()}


def charted_instruments(song_dir):
    """Decide which instruments get a stem: song.ini first, notes.mid where ini has no tag."""
    ini = charted_from_ini(song_dir)
    mid = charted_from_mid(song_dir)
    result = {}
    for stem in STEM_MAP:
        if ini[stem] is not None:
            result[stem] = ini[stem]
        elif mid is not None:
            result[stem] = mid[stem]
        else:
            result[stem] = True  # nothing to go on: keep it and let the silence check decide
    return result


_separators = {}


def get_separator(model):
    if model not in _separators:
        from audio_separator.separator import Separator
        os.environ["PATH"] = str(Path(ffmpeg_exe()).parent) + os.pathsep + os.environ["PATH"]
        sep = Separator(output_dir=tempfile.gettempdir(), model_file_dir=str(MODELS_DIR), log_level=30)
        sep.load_model(model_filename=model)
        _separators[model] = sep
    return _separators[model]


def demix_demucs(mi, x):
    """Demucs equivalent of demix(): returns {stem name: (channels, frames)} without normalising."""
    from audio_separator.separator.architectures import demucs_separator as ds
    if getattr(mi, "_loaded_demucs", None) is None:
        m = ds.get_demucs_model(name=Path(mi.model_path).stem, repo=Path(mi.model_path).parent)
        m = ds.demucs_segments(mi.segment_size, m)
        m.to(mi.torch_device).eval()
        mi._loaded_demucs = m
    mi.demucs_model_instance = mi._loaded_demucs
    out = mi.demix_demucs(x)
    names = list(mi._loaded_demucs.sources)
    names[0], names[1] = names[1], names[0]  # demix_demucs swaps the first two sources
    return dict(zip(names, out))


def separate(mix, sr, model):
    """Run the model on a mix; return {model stem name (lower-case): array matching mix}.

    Calls the model's demix() directly instead of Separator.separate(): the latter
    peak-normalises the input and each stem independently and rounds to 16-bit,
    which breaks "original minus stems" (a ghost of every instrument stays in song.ogg).
    """
    import librosa
    frames, channels = mix.shape
    x = mix.T.astype(np.float32)  # (channels, frames)
    if sr != MODEL_SR:
        x = librosa.resample(x, orig_sr=sr, target_sr=MODEL_SR)
    if channels == 1:
        x = np.repeat(x, 2, axis=0)
    mi = get_separator(model).model_instance
    source = demix_demucs(mi, x) if hasattr(mi, "demix_demucs") else mi.demix(mix=x)
    stems = {}
    for name, data in source.items():
        data = np.asarray(data, dtype=np.float32)
        if sr != MODEL_SR:
            data = librosa.resample(data, orig_sr=MODEL_SR, target_sr=sr)
        stems[name.lower()] = fit_length(data.T, frames, channels)
    return stems


# Expert-difficulty gem notes per instrument in notes.mid (vocals: pitched lyric notes).
CHART_NOTES = {"guitar": range(96, 101), "bass": range(96, 101), "piano": range(96, 101),
               "drums": range(95, 101), "vocals": range(36, 85)}
# A second of a stem counts as "playing" if it is within this many dB of the mix.
ACTIVE_DB = -25.0


def chart_spans(song_dir, gap=1.0, pad=0.25):
    """{model stem: [(start s, end s), ...]} where the main chart track has expert notes."""
    mid = Path(song_dir) / "notes.mid"
    if not mid.exists():
        return {}
    import mido
    mf = mido.MidiFile(str(mid), clip=True)
    by_name = {t.name.strip().upper(): t for t in mf.tracks}
    spans = {}
    for stem, (_, _, track_names) in STEM_MAP.items():
        track = next((by_name[n] for n in track_names if n in by_name), None)
        if track is None:
            continue
        m = mido.MidiFile(ticks_per_beat=mf.ticks_per_beat)
        m.tracks = [mf.tracks[0], track]  # tempo map + instrument
        now, held, notes = 0.0, {}, []
        for msg in m:
            now += msg.time
            if msg.type not in ("note_on", "note_off") or msg.note not in CHART_NOTES[stem]:
                continue
            if msg.type == "note_on" and msg.velocity > 0:
                held.setdefault(msg.note, now)
            elif msg.note in held:
                notes.append((held.pop(msg.note), now))
        merged = []
        for start, end in sorted(notes):
            start, end = max(0.0, start - pad), end + pad
            if merged and start - merged[-1][1] < gap:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        spans[stem] = [(round(a, 2), round(b, 2)) for a, b in merged]
    return spans


def span_gate(spans, frames, sr, fade=0.05):
    """0..1 gain curve that is 1 inside the spans, with short fades to avoid clicks."""
    gate = np.zeros(frames, dtype=np.float32)
    for a, b in spans:
        gate[int(a * sr):int(b * sr)] = 1.0
    n = int(fade * sr)
    if n > 1:
        gate = np.convolve(gate, np.hanning(2 * n) / np.hanning(2 * n).sum(), mode="same").astype(np.float32)
    return gate[:, None]


def activity(x, mix, sr):
    """Per-second level of x relative to the mix in that second (dB, rounded)."""
    secs = len(mix) // sr
    a = np.square(x[:secs * sr], dtype=np.float64).reshape(secs, sr, -1).mean(axis=(1, 2))
    b = np.square(mix[:secs * sr], dtype=np.float64).reshape(secs, sr, -1).mean(axis=(1, 2))
    return np.round(10 * np.log10((a + 1e-12) / (b + 1e-9)), 0).clip(-90, 10).astype(int).tolist()


def coverage(act, spans):
    """Fraction of charted seconds in which the stem is audibly playing (None if nothing charted)."""
    charted = [s for a, b in spans for s in range(int(a), min(int(np.ceil(b)), len(act)))]
    charted = sorted(set(charted))
    if not charted:
        return None
    return round(sum(act[s] > ACTIVE_DB for s in charted) / len(charted), 2)


def gate_curve(x, mix, sr, threshold_db, block=0.01, hold=0.15, attack=0.005, release=0.2):
    """0..1 gain that opens where x is within threshold_db of the mix (10 ms blocks)."""
    b = int(sr * block)
    k = len(x) // b + 1
    pad = k * b - len(x)
    px = np.square(np.pad(x, ((0, pad), (0, 0))), dtype=np.float64).reshape(k, b, -1).mean(axis=(1, 2))
    pm = np.square(np.pad(mix, ((0, pad), (0, 0))), dtype=np.float64).reshape(k, b, -1).mean(axis=(1, 2))
    is_open = 10 * np.log10((px + 1e-12) / (pm + 1e-9)) > threshold_db
    held = is_open.copy()
    for i in range(1, int(hold / block) + 1):  # keep the gate open briefly after the sound drops
        held[i:] |= is_open[:-i]
    a, r = np.exp(-block / attack), np.exp(-block / release)
    g, level = np.empty(k, dtype=np.float32), 0.0
    for i, target in enumerate(held):
        coef = a if target > level else r
        level = target + (level - target) * coef
        g[i] = level
    return np.interp(np.arange(len(x)), np.arange(k) * b + b / 2, g).astype(np.float32)[:, None]


def redistribute(removed, stems, sr, n_fft=2048, hop=512, memory=1.0):
    """Split `removed` among stems by each stem's recent energy per frequency bin.

    Echo/reverb tails follow the instrument that just played at that frequency, so each
    stem's magnitude is held with a ~1 s decay before weighting. Returns {name: addition};
    the additions sum back to `removed`, so the full mix is unchanged.
    """
    import librosa
    names = list(stems)
    adds = {n: np.zeros_like(removed) for n in names}
    decay = np.exp(-hop / (sr * memory))
    for ch in range(removed.shape[1]):
        spec = librosa.stft(removed[:, ch], n_fft=n_fft, hop_length=hop)
        mags = []
        for n in names:
            m = np.abs(librosa.stft(stems[n][:, ch], n_fft=n_fft, hop_length=hop))
            for t in range(1, m.shape[1]):
                np.maximum(m[:, t], m[:, t - 1] * decay, out=m[:, t])
            mags.append(m)
        eps = 1e-6
        total = sum(mags) + eps * len(names)
        for n, m in zip(names, mags):
            adds[n][:, ch] = librosa.istft(spec * ((m + eps) / total), hop_length=hop, length=len(removed))
    return adds


def assemble(mix, sr, stems, charted, spans, fill=(), gate_song_db=None):
    """Decide which stems to keep and build the output files. Returns (files, report).

    fill: model stems (e.g. "piano") that should also take the model's "other" output
    during their charted sections. Earlier entries win where charted sections overlap.
    gate_song_db: gate song.ogg so only sounds within this many dB of the mix pass (e.g. -15);
    what the gate removes is handed to the kept stems, so the full mix stays the same.
    """
    mix_db = rms_db(mix)
    sources = {k: v.copy() for k, v in stems.items()}
    claimed = np.zeros((len(mix), 1), dtype=np.float32)
    for stem in fill:
        if "other" in stems and stem in spans and charted.get(stem):
            gate = span_gate(spans[stem], len(mix), sr) * (1 - claimed)
            sources[stem] = sources.get(stem, 0) + stems["other"] * gate
            claimed += gate
    files, report = {}, {"stems": {}, "fill": list(fill), "chart": {}, "activity": {}}
    residual = mix.copy()
    for stem, (yarg_name, _, _) in STEM_MAP.items():
        if stem not in sources:
            report["stems"][stem] = {"file": yarg_name, "kept": False, "reason": "model has no such stem"}
            continue
        level = round(rms_db(sources[stem]) - mix_db, 1)
        entry = {"file": yarg_name, "level_db": level}
        if not charted[stem]:
            entry.update(kept=False, reason="not charted - left in song.ogg")
        elif level < SILENCE_DB:
            entry.update(kept=False, reason=f"near-silent ({level} dB) - left in song.ogg")
        else:
            entry.update(kept=True)
            files[yarg_name] = sources[stem]
            residual -= sources[stem]
            if stem in fill:
                entry["reason"] = "includes model 'other' during charted sections"
        if stem in spans:
            report["chart"][yarg_name] = spans[stem]
            entry["chart_coverage"] = coverage(activity(sources[stem], mix, sr), spans[stem])
        report["stems"][stem] = entry
    reason = "original minus kept stems"
    kept = dict(files)  # stems only; song not added yet
    if gate_song_db is not None and kept:
        gate = gate_curve(residual, mix, sr, gate_song_db)
        removed = residual * (1 - gate)
        for name, add in redistribute(removed, kept, sr).items():
            files[name] = files[name] + add
        residual = residual * gate
        report["song_gate_db"] = gate_song_db
        reason += f", gated at {gate_song_db:g} dB (removed echoes moved to the instruments)"
    files["song"] = residual
    report["stems"]["other"] = {"file": "song", "kept": True,
                                 "level_db": round(rms_db(residual) - mix_db, 1), "reason": reason}
    for name, data in files.items():
        report["activity"][name] = activity(data, mix, sr)
    for name, data in stems.items():
        report["activity"][f"raw/{name}"] = activity(data, mix, sr)
    report["flags"] = attention(report, charted)
    return files, report


# Below this share of charted time being audible, a stem is flagged for a closer listen.
FLAG_COVERAGE = 0.5
# Order in which "fill from other" assigns overlapping charted sections (first wins).
FILL_ORDER = ["guitar", "piano", "bass", "vocals", "drums"]
FILL_LABEL = {"guitar": "guitar", "piano": "keys", "bass": "bass", "vocals": "vocals", "drums": "drums"}


def attention(report, charted):
    """Human-readable reasons a split needs a closer listen (empty if it looks fine)."""
    flags = []
    for stem, e in report["stems"].items():
        cov = e.get("chart_coverage")
        if not charted.get(stem) or cov is None or cov >= FLAG_COVERAGE:
            continue
        if e.get("kept"):
            flags.append(f"{e['file']}: audible in only {cov:.0%} of charted time")
        else:
            flags.append(f"{e['file']}: charted, but the model found none (left in song.ogg)")
    return flags


def split_mix(mix, sr, charted, model, spans=None):
    """Separate a mix and assemble the outputs. Returns (files, report, raw model stems)."""
    t0 = time.time()
    stems = separate(mix, sr, model)
    seconds = time.time() - t0
    files, report = assemble(mix, sr, stems, charted, spans or {})
    report.update(model=model, seconds=round(seconds, 1))
    return files, report, stems


def write_outputs(out_dir, files, sr, report, original=None, raw=None):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for name, data in files.items():
        write_ogg(out_dir / f"{name}.ogg", data, sr)
    if original is not None:
        shutil.copy2(original, out_dir / f"original{Path(original).suffix.lower()}")
    if raw:  # every model output, kept or not, for diagnosis and re-assignment
        (out_dir / "raw").mkdir()
        for name, data in raw.items():
            write_ogg(out_dir / "raw" / f"{name}.ogg", data, sr)
    (out_dir / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


# Audio file names (without extension) that mean a song already has separate stems.
STEM_FILE_NAMES = {"guitar", "rhythm", "bass", "keys", "drums", "drums_1", "drums_2", "drums_3", "drums_4",
                   "vocals", "vocals_1", "vocals_2"}
AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".flac"}
# Single-file song audio we can split, in the order YARG-style folders usually use them.
SONG_EXTS = (".ogg", ".opus", ".mp3", ".wav")


def song_audio(song_dir):
    """The song's single audio file (song.ogg / .opus / .mp3 / .wav), or None."""
    for ext in SONG_EXTS:
        p = Path(song_dir) / f"song{ext}"
        if p.exists():
            return p
    return None


def existing_stems(song_dir):
    return sorted(p.name for p in Path(song_dir).iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_EXTS and p.stem.lower() in STEM_FILE_NAMES)


def read_manifest(folder):
    try:
        data = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if "flags" not in data and "charted" in data:  # staged before flags existed
        data["flags"] = attention(data, data["charted"])
    return data


def staged_name(staging, song_dir):
    """Staging folder name for a song; disambiguated if another song has the same folder name."""
    name = song_dir.name
    m = read_manifest(staging / name)
    if (staging / name).exists() and (m is None or m.get("source") != str(song_dir)):
        name = f"{song_dir.name} ~{hashlib.sha1(str(song_dir).encode()).hexdigest()[:6]}"
    return name


def split_song(song_dir, model=DEFAULT_MODEL, staging=STAGING_DIR):
    """Separate one song folder into staging. Returns (staging folder name, report)."""
    song_dir, staging = Path(song_dir).resolve(), Path(staging)
    src = song_audio(song_dir)
    if src is None:
        raise ValueError(f"no song audio ({', '.join('song' + e for e in SONG_EXTS)}) in {song_dir}")
    mix, sr = read_audio(src)
    charted = charted_instruments(song_dir)
    files, report, raw = split_mix(mix, sr, charted, model, chart_spans(song_dir))
    report.update(song=song_dir.name, source=str(song_dir), kind="split", charted=charted,
                  song_file=src.name, original_file=f"original{src.suffix.lower()}")
    name = staged_name(staging, song_dir)
    for other in staging.glob("*/manifest.json"):  # variants of an earlier split are now stale
        m = read_manifest(other.parent)
        if m and m.get("source") == str(song_dir) and m.get("variant_of"):
            shutil.rmtree(other.parent)
    write_outputs(staging / name, files, sr, report, original=src, raw=raw)
    return name, report


def remix(staged, fill=(), gate_song_db=None):
    """Build a variant of a staged song from its raw model outputs. Returns the new folder name."""
    staged = Path(staged)
    report = read_manifest(staged)
    base_folder = report.get("raw_from") or staged.name
    raw_dir = staged / "raw" if (staged / "raw").exists() else staged.parent / base_folder / "raw"
    original = staged / report.get("original_file", "original.ogg")
    mix, sr = read_audio(original)
    stems = {p.stem: fit_length(read_audio(p)[0], len(mix), mix.shape[1]) for p in raw_dir.glob("*.ogg")}
    files, new = assemble(mix, sr, stems, report["charted"], chart_spans(report["source"]), list(fill), gate_song_db)
    parts = []
    if fill:
        parts.append("+".join({"piano": "keys"}.get(s, s) for s in fill) + " from other")
    if gate_song_db is not None:
        parts.append("song gated" + ("" if gate_song_db == -15 else f" {gate_song_db:g} dB"))
    label = ", ".join(parts) or "plain"
    base = report.get("variant_of", report["song"])
    new.update(model=report["model"], seconds=report["seconds"], song=f"{base} [{label}]", variant=label,
               source=report["source"], kind="split", charted=report["charted"], variant_of=base,
               raw_from=base_folder, song_file=report.get("song_file", "song.ogg"),
               original_file=original.name)
    name = f"{base_folder} [{label}]"
    write_outputs(staged.parent / name, files, sr, new, original=original)
    return name


def cmd_split(args):
    for song_dir in map(Path, args.songs):
        if song_audio(song_dir) is None:
            print(f"SKIP {song_dir.name}: no song.ogg/.opus/.mp3/.wav")
            continue
        existing = existing_stems(song_dir)
        if existing and not args.force:
            print(f"SKIP {song_dir.name}: already has stems ({', '.join(existing)})")
            continue
        print(f"Splitting {song_dir.name} ...", flush=True)
        _, report = split_song(song_dir, args.model, args.staging)
        print_report(report)


def cmd_remix(args):
    """Re-assemble a staged song from its saved raw model outputs (no re-separation)."""
    fill = [s.strip() for s in args.fill.split(",") if s.strip()]
    if not fill and args.gate_song is None:
        sys.exit("remix: give --fill and/or --gate-song")
    for staged in map(Path, args.staged):
        name = remix(staged, fill, args.gate_song)
        print(f"Remixed {name}")
        print_report(read_manifest(staged.parent / name))


def sdr(ref, est):
    return 10 * np.log10(np.sum(ref.astype(np.float64) ** 2) / (np.sum((ref - est).astype(np.float64) ** 2) + 1e-12) + 1e-12)


def cmd_bench(args):
    """Mix a song's real stems, separate the mix, and score each stem against the real one."""
    for song_dir in map(Path, args.songs):
        stems = {p.stem: read_audio(p) for p in song_dir.glob("*.ogg") if p.stem != "preview"}
        sr = next(iter(stems.values()))[1]
        frames = max(len(d) for d, _ in stems.values())
        channels = max(d.shape[1] for d, _ in stems.values())
        truth = {name: fit_length(d, frames, channels) for name, (d, s) in stems.items()}
        mix = sum(truth.values())
        print(f"Benchmarking {song_dir.name} (real stems: {', '.join(sorted(truth))}) ...", flush=True)
        # Score every instrument the song has real audio for, regardless of chart.
        charted = {stem: yarg in truth for stem, (yarg, _, _) in STEM_MAP.items()}
        files, report, raw = split_mix(mix, sr, charted, args.model)
        # The real song.ogg also holds crowd and anything uncharted; fold those into the reference.
        ref_song = mix - sum(truth[n] for n in files if n != "song" and n in truth)
        scores = {}
        for name, est in files.items():
            ref = ref_song if name == "song" else truth.get(name)
            if ref is not None:
                scores[name] = round(sdr(ref, est), 2)
        report.update(song=song_dir.name, source=str(song_dir), kind="bench", charted=charted, sdr_db=scores)
        out_dir = Path(args.staging) / f"BENCH - {song_dir.name}"
        write_outputs(out_dir, files, sr, report, raw=raw)
        write_ogg(out_dir / "original.ogg", mix, sr)
        truth_dir = out_dir / "truth"
        truth_dir.mkdir()
        for name in files:
            ref = ref_song if name == "song" else truth.get(name)
            if ref is not None:
                write_ogg(truth_dir / f"{name}.ogg", ref, sr)
        print_report(report)


def print_report(report):
    print(f"  model {report['model']} took {report['seconds']}s")
    for stem, e in report["stems"].items():
        status = "KEEP" if e.get("kept") else "skip"
        extra = f" SDR {report['sdr_db'][e['file']]} dB" if e["file"] in report.get("sdr_db", {}) and e.get("kept") else ""
        if e.get("chart_coverage") is not None:
            extra += f" chart coverage {e['chart_coverage']:.0%}"
        print(f"  {status} {stem:7} -> {e['file']}.ogg  level {e.get('level_db', '?')} dB{extra}  {e.get('reason', '')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("remix", help="rebuild a staged song from its raw outputs, e.g. --fill piano --gate-song")
    p.add_argument("staged", nargs="+", help="staging/<song> folder(s)")
    p.add_argument("--fill", default="",
                   help="comma-separated model stems (guitar, piano, ...) that take 'other' while charted")
    p.add_argument("--gate-song", type=float, nargs="?", const=-15.0, default=None, metavar="DB",
                   help="gate song.ogg: only sounds within DB of the mix pass (default -15); "
                        "the removed echoes go to the instrument stems")
    for name in ("split", "bench"):
        p = sub.add_parser(name)
        p.add_argument("songs", nargs="+")
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--staging", default=str(STAGING_DIR))
        if name == "split":
            p.add_argument("--force", action="store_true", help="split even if the folder already has stems")
    p = sub.add_parser("queue", help="split every single-file song under the given folders (resumable)")
    p.add_argument("roots", nargs="+", help="song library folder(s), searched recursively")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--staging", default=str(STAGING_DIR))
    p.add_argument("--retry-failed", action="store_true", help="also retry songs that failed before")
    p.add_argument("--limit", type=int, default=0, help="stop after this many songs")
    p.add_argument("--no-variants", action="store_true",
                   help="don't prepare a fill+gate variant for flagged songs")
    p = sub.add_parser("undo", help="undo a commit: remove added stems, restore the original song audio")
    p.add_argument("songs", nargs="+", help="song folder(s)")
    p.add_argument("--staging", default=str(STAGING_DIR))
    args = ap.parse_args()
    if args.cmd in ("queue", "undo"):
        import library
        if args.cmd == "queue":
            return library.cmd_queue(args)
        for song in args.songs:
            library.undo(args.staging, Path(song).resolve())
            print(f"Restored {Path(song).name}")
        return
    {"split": cmd_split, "bench": cmd_bench, "remix": cmd_remix}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
