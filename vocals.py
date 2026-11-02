"""Chart lead vocals (PART VOCALS) from pasted lyrics and the separated vocal stem.

  lyrics -> forced alignment (torchaudio's MMS aligner) -> a time for every word and letter
  vocal stem -> pitch track (CREPE) -> one note per syllable, slides where the pitch moves
  notes -> snapped to the chart's own tempo map and written into a copy of notes.mid
  (a notes.chart is converted to notes.mid first: a .chart has no way to store pitched vocals)

Used by the review page for staged songs that have no vocals charted: add_vocals() charts
them and re-assembles the staged split from its saved raw model outputs, so vocals.ogg is
split out too. For songs committed earlier with a vocals.ogg, stage_committed() charts them
from that file. Nothing in the song folder changes until the chart is committed.
"""

import json
import re
import shutil
import unicodedata
from pathlib import Path

import numpy as np

import stemsplit

SR = 16000  # both models work at 16 kHz
HOP = 0.01  # pitch frames
GRID_DIVISION = 8  # snap notes to 1/32 (ticks per beat / 8)
VOCAL_RANGE = (36, 84)  # pitches YARG / Rock Band accept for vocal notes
PHRASE_NOTE, OVERDRIVE_NOTE = 105, 116
ALIGN_MODEL_FILE = "mms-fa-model.pt"
CHART_DIR = "chart"  # inside a staged folder: the generated notes.mid / song.ini
MAX_DELAY_MS = 100  # song.ini delay / .chart Offset small enough for vocal notes to ignore


def available():
    """(True, "") if vocal charting can run here, else (False, reason)."""
    import importlib
    try:
        for name in ("pyphen", "torchcrepe", "torchaudio.pipelines", "torchaudio.functional"):
            importlib.import_module(name)
        import torchaudio
        getattr(torchaudio.pipelines, "MMS_FA")
        getattr(torchaudio.functional, "forced_align")
    except (ImportError, AttributeError) as e:
        return False, f"vocal charting needs packages this setup doesn't have ({e}); run the launcher again to update"
    return True, ""


# ---------------------------------------------------------------- lyrics

QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'", "“": '"', "”": '"',
                        "–": "-", "—": " ", "…": " "})
# Characters with a special meaning in YARG / Rock Band lyric events.
LYRIC_SPECIAL = re.compile(r"[#^+%$/=\"*§_<>\[\]{}]")


def latin1(text):
    """MIDI lyric events are Latin-1: drop accents only where a character doesn't fit."""
    return "".join(c if ord(c) < 256 else unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
                   for c in text)


def letters(text):
    """What the aligner can hear: lower-case a-z and apostrophes."""
    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z']", "", text)


def parse_lyrics(text):
    """Lead-vocal lines from pasted lyrics.

    Skips blank lines and section labels like [Chorus]; text in (parentheses) is backing
    vocals and is left out. Returns (lines, notes): lines are lists of display words."""
    lines, backing, unplayable = [], 0, []
    for raw in text.translate(QUOTES).splitlines():
        raw = raw.strip()
        if not raw or re.fullmatch(r"\[.*\]|\{.*\}|(?i:(pre-?)?(verse|chorus|bridge|intro|outro|refrain|hook|solo)\b.*):", raw):
            continue
        lead = re.sub(r"\([^)]*\)?", " ", raw)
        if lead.strip() != raw:
            backing += 1
        words = []
        for w in LYRIC_SPECIAL.sub("", lead).split():
            w = w.strip(",.;:!?").strip("-")
            if not w:
                continue
            if not letters(w):
                unplayable.append(w)
                continue
            words.append(latin1(w))
        if words:
            lines.append(words)
    notes = []
    if backing:
        notes.append(f"{backing} line{'s' if backing > 1 else ''} with backing vocals in (parentheses): those parts were left out")
    if unplayable:
        notes.append(f"couldn't chart {', '.join(sorted(set(unplayable)))} (write numbers and symbols out as words)")
    return lines, notes


def syllables(word, dic):
    """Display syllables of a word: 'hangin'' -> ['hang-', "in'"]; 'twenty-one' -> ['twen-', 'ty=', 'one'].

    A trailing '-' joins a syllable to the next one; '=' shows as a real hyphen in game."""
    out = []
    parts = [p for p in word.split("-") if p]
    for pi, part in enumerate(parts):
        sy = dic.inserted(part).split("-") if len(letters(part)) > 3 else [part]
        sy = [s for s in sy if s]
        for si, s in enumerate(sy):
            last_of_part, last_of_word = si == len(sy) - 1, pi == len(parts) - 1 and si == len(sy) - 1
            out.append(s if last_of_word else s + ("=" if last_of_part else "-"))
    return out


# ---------------------------------------------------------------- analysis

def device():
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def to_16k_mono(audio, sr):
    import librosa
    x = librosa.resample(audio.mean(axis=1), orig_sr=sr, target_sr=SR)
    return (x / (np.abs(x).max() + 1e-9) * 0.9).astype(np.float32)


def align(x, lines, dev):
    """Force-align lyric lines to 16 kHz vocal audio. Returns per line: [{word, start, end, chars, score}]."""
    import torch
    import torchaudio
    import torchaudio.functional as F

    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model(with_star=True, dl_kwargs={"model_dir": str(stemsplit.MODELS_DIR),
                                                       "file_name": ALIGN_MODEL_FILE}).to(dev).eval()
    dic = {c: i for i, c in enumerate(bundle.get_labels(star="*"))}
    chunk, ctx = 20 * SR, 2 * SR  # 20 s windows with 2 s of context either side
    ems = []
    with torch.inference_mode():
        for s in range(0, len(x), chunk):
            a, b = max(0, s - ctx), min(len(x), s + chunk + ctx)
            e, _ = model(torch.from_numpy(x[a:b])[None].to(dev))
            fps = e.shape[1] / ((b - a) / SR)
            i0 = int(round((s - a) / SR * fps))
            ems.append(e[0, i0:i0 + int(round(min(chunk, len(x) - s) / SR * fps))].float().cpu())
    del model
    em = torch.log_softmax(torch.cat(ems), dim=-1)
    fps = em.shape[0] / (len(x) / SR)

    tokens, owner = [], []  # a "*" token between lines soaks up ad-libs and backing vocals
    for li, words in enumerate(lines):
        tokens.append(dic["*"]); owner.append(None)
        for wi, w in enumerate(words):
            for c in letters(w):
                tokens.append(dic[c]); owner.append((li, wi))
    tokens.append(dic["*"]); owner.append(None)
    if len(tokens) * 2 > em.shape[0]:
        raise ValueError("the lyrics are far longer than the song: are they for this song?")
    ali, scores = F.forced_align(em[None], torch.tensor([tokens], dtype=torch.int32), blank=0)
    spans = F.merge_tokens(ali[0], scores[0].exp())
    chars = {}
    for sp, o in zip(spans, owner):
        if o is not None:
            chars.setdefault(o, []).append((sp.start / fps, sp.end / fps, float(sp.score)))
    out = []
    for li, words in enumerate(lines):
        wl = []
        for wi, w in enumerate(words):
            cs = chars[(li, wi)]
            wl.append({"word": w, "start": cs[0][0], "end": cs[-1][1], "chars": [c[:2] for c in cs],
                       "score": float(np.mean([c[2] for c in cs]))})
        out.append(wl)
    return out


def track_pitch(x, dev):
    """(MIDI pitch per 10 ms frame with NaN where unvoiced, voiced mask)."""
    import librosa
    import torch
    import torchcrepe
    f0, per = torchcrepe.predict(torch.from_numpy(x)[None], SR, hop_length=int(SR * HOP), fmin=60, fmax=1000,
                                 model="full", batch_size=1024, device=dev, return_periodicity=True,
                                 decoder=torchcrepe.decode.viterbi)
    rms = librosa.feature.rms(y=x, frame_length=1024, hop_length=int(SR * HOP), center=True)[0]
    n = min(f0.shape[1], len(rms))
    f0, per, db = f0[0, :n].cpu().numpy(), per[0, :n].cpu().numpy(), 20 * np.log10(rms[:n] + 1e-9)
    voiced = (per > 0.4) & (db > np.percentile(db, 95) - 30)
    return np.where(voiced, 69 + 12 * np.log2(np.maximum(f0, 1) / 440), np.nan), voiced


def voiced_end(voiced, a, limit, max_gap=6):
    """Last frame of the voiced run starting at frame a (gaps under 60 ms bridged), capped at limit."""
    gap = 0
    for i in range(a, limit):
        gap = 0 if voiced[i] else gap + 1
        if gap > max_gap:
            return i - gap + 1
    return limit


def syllable_notes(aligned, midi, voiced):
    """[[start s, end s, pitch, lyric, line index], ...] with '+' slide notes where the pitch moves."""
    import pyphen
    from scipy.ndimage import median_filter

    dic = pyphen.Pyphen(lang="en_US")
    notes = []
    for li, words in enumerate(aligned):
        for w in words:
            pos, chars = 0, w["chars"]
            for s in syllables(w["word"], dic):
                n = max(1, len(letters(s)))
                c0, c1 = chars[min(pos, len(chars) - 1)], chars[min(pos + n, len(chars)) - 1]
                notes.append({"line": li, "text": s, "start": c0[0], "end": c1[1]})
                pos += n
    for i, n in enumerate(notes):  # hold each syllable for as long as the singing carries on
        nxt = notes[i + 1]["start"] if i + 1 < len(notes) else n["end"] + 2
        n["end"] = max(n["end"], voiced_end(voiced, int(n["start"] / HOP), min(len(voiced), int(nxt / HOP) - 3)) * HOP)
        n["end"] = min(n["end"], nxt - 0.03, n["start"] + 4.0)

    events, last_pitch = [], 60
    for n in notes:
        a, b = n["start"], n["end"]
        seg = midi[int(a / HOP):int(b / HOP)]
        ok = ~np.isnan(seg)
        if ok.sum() < 3:  # nothing pitched under this syllable: carry the previous pitch
            events.append([a, b, last_pitch, n["text"], n["line"]])
            continue
        sm = median_filter(np.where(ok, seg, np.nanmedian(seg)), size=7)
        r = np.round(sm).astype(int)
        parts, s0 = [], 0  # runs of steady pitch; runs under 150 ms join the one before
        for i in range(1, len(r) + 1):
            if i == len(r) or (abs(r[i] - r[s0]) >= 1 and abs(sm[i] - np.median(sm[s0:i])) > 0.8):
                if i - s0 >= 15 or not parts:
                    parts.append([s0, i, int(round(np.median(sm[s0:i])))])
                else:
                    parts[-1][1] = i
                s0 = i
        merged = [parts[0]]
        for p in parts[1:]:
            if abs(p[2] - merged[-1][2]) < 2:
                merged[-1][1] = p[1]
            else:
                merged.append(p)
        if b - a < 0.45:
            merged = [[0, len(r), int(round(np.median(sm)))]]
        merged = merged[:3]
        for k, (x0, _, pitch) in enumerate(merged):
            s = a if k == 0 else a + x0 * HOP
            e = b if k == len(merged) - 1 else a + merged[k + 1][0] * HOP
            events.append([s, e, pitch, n["text"] if k == 0 else "+", n["line"]])
        last_pitch = merged[-1][2]

    if events:  # octave errors: pull stray notes towards the singer's usual range
        med = np.median([e[2] for e in events])
        for e in events:
            while e[2] - med > 9:
                e[2] -= 12
            while med - e[2] > 9:
                e[2] += 12
            e[2] = int(min(VOCAL_RANGE[1], max(VOCAL_RANGE[0], e[2])))
    return events


# ---------------------------------------------------------------- chart writing

def seconds_to_ticks(mid):
    """Function mapping seconds to (fractional) ticks on the chart's tempo map."""
    tpb, tempos, tick = mid.ticks_per_beat, [], 0
    for m in mid.tracks[0]:
        tick += m.time
        if m.type == "set_tempo":
            tempos.append((tick, m.tempo))
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0, 500000))
    segs, sec = [], 0.0
    for i, (t, tempo) in enumerate(tempos):
        if i:
            sec += (t - tempos[i - 1][0]) * tempos[i - 1][1] / 1e6 / tpb
        segs.append((t, sec, tempo / 1e6 / tpb))

    def to_tick(s):
        t0, s0, sec_per_tick = next(g for g in reversed(segs) if g[1] <= s or g is segs[0])
        return t0 + (s - s0) / sec_per_tick
    return to_tick


def quantise(events, to_tick, grid):
    """Snap to the grid; keep notes from overlapping, with a small gap between them."""
    snap = lambda s: int(round(to_tick(s) / grid)) * grid  # noqa: E731
    q = []
    for s, e, pitch, text, line in events:
        a = snap(s)
        if q and a <= q[-1][0]:
            a = q[-1][0] + grid
        b = max(snap(e), a + grid // 2)
        if q and q[-1][1] > a - grid // 2:
            q[-1][1] = max(q[-1][0] + grid // 2, a - grid // 2)
        q.append([a, b, pitch, text, line])
    return q


def vocal_track(notes, overdrive_every=4):
    """PART VOCALS track: notes, lyric events, a phrase per lyric line, overdrive on every 4th phrase."""
    import mido
    ev = []
    lines = sorted({n[4] for n in notes})
    for k, li in enumerate(lines):
        ns = [n for n in notes if n[4] == li]
        a, b = ns[0][0], ns[-1][1]
        for note in (PHRASE_NOTE,) + ((OVERDRIVE_NOTE,) if k % overdrive_every == overdrive_every - 1 else ()):
            ev += [(a, 1, mido.Message("note_on", note=note, velocity=100)),
                   (b, 0, mido.Message("note_off", note=note, velocity=0))]
    for a, b, pitch, text, _ in notes:
        ev += [(a, 2, mido.MetaMessage("lyrics", text=text)),
               (a, 3, mido.Message("note_on", note=pitch, velocity=100)),
               (b, 0, mido.Message("note_off", note=pitch, velocity=0))]
    ev.sort(key=lambda e: (e[0], e[1]))
    track, now = mido.MidiTrack([mido.MetaMessage("track_name", name="PART VOCALS", time=0)]), 0
    for at, _, m in ev:
        track.append(m.copy(time=at - now))
        now = at
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track, len(lines)


def write_song_ini(src, dst, extra=None):
    """Copy song.ini with diff_vocals set, keeping its formatting and line endings.

    extra: {key: value} to add, for settings song.ini doesn't have yet (made from scratch if src is missing)."""
    raw = src.read_bytes() if src.exists() else b"[song]\r\n"
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    lines = raw.decode("utf-8-sig", errors="replace").splitlines()
    found = False
    for i, line in enumerate(lines):
        m = re.match(r"\s*diff_vocals\s*=\s*(-?\d+)?", line, re.I)
        if m:
            found = True
            if m.group(1) is None or int(m.group(1)) < 0:
                lines[i] = "diff_vocals = 2"
    add = ([] if found else ["diff_vocals = 2"]) + [f"{k} = {v}" for k, v in (extra or {}).items()]
    if add:
        head = next((i for i, line in enumerate(lines) if line.strip().lower() == "[song]"), None)
        if head is None:
            lines[:0] = ["[song]"]
            head = 0
        lines[head + 1:head + 1] = add
    dst.write_bytes(nl.join(s.encode("utf-8") for s in lines) + nl)


def delay_ms(chart_src):
    """How far the chart is shifted against the audio: song.ini's delay, else a .chart's Offset."""
    import chartfile
    try:
        return float(chartfile.ini_values(Path(chart_src) / "song.ini")["delay"])
    except (KeyError, ValueError):
        pass
    src = stemsplit.chart_file(chart_src)
    return chartfile.parse(src).offset * 1000 if src is not None and src.suffix == ".chart" else 0.0


def chart(vocal_audio, sr, lyrics, chart_src, out_dir, log=print):
    """Write out_dir/notes.mid (chart_src's chart plus PART VOCALS) and song.ini. Returns a report.

    A notes.chart is converted to notes.mid; song.ini gains any settings the conversion needs."""
    import chartfile
    lines, notes_text = parse_lyrics(lyrics)
    if not lines:
        raise ValueError("no lyrics to chart: paste the words, one sung line per line")
    chart_src = Path(chart_src)
    src = stemsplit.chart_file(chart_src)
    if src is None:
        raise ValueError("this song has no notes.mid or notes.chart to add the vocals to")
    delay = delay_ms(chart_src)
    if abs(delay) >= MAX_DELAY_MS:
        raise ValueError(f"this song's chart is shifted {delay:g} ms against its audio (song.ini's delay or the "
                         ".chart's Offset), which vocal charting can't allow for yet")
    extra = {}
    if src.suffix == ".chart":
        extra = chartfile.ini_additions(chartfile.parse(src), chartfile.ini_values(chart_src / "song.ini"))
    mid = stemsplit.load_chart(chart_src)
    dev = device()
    x = to_16k_mono(vocal_audio, sr)
    log("aligning lyrics")
    aligned = align(x, lines, dev)
    log("tracking pitch")
    midi, voiced = track_pitch(x, dev)
    events = syllable_notes(aligned, midi, voiced)

    grid = mid.ticks_per_beat // GRID_DIVISION
    notes = quantise(events, seconds_to_ticks(mid), grid)
    track, phrases = vocal_track(notes)
    mid.tracks = [t for t in mid.tracks if t.name.strip().upper() != "PART VOCALS"]
    mid.tracks.insert(1, track)
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    mid.save(str(out_dir / "notes.mid"))
    if (chart_src / "song.ini").exists() or extra:
        write_song_ini(chart_src / "song.ini", out_dir / "song.ini", extra)

    # Where to look first when checking: lines the aligner was unsure of, and singing with no lyrics.
    unsure = []
    for words in aligned:
        frames = np.zeros(len(voiced), bool)
        for w in words:
            frames[int(w["start"] / HOP):int(w["end"] / HOP) + 1] = True
        score, sung = np.mean([w["score"] for w in words]), voiced[frames].mean() if frames.any() else 0
        if score < 0.15 or sung < 0.6:
            unsure.append({"time": round(words[0]["start"], 1), "text": " ".join(w["word"] for w in words)})
    covered = np.zeros(len(voiced), bool)
    for words in aligned:
        for w in words:
            covered[max(0, int(w["start"] / HOP) - 50):int(w["end"] / HOP) + 50] = True
    runs, run = [], None  # sung stretches away from any lyric, gaps under a second bridged
    for i, v in enumerate(np.append(voiced & ~covered, False)):
        if v and run is None:
            run = i
        elif not v and run is not None:
            if i - run >= 20:
                runs.append([run * HOP, i * HOP])
            run = None
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] < 1:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    merged = [[round(a, 1), round(b, 1)] for a, b in merged if b - a >= 1]
    return {"lyrics": lyrics, "notes": len(notes), "phrases": phrases, "lines": len(lines),
            "source_file": src.name, "ini_added": extra,
            "pitch_range": [min(n[2] for n in notes), max(n[2] for n in notes)],
            "unsure": unsure, "uncharted": merged, "notes_text": notes_text}


# ---------------------------------------------------------------- staged songs

def _restage(staged, m, charted, chart_dir, extra):
    """Re-assemble a staged base split with a different chart, from its saved raw model outputs."""
    staged = Path(staged)
    original = staged / m.get("original_file", "original.ogg")
    mix, sr = stemsplit.read_audio(original)
    stems = {p.stem: stemsplit.fit_length(stemsplit.read_audio(p)[0], len(mix), mix.shape[1])
             for p in (staged / "raw").glob("*.ogg")}
    files, report = stemsplit.assemble(mix, sr, stems, charted, stemsplit.chart_spans(chart_dir))
    keep = ("model", "seconds", "song", "source", "kind", "song_file", "original_file", "con", "chart_source")
    report.update({k: m[k] for k in keep if k in m}, charted=charted, chart_dir=str(chart_dir), **extra)
    for p in staged.glob("*.ogg"):
        if p.name != original.name:
            p.unlink()
    for name, data in files.items():
        stemsplit.write_ogg(staged / f"{name}.ogg", data, sr)
    (staged / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for other in staged.parent.glob("*/manifest.json"):  # variants were built from the old chart
        v = stemsplit.read_manifest(other.parent)
        if v and v.get("source") == m["source"] and v.get("variant_of"):
            shutil.rmtree(other.parent)
    return report


def base_folder(staging, folder):
    """The staged base split for a staged folder (variants point at their base)."""
    m = stemsplit.read_manifest(Path(staging) / folder)
    if not m or m.get("kind") != "split":
        raise ValueError("not a staged split")
    base = m.get("raw_from") or folder
    if not (Path(staging) / base / "raw").exists():
        raise ValueError("the raw model outputs for this song are missing; split it again")
    return Path(staging) / base


def add_vocals(staged, lyrics, log=print):
    """Chart vocals for a staged split from lyrics, then re-assemble it with vocals.ogg split out."""
    ok, why = available()
    if not ok:
        raise ValueError(why)
    staged = Path(staged)
    m = stemsplit.read_manifest(staged)
    chart_src = Path(m.get("chart_source") or m.get("chart_dir") or m["source"])
    vocal_path = staged / "raw" / "vocals.ogg"
    if not vocal_path.exists():
        raise ValueError("the model produced no vocal track for this song")
    audio, sr = stemsplit.read_audio(vocal_path)
    report = chart(audio, sr, lyrics, chart_src, staged / CHART_DIR, log)
    report["source_sha1"] = _sha1(chart_src / report["source_file"])
    log("re-assembling the split with vocals")
    return _restage(staged, m, {**m["charted"], "vocals": True}, staged / CHART_DIR,
                    {"chart_source": str(chart_src), "vocals_chart": report})


def remove_vocals(staged):
    """Undo add_vocals: back to the song's own chart, vocals left in song.ogg."""
    staged = Path(staged)
    m = stemsplit.read_manifest(staged)
    if not m.get("vocals_chart"):
        raise ValueError("this split has no generated vocals")
    chart_src = Path(m["chart_source"])
    report = _restage(staged, m, stemsplit.charted_instruments(chart_src), chart_src, {})
    shutil.rmtree(staged / CHART_DIR, ignore_errors=True)
    return report


def stage_committed(song_dir, staged, lyrics, source=None, original=None, log=print):
    """Chart vocals for a song committed earlier, from the vocals.ogg in its folder.

    Stages the new chart (manifest kind "vocals") with copies of the song's tracks to listen
    to; library.commit_vocals() writes the chart into the song folder. original: the backup
    of the song's original audio, if there is one. Returns the manifest."""
    ok, why = available()
    if not ok:
        raise ValueError(why)
    song_dir, staged = Path(song_dir), Path(staged)
    if not (song_dir / "vocals.ogg").exists():
        raise ValueError("this song has no vocals.ogg to chart the vocals from")
    names = {yarg for yarg, _, _ in stemsplit.STEM_MAP.values()} | {"song"}
    tracks = {p.stem: p for p in sorted(song_dir.glob("*.ogg")) if p.stem in names}
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    try:
        audio, sr = stemsplit.read_audio(song_dir / "vocals.ogg")
        report = chart(audio, sr, lyrics, song_dir, staged / CHART_DIR, log)
        report["source_sha1"] = _sha1(song_dir / report["source_file"])
        log("preparing the review")
        data = {n: stemsplit.read_audio(p)[0] for n, p in tracks.items()}
        frames, channels = max(len(x) for x in data.values()), max(x.shape[1] for x in data.values())
        data = {n: stemsplit.fit_length(x, frames, channels) for n, x in data.items()}
        mix = sum(data.values())
        mix_db = stemsplit.rms_db(mix)
        spans = stemsplit.chart_spans(staged / CHART_DIR)
        stems = {}
        for stem, (yarg, _, _) in stemsplit.STEM_MAP.items():
            if yarg not in data:
                stems[stem] = {"file": yarg, "kept": False, "reason": f"no {yarg}.ogg in the song folder"}
                continue
            stems[stem] = {"file": yarg, "kept": True, "level_db": round(stemsplit.rms_db(data[yarg]) - mix_db, 1)}
            if stem in spans:
                stems[stem]["chart_coverage"] = stemsplit.coverage(stemsplit.activity(data[yarg], mix, sr), spans[stem])
        stems["other"] = {"file": "song", "kept": True, "reason": "the song's own song.ogg"}
        for p in tracks.values():
            shutil.copy2(p, staged / p.name)
        if original and Path(original).exists():
            original_file = "original" + Path(Path(original).stem).suffix  # song.opus.orig -> original.opus
            shutil.copy2(original, staged / original_file)
        else:
            original_file = "original.ogg"
            stemsplit.write_ogg(staged / original_file, mix, sr)
        manifest = {"kind": "vocals", "song": song_dir.name, "source": str(source or song_dir),
                    "song_dir": str(song_dir), "chart_dir": str(staged / CHART_DIR), "chart_source": str(song_dir),
                    "vocals_chart": report, "charted": stemsplit.charted_instruments(staged / CHART_DIR),
                    "stems": stems, "chart": {stemsplit.STEM_MAP[s][0]: v for s, v in spans.items()},
                    "activity": {n: stemsplit.activity(x, mix, sr) for n, x in data.items()},
                    "flags": [], "original_file": original_file}
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return manifest


def _sha1(path):
    import hashlib
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()
