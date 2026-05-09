"""Command-line entry point.

Usage::

    python -m resolve_markers path/to/Project.db -o ./out
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .extractor import (
    DEFAULT_FPS,
    extract,
    frame_to_tc,
    marker_still_filename,
)


def _write_segments_csv(path: Path, segments, fps: int) -> None:
    cols = [
        "segment_index",
        "segment_file_expected",
        "source_file",
        "timeline_start_tc",
        "timeline_end_tc",
        "duration_tc",
        "source_in_tc",
        "source_out_tc",
        "timeline_start_frame",
        "timeline_end_frame",
        "source_in_frame",
        "source_out_frame",
        "duration_frames",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in segments:
            w.writerow(
                {
                    "segment_index": s.segment_index,
                    "segment_file_expected": s.segment_file_expected,
                    "source_file": s.source_file,
                    "timeline_start_tc": frame_to_tc(s.timeline_start_frame, fps),
                    "timeline_end_tc": frame_to_tc(s.timeline_end_frame, fps),
                    "duration_tc": frame_to_tc(s.duration_frames, fps),
                    "source_in_tc": frame_to_tc(s.source_in_frame, fps),
                    "source_out_tc": frame_to_tc(s.source_out_frame, fps),
                    "timeline_start_frame": s.timeline_start_frame,
                    "timeline_end_frame": s.timeline_end_frame,
                    "source_in_frame": s.source_in_frame,
                    "source_out_frame": s.source_out_frame,
                    "duration_frames": s.duration_frames,
                }
            )


def _write_markers_csv(path: Path, markers, fps: int, slim: bool) -> None:
    full_cols = [
        "marker_id",
        "timeline_tc",
        "timeline_frame",
        "segment_index",
        "segment_file_expected",
        "relative_tc_in_segment",
        "relative_frame_in_segment",
        "relative_seconds_in_segment",
        "source_file",
        "source_tc",
        "source_frame",
        "marker_name",
        "video_marker_name",
        "audio_marker_name",
        "is_slide_marker",
        "same_name_occurrence",
        "is_repeated_name",
        "video_names_seen",
        "audio_names_seen",
        "still_filename",
        "ffmpeg_from_segment",
    ]
    slim_cols = [
        "marker_id",
        "timeline_tc",
        "segment_index",
        "segment_file_expected",
        "relative_tc_in_segment",
        "relative_seconds_in_segment",
        "marker_name",
        "video_marker_name",
        "audio_marker_name",
        "is_slide_marker",
        "same_name_occurrence",
        "still_filename",
    ]
    cols = slim_cols if slim else full_cols

    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for m in markers:
            still = marker_still_filename(m, fps)
            rel_seconds = round(m.relative_frame_in_segment / fps, 3)
            row = {
                "marker_id": m.marker_id,
                "timeline_tc": frame_to_tc(m.timeline_frame, fps),
                "timeline_frame": m.timeline_frame,
                "segment_index": m.segment.segment_index,
                "segment_file_expected": m.segment.segment_file_expected,
                "relative_tc_in_segment": frame_to_tc(m.relative_frame_in_segment, fps),
                "relative_frame_in_segment": m.relative_frame_in_segment,
                "relative_seconds_in_segment": rel_seconds,
                "source_file": m.source_file,
                "source_tc": frame_to_tc(m.source_frame, fps),
                "source_frame": m.source_frame,
                "marker_name": m.marker_name,
                "video_marker_name": m.video_marker_name,
                "audio_marker_name": m.audio_marker_name,
                "is_slide_marker": m.is_slide_marker,
                "same_name_occurrence": m.same_name_occurrence,
                "is_repeated_name": m.is_repeated_name,
                "video_names_seen": "; ".join(m.video_names_seen),
                "audio_names_seen": "; ".join(m.audio_names_seen),
                "still_filename": still,
                "ffmpeg_from_segment": (
                    f'ffmpeg -ss {rel_seconds:.3f} '
                    f'-i "{m.segment.segment_file_expected}" -frames:v 1 '
                    f'"stills/{still}"'
                ),
            }
            w.writerow(row)


def _write_stills_scripts(out_dir: Path, markers, fps: int) -> None:
    sh = out_dir / "extract_stills.sh"
    bat = out_dir / "extract_stills.bat"
    with sh.open("w", encoding="utf-8") as f:
        f.write("#!/bin/bash\nset -e\nmkdir -p stills\n")
        for m in markers:
            still = marker_still_filename(m, fps)
            rel_seconds = m.relative_frame_in_segment / fps
            f.write(
                f'ffmpeg -ss {rel_seconds:.3f} '
                f'-i "{m.segment.segment_file_expected}" -frames:v 1 '
                f'"stills/{still}"\n'
            )
    with bat.open("w", encoding="utf-8") as f:
        f.write("@echo off\r\nmkdir stills 2>nul\r\n")
        for m in markers:
            still = marker_still_filename(m, fps)
            rel_seconds = m.relative_frame_in_segment / fps
            f.write(
                f'ffmpeg -ss {rel_seconds:.3f} '
                f'-i "{m.segment.segment_file_expected}" -frames:v 1 '
                f'"stills\\{still}"\r\n'
            )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Extract timeline markers from a DaVinci Resolve Project.db. "
            "Open a copy of the database — never the live one."
        )
    )
    p.add_argument("db", help="Path to Project.db (work on a copy)")
    p.add_argument(
        "-o", "--out", default="markers_out",
        help="Output directory (default: ./markers_out)",
    )
    p.add_argument(
        "--fps", type=int, default=DEFAULT_FPS,
        help=f"Timeline frame rate (default: {DEFAULT_FPS})",
    )
    p.add_argument(
        "--no-stills-script", action="store_true",
        help="Skip writing the ffmpeg extract_stills helpers",
    )
    args = p.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments, markers = extract(args.db, fps=args.fps)

    _write_segments_csv(out_dir / "segments.csv", segments, args.fps)
    _write_markers_csv(out_dir / "markers_full.csv", markers, args.fps, slim=False)
    _write_markers_csv(out_dir / "markers_clean.csv", markers, args.fps, slim=True)
    if not args.no_stills_script:
        _write_stills_scripts(out_dir, markers, args.fps)

    print(f"Segments: {len(segments)}")
    print(f"Markers : {len(markers)}")
    print(f"Output  : {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
