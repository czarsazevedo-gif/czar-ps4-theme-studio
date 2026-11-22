"""Deterministic, order-independent PT-BR / EN / ES PS4 icon filename mapper.

Maps user-supplied icon filenames (with accents, uppercase, mixed camelCase,
suffixes like 'BrancoOut_refinado', or '_glow' variants) onto canonical PS4
Studio slots (`fn:*` and `ct:*`), rejecting ambiguous duplicate targets
explicitly instead of silently overwriting based on input order.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Sequence

from .config import CONTENT_ICONS, FUNCTION_ICONS, IMAGE_SLOTS

# Ordered from most specific (compound phrases) to general aliases.
# Every token is matched against the accent-stripped, ASCII-lowercased filename stem.
_SLOT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Compound / overlapping content icons FIRST (e.g. "SobreposicaoDeDisco" before "Disco")
    (
        "ct:discoverlay",
        (
            "discoverlay",
            "disc_overlay",
            "discoverlay",
            "sobreposicaodedisco",
            "sobreposicaododisco",
            "sobreposicao_de_disco",
            "sobreposicao_do_disco",
            "sobreposicao_disco",
            "sobreposicaodisco",
            "discosuperpuesto",
            "disco_superpuesto",
        ),
    ),
    (
        "ct:livefromps",
        (
            "livefromps",
            "live_from_ps",
            "livefromplaystation",
            "live_from_playstation",
            "aovivodaplaystation",
            "aovivodoplaystation",
            "ao_vivo_da_playstation",
            "ao_vivo_do_playstation",
            "aovivops",
            "ao_vivo",
            "aovivo",
        ),
    ),
    (
        "ct:gallery",
        (
            "gallery",
            "capturegallery",
            "capture_gallery",
            "galeriadecapturas",
            "galeria_de_capturas",
            "galeriadecaptura",
            "galeria_de_captura",
            "galeria",
        ),
    ),
    (
        "ct:usbmusic",
        (
            "usbmusic",
            "usb_music",
            "musicausb",
            "musica_usb",
            "musicusb",
            "usb_musica",
        ),
    ),
    (
        "ct:shareplay",
        (
            "shareplay",
            "share_play",
        ),
    ),
    (
        "ct:tvvideo",
        (
            "tvvideo",
            "tv_video",
            "tvevideo",
            "tv_e_video",
            "tvyvideo",
            "tv_y_video",
            "tvandvideo",
            "tv_and_video",
        ),
    ),
    (
        "ct:library",
        (
            "library",
            "biblioteca",
        ),
    ),
    (
        "ct:browser",
        (
            "browser",
            "navegador",
            "webbrowser",
            "internetbrowser",
        ),
    ),
    (
        "ct:folder",
        (
            "folder",
            "pasta",
            "carpeta",
        ),
    ),
    (
        "ct:disc",
        (
            "disc",
            "disco",
        ),
    ),
    # Function icons (9 canonical slots, each supports base 128x128 + glow 152x152)
    (
        "fn:community",
        (
            "community",
            "communities",
            "comunidade",
            "comunidades",
        ),
    ),
    (
        "fn:notification",
        (
            "notification",
            "notifications",
            "notificacao",
            "notificacoes",
            "notificacion",
            "notificaciones",
        ),
    ),
    (
        "fn:setting",
        (
            "setting",
            "settings",
            "configuracao",
            "configuracoes",
            "config",
            "ajustes",
        ),
    ),
    (
        "fn:message",
        (
            "message",
            "messages",
            "mensagem",
            "mensagens",
            "mensaje",
            "mensajes",
        ),
    ),
    (
        "fn:friend",
        (
            "friend",
            "friends",
            "amigo",
            "amigos",
        ),
    ),
    (
        "fn:event",
        (
            "event",
            "events",
            "evento",
            "eventos",
        ),
    ),
    (
        "fn:party",
        (
            "party",
            "grupo",
            "grupos",
        ),
    ),
    (
        "fn:power",
        (
            "power",
            "energia",
            "encendido",
        ),
    ),
    (
        "fn:trophy",
        (
            "trophy",
            "trophies",
            "trofeu",
            "trofeus",
            "trofeo",
            "trofeos",
        ),
    ),
)

_GLOW_RE = re.compile(r"(?:^|[_\-\s])glow(?:$|[_\-\s])|glow$", re.IGNORECASE)


def _strip_accents_ascii(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _normalize_stem(filename: str) -> tuple[str, str, bool]:
    """Return (compact_alnum, snake_tokens, is_glow) from a raw filename."""
    stem = Path(filename.replace("\\", "/")).stem
    # Split camelCase before lowercasing so tokens like "SobreposicaoDeDiscoGlow" separate cleanly
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", _strip_accents_ascii(stem))
    lower = spaced.lower()
    is_glow = bool(_GLOW_RE.search(lower))
    if is_glow:
        lower = _GLOW_RE.sub("_", lower)
    snake = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")
    compact = re.sub(r"[^a-z0-9]+", "", lower)
    return compact, snake, is_glow


def resolve_icon_filename(filename: str) -> tuple[str | None, bool]:
    """Resolve a single icon filename into (slot_id, is_glow). Returns (None, False) if unmatched."""
    compact, snake, is_glow = _normalize_stem(filename)
    if not compact:
        return None, False

    tokens = set(snake.split("_")) if snake else set()
    for slot_id, aliases in _SLOT_RULES:
        for alias in aliases:
            alias_compact = alias.replace("_", "")
            # Exact stem match, token match, prefix match, or compound substring match
            if (
                compact == alias_compact
                or alias in tokens
                or compact.startswith(alias_compact)
                or (len(alias_compact) >= 5 and alias_compact in compact)
                or f"_{alias}_" in f"_{snake}_"
            ):
                # Only function icons have canonical _glow slots
                effective_glow = is_glow and slot_id.startswith("fn:")
                return slot_id, effective_glow
    return None, False


def map_icon_filenames(filenames: Sequence[str]) -> dict:
    """Deterministically map a collection of icon filenames onto PS4 theme slots.

    Order-independent: sorts inputs internally before conflict checking so reversing
    or permuting the list always yields the exact same `mapped`, `mapped_glow`,
    `conflicts`, and `unmatched` results. Ambiguous targets (2+ files resolving to
    the same slot and kind) are excluded from `mapped`/`mapped_glow` and reported
    explicitly in `conflicts`.
    """
    ordered = sorted(str(f) for f in filenames)
    buckets: dict[tuple[str, str], list[str]] = {}
    unmatched: list[str] = []
    entries: list[dict] = []

    for fname in ordered:
        slot_id, is_glow = resolve_icon_filename(fname)
        if slot_id is None:
            unmatched.append(fname)
            entries.append({"filename": fname, "slot": None, "kind": None, "status": "unmatched"})
            continue
        kind = "glow" if is_glow else "base"
        buckets.setdefault((slot_id, kind), []).append(fname)
        entries.append(
            {
                "filename": fname,
                "slot": slot_id,
                "kind": kind,
                "target_path": IMAGE_SLOTS[slot_id]["glow_path"] if is_glow else IMAGE_SLOTS[slot_id]["path"],
                "status": "candidate",
            }
        )

    mapped: dict[str, str] = {}
    mapped_glow: dict[str, str] = {}
    conflicts: list[dict] = []
    conflicted_keys: set[tuple[str, str]] = set()

    for (slot_id, kind), files in sorted(buckets.items()):
        if len(files) > 1:
            conflicted_keys.add((slot_id, kind))
            conflicts.append(
                {
                    "slot": slot_id,
                    "kind": kind,
                    "files": sorted(files),
                    "reason": (
                        f"Ambiguidade: {len(files)} arquivos disputam o mesmo slot "
                        f"{slot_id} ({kind}): {', '.join(sorted(files))}."
                    ),
                }
            )
        else:
            if kind == "glow":
                mapped_glow[slot_id] = files[0]
            else:
                mapped[slot_id] = files[0]

    for ent in entries:
        if ent["slot"] is not None:
            ent["status"] = "conflict" if (ent["slot"], ent["kind"]) in conflicted_keys else "mapped"

    return {
        "mapped": mapped,
        "mapped_glow": mapped_glow,
        "conflicts": conflicts,
        "unmatched": sorted(unmatched),
        "entries": entries,
        "total_supported_slots": len(FUNCTION_ICONS) + len(CONTENT_ICONS),
    }
