"""Shared PSF container for the metadata defined by the upstream SFX builder."""
import struct
import xml.etree.ElementTree as ET

from ..pkg import PackageMeta, build_param_sfx

HEADER = struct.Struct("<4s4I")
ENTRY = struct.Struct("<HHIII")
MAGIC = b"\x00PSF"
VERSION = 0x101
UTF8 = 0x0204
INTEGER = 0x0404


def encode(meta: PackageMeta) -> bytes:
    items = ET.fromstring(build_param_sfx(meta))
    keys, data, entries = bytearray(), bytearray(), bytearray()
    for item in items:
        if item.get("fmt") == "int32":
            kind, value, maximum = INTEGER, struct.pack("<I", int(item.text)), 4
        else:
            kind = UTF8
            value = (item.text or "").encode("utf-8") + b"\0"
            maximum = int(item.get("max_len"))
            if len(value) > maximum:
                raise ValueError("O título é longo demais em UTF-8 para param.sfo.")
        entries += ENTRY.pack(len(keys), kind, len(value), maximum, len(data))
        keys += item.attrib["key"].encode("ascii") + b"\0"
        data += value.ljust(maximum, b"\0")
    keys += b"\0" * (-len(keys) % 4)
    key_base = HEADER.size + len(entries)
    data_base = key_base + len(keys)
    return HEADER.pack(MAGIC, VERSION, key_base, data_base, len(items)) + entries + keys + data


def parse(blob: bytes) -> dict:
    magic, version, keys, data, count = HEADER.unpack_from(blob)
    if magic != MAGIC or version != VERSION or not HEADER.size + count * ENTRY.size <= keys <= data <= len(blob):
        raise ValueError("Cabeçalho PSF inválido.")
    result = {}
    for i in range(count):
        key_offset, kind, length, maximum, offset = ENTRY.unpack_from(blob, HEADER.size + i * ENTRY.size)
        start = keys + key_offset
        stop = blob.index(b"\0", start, data)
        key = blob[start:stop].decode("ascii")
        if not key or key in result or offset % 4 or not 0 < length <= maximum or data + offset + maximum > len(blob):
            raise ValueError("Entrada PSF inválida.")
        raw = blob[data + offset:data + offset + length]
        if kind == UTF8 and raw.endswith(b"\0") and b"\0" not in raw[:-1]:
            result[key] = raw[:-1].decode("utf-8")
        elif kind == INTEGER and length == maximum == 4:
            result[key] = struct.unpack("<I", raw)[0]
        else:
            raise ValueError("Tipo PSF inválido.")
    return result
