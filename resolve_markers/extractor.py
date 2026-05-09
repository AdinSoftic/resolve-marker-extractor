"""
DaVinci Resolve timeline marker extractor.

Reads the SQLite ``Project.db`` that Resolve writes for every project,
decompresses the per-clip blobs that store marker records, and emits a
clean per-segment marker table.

The extraction is read-only. Open a *copy* of ``Project.db``; never run
this against the live database while Resolve is open.

Tested against:
    DbAppVer 21.0.0b.0020 / DbPrjVer 17

The internal schema is undocumented and may change in future Resolve
releases. If a future version stops parsing, the most likely cause is
a change to the FieldsBlob layout described in ``BLOB_FORMAT.md``.
"""
from __future__ import annotations

import collections
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import zstandard as zstd

# Zstd frame magic. Resolve prefixes the FieldsBlob with a small wrapper
# (length + the literal "BlobData" UTF-16 string + a few length fields)
# and then a single 0x81 byte before the zstd frame begins.
ZSTD_MAGIC = bytes.fromhex("28b52ffd")

DEFAULT_FPS = 30


# --------------------------------------------------------------------------- #
# protobuf-style varint + record parser
# --------------------------------------------------------------------------- #

def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    res = 0
    shift = 0
    while i < len(buf):
        c = buf[i]
        i += 1
        res |= (c & 0x7F) << shift
        if c < 128:
            return res, i
        shift += 7
        if shift > 70:
            break
    raise ValueError("malformed varint")


@dataclass
class RawMarker:
    """One marker as encoded inside a FieldsBlob.

    ``frame`` is in source-media frames (already divided by 2 from the
    on-disk ``time2`` value). The Resolve schema stores marker times as
    ``frame * 2`` in a 1/60s-style tick.
    """
    time2: int
    frame: int
    name: str | None


def parse_markers(decompressed: bytes) -> list[RawMarker]:
    """Parse marker records from a decompressed FieldsBlob payload.

    The wire format observed for ``Sm2TiItemLockableBlob`` payloads is::

        0x12 <varint:total_len>
            ( record )*

    where each *record* is::

        0x12 <varint:rec_len>
            0x0A <varint:tlen> 0x08 <varint:time2>
            0x12 <varint:inner_len>
                <8 byte fixed header>
                0x0A <varint> 0x08 0x02 0x1A 0x00 0x1A 0x01 0x31
                0x1A <varint:name_len> <utf-8 name>

    We locate the name by scanning for the constant prefix
    ``1A 01 31 1A`` because the surrounding scaffolding is invariant
    across every record we have observed.
    """
    out: list[RawMarker] = []
    i = 0

    # Skip the outer wrapper if present.
    if i < len(decompressed) and decompressed[i] == 0x12:
        i += 1
        try:
            _, i = _read_varint(decompressed, i)
        except ValueError:
            return out

    name_sig = bytes([0x1A, 0x01, 0x31, 0x1A])

    while i < len(decompressed):
        if decompressed[i] != 0x12:
            i += 1
            continue
        i += 1
        try:
            rec_len, i = _read_varint(decompressed, i)
        except ValueError:
            break
        rec_end = i + rec_len
        if rec_end > len(decompressed):
            break
        rec = decompressed[i:rec_end]
        i = rec_end

        if not rec or rec[0] != 0x0A:
            continue
        j = 1
        try:
            tlen, j = _read_varint(rec, j)
        except ValueError:
            continue
        time_block = rec[j:j + tlen]
        j += tlen
        if not time_block or time_block[0] != 0x08:
            continue
        try:
            time2, _ = _read_varint(time_block, 1)
        except ValueError:
            continue

        if j >= len(rec) or rec[j] != 0x12:
            out.append(RawMarker(time2=time2, frame=int(time2 / 2), name=None))
            continue
        j += 1
        try:
            ilen, j = _read_varint(rec, j)
        except ValueError:
            continue
        inner = rec[j:j + ilen]

        name: str | None = None
        idx = inner.find(name_sig)
        if idx >= 0:
            ns = idx + 4
            try:
                nlen, ns2 = _read_varint(inner, ns)
                name = inner[ns2:ns2 + nlen].decode("utf-8", errors="replace")
            except ValueError:
                pass
        out.append(RawMarker(time2=time2, frame=int(time2 / 2), name=name))

    return out


def decode_blob(blob: bytes) -> list[RawMarker]:
    """Decompress (if needed) and parse a single ``FieldsBlob`` value."""
    if not blob:
        return []
    idx = blob.find(ZSTD_MAGIC)
    if idx >= 0:
        try:
            decomp = zstd.ZstdDecompressor().decompress(blob[idx:])
            return parse_markers(decomp)
        except zstd.ZstdError:
            pass
    # Small blobs (< ~120 bytes) are stored uncompressed. Skip the
    # variable-length wrapper and parse from the first plausible record
    # start byte.
    for start in range(40, min(len(blob), 80)):
        if blob[start] == 0x12:
            try:
                m = parse_markers(blob[start:])
                if m:
                    return m
            except ValueError:
                continue
    return []


# --------------------------------------------------------------------------- #
# timecode helpers
# --------------------------------------------------------------------------- #

def frame_to_tc(frame: float | int, fps: int = DEFAULT_FPS) -> str:
    """Frames -> non-drop-frame ``HH:MM:SS:FF`` timecode."""
    frame = int(round(frame))
    sign = "-" if frame < 0 else ""
    frame = abs(frame)
    h = frame // (fps * 3600)
    frame %= fps * 3600
    m = frame // (fps * 60)
    frame %= fps * 60
    s = frame // fps
    f = frame % fps
    return f"{sign}{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def tc_to_frame(tc: str, fps: int = DEFAULT_FPS) -> int:
    h, m, s, f = (int(x) for x in tc.split(":"))
    return ((h * 3600 + m * 60 + s) * fps) + f


def safe_filename(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]+', "_", text or "marker")
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:80] or "marker"


# --------------------------------------------------------------------------- #
# data classes
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    """A timeline video clip plus its source-media slice."""
    segment_index: int
    item_id: str
    source_file: str
    timeline_start_frame: int
    timeline_end_frame: int
    source_in_frame: int
    source_out_frame: int
    duration_frames: int

    @property
    def segment_file_expected(self) -> str:
        return f"segment_{self.segment_index:03d}.mp4"


@dataclass
class Marker:
    timeline_frame: int
    segment: Segment
    relative_frame_in_segment: int
    source_file: str
    source_frame: int
    marker_name: str
    video_marker_name: str
    audio_marker_name: str
    is_slide_marker: bool = False
    same_name_occurrence: int = 1
    is_repeated_name: bool = False
    marker_id: str = ""
    video_names_seen: list[str] = field(default_factory=list)
    audio_names_seen: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# main pipeline
# --------------------------------------------------------------------------- #

_MARKER_N_RE = re.compile(r"^Marker \d+$")


def extract(db_path: str | Path, fps: int = DEFAULT_FPS) -> tuple[list[Segment], list[Marker]]:
    """Read ``db_path`` and return ``(segments, markers)``.

    ``segments`` is the list of timeline video clips, ordered by their
    start frame on the timeline. ``markers`` is the deduplicated marker
    list across all clips.
    """
    db_path = Path(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    items = {
        r["Sm2TiItem_id"]: dict(r)
        for r in cur.execute(
            'SELECT Sm2TiItem_id, Name, Start, Duration, "In" AS InVal, '
            "MediaTrackIdx FROM Sm2TiItem"
        )
    }

    # Parse every marker blob, keyed by (source_file, source_frame).
    position_map: dict[tuple[str, int], dict[str, list[str]]] = (
        collections.defaultdict(lambda: {"video": [], "audio": []})
    )
    for r in cur.execute(
        "SELECT BlobOwner, FieldsBlob FROM BtLockableBlob "
        'WHERE DbType="Sm2TiItemLockableBlob"'
    ):
        item = items.get(r["BlobOwner"])
        if not item:
            continue
        track = "audio" if item["MediaTrackIdx"] is not None else "video"
        for m in decode_blob(r["FieldsBlob"] or b""):
            if m.name is None:
                continue
            position_map[(item["Name"], m.frame)][track].append(m.name)

    con.close()

    # Build the segment list from the video clips on the timeline.
    video_items = [it for it in items.values() if it["MediaTrackIdx"] is None]
    video_items.sort(key=lambda it: int(it["Start"]))
    segments: list[Segment] = []
    for i, it in enumerate(video_items, 1):
        start = int(it["Start"])
        dur = int(it["Duration"])
        in_frame = int(it["InVal"])
        segments.append(
            Segment(
                segment_index=i,
                item_id=it["Sm2TiItem_id"],
                source_file=it["Name"],
                timeline_start_frame=start,
                timeline_end_frame=start + dur,
                source_in_frame=in_frame,
                source_out_frame=in_frame + dur,
                duration_frames=dur,
            )
        )

    # Map every (source_file, source_frame) onto exactly one segment.
    def find_segment(source_file: str, source_frame: int) -> Segment | None:
        for seg in segments:
            if (
                seg.source_file == source_file
                and seg.source_in_frame <= source_frame < seg.source_out_frame
            ):
                return seg
        return None

    raw_rows: list[Marker] = []
    for (source_file, source_frame), data in position_map.items():
        seg = find_segment(source_file, source_frame)
        if seg is None:
            # Marker decoded from a clip blob but the source frame is not
            # covered by any *active* timeline clip. Skip silently — these
            # are usually leftovers from clips that have been edited out.
            continue

        rel = source_frame - seg.source_in_frame
        timeline_frame = seg.timeline_start_frame + rel

        video_names = list(dict.fromkeys(data["video"]))
        audio_names = list(dict.fromkeys(data["audio"]))

        best_video = next((n for n in video_names if n and not _MARKER_N_RE.match(n)), None)
        if not best_video and video_names:
            best_video = video_names[0]
        audio_marker = next((n for n in audio_names if _MARKER_N_RE.match(n)), None)
        if not audio_marker and audio_names:
            audio_marker = audio_names[0]

        label = best_video or audio_marker or "UNNAMED"
        raw_rows.append(
            Marker(
                timeline_frame=timeline_frame,
                segment=seg,
                relative_frame_in_segment=rel,
                source_file=source_file,
                source_frame=source_frame,
                marker_name=label,
                video_marker_name=best_video or "",
                audio_marker_name=audio_marker or "",
                is_slide_marker=label.startswith("Slide_"),
                video_names_seen=video_names,
                audio_names_seen=audio_names,
            )
        )

    raw_rows.sort(key=lambda m: m.timeline_frame)

    name_counts = collections.Counter(m.marker_name for m in raw_rows)
    running: collections.Counter[str] = collections.Counter()
    for i, m in enumerate(raw_rows, 1):
        running[m.marker_name] += 1
        m.marker_id = f"m{i:03d}"
        m.same_name_occurrence = running[m.marker_name]
        m.is_repeated_name = name_counts[m.marker_name] > 1

    return segments, raw_rows


def marker_still_filename(m: Marker, fps: int = DEFAULT_FPS) -> str:
    rel_tc = frame_to_tc(m.relative_frame_in_segment, fps).replace(":", "-")
    return (
        f"{m.marker_id}__seg{m.segment.segment_index:03d}__"
        f"{rel_tc}__{safe_filename(m.marker_name)}.jpg"
    )
