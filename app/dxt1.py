"""DXT1a (BC1) encoder vectorised with numpy, plus the .dds container.

DXT1 guarda cada bloque de 4x4 pixeles en 8 bytes: dos colores RGB565 y
16 indices de 2 bits. El bloque tiene dos modos, y cual se usa se decide
comparando los dos colores como enteros sin signo:

    color0 >  color1  -> 4 colores opacos
                         p2 = (2*c0 + c1)/3 , p3 = (c0 + 2*c1)/3
    color0 <= color1  -> 3 colores + punchthrough  ("la 'a' de DXT1a")
                         p2 = (c0 + c1)/2   , p3 = transparente

O sea que DXT1a no es un formato aparte: es DXT1 usando el segundo modo en
los bloques que tienen pixeles transparentes. Por eso el FourCC sigue
siendo 'DXT1'.

El ajuste de color por bloque usa el eje principal (power iteration sobre
la covarianza de los 16 pixeles), que se porta bastante mejor que un
bounding box en degradados -- justo lo que abunda en un fondo de tema.
"""

from __future__ import annotations

import struct

import numpy as np

#: Below this alpha a pixel counts as transparent (punchthrough).
ALPHA_THRESHOLD = 128

_POWER_ITERATIONS = 8


def _quantize_565(colors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Packs 0..255 colours into RGB565 and also returns the colour
    que el decodificador va a reconstruir a partir de el.

    Devolver el color reconstruido importa: los indices se tienen que elegir
    contra la paleta real que vera la consola, no contra la ideal.
    """
    c = np.clip(colors, 0.0, 255.0)
    r5 = np.rint(c[..., 0] * (31.0 / 255.0)).astype(np.uint32)
    g6 = np.rint(c[..., 1] * (63.0 / 255.0)).astype(np.uint32)
    b5 = np.rint(c[..., 2] * (31.0 / 255.0)).astype(np.uint32)

    packed = (r5 << 11) | (g6 << 5) | b5

    # Expansion by replicating the high bits, the way the hardware does.
    dequant = np.empty(c.shape, dtype=np.float32)
    dequant[..., 0] = (r5 << 3) | (r5 >> 2)
    dequant[..., 1] = (g6 << 2) | (g6 >> 4)
    dequant[..., 2] = (b5 << 3) | (b5 >> 2)
    return packed.astype(np.uint32), dequant


def _principal_axis(centered: np.ndarray, weights: np.ndarray | None) -> np.ndarray:
    """Axis of greatest variance per block, via power iteration.

    `centered` es (N, 16, 3) ya restada la media; `weights` (N, 16) o None (cuando
    todo o quadro é opaco) pone en cero los pixeles transparentes.

    Preserves exact upstream `einsum` / `linalg.norm` arithmetic inside the tiny
    `(N, 3, 3)` power iteration so even degenerate blocks whose bounding-box
    diagonal aligns with an unstable minor eigenvector remain 100% byte-identical
    to upstream `Imxnxl/PS4-Ultimate-Theme-Creator`.
    """
    w = centered if weights is None else (centered * weights[..., None])
    cov = np.einsum("nij,nik->njk", w, w, optimize=True)  # (N, 3, 3)

    # Starting from the bounding box diagonal converges in a few rounds and
    # avoids the pathological case of an initial vector orthogonal to the axis.
    axis = w.max(axis=1) - w.min(axis=1)
    norm = np.linalg.norm(axis, axis=1, keepdims=True)
    fallback = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    axis = np.where(norm > 1e-6, axis / np.maximum(norm, 1e-9), fallback)

    for _ in range(_POWER_ITERATIONS):
        axis = np.einsum("nij,nj->ni", cov, axis, optimize=True)
        norm = np.linalg.norm(axis, axis=1, keepdims=True)
        # Flat colour block: covariance is zero and the axis does not matter.
        axis = np.where(norm > 1e-9, axis / np.maximum(norm, 1e-12), fallback)

    return axis.astype(np.float32)


def compress_dxt1a(rgba: np.ndarray) -> bytes:
    """Comprime una imagen RGBA (H, W, 4) uint8 a bloques DXT1a crudos."""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError(f"expected (H, W, 4) uint8, got {rgba.shape}")

    height, width = rgba.shape[:2]
    pad_h = (-height) % 4
    pad_w = (-width) % 4
    if pad_h or pad_w:
        # Replicating the edge instead of padding with black keeps the last
        # block from dragging the colour fit toward a colour not in the image.
        rgba = np.pad(rgba, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
        height, width = rgba.shape[:2]

    bh, bw = height // 4, width // 4
    blocks = (
        rgba.reshape(bh, 4, bw, 4, 4)
        .transpose(0, 2, 1, 3, 4)
        .reshape(-1, 16, 4)
    )
    n = blocks.shape[0]

    colors = blocks[..., :3].astype(np.float32)
    opaque = blocks[..., 3] >= ALPHA_THRESHOLD          # (N, 16)
    has_any_alpha = not bool(opaque.all())

    if not has_any_alpha:
        needs_alpha = np.zeros(n, dtype=bool)
        all_clear = needs_alpha
        mean = colors.mean(axis=1)
        centered = colors - mean[:, None, :]
        axis = _principal_axis(centered, None)
        proj = np.matmul(centered, axis[..., None])[..., 0]
        t_min = proj.min(axis=1)
        t_max = proj.max(axis=1)
    else:
        weights = opaque.astype(np.float32)
        opaque_count = weights.sum(axis=1)                   # (N,)
        needs_alpha = ~opaque.all(axis=1)
        all_clear = opaque_count == 0
        safe_count = np.maximum(opaque_count, 1.0)[:, None]
        mean = (colors * weights[..., None]).sum(axis=1) / safe_count   # (N, 3)
        centered = colors - mean[:, None, :]
        axis = _principal_axis(centered, weights)
        proj = np.matmul(centered, axis[..., None])[..., 0]    # (N, 16)
        big = np.float32(np.inf)
        t_min = np.where(opaque, proj, big).min(axis=1)
        t_max = np.where(opaque, proj, -big).max(axis=1)
        t_min = np.where(all_clear, 0.0, t_min)
        t_max = np.where(all_clear, 0.0, t_max)

    end_a = mean + t_max[:, None] * axis
    end_b = mean + t_min[:, None] * axis

    packed_a, color_a = _quantize_565(end_a)
    packed_b, color_b = _quantize_565(end_b)

    # Order the endpoints according to the mode each block needs.
    #   opaco        -> queremos packed0 >  packed1 (4 colores)
    #   punchthrough -> queremos packed0 <= packed1 (3 colores + alpha)
    want_greater = ~needs_alpha
    is_greater = packed_a > packed_b
    swap = np.where(want_greater, ~is_greater, is_greater)

    packed0 = np.where(swap, packed_b, packed_a)
    packed1 = np.where(swap, packed_a, packed_b)
    color0 = np.where(swap[:, None], color_b, color_a)
    color1 = np.where(swap[:, None], color_a, color_b)

    four_color = packed0 > packed1

    c2 = np.where(
        four_color[:, None],
        (2.0 * color0 + color1) / 3.0,
        (color0 + color1) / 2.0,
    ).astype(np.float32)
    c3 = np.where(four_color[:, None], (color0 + 2.0 * color1) / 3.0, 0.0).astype(np.float32)

    # Squared distance without allocating a huge 4D (N, 16, 4, 3) intermediate buffer.
    # Uses batched matmul (same inner-product FMA kernel as einsum(..., optimize=True))
    # so tie-breaking remains 100% byte-for-byte identical to the reference encoder.
    d0 = colors - color0[:, None, :]
    best_dist = np.matmul(d0[..., None, :], d0[..., :, None])[..., 0, 0]
    indices = np.zeros((n, 16), dtype=np.uint32)

    d1 = colors - color1[:, None, :]
    dist1 = np.matmul(d1[..., None, :], d1[..., :, None])[..., 0, 0]
    m1 = dist1 < best_dist
    best_dist = np.where(m1, dist1, best_dist)
    indices = np.where(m1, np.uint32(1), indices)

    d2 = colors - c2[:, None, :]
    dist2 = np.matmul(d2[..., None, :], d2[..., :, None])[..., 0, 0]
    m2 = dist2 < best_dist
    best_dist = np.where(m2, dist2, best_dist)
    indices = np.where(m2, np.uint32(2), indices)

    d3 = colors - c3[:, None, :]
    dist3 = np.matmul(d3[..., None, :], d3[..., :, None])[..., 0, 0]
    # In 3-colour mode index 3 means "transparent", so an opaque pixel must never pick it.
    m3 = four_color[:, None] & (dist3 < best_dist)
    indices = np.where(m3, np.uint32(3), indices)

    if has_any_alpha:
        indices = np.where(opaque, indices, np.uint32(3))
        if all_clear.any():
            indices[all_clear] = 3

    # Pack 16 x 2-bit indices into 4 little-endian bytes directly without (N, 16) shift reduction overhead.
    out = np.empty((n, 8), dtype=np.uint8)
    out[:, 0] = packed0 & 0xFF
    out[:, 1] = (packed0 >> 8) & 0xFF
    out[:, 2] = packed1 & 0xFF
    out[:, 3] = (packed1 >> 8) & 0xFF
    out[:, 4] = indices[:, 0] | (indices[:, 1] << 2) | (indices[:, 2] << 4) | (indices[:, 3] << 6)
    out[:, 5] = indices[:, 4] | (indices[:, 5] << 2) | (indices[:, 6] << 4) | (indices[:, 7] << 6)
    out[:, 6] = indices[:, 8] | (indices[:, 9] << 2) | (indices[:, 10] << 4) | (indices[:, 11] << 6)
    out[:, 7] = indices[:, 12] | (indices[:, 13] << 2) | (indices[:, 14] << 4) | (indices[:, 15] << 6)
    return out.tobytes()


# --- Contenedor .dds ------------------------------------------------------

_DDSD_CAPS = 0x1
_DDSD_HEIGHT = 0x2
_DDSD_WIDTH = 0x4
_DDSD_PIXELFORMAT = 0x1000
_DDSD_LINEARSIZE = 0x80000
_DDPF_FOURCC = 0x4
_DDSCAPS_TEXTURE = 0x1000


def dds_header(width: int, height: int, linear_size: int) -> bytes:
    """128-byte DDS header for a DXT1 texture without mipmaps."""
    flags = _DDSD_CAPS | _DDSD_HEIGHT | _DDSD_WIDTH | _DDSD_PIXELFORMAT | _DDSD_LINEARSIZE
    # 28 (7I) + 44 (reserved1) + 32 (pixelformat) + 16 (caps) + 4 = 124
    return b"DDS " + struct.pack(
        "<7I44xIII20xIIII4x",
        124,            # dwSize
        flags,
        height,
        width,
        linear_size,    # dwPitchOrLinearSize
        0,              # dwDepth
        0,              # dwMipMapCount (sin flag DDSD_MIPMAPCOUNT => sin mipmaps)
        32,             # DDS_PIXELFORMAT.dwSize
        _DDPF_FOURCC,
        # dwFourCC is split out so it reads as 4 chars, not an integer:
        *struct.unpack("<I", b"DXT1"),
        _DDSCAPS_TEXTURE,
        0, 0, 0,        # dwCaps2..4
    )


def encode_dds(rgba: np.ndarray) -> bytes:
    """Imagen RGBA -> archivo .dds completo (DXT1a, sin mipmaps)."""
    height, width = rgba.shape[:2]
    payload = compress_dxt1a(rgba)
    return dds_header(width, height, len(payload)) + payload


def decode_dds(data: bytes) -> np.ndarray:
    """Decodes a DXT1/DXT1a .dds to RGBA. Used to verify the encoder."""
    if data[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    fourcc = data[84:88]
    if fourcc != b"DXT1":
        raise ValueError(f"expected DXT1, got {fourcc!r}")

    bh, bw = (height + 3) // 4, (width + 3) // 4
    raw = np.frombuffer(data, dtype=np.uint8, count=bh * bw * 8, offset=128)
    blocks = raw.reshape(-1, 8)

    p0 = blocks[:, 0].astype(np.uint32) | (blocks[:, 1].astype(np.uint32) << 8)
    p1 = blocks[:, 2].astype(np.uint32) | (blocks[:, 3].astype(np.uint32) << 8)
    bits = (
        blocks[:, 4].astype(np.uint32)
        | (blocks[:, 5].astype(np.uint32) << 8)
        | (blocks[:, 6].astype(np.uint32) << 16)
        | (blocks[:, 7].astype(np.uint32) << 24)
    )

    def unpack(p: np.ndarray) -> np.ndarray:
        r5, g6, b5 = (p >> 11) & 0x1F, (p >> 5) & 0x3F, p & 0x1F
        return np.stack(
            [(r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)],
            axis=-1,
        ).astype(np.float32)

    c0, c1 = unpack(p0), unpack(p1)
    four = (p0 > p1)[:, None]

    n = blocks.shape[0]
    pal = np.empty((n, 4, 4), dtype=np.float32)
    pal[:, 0, :3], pal[:, 1, :3] = c0, c1
    pal[:, 2, :3] = np.where(four, (2 * c0 + c1) / 3.0, (c0 + c1) / 2.0)
    pal[:, 3, :3] = np.where(four, (c0 + 2 * c1) / 3.0, 0.0)
    pal[:, :, 3] = 255.0
    pal[:, 3, 3] = np.where(four[:, 0], 255.0, 0.0)

    idx = (bits[:, None] >> (2 * np.arange(16, dtype=np.uint32))[None, :]) & 0x3
    px = np.take_along_axis(pal, idx[:, :, None], axis=1)  # (N, 16, 4)

    out = (
        np.rint(px)
        .astype(np.uint8)
        .reshape(bh, bw, 4, 4, 4)
        .transpose(0, 2, 1, 3, 4)
        .reshape(bh * 4, bw * 4, 4)
    )
    return out[:height, :width]
