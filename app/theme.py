"""Output assembly: index.xml, frame names and .zip packaging."""

from __future__ import annotations

import zipfile
from pathlib import Path

from .config import MAX_FRAME_COUNT, RAF_DIR_NAME, RAF_PARENT, RAF_SIZE_LIMIT


def frame_name(index: int, ext: str = "dds") -> str:
    """00.dds, 01.dds, 02.dds ... the way the PS4 expects them."""
    return f"{index:02d}.{ext}"


def write_index_xml(dest: Path, content: str) -> Path:
    """Writes index.xml always with LF line endings.

    `Path.write_text` on Windows turns \\n into \\r\\n, so the file would differ
    depending on where the app runs. Forcing newline="\\n" keeps the bytes
    identical everywhere.
    """
    with open(dest, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return dest


def check_frame_count(frames: int) -> None:
    """Frame-count cap, separate from the weight one.

    They are independent limits: there are packages of the same weight where
    one applies and the other does not, and the only difference is how many
    frames they carry.
    """
    if frames > MAX_FRAME_COUNT:
        raise ValueError(
            f"{frames} frames exceed the maximum of {MAX_FRAME_COUNT} the scene "
            "accepts, however light they are. Lower the count."
        )


def check_budget(total_bytes: int) -> None:
    """Stops the export when the .dds files do not fit in the folder.

    A hard console limit: go over it and the theme will not apply.
    """
    if total_bytes > RAF_SIZE_LIMIT:
        over = total_bytes - RAF_SIZE_LIMIT
        raise ValueError(
            f"The frames add up to {total_bytes / 1048576:.2f} MB and the "
            f"background.raf limit is {RAF_SIZE_LIMIT / 1048576:.0f} MB "
            f"({over / 1048576:.2f} MB over). Lower the frame count or the "
            "animated background resolution."
        )


def zip_raf_folder(src_dir: Path, dest_zip: Path) -> Path:
    """Packs the folder as scene/background.raf/ inside the .zip.

    The full path is kept so that unzipping drops it exactly where it belongs
    inside the theme.
    """
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{RAF_PARENT}/{RAF_DIR_NAME}"
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(src_dir.iterdir()):
            if path.is_file():
                zf.write(path, f"{prefix}/{path.name}")
    return dest_zip
