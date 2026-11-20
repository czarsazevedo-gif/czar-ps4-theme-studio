"""Native binary PS4 param.sfo (PSF v1.1) writer and independent parser.

Implements the exact binary layout written by Sony's sfo_create for PS4 themes:
- Magic: \\x00PSF, version 0x00000101
- Entries sorted lexicographically by key
- Formats: 0x0204 (UTF-8 null-terminated), 0x0404 (uint32/int32 LE)
- Theme mode keys: ATTRIBUTE, CATEGORY='ac', CONTENT_ID, CONTENT_VER='01.00',
  FORMAT='obs', IRO_TAG=2, PUBTOOLINFO, TITLE, TITLE_ID, VERSION='01.00'
  (no APP_TYPE, APP_VER, DOWNLOAD_DATA_SIZE, PARENTAL_LEVEL, SYSTEM_VER).
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass
from typing import Any

from ..pkg import PackageMeta

SFO_MAGIC = b"\x00PSF"
SFO_VERSION = 0x00000101

FMT_UTF8_SPECIAL = 0x0004
FMT_UTF8 = 0x0204
FMT_INT32 = 0x0404


@dataclass(frozen=True)
class SfoEntry:
    key: str
    fmt: int
    value: str | int
    max_len: int


def _align4(n: int) -> int:
    return (n + 3) & ~3


def build_sfo_entries(meta: PackageMeta, *, c_date: str | None = None) -> list[SfoEntry]:
    """Construct canonical sorted param.sfo entries matching app/pkg.py:build_param_sfx."""
    meta.validate()
    iro_tag = meta.profile["iro_tag"]
    entries: list[SfoEntry] = [
        SfoEntry("ATTRIBUTE", FMT_INT32, 0, 4),
        SfoEntry("CATEGORY", FMT_UTF8, meta.category, 4),
        SfoEntry("CONTENT_ID", FMT_UTF8, meta.content_id, 48),
        SfoEntry("FORMAT", FMT_UTF8, "obs", 4),
        SfoEntry("TITLE", FMT_UTF8, meta.title, 128),
        SfoEntry("TITLE_ID", FMT_UTF8, meta.title_id, 12),
        SfoEntry("VERSION", FMT_UTF8, "01.00", 8),
    ]
    if iro_tag is not None:
        entries.extend(
            [
                SfoEntry("CONTENT_VER", FMT_UTF8, "01.00", 8),
                SfoEntry("IRO_TAG", FMT_INT32, int(iro_tag), 4),
            ]
        )
    else:
        entries.extend(
            [
                SfoEntry("APP_TYPE", FMT_INT32, 1, 4),
                SfoEntry("APP_VER", FMT_UTF8, "01.00", 8),
                SfoEntry("DOWNLOAD_DATA_SIZE", FMT_INT32, 0, 4),
                SfoEntry("PARENTAL_LEVEL", FMT_INT32, 1, 4),
                SfoEntry("SYSTEM_VER", FMT_INT32, 0, 4),
            ]
        )

    date_str = c_date or f"{_dt.date.today():%Y%m%d}"
    info = [f"c_date={date_str}"]
    if meta.bgm_loudness is not None:
        info.append(f"bgmhome_loud={meta.bgm_loudness:.2f}")
    entries.append(SfoEntry("PUBTOOLINFO", FMT_UTF8, ",".join(info), 512))
    entries.sort(key=lambda e: e.key.encode("ascii"))
    return entries


def encode_param_sfo(entries: list[SfoEntry]) -> bytes:
    """Serialize sorted SfoEntry items into a canonical binary PSF v1.1 param.sfo."""
    ordered = sorted(entries, key=lambda e: e.key.encode("ascii"))
    key_table = bytearray()
    data_table = bytearray()
    index_rows: list[bytes] = []

    for entry in ordered:
        key_offset = len(key_table)
        key_table += entry.key.encode("ascii") + b"\x00"

        data_offset = len(data_table)
        if entry.fmt == FMT_INT32:
            raw_val = struct.pack("<i", int(entry.value))
            data_len = 4
            max_len = max(4, _align4(entry.max_len))
            padded = raw_val.ljust(max_len, b"\x00")
        elif entry.fmt in (FMT_UTF8, FMT_UTF8_SPECIAL):
            encoded = str(entry.value).encode("utf-8")
            max_len = _align4(max(entry.max_len, len(encoded) + (1 if entry.fmt == FMT_UTF8 else 0)))
            if entry.fmt == FMT_UTF8:
                if len(encoded) >= max_len:
                    encoded = encoded[: max_len - 1]
                raw_val = encoded + b"\x00"
            else:
                raw_val = encoded[:max_len]
            data_len = len(raw_val)
            padded = raw_val.ljust(max_len, b"\x00")
        else:
            raise ValueError(f"Formato SFO não suportado: 0x{entry.fmt:04x}")

        data_table += padded
        index_rows.append(
            struct.pack("<HHIII", key_offset, entry.fmt, data_len, max_len, data_offset)
        )

    # Pad key table to 4-byte boundary so data_table_start is 4-byte aligned
    while len(key_table) % 4 != 0:
        key_table.append(0)

    header_size = 20
    index_size = len(ordered) * 16
    key_table_start = header_size + index_size
    data_table_start = key_table_start + len(key_table)

    header = struct.pack(
        "<4sIIII",
        SFO_MAGIC,
        SFO_VERSION,
        key_table_start,
        data_table_start,
        len(ordered),
    )
    return header + b"".join(index_rows) + bytes(key_table) + bytes(data_table)


def build_param_sfo_bytes(meta: PackageMeta, *, c_date: str | None = None) -> bytes:
    """Build canonical binary param.sfo bytes for a PackageMeta."""
    return encode_param_sfo(build_sfo_entries(meta, c_date=c_date))


def parse_param_sfo(data: bytes) -> dict[str, Any]:
    """Independently parse a binary PSF v1.1 param.sfo into a dictionary of values + metadata."""
    if len(data) < 20:
        raise ValueError("param.sfo truncado (< 20 bytes).")
    magic, version, key_table_start, data_table_start, count = struct.unpack_from("<4sIIII", data, 0)
    if magic != SFO_MAGIC:
        raise ValueError(f"Assinatura PSF inválida no param.sfo: {magic!r}")
    if key_table_start < 20 + count * 16 or data_table_start < key_table_start or data_table_start > len(data):
        raise ValueError("Offsets de tabela inválidos no param.sfo.")

    values: dict[str, str | int] = {}
    entries_meta: dict[str, dict[str, Any]] = {}
    keys_order: list[str] = []

    for i in range(count):
        off = 20 + i * 16
        key_off, fmt, data_len, max_len, data_off = struct.unpack_from("<HHIII", data, off)
        k_start = key_table_start + key_off
        k_end = data.find(b"\x00", k_start, data_table_start)
        if k_end == -1:
            raise ValueError(f"Chave SFO sem terminador nulo na entrada {i}.")
        key = data[k_start:k_end].decode("ascii")
        d_start = data_table_start + data_off
        d_end = d_start + max_len
        if d_end > len(data):
            raise ValueError(f"Dados SFO truncados na chave '{key}'.")
        raw = data[d_start : d_start + data_len]
        if fmt == FMT_INT32:
            if len(raw) < 4:
                raise ValueError(f"Valor int32 truncado na chave '{key}'.")
            val: str | int = struct.unpack_from("<i", raw, 0)[0]
        elif fmt in (FMT_UTF8, FMT_UTF8_SPECIAL):
            val = raw.rstrip(b"\x00").decode("utf-8", "strict")
        else:
            raise ValueError(f"Formato desconhecido 0x{fmt:04x} na chave '{key}'.")
        values[key] = val
        keys_order.append(key)
        entries_meta[key] = {
            "fmt": fmt,
            "data_len": data_len,
            "max_len": max_len,
            "value": val,
        }

    return {
        "version": f"{version & 0xFF}.{(version >> 8) & 0xFF:02d}",
        "count": count,
        "keys": keys_order,
        "values": values,
        "entries": entries_meta,
    }
