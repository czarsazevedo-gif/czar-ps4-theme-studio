"""Canonical contracts and schemas for CZAR Forge (Audio Studio, Validator, PKG Forge)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ForgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# --- Canonical PS4 Theme Constants -----------------------------------------

PS4_ATRAC9_SAMPLE_RATE = 48000
PS4_ATRAC9_CHANNELS = 2
PS4_ATRAC9_BITRATE_KBPS = 144
PS4_ATRAC9_BYTE_RATE = 18000          # 144 kbps = 18000 B/s
PS4_ATRAC9_BLOCK_ALIGN = 384          # 4 frames * 96 B/frame
PS4_ATRAC9_CONFIG_DATA_HEX = "fe740bf0"
PS4_ATRAC9_SUPERFRAME_SAMPLES = 1024
PS4_BGM_REFERENCE_LUFS = -32.0

PS4_THEME_VOLUME_TYPE = "pkg_ps4_theme"
PS4_THEME_CATEGORY = "ac"
PS4_THEME_IRO_TAG = 2
PS4_PKG_CONTENT_TYPE_AC = 0x1B
PS4_PKG_DEFAULT_PASSCODE = "0" * 32

# Pinned external upstream tool references (installed externally, never vendored)
EXTERNAL_TOOLS = {
    "psgfx": {
        "repo": "https://github.com/elripalda/psgfx",
        "commit": "5ee7aa9e7d91c9f518322847d13a6bda5ba97837",
        "license": "GPL-3.0-or-later",
        "role": "Native Linux ATRAC9 encoder CLI (external subprocess)",
    },
    "ffmpeg": {
        "repo": "https://github.com/FFmpeg/FFmpeg",
        "license": "GPL-2.0-or-later / LGPL-2.1-or-later (system Debian package)",
        "role": "Independent audio/video decoder, ATRAC9 validator, and EBU R128 loudness meter",
    },
    "liborbispkg": {
        "repo": "https://github.com/OpenOrbis/LibOrbisPkg",
        "commit": "92a4e7f8e5914cb06b52ba3ae6b0675da438a051",
        "license": "LGPL-3.0",
        "role": "PS4 PKG/PFS/SFO format reference and optional .NET packager",
    },
    "orbispkgtool": {
        "repo": "https://github.com/pearlxcore/OrbisPkgTool",
        "commit": "3de6da849b64a8ad0d5ba00dd4e8919934b868b0",
        "license": "MIT",
        "role": "PS4 PKG/PFS/SFO builder & 8-stage validator reference",
    },
}

VolumeMode = Literal["original", "ambient", "manual", "assisted"]
ForgeStatus = Literal[
    "ready",
    "processing",
    "validating",
    "completed",
    "failed",
    "pending_physical_test",
]
PhysicalCompatStatus = Literal["pending", "verified", "failed"]


class LoudnessReport(ForgeModel):
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    loudness_range_lu: float | None = None
    peak_dbfs: float | None = None
    rms_dbfs: float | None = None
    dynamic_range_db: float | None = None
    clipped_samples: int = 0
    reference_lufs: float = PS4_BGM_REFERENCE_LUFS
    delta_vs_reference_db: float | None = None
    bgmhome_loud: str | None = None
    method: str = "ebur128"


class LoopFinishReport(ForgeModel):
    mode: Literal["whole", "manual_excerpt", "auto_refined"] = "whole"
    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)
    sample_rate: int = PS4_ATRAC9_SAMPLE_RATE
    duration_seconds: float = Field(gt=0)
    boundary_discontinuity_before: float = Field(ge=0)
    boundary_discontinuity_after: float = Field(ge=0)
    energy_continuity_db: float = 0.0
    spectral_similarity: float = Field(ge=0.0, le=1.0)
    phase_correlation: float = Field(ge=-1.0, le=1.0)
    leading_silence_sec: float = Field(ge=0.0)
    trailing_silence_sec: float = Field(ge=0.0)
    click_risk_before: float = Field(ge=0.0)
    click_risk_after: float = Field(ge=0.0)
    crossfade_ms: float = Field(ge=0.0)
    fade_applied: bool = False
    convincing: bool = True
    warnings: list[str] = Field(default_factory=list)


class At9ValidationReport(ForgeModel):
    valid: bool
    codec: str
    sample_rate: int
    channels: int
    bitrate_bps: int
    byte_rate: int
    block_align: int
    config_data_hex: str
    riff_chunks: list[str] = Field(default_factory=list)
    has_fact: bool = False
    fact_samples: int | None = None
    encoder_delay: int | None = None
    has_smpl: bool = False
    loop_start_sample: int | None = None
    loop_end_sample: int | None = None
    play_count: int | None = None
    duration_seconds: float = 0.0
    decoded_samples: int = 0
    decoded_duration_seconds: float = 0.0
    decoded_peak_dbfs: float | None = None
    decoded_clipped_samples: int = 0
    truncated: bool = False
    validator: str = "ffmpeg+riff"
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BundleCheckItem(ForgeModel):
    id: str
    category: Literal["structure", "xml", "textures", "icons", "raf", "audio", "sfo", "gp4", "metadata"]
    label: str
    status: Literal["pass", "warn", "fail"]
    detail: str
    path: str | None = None


class BundleValidationReport(ForgeModel):
    valid: bool
    theme_kind: Literal["static", "animated"]
    has_music: bool
    has_sfx: bool
    sfx_slots: list[str] = Field(default_factory=list)
    checks: list[BundleCheckItem] = Field(default_factory=list)
    files: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


PkgVerificationLevel = Literal[
    "failed",
    "header_and_sc0_only",
    "extracted_uncompared",
    "full_bundle_verified",
]


class PkgValidationReport(ForgeModel):
    pkg_created: bool
    structure_validated: bool
    theme_type_valid: bool
    verification_level: PkgVerificationLevel = "failed"
    header_and_crypto_valid: bool = False
    sc0_validated: bool = False
    image0_extracted: bool = False
    bundle_compared: bool = False
    bundle_hashes_matched: bool = False
    content_id: str = ""
    title_id: str = ""
    volume_type: str = ""
    category: str = ""
    iro_tag_header: int | None = None
    iro_tag_sfo: int | None = None
    content_type_header: int | None = None
    bgmhome_loud: str | None = None
    pkg_bytes: int = 0
    pkg_sha256: str = ""
    files: list[dict] = Field(default_factory=list)
    extracted_match: bool = False
    hash_mismatches: list[str] = Field(default_factory=list)
    physical_compatibility: PhysicalCompatStatus = "pending"
    builder_backend: str = ""
    validator_backend: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
