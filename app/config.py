"""Shared constants.

Everything the PS4 imposes as a fixed format lives here, so there are no magic
numbers scattered around the code.
"""

from __future__ import annotations

from pathlib import Path

# --- Resolutions the PS4 requires -----------------------------------------
STATIC_SIZE = (1920, 1080)   # background.dds
ANIMATED_SIZE = (1280, 720)  # resolucion por defecto del fondo animado
ICON_SIZE = (128, 128)       # icono de funcion (.png)

#: Resolutions offered for the animated background. Since the limit is in
#: bytes, dropping the resolution buys considerably longer animations.
#: 960x540 is what the reference theme uses for its animated layers.
ANIMATED_SIZES = [(1280, 720), (960, 540), (640, 360)]

# --- Hard limit on the background.raf folder ------------------------------
# If the .dds files add up to more than this, the console applies the theme
# and throws "data corrupted" (CE-38196-7).
#
# MEASURED ON A CONSOLE, not taken from a tutorial. The spec going around says
# 16 MB and it is false: with real packages, 5 531 136 B (12 frames at 720p)
# applies and 6 452 992 B (14 frames) does not. The only round value in that
# range is 6 MiB, which also fits 24 frames at 640x360 (2.7 MB) working.
#
# The limit is on BYTES, not on frame count: lowering the resolution fits many
# more. That is why ANIMATED_SIZES offers several.
RAF_SIZE_LIMIT = 6 * 1024 * 1024  # 6 MiB

#: On top of the weight there is a separate cap on the NUMBER of frames.
#: It shows up comparing two packages that are nearly identical in bytes:
#:   24 frames at 960x540 = 6 223 872 B  applies
#:   54 frames at 640x360 = 6 227 712 B  throws CE-38196-7
#: The real cut is between 48 (tested, works) and 54 (fails). It stays at 48
#: because that is the last value verified on a console; narrowing it further
#: buys nothing. Probably a scene limit (one material and one actor per frame)
#: rather than a file one.
MAX_FRAME_COUNT = 48

# --- Animation timing -----------------------------------------------------
# The <case> wait attribute is in SECONDS and takes decimals. Confirmed by
# reading the scene of a theme that works: it uses wait="0.06" (~16 fps) for
# the rain and wait="0.3" for the camera moves.
# Careful: tutorials that read wait="2" as "2 units of 100 ms" are wrong;
# there wait="2" would be 2 seconds per frame.
DEFAULT_WAIT_SECONDS = 0.1
MIN_WAIT_SECONDS = 0.02
MAX_WAIT_SECONDS = 5.0

# --- Paths ----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "workspace"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Destination folder inside the theme, exactly as the PS4 expects it.
RAF_DIR_NAME = "background.raf"
RAF_PARENT = "scene"

# --- Packaging (.pkg) -----------------------------------------------------

def _find_pkg_tools() -> Path:
    """Locates the packaging tools.

    `tools/` only holds the binaries that are actually needed (verified: the
    package that comes out is byte-for-byte identical to the one from the full
    distribution). Keeping the original folder around works too.
    """
    candidates = (
        PROJECT_ROOT / "tools",
        PROJECT_ROOT / "PS4_Fake_PKG_Tools_3.87_V7",
    )
    for candidate in candidates:
        if (candidate / "orbis-pub-cmd.exe").is_file():
            return candidate
    return candidates[0]


PKG_TOOLS_DIR = _find_pkg_tools()

# Icon the PS4 shows in the menu. It is 512x512 and lives in sce_sys; not to
# be confused with the 128x128 function icon, which goes inside the theme.
SCE_ICON_SIZE = (512, 512)
PIC1_SIZE = (1920, 1080)

#: What makes the console treat the package as a THEME rather than an app is
#: the pair (volume_type=pkg_ps4_theme, IRO_TAG=2) in param.sfo.
#: Without IRO_TAG the package installs as an app, shows a "Start" button and
#: throws CE-37695-0 on open, because the PS4 looks for an eboot.bin that does
#: not exist. The value 2 came from sweeping the full matrix against
#: img_create: it is the only one the validator accepts with the theme volume.
PKG_MODES = {
    "theme": {
        "label": "Theme",
        "volume_type": "pkg_ps4_theme",
        "iro_tag": 2,
        # The tool itself skips keystone for themes: there is no save data.
        "keystone": False,
        "extra_package_attrs": {},
        # Themes carry no PlayGo chunks.
        "chunk_info": False,
        # A theme can ONLY be 'ac'. With 'gd' it installs and even shows up in
        # the list, but the console crashes when applying it (CE-36329-3). Not
        # a preference: it is the only one that works, hence no selector.
        "categories": ["ac"],
    },
    "app": {
        "label": "Application",
        "volume_type": "pkg_ps4_app",
        "iro_tag": None,
        "keystone": True,
        "extra_package_attrs": {"storage_type": "digital50", "app_type": "full"},
        "chunk_info": True,
    },
}

#: Categories orbis-pub-cmd accepts; anything else gives
#: "unsupported app_category". Careful: 'gdt' is NOT on the list, whatever the
#: tutorials say. Which one is usable depends on the mode: see
#: PKG_MODES[...]["categories"], which is what validation actually checks.
PKG_CATEGORIES = [
    "gd", "gde", "gdc", "gdk", "gdl", "gdn", "gdo",
    "gp", "gpe", "gpc", "gpk", "gpl", "gpn", "gpo",
    "ac", "md",
]

#: A theme is "additional content", not an application. Confirmed by reading
#: the param.sfo of a Sony retail theme (Call of Duty AW, c_date=20141010):
#: CATEGORY='ac' + IRO_TAG=2, with no APP_VER and no APP_TYPE.
#: With CATEGORY='gd' the theme shows up in Themes but the console crashes
#: when applying it (CE-36329-3).
PKG_DEFAULTS = {
    "mode": "theme",
    "category": "ac",
    "passcode": "0" * 32,
    "content_prefix": "UP9000",
    "title_id": "CUSA00000",
}

# --- Canonical theme layout -----------------------------------------------
# Taken from the orbis-pub-cmd validator and from a real extracted theme.
# Not configurable: the console looks for these exact names.
THEME_PATHS = {
    "homescreen": "texture/background/homescreen.png",
    "functionscreen": "texture/background/functionscreen.png",
    "preview": "texture/preview.png",
    "theme_xml": "theme.xml",
    "function_icon_dir": "texture/function_icon",
    "raf_dir": RAF_PARENT + "/" + RAF_DIR_NAME,
}

#: Thumbnail shown in the theme list.
PREVIEW_SIZE = (740, 416)

#: Function icons are 128x128 and their glow variant 152x152.
FUNCTION_ICON_SIZE = ICON_SIZE
FUNCTION_ICON_GLOW_SIZE = (152, 152)

PKG_FIXED_PATHS = {
    "sce_icon": "sce_sys/icon0.png",
    "pic1": "sce_sys/pic1.png",
}

def L(en: str, es: str, pt_br: str) -> dict:
    """A label in both languages.

    The interface picks by the active language; the key is always present so a
    missing translation shows up as a KeyError here, not as a blank in the UI.
    """
    return {"en": en, "es": es, "pt-BR": pt_br}


#: What each icon is called on the console. The key is the FILE name, which is
#: NOT configurable: the PS4 looks for those literals. The interface shows the
#: label with the file name underneath, because knowing which one you are
#: generating stays useful when debugging.
ICON_LABELS = {
    # function
    "community": L("Communities", "Comunidades", "Comunidades"),
    "event": L("Events", "Eventos", "Eventos"),
    "friend": L("Friends", "Amigos", "Amigos"),
    "message": L("Messages", "Mensajes", "Mensagens"),
    "notification": L("Notifications", "Notificaciones", "Notificações"),
    "party": L("Party", "Grupo", "Grupo"),
    "power": L("Power", "Encendido", "Energia"),
    "setting": L("Settings", "Ajustes", "Configurações"),
    "trophy": L("Trophies", "Trofeos", "Troféus"),
    # content
    "browser": L("Browser", "Navegador", "Navegador"),
    "disc": L("Disc", "Disco", "Disco"),
    "discoverlay": L("Disc (overlay)", "Disco (superpuesto)", "Sobreposição do disco"),
    "folder": L("Folder", "Carpeta", "Pasta"),
    "gallery": L("Capture Gallery", "Galeria de captura", "Galeria de capturas"),
    "library": L("Library", "Biblioteca", "Biblioteca"),
    "livefromps": L("Live from PlayStation", "Live from PlayStation", "Ao vivo do PlayStation"),
    "shareplay": L("Share Play", "Share Play", "Share Play"),
    "tvvideo": L("TV & Video", "TV y video", "TV e vídeo"),
    "usbmusic": L("USB Music", "Musica USB", "Música USB"),
}

#: Names the console expects in texture/function_icon/.
FUNCTION_ICONS = [
    "community", "event", "friend", "message", "notification",
    "party", "power", "setting", "trophy",
]

#: Content icons, in texture/content_icon/. They are 512x512.
CONTENT_ICONS = [
    "browser", "disc", "discoverlay", "folder", "gallery",
    "library", "livefromps", "shareplay", "tvvideo", "usbmusic",
]
CONTENT_ICON_SIZE = (512, 512)


def _build_slots() -> dict:
    """Cada hueco personalizable del tema: a donde va, a que tamano y de que
    otro hueco copia si lo dejas vacio.

    `fallback` es lo que hace que subir una sola imagen siga funcionando: el
    resto de fondos se derivan de ella salvo que les pongas la suya.
    """
    slots: dict[str, dict] = {
        "home": {
            "label": L("Home background", "Fondo de la home", "Fundo da tela inicial"),
            "path": THEME_PATHS["homescreen"],
            "size": STATIC_SIZE,
            "group": "background",
            "required": True,
            # Still required even with an animated background: the thumbnail,
            # the icon and the store pic1 come from here. That is why it gets
            # renamed instead of disappearing like the top-menu one.
            "hidden_by_raf": True,
            "raf_label": L("Theme image", "Imagen del tema", "Imagem do tema"),
        },
        "function": {
            "label": L("Top menu background", "Fondo del menú superior", "Fundo do menu superior"),
            "path": THEME_PATHS["functionscreen"],
            "size": STATIC_SIZE,
            "group": "background",
            "fallback": "home",
            "hidden_by_raf": True,
        },
        "preview": {
            "label": L("Theme thumbnail", "Miniatura del tema", "Miniatura do tema"),
            "path": THEME_PATHS["preview"],
            "size": PREVIEW_SIZE,
            "group": "background",
            "fallback": "home",
        },
        "icon0": {
            "label": L("Content icon", "Icono del contenido", "Ícone de conteúdo"),
            "path": PKG_FIXED_PATHS["sce_icon"],
            "size": SCE_ICON_SIZE,
            "group": "background",
            "fallback": "home",
        },
    }
    for name in FUNCTION_ICONS:
        slots[f"fn:{name}"] = {
            "label": ICON_LABELS.get(name, name),
            "file": name,
            "path": f"{THEME_PATHS['function_icon_dir']}/{name}.png",
            "size": FUNCTION_ICON_SIZE,
            "group": "function_icon",
            # The glow variant comes from the same image, just bigger.
            "glow_path": f"{THEME_PATHS['function_icon_dir']}/{name}_glow.png",
            "glow_size": FUNCTION_ICON_GLOW_SIZE,
        }
    for name in CONTENT_ICONS:
        slots[f"ct:{name}"] = {
            "label": ICON_LABELS.get(name, name),
            "file": name,
            "path": f"texture/content_icon/{name}.png",
            "size": CONTENT_ICON_SIZE,
            "group": "content_icon",
        }
    return slots


IMAGE_SLOTS = _build_slots()

SLOT_GROUPS = [
    {"id": "background", "label": L("Backgrounds", "Fondos", "Fundos")},
    {"id": "function_icon", "label": L("Function icons", "Iconos de función", "Ícones de função")},
    {"id": "content_icon", "label": L("Content icons", "Iconos de contenido", "Ícones de conteúdo")},
]

# --- Audio ----------------------------------------------------------------
# Menu music is ATRAC9 (.at9) and the effects are Sony ADPCM (.vag). Both are
# proprietary: ffmpeg decodes ATRAC9 but does not encode it, and the official
# encoder lives in a 32-bit libatrac9.dll with no public API. Music is
# therefore accepted already converted (or run through at9tool when present),
# while the effects are generated here.
#: Music has to arrive as .at9; the effects are converted here.
AUDIO_SLOTS = {
    "bgm": {
        "label": L("Menu music", "Música del menú", "Música do menu"),
        "path": "sound/bgm_home.at9",
        "codec": "atrac9",
        "rate": 48000,
        "convert": False,
        "exts": {".at9"},
    },
    "se_decide": {
        "label": L("Confirm", "Aceptar", "Confirmar"),
        "path": "sound/se_decide.vag",
        "rate": 48000,
        "convert": True,
        "exts": None,          # cualquier audio que lea ffmpeg
    },
    "se_cursor": {
        "label": L("Move cursor", "Mover el cursor", "Mover cursor"),
        "path": "sound/se_cursor.vag",
        "rate": 48000,
        "convert": True,
        "exts": None,
    },
    "se_cancel": {
        "label": L("Cancel", "Cancelar", "Cancelar"),
        "path": "sound/se_cancel.vag",
        "rate": 48000,
        "convert": True,
        "exts": None,
    },
}

#: Effects are converted to VAG, so almost anything goes in.
SOUND_EXTS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma", ".at9", ".vag"}
AUDIO_EXTS = {".at9"}

#: A system effect lasts an instant; trimming stops a whole song from ending
#: up in an effect slot by accident.
MAX_SFX_SECONDS = 5.0



# How many old jobs to keep before pruning the workspace.
# A single theme can use 23 image slots plus the animated background, and all
# of them have to still be alive when Package is pressed: if pruning takes one
# mid-session the build fails with a 404. Hence the headroom.
MAX_JOBS_KEPT = max(150, len(IMAGE_SLOTS) * 5)

# Quality of the webp sent to the browser for the preview.
PREVIEW_WEBP_QUALITY = 82

# Extensions accepted by each screen.
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif", ".apng", ".wmv", ".flv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".dds"}
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def dds_bytes_for(width: int, height: int) -> int:
    """Exact size of a DXT1 .dds without mipmaps.

    DXT1 is fixed-rate: every 4x4 pixel block takes 8 bytes, so the size does
    not depend on the image content. That is what lets the interface show the
    real weight before compressing anything.
    """
    blocks = ((width + 3) // 4) * ((height + 3) // 4)
    return 128 + blocks * 8  # 128 = DDS header


#: Weight of one animated background frame (1280x720) -> 460_928 bytes.
ANIMATED_FRAME_BYTES = dds_bytes_for(*ANIMATED_SIZE)


def max_frames_for(size: tuple[int, int]) -> int:
    """How many frames of that resolution the console accepts.

    The stricter of the two limits wins: the weight (DXT1 is fixed-rate, so it
    comes out exact) and the frame-count cap.
    """
    return min(RAF_SIZE_LIMIT // dds_bytes_for(*size), MAX_FRAME_COUNT)


#: Cap at the default resolution.
MAX_FRAMES = max_frames_for(ANIMATED_SIZE)
