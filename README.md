# resolve-marker-extractor

Read every clip marker out of a DaVinci Resolve `Project.db`, deduplicate the
ones that come from paired video/audio clips, map each marker onto the
timeline segment that contains it, and emit clean CSVs plus a ready-to-run
`ffmpeg` script that grabs one still per marker.

Built because Resolve's own GUI export paths leave most clip markers behind
in long projects, the marker-grabbing community script needs Resolve Studio,
and the SQL data is *right there* on disk.

> ⚠️ **Unofficial.** Blackmagic does not document or support direct access to
> `Project.db`. The schema may change between Resolve versions. Always work on
> a **copy** of the database, and never run anything against it while Resolve
> is open. This tool is **read-only**.

## What it gets you

For a project with 14 timeline segments and ~160 markers, you get:

```
markers_out/
├── markers_clean.csv      # slim, AI-friendly columns
├── markers_full.csv       # everything, including raw-name traces
├── segments.csv           # the 14 segment definitions, timeline ↔ source
├── extract_stills.sh      # ffmpeg one-frame-per-marker (bash)
└── extract_stills.bat     # ffmpeg one-frame-per-marker (Windows)
```

`markers_clean.csv` columns:

| column | meaning |
|---|---|
| `marker_id` | `m001`, `m002`, … (stable across the whole project) |
| `timeline_tc` | absolute timeline timecode `HH:MM:SS:FF` |
| `segment_index` / `segment_file_expected` | which exported clip the marker lives in |
| `relative_tc_in_segment` / `relative_seconds_in_segment` | offset *inside* that exported clip — what `ffmpeg -ss` wants |
| `marker_name` | chosen display name from the Resolve project; if you did not rename it, Resolve's default `Marker N` label is used |
| `video_marker_name` / `audio_marker_name` | names seen on both sides of a paired clip, kept for cross-reference |
| `is_slide_marker` | `True` for `Slide_*` markers |
| `same_name_occurrence` | 1, 2, 3 … for repeated names like `Slide_Introduction` |
| `still_filename` | what the ffmpeg script will write |

## Install

```bash
pip install zstandard
git clone https://github.com/AdinSoftic/resolve-marker-extractor.git
cd resolve-marker-extractor
```

Python 3.10+. The only runtime dependency is [`zstandard`](https://pypi.org/project/zstandard/) — Resolve compresses the marker payloads with zstd.

## Usage

```bash
# 1. Copy your project DB out of the way. Never read the live one.
cp ~/Library/Application\ Support/Blackmagic\ Design/DaVinci\ Resolve/\
Resolve\ Disk\ Database/Resolve\ Projects/Users/guest/Projects/<your-project>/Project.db \
./Project.db

# 2. Run the extractor.
python -m resolve_markers ./Project.db -o ./markers_out

# 3. Export your timeline as 14 individual clips from Resolve's Deliver page,
#    naming them segment_001.mp4 … segment_014.mp4.
#    Drop them next to the script.

# 4. Grab the stills.
bash markers_out/extract_stills.sh   # macOS / Linux
markers_out\extract_stills.bat       # Windows
```

You now have one JPEG per marker in `./stills/`, named like
`m042__seg007__00-05-10-06__Marker_1.jpg`.

### As a library

```python
from resolve_markers import extract, frame_to_tc

segments, markers = extract("Project.db", fps=30)

for m in markers:
    print(frame_to_tc(m.timeline_frame), m.marker_name,
          "→ seg", m.segment.segment_index)
```

## How it works

`Project.db` is a SQLite file. The interesting tables are:

- `Sm2TiItem` — one row per timeline clip. Holds `Start`, `Duration`, `In`,
  `Name` (= the source media filename), and a `MediaTrackIdx` that's `NULL`
  for video clips and `0` for audio clips.
- `BtLockableBlob` with `DbType = "Sm2TiItemLockableBlob"` — one row per
  clip, holding a `FieldsBlob` BLOB.

Each `FieldsBlob` is a small wrapper followed by a **zstd frame**
(magic `28 B5 2F FD`). Decompress that and you get a protobuf-ish stream of
marker records:

```
0x12 <varint:total_len>
   ( record )*

record:
   0x12 <varint:rec_len>
       0x0A <varint:tlen> 0x08 <varint:time2>
       0x12 <varint:inner_len>
           <8-byte fixed header>
           0x0A <varint> 0x08 0x02 0x1A 0x00 0x1A 0x01 0x31
           0x1A <varint:name_len> <utf-8 name>
```

`time2 / 2` is the marker's source-media frame at the project frame rate.
Two non-obvious points worth knowing if you want to fork or fix this:

1. **Each clip's blob carries every marker for the whole source media file**,
   not only the markers inside that clip's `[In, In+Duration)` range. So we
   deduplicate by `(source_file, source_frame)`.
2. **Marker names are taken directly from the Resolve project.** If you named
  a marker in DaVinci Resolve, that name is preserved. If you did not,
  Resolve itself stores the default `Marker 1`, `Marker 2`, … label. We keep
  both `video_marker_name` and `audio_marker_name` when both are present, and
  `marker_name` picks the best display label available.

See [`BLOB_FORMAT.md`](BLOB_FORMAT.md) for the full byte-by-byte breakdown.

## Tested against

- DaVinci Resolve 21.0.0b.0020, project schema version 17 (`DbPrjVer 17`).
- 30 fps non-drop-frame timelines.

If you confirm it on a different version, please open an issue or PR with
the version string and a small redacted sample.

## Limitations and known gotchas

- **Read-only by design.** We never write to `Project.db`. Don't add a write
  path. You will lose data.
- Drop-frame timecode is not implemented. NDF math only.
- Timeline markers (the ones on the ruler, not on a clip) are not in the
  same blob and aren't extracted.
- Stale/deleted markers from previous edits sometimes still sit in the blob.
  We filter them by requiring each `(source_file, source_frame)` to fall
  inside an active timeline clip's `[In, In+Duration)` range.

## Contributing

Bug reports with a redacted minimum-reproducer DB are very welcome. Please
strip everything other than `Sm2TiItem` and `BtLockableBlob` rows before
attaching, and double-check that no clip filenames or media paths in your
DB leak anything you'd rather keep private.

## License

[MIT](LICENSE).

## Disclaimer

This is an independent project. Not affiliated with, endorsed by, or
supported by Blackmagic Design Pty. Ltd. *DaVinci Resolve* is a trademark of
Blackmagic Design. Use at your own risk; back up your projects.
