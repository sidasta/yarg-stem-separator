"""Read Clone Hero / Moonscraper .chart files and convert them to notes.mid.

A .chart is plain text: [Song] properties, a [SyncTrack] tempo map, global [Events], and one
section per instrument and difficulty (e.g. [ExpertSingle]) of "tick = N lane length" notes.
to_midi() writes the same chart as a Rock Band / Clone Hero style MIDI file, which YARG plays
like any other notes.mid. The rest of the program reads .chart songs through it, and vocal
charting uses it to add a PART VOCALS track (a .chart has no way to store pitched vocals).

  python chartfile.py "path/to/notes.chart" [out.mid]
"""

import re
import sys
from pathlib import Path

DIFFICULTIES = {"Expert": 3, "Hard": 2, "Medium": 1, "Easy": 0}
# .chart instrument -> notes.mid track
TRACKS = {"Single": "PART GUITAR", "DoubleGuitar": "PART GUITAR COOP", "DoubleBass": "PART BASS",
          "DoubleRhythm": "PART RHYTHM", "Keyboard": "PART KEYS", "Drums": "PART DRUMS",
          "GHLGuitar": "PART GUITAR GHL", "GHLBass": "PART BASS GHL", "GHLRhythm": "PART RHYTHM GHL",
          "GHLCoop": "PART GUITAR COOP GHL"}
GHL = {"GHLGuitar", "GHLBass", "GHLRhythm", "GHLCoop"}

# MIDI note numbers, by difficulty (0 easy .. 3 expert)
GEM_BASE = {3: 96, 2: 84, 1: 72, 0: 60}  # 5-fret green / drum kick; open is one below (ENHANCED_OPENS)
GHL_BASE = {3: 94, 2: 82, 1: 70, 0: 58}  # 6-fret open, then W1 W2 W3 B1 B2 B3
FORCE_HOPO = {d: b + 5 for d, b in GEM_BASE.items()}  # force strum is one above
GHL_LANES = {7: 0, 0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 8: 6}  # .chart lane -> offset from GHL_BASE
SOLO, TAP, STAR_POWER, FACE_OFF = 103, 104, 116, (105, 106)
DRUM_FILL, DRUM_ROLL, DRUM_ROLL_2 = range(120, 125), 126, 127
TOM_MARKER = {2: 110, 3: 111, 4: 112}  # yellow / blue / green
DOUBLE_KICK = 95
ACCENT, GHOST, NORMAL = 127, 1, 100

# [Song] details -> song.ini keys
SONG_DETAILS = {"Name": "name", "Artist": "artist", "Album": "album", "Genre": "genre", "Year": "year",
                "Charter": "charter"}

# Global events that belong to the lyrics; the converted chart leaves them out.
LYRIC_EVENTS = ("lyric ", "phrase_start", "phrase_end")


class Chart:
    def __init__(self):
        self.song = {}  # [Song] properties, quotes removed
        self.resolution = 192
        self.offset = 0.0  # seconds; YARG treats it like song.ini's delay
        self.tempos = []  # [(tick, bpm * 1000)]
        self.time_sigs = []  # [(tick, numerator, denominator exponent)]
        self.events = []  # [(tick, text)]
        self.tracks = {}  # {(instrument, difficulty 0-3): {"notes": [(tick, lane, length)], "special": [...], "events": [...]}}


LINE = re.compile(r"(\d+)\s*=\s*(\w+)\s*(.*)$")


def _decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def parse(path):
    chart, section = Chart(), None
    for line in _decode(Path(path).read_bytes()).splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            continue
        if not s or s in ("{", "}") or section is None:
            continue
        if section == "Song":
            key, _, value = s.partition("=")
            chart.song[key.strip()] = value.strip().strip('"')
            continue
        m = LINE.match(s)
        if not m:
            continue
        tick, kind, rest = int(m.group(1)), m.group(2), m.group(3).strip()
        values = rest.split()
        if section == "SyncTrack":
            if kind == "B" and values:
                chart.tempos.append((tick, int(values[0])))
            elif kind == "TS" and values:
                chart.time_sigs.append((tick, int(values[0]), int(values[1]) if len(values) > 1 else 2))
        elif section == "Events":
            if kind == "E":
                chart.events.append((tick, rest.strip('"')))
        else:
            sm = re.fullmatch(r"(Expert|Hard|Medium|Easy)(\w+)", section)
            if not sm or sm.group(2) not in TRACKS:
                continue
            track = chart.tracks.setdefault((sm.group(2), DIFFICULTIES[sm.group(1)]),
                                            {"notes": [], "special": [], "events": []})
            try:
                if kind == "N":
                    track["notes"].append((tick, int(values[0]), int(values[1]) if len(values) > 1 else 0))
                elif kind == "S":
                    track["special"].append((tick, int(values[0]), int(values[1]) if len(values) > 1 else 0))
                elif kind == "E":
                    track["events"].append((tick, rest.strip('"')))
            except (ValueError, IndexError):
                continue
    try:
        chart.resolution = int(float(chart.song.get("Resolution", 192))) or 192
    except ValueError:
        pass
    try:
        chart.offset = float(chart.song.get("Offset", 0) or 0)
    except ValueError:
        pass
    for part in (chart.tempos, chart.time_sigs, chart.events):
        part.sort(key=lambda e: e[0])
    return chart


def lyrics_text(chart):
    """The chart's own lyric events as plain lines (a starting point for vocal charting), or ""."""
    lines, words, word = [], [], ""
    for _, text in chart.events:
        if text.startswith("phrase_start") or text.startswith("phrase_end"):
            if word:
                words.append(word)
            if words:
                lines.append(" ".join(words))
            words, word = [], ""
        elif text.startswith("lyric "):
            syl = re.sub(r"[#^*%$/_§+]", "", re.sub(r"<[^>]*>", "", text[6:])).strip()
            if not syl:
                continue
            joined = syl.endswith(("-", "="))
            syl = syl.rstrip("-=") + ("-" if syl.endswith("=") else "")
            word += syl
            if not joined:
                words.append(word)
                word = ""
    if word:
        words.append(word)
    if words:
        lines.append(" ".join(words))
    return "\n".join(line for line in lines if line.strip())


# ---------------------------------------------------------------- conversion

def _latin1(text):
    return text.encode("latin-1", "replace").decode("latin-1")


def ini_values(path):
    """{lower-case key: value} from song.ini's [song] section ({} if there is none)."""
    import configparser
    try:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, configparser.Error):
        return {}
    section = next((s for s in cp.sections() if s.lower() == "song"), None)
    return {k.lower(): v.strip() for k, v in cp[section].items()} if section else {}


def _int(ini, key):
    try:
        return int(float(ini.get(key, "")))
    except ValueError:
        return None


def _hopo_thresholds(res, ini):
    """(.chart, notes.mid) natural HOPO thresholds in ticks, as the games work them out.

    By default .chart uses 65/192 of a beat and notes.mid a third of a beat plus a tick; where
    they disagree, notes get an explicit HOPO / strum marker."""
    if _int(ini, "hopo_frequency"):
        return _int(ini, "hopo_frequency"), _int(ini, "hopo_frequency")
    if ini.get("eighthnote_hopo", "").lower() in ("1", "true"):
        return 1 + res // 2, 1 + res // 2
    return int(65 / 192 * res), 1 + res // 3


def ini_additions(chart, ini):
    """song.ini settings the converted notes.mid needs to play like the .chart ({key: value}).

    notes.mid drops sustains up to a third of a beat long and .chart keeps them; .chart's
    Offset is song.ini's delay; and the games read song details from the .chart that
    song.ini leaves out, which a notes.mid has no place for."""
    add = {}
    for key, ini_key in SONG_DETAILS.items():
        value = chart.song.get(key, "").strip()
        if key == "Year":
            value = value.lstrip(", ")
        if value and ini_key not in ini:
            add[ini_key] = value
    try:
        preview = float(chart.song.get("PreviewStart", 0) or 0)
    except ValueError:
        preview = 0
    if preview > 0 and "preview_start_time" not in ini:
        add["preview_start_time"] = round(preview * 1000)
    if _int(ini, "sustain_cutoff_threshold") is None and any(
            0 < length <= 1 + chart.resolution // 3 for t in chart.tracks.values() for _, _, length in t["notes"]):
        add["sustain_cutoff_threshold"] = 1  # only the one-tick placeholders of notes with no sustain
    if chart.offset and "delay" not in ini:
        add["delay"] = round(chart.offset * 1000)
    return add


class _Events:
    """Collects (tick, message) pairs for one MIDI track."""

    def __init__(self):
        self.items = []  # (tick, order, message); order puts note-offs before note-ons at a tick

    def note(self, tick, end, pitch, velocity=NORMAL):
        import mido
        self.items.append([tick, 3, mido.Message("note_on", note=pitch, velocity=velocity), max(end, tick + 1)])

    def text(self, tick, text, kind="text"):
        import mido
        self.items.append([tick, 0, mido.MetaMessage(kind, text=_latin1(text)), None])

    def sysex(self, tick, data):
        import mido
        self.items.append([tick, 2, mido.Message("sysex", data=data), None])

    def track(self, name):
        import mido
        notes = sorted((i for i in self.items if i[3] is not None), key=lambda i: (i[2].note, i[0]))
        for a, b in zip(notes, notes[1:]):  # a sustain may not run into the next note of the same pitch
            if a[2].note == b[2].note and a[3] > b[0]:
                a[3] = b[0] if b[0] > a[0] else a[0]  # same tick: a duplicate, dropped below
        ev = []
        for tick, order, msg, end in self.items:
            if end is not None and end <= tick:
                continue  # a duplicate note at the same tick
            ev.append((tick, order, msg))
            if end is not None:
                ev.append((end, 1, mido.Message("note_off", note=msg.note, velocity=0)))
        ev.sort(key=lambda e: (e[0], e[1]))
        track, now = mido.MidiTrack([mido.MetaMessage("track_name", name=name, time=0)]), 0
        for tick, _, msg in ev:
            track.append(msg.copy(time=tick - now))
            now = tick
        track.append(mido.MetaMessage("end_of_track", time=0))
        return track


def _main_difficulty(chart, instrument):
    return max(d for (i, d) in chart.tracks if i == instrument)


def _shared_markers(chart, instrument, ev):
    """Star power, face-offs, solos and other track events, taken from the hardest difficulty."""
    t = chart.tracks[(instrument, _main_difficulty(chart, instrument))]
    for tick, kind, length in t["special"]:
        if kind == 2:
            ev.note(tick, tick + length, STAR_POWER)
        elif kind in (0, 1):
            ev.note(tick, tick + length, FACE_OFF[kind])
    solo = None
    for tick, text in t["events"]:
        if text == "solo":
            solo = tick
        elif text == "soloend" and solo is not None:
            ev.note(solo, tick + 1, SOLO)
            solo = None
        elif text not in ("solo", "soloend"):
            ev.text(tick, text if text.startswith("[") else f"[{text}]")


def _frets(chart, instrument, ev, ini):
    """5-fret and 6-fret guitar, bass and keys."""
    ghl = instrument in GHL
    t_chart, t_mid = _hopo_thresholds(chart.resolution, ini)
    lo, hi = min(t_chart, t_mid), max(t_chart, t_mid)
    taps = {}  # tick -> {difficulty: tapped?}
    opens = False
    for (inst, diff), t in sorted(chart.tracks.items()):
        if inst != instrument:
            continue
        groups = {}
        for tick, lane, length in t["notes"]:
            g = groups.setdefault(tick, {"lanes": {}, "forced": False, "tap": False})
            if lane == 5:
                g["forced"] = True
            elif lane == 6:
                g["tap"] = True
            elif lane in (GHL_LANES if ghl else (0, 1, 2, 3, 4, 7)):
                g["lanes"][lane] = max(length, g["lanes"].get(lane, 0))
        prev = None
        for tick in sorted(groups):
            g = groups[tick]
            lanes = g["lanes"]
            if not lanes:
                continue
            for lane, length in lanes.items():
                if ghl:
                    pitch = GHL_BASE[diff] + GHL_LANES[lane]
                elif lane == 7:
                    pitch, opens = GEM_BASE[diff] - 1, True
                else:
                    pitch = GEM_BASE[diff] + lane
                ev.note(tick, tick + length, pitch)
            taps.setdefault(tick, {})[diff] = g["tap"]
            chord = len(lanes) > 1
            if prev is None:
                natural, sure = False, True
            else:
                gap, prev_lanes = tick - prev, groups[prev]["lanes"]
                prev_chord = len(prev_lanes) > 1
                natural = not chord and (prev_chord or set(prev_lanes) != set(lanes)) and gap <= t_chart
                # Chords and repeated notes are never natural HOPOs; a single note right after a
                # chord is where the games' rules differ, so it is always marked.
                sure = (gap > hi or chord or (not prev_chord and set(prev_lanes) == set(lanes))
                        or (gap <= lo and not prev_chord))
            hopo = natural != g["forced"]
            if not g["tap"] and (hopo != natural or not sure):
                ev.note(tick, tick + 1, FORCE_HOPO[diff] + (0 if hopo else 1))
            prev = tick
    for tick, by_diff in taps.items():
        if not any(by_diff.values()):
            continue
        if all(by_diff.values()):
            ev.note(tick, tick + 1, TAP)
        else:  # only some difficulties tap here: Phase Shift's per-difficulty tap event
            for diff, tapped in by_diff.items():
                if tapped:
                    ev.sysex(tick, [0x50, 0x53, 0, 0, diff, 4, 1])
                    ev.sysex(tick + 1, [0x50, 0x53, 0, 0, diff, 4, 0])
    if opens and not ghl:
        ev.text(0, "[ENHANCED_OPENS]")
    _shared_markers(chart, instrument, ev)


def _drums(chart, ev):
    toms = {}  # (tick, lane) -> tom?  hardest difficulty wins: MIDI tom markers cover every difficulty
    dynamics = five_lane = False
    for (inst, diff), t in sorted(chart.tracks.items()):
        if inst != "Drums":
            continue
        five_lane = five_lane or any(lane == 5 for _, lane, _ in t["notes"])
    for (inst, diff), t in sorted(chart.tracks.items()):
        if inst != "Drums":
            continue
        flags = {}
        for tick, lane, _ in t["notes"]:
            flags.setdefault(tick, set()).add(lane)
        for tick, lane, length in t["notes"]:
            f = flags[tick]
            if lane == 32:
                ev.note(tick, tick + length, DOUBLE_KICK)
            elif 0 <= lane <= 5:
                velocity = ACCENT if lane + 33 in f else GHOST if lane + 39 in f else NORMAL
                dynamics = dynamics or velocity != NORMAL
                ev.note(tick, tick + length, GEM_BASE[diff] + lane, velocity)
                if lane in TOM_MARKER and not five_lane:
                    toms[(tick, lane)] = lane + 64 not in f
    for (tick, lane), tom in toms.items():
        if tom:
            ev.note(tick, tick + 1, TOM_MARKER[lane])
    if dynamics:
        ev.text(0, "[ENABLE_CHART_DYNAMICS]")
    t = chart.tracks[("Drums", _main_difficulty(chart, "Drums"))]
    for tick, kind, length in t["special"]:
        if kind == 64:
            for pitch in DRUM_FILL:
                ev.note(tick, tick + length, pitch)
        elif kind in (65, 66):
            ev.note(tick, tick + length, DRUM_ROLL if kind == 65 else DRUM_ROLL_2)
    _shared_markers(chart, "Drums", ev)


def to_midi(chart, ini=None):
    """The chart as a mido.MidiFile (same resolution, so every tick stays where it was).

    ini: the song's song.ini values (ini_values()), for its HOPO settings."""
    import mido
    mid = mido.MidiFile(type=1, ticks_per_beat=chart.resolution)
    tempo = mido.MidiTrack([mido.MetaMessage("track_name", name=_latin1(chart.song.get("Name", "")), time=0)])
    sync = [(t, 0, mido.MetaMessage("time_signature", numerator=n, denominator=2 ** e)) for t, n, e in chart.time_sigs]
    sync += [(t, 1, mido.MetaMessage("set_tempo", tempo=round(60e9 / b))) for t, b in chart.tempos if b > 0]
    now = 0
    for tick, _, msg in sorted(sync, key=lambda e: (e[0], e[1])):
        tempo.append(msg.copy(time=tick - now))
        now = tick
    tempo.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tempo)

    events = _Events()
    for tick, text in chart.events:
        if text.startswith(LYRIC_EVENTS) or not text:
            continue
        events.text(tick, text if text.startswith("[") else f"[{text}]")
    mid.tracks.append(events.track("EVENTS"))

    for instrument, name in TRACKS.items():
        if not any(i == instrument for i, _ in chart.tracks):
            continue
        ev = _Events()
        _drums(chart, ev) if instrument == "Drums" else _frets(chart, instrument, ev, ini or {})
        mid.tracks.append(ev.track(name))
    return mid


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name("notes.mid")
    if out.exists():
        sys.exit(f"{out} already exists; give another output path")
    chart, ini = parse(src), ini_values(src.with_name("song.ini"))
    to_midi(chart, ini).save(str(out))
    print(f"Wrote {out}")
    for key, value in ini_additions(chart, ini).items():
        print(f"Add {key} = {value} to song.ini so it plays the same as the .chart.")


if __name__ == "__main__":
    main()
