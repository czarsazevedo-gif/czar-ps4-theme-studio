"""Generador del theme.xml -- el descriptor que la PS4 lee al aplicar el tema.

El esquema esta sacado del theme.xml de un tema que funciona en consola, que
ademas trae los comentarios de documentacion de Sony dentro. De ahi salen las
dos reglas que importan:

* `format-version` y `themecolor` son OBLIGATORIOS.
* El resto es opcional; si se omite, la consola usa su valor por defecto.

Sin este archivo el tema se instala pero la consola se cae al aplicarlo.
"""

from __future__ import annotations

import re

from .config import L

#: Format version. 4.0 is the reference theme's; higher versions make the
#: system demand more files.
FORMAT_VERSION = "4.0"

#: The 8 system colours, exactly as Sony documents them inside the xml.
THEME_COLORS = [
    (0, L("blue (default)", "azul (por defecto)", "Azul (padrão)")),
    (1, L("pink", "rosa", "Rosa")),
    (2, L("red", "rojo", "Vermelho")),
    (3, L("navy", "azul marino", "Azul-marinho")),
    (4, L("dark grey", "gris oscuro", "Cinza-escuro")),
    (5, L("gold", "dorado", "Dourado")),
    (6, L("light steel blue", "azul acero claro", "Azul-aço claro")),
    (7, L("purple", "morado", "Roxo")),
]

ARGB_RE = re.compile(r"^#[0-9A-Fa-f]{8}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}$")

DEFAULTS = {
    "themecolor": 0,
    "fontcolor": "#FFFFFFFF",
    "fontshadowcolor": "#FF000000",
    "focuscolor": "#FF1E9BFF",
    "homescreen_dimmer": "#00FFFFFF",
    "functionscreen_dimmer": "#00FFFFFF",
    "titlename_dimmer": "#00FFFFFF",
    "homebgm_enable": False,
}


class ThemeXmlError(ValueError):
    """Ajuste invalido del theme.xml."""


def sanitize_label(value: str) -> str:
    """El label solo admite [a-zA-Z0-9_-] y hasta 64 caracteres.

    No se muestra en pantalla (solo sale en el log de la consola), asi que se
    limpia en silencio en vez de rechazar el titulo del usuario.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", (value or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:64] or "custom-theme"


def _check_argb(value: str, field: str) -> str:
    if not ARGB_RE.match(value or ""):
        raise ThemeXmlError(
            f"{field}: se espera un color ARGB de 8 digitos como #FF1E9BFF, llego '{value}'."
        )
    return value.upper()


def build_theme_xml(
    *,
    label: str,
    themecolor: int = DEFAULTS["themecolor"],
    fontcolor: str = DEFAULTS["fontcolor"],
    fontshadowcolor: str = DEFAULTS["fontshadowcolor"],
    focuscolor: str = DEFAULTS["focuscolor"],
    homescreen_dimmer: str = DEFAULTS["homescreen_dimmer"],
    functionscreen_dimmer: str = DEFAULTS["functionscreen_dimmer"],
    titlename_dimmer: str = DEFAULTS["titlename_dimmer"],
    homebgm_enable: bool = DEFAULTS["homebgm_enable"],
) -> str:
    """Arma el theme.xml.

    `homebgm_enable` en True sin un sound/bgm_home.at9 dentro hace que la
    consola ponga su musica por defecto, asi que por defecto va en False.
    """
    try:
        color = int(themecolor)
    except (TypeError, ValueError) as exc:
        raise ThemeXmlError(f"themecolor must be a number 0-7, got '{themecolor}'.") from exc
    if not 0 <= color <= 7:
        raise ThemeXmlError(f"themecolor must be between 0 and 7, got {color}.")

    rows = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<theme>",
        f"    <format-version>{FORMAT_VERSION}</format-version>",
        f"    <themecolor>{color}</themecolor>",
        f"    <label>{sanitize_label(label)}</label>",
        f'    <fontcolor>{_check_argb(fontcolor, "fontcolor")}</fontcolor>',
        f'    <fontshadowcolor>{_check_argb(fontshadowcolor, "fontshadowcolor")}</fontshadowcolor>',
        f'    <focuscolor>{_check_argb(focuscolor, "focuscolor")}</focuscolor>',
        f'    <homescreen-dimmer>{_check_argb(homescreen_dimmer, "homescreen-dimmer")}</homescreen-dimmer>',
        f'    <functionscreen-dimmer>{_check_argb(functionscreen_dimmer, "functionscreen-dimmer")}</functionscreen-dimmer>',
        f'    <titlename-dimmer>{_check_argb(titlename_dimmer, "titlename-dimmer")}</titlename-dimmer>',
        f'    <homebgm-enable>{"True" if homebgm_enable else "False"}</homebgm-enable>',
        "</theme>",
        "",
    ]
    return "\n".join(rows)
