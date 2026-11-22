"""Post-extraction checks must reject a sound PKG container with broken theme assets.

Unlike the end-to-end tests, these use a mocked external extractor so the
regressions also run in CI without the platform-specific packaging toolchain.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest import mock

from PIL import Image

from app import pkg
from app.forge import pkg_inspector
from app.forge.sfo import build_param_sfo_bytes


def _png(size: tuple[int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, (255, 255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


def _parsed_with_valid_container() -> dict:
    meta = pkg.PackageMeta(
        title="Post-extraction audit",
        title_id="CUSA00000",
        content_id="UP9000-CUSA00000_00-" + "A" * 16,
        category="ac",
        passcode="0" * 32,
        mode="theme",
    )
    return {
        "file_size": 8,
        "package_size": 8,
        "drm_type": 0xF,
        "content_type": 0x1B,
        "iro_tag": 2,
        "content_id": meta.content_id,
        "header_digest_ok": True,
        "header_signature_present": True,
        "body_digest_ok": True,
        "digest_table_ok": True,
        "pfs_image_digest_ok": True,
        "pfs_signed_digest_ok": True,
        "sc_entries1_hash_ok": True,
        "sc_entries2_hash_ok": True,
        "entry_digest_mismatches": [],
        "sc0_blobs": {
            "Sc0/param.sfo": build_param_sfo_bytes(meta),
            "Sc0/icon0.png": _png((512, 512)),
            "Sc0/pic1.png": _png((1920, 1080)),
        },
    }


def _inspect_fake_extracted_pkg(
    tmp_path: Path, parsed: dict, xml: str, *, unsafe_link_target: Path | None = None,
):
    fake_pkg = tmp_path / "fake.pkg"
    fake_pkg.write_bytes(b"PKG FILE")

    def _fake_extract(_pkg, dest, **_kwargs):
        img0 = dest / "uroot"
        img0.mkdir()
        if unsafe_link_target is not None:
            (img0 / "sce_sys").symlink_to(unsafe_link_target, target_is_directory=True)
        (img0 / "theme.xml").write_text(xml, encoding="utf-8")
        return True, "Extracted"

    with (
        mock.patch.object(pkg_inspector, "parse_pkg_binary", return_value=parsed),
        mock.patch.object(pkg_inspector, "_extract_pkg_image0", side_effect=_fake_extract),
    ):
        return pkg_inspector.inspect_and_validate_pkg(fake_pkg)


def test_rejects_pkg_with_corrupt_theme_xml_despite_valid_container(tmp_path):
    report = _inspect_fake_extracted_pkg(
        tmp_path, _parsed_with_valid_container(), "<theme><broken>",
    )
    assert report.image0_extracted is True
    assert report.header_and_crypto_valid is True
    assert report.structure_validated is False
    assert report.verification_level == "failed"
    assert any("theme.xml" in error for error in report.errors)


def test_rejects_corrupt_sc0_icon_even_when_pkg_digests_appear_valid(tmp_path):
    parsed = _parsed_with_valid_container()
    parsed["sc0_blobs"]["Sc0/icon0.png"] = b"not-a-png"
    report = _inspect_fake_extracted_pkg(
        tmp_path,
        parsed,
        "<theme><format-version>4.0</format-version>"
        "<themecolor>4</themecolor><homebgm-enable>False</homebgm-enable></theme>",
    )
    assert report.structure_validated is False
    assert report.verification_level == "failed"
    assert any("icon0.png" in error for error in report.errors)


def test_rejects_symlinked_sc0_without_overwriting_external_files(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "icon0.png"
    protected.write_bytes(b"DO-NOT-OVERWRITE")

    report = _inspect_fake_extracted_pkg(
        tmp_path, _parsed_with_valid_container(), "<theme/>", unsafe_link_target=outside,
    )
    assert report.structure_validated is False
    assert any("link simbólico" in error for error in report.errors)
    assert protected.read_bytes() == b"DO-NOT-OVERWRITE"
