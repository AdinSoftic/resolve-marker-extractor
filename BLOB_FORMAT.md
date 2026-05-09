# `Sm2TiItemLockableBlob.FieldsBlob` format

This is the on-disk layout of the per-clip `FieldsBlob` value in
`BtLockableBlob` rows where `DbType = 'Sm2TiItemLockableBlob'`. It is the
container that holds the markers for one timeline clip.

Reverse-engineered against:

- DaVinci Resolve **21.0.0b.0020**
- `DbAppVer = 21.0.0b.0020`, `DbPrjVer = 17`
- Project frame rate: **30 fps non-drop-frame**

The format is undocumented and may change. If a future Resolve breaks
parsing, look here first.

## High-level layout

```
+-----------------------------+
| Wrapper header (~46 bytes)  |   includes the literal "BlobData" string
|                             |   in UTF-16LE plus a few length fields
+-----------------------------+
| Single padding/version byte |   typically 0x81
+-----------------------------+
| zstd frame (magic 28B52FFD) |   compressed marker payload
+-----------------------------+
```

Two clips have such a small marker count that the whole payload fits
under ~120 bytes; in those cases the marker records are stored inline
**without** the zstd frame. The parser tries zstd first and falls back to
scanning for a record start byte.

## After decompression

The decompressed payload is a length-prefixed protobuf-ish container.
Top level:

```
0x12 <varint:total_len>
    ( record )*
```

Each `record`:

```
0x12 <varint:rec_len>
    0x0A <varint:tlen>
        0x08 <varint:time2>            ← source-frame * 2
    0x12 <varint:inner_len>
        <8-byte fixed header>          ← e.g. 00 00 00 02 00 00 00 <strlen>
        0x0A <varint:r2_len>
            0x08 0x02
            0x1A 0x00
            0x1A 0x01 0x31             ← invariant prefix
            0x1A <varint:name_len> <utf-8 name>
```

The constant byte sequence `1A 01 31 1A` (size-1 field "1", then a length
prefix for the name) is invariant across every record we have observed
and is the easiest anchor to find the marker name.

## time2 → frame

```
frame = time2 // 2
```

`time2` is stored as a [protobuf varint][varint]. Multiply or divide by
the project frame rate (30 fps NDF in our test data) to convert to
seconds or to `HH:MM:SS:FF` timecode.

[varint]: https://protobuf.dev/programming-guides/encoding/#varints

## Mapping a marker to the timeline

Each `Sm2TiItem` row gives you `Start`, `Duration`, and `In`:

- `Start`: timeline-frame where the clip begins
- `In`: source-media frame where the clip starts taking footage from
- `Duration`: clip length in frames

For a marker at `source_frame`:

```
relative_in_segment = source_frame - In
timeline_frame      = Start + relative_in_segment
```

…provided `In <= source_frame < In + Duration`.

## Why we deduplicate by `(source_file, source_frame)`

Each clip's blob actually holds **every** marker recorded against the
underlying source media file, not only those that fall inside the clip's
`[In, In + Duration)` range. So a single source frame may appear in
multiple clip blobs. We collapse all of those into a single output row
keyed by `(source_file, source_frame)`, then map that pair onto the one
active clip whose source range contains it.

## Video vs audio markers

A single video timeline clip and its paired audio clip share the same
`Start` and the same source range. Both keep their own `FieldsBlob` and
both can have markers.

The extractor surfaces both names as they are stored in the Resolve
project. If a marker was not explicitly renamed, Resolve itself usually
stores a default `Marker N` label. The output keeps both
`video_marker_name` and `audio_marker_name`, while `marker_name` picks
the best display label available.
