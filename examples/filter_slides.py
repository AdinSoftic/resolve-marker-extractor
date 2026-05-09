"""Example: filter to slide markers and print one ffmpeg command per slide.

Run from a directory containing your Project.db:

    python examples/filter_slides.py Project.db
"""
from __future__ import annotations

import sys
from pathlib import Path

from resolve_markers.extractor import extract, frame_to_tc, marker_still_filename


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python examples/filter_slides.py path/to/Project.db")
        return 1

    db = Path(sys.argv[1])
    segments, markers = extract(db, fps=30)

    slides = [m for m in markers if m.is_slide_marker]
    print(f"{len(segments)} segments, {len(markers)} markers, "
          f"{len(slides)} slide markers\n")

    for m in slides:
        rel_seconds = m.relative_frame_in_segment / 30
        still = marker_still_filename(m)
        print(
            f'ffmpeg -ss {rel_seconds:.3f} '
            f'-i "{m.segment.segment_file_expected}" -frames:v 1 '
            f'"stills/{still}"  # {frame_to_tc(m.timeline_frame)} '
            f'#{m.same_name_occurrence}'
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
