"""Packaging to .pkg with the PS4 Fake PKG Tools.

Everything goes through `orbis-pub-cmd.exe`, the command-line build, so no GUI
is needed. The chain is:

    sfo_create      -> sce_sys/param.sfo
    ks_create       -> sce_sys/keystone   (application mode only)
    (the gp4 is generated here, it is plain XML)
    img_create      -> the .pkg

What decides whether the PS4 sees a THEME or an APPLICATION is two things
together: the `pkg_ps4_theme` volume and `IRO_TAG = 2` in param.sfo. Without
IRO_TAG the package installs as an application, shows up with a "Start" button
and throws CE-37695-0 on open, because the console looks for an eboot.bin that
a theme does not have.

The value 2 came from sweeping the whole matrix (the 16 valid categories x
several IRO_TAG values) against img_create, and was then confirmed by reading
the param.sfo of a Sony retail theme: CATEGORY='ac' + IRO_TAG=2.

The category has to be 'ac' (additional content), not 'gd'. With 'gd' the
theme does show up in the Themes list but the console crashes when applying it
with CE-36329-3.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import PKG_CATEGORIES, PKG_DEFAULTS, PKG_MODES, PKG_TOOLS_DIR
from .media import NO_WINDOW

ORBIS_CMD = PKG_TOOLS_DIR / "orbis-pub-cmd.exe"

#: img_create can take a while with many frames, though in practice it is seconds.
BUILD_TIMEOUT = 30 * 60

CONTENT_ID_RE = re.compile(r"^[A-Z0-9]{6}-[A-Z0-9]{9}_00-[A-Z0-9]{16}$")
TITLE_ID_RE = re.compile(r"^[A-Z]{4}[0-9]{5}$")
PASSCODE_RE = re.compile(r"^[A-Za-z0-9]{32}$")


class PackageError(RuntimeError):
    """Packaging failure, carrying the useful part of the tool's output."""


def tools_available() -> bool:
    return ORBIS_CMD.is_file()


def calc_loudness(path) -> float | None:
    """Loudness in LKFS of a .wav or .at9, or None when it cannot be measured.

    The PS4 uses it to level the theme's music; the reference theme carries it
    in its PUBTOOLINFO as `bgmhome_loud`.
    For .at9 files ext/libatrac9.dll must sit next to the exe.
    """
    if not tools_available():
        return None
    try:
        proc = subprocess.run(
            [str(ORBIS_CMD), "file_calc_loudness", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None
    match = re.search(r"(-?\d+\.\d+)\s*LKFS", proc.stdout.decode("utf-8", "replace"))
    return float(match.group(1)) if match else None


@dataclass
class BuildLog:
    """Record of the commands that ran, to show them in the UI."""

    steps: list[dict] = field(default_factory=list)

    def add(self, label: str, cmd: list[str], out: str, seconds: float) -> None:
        self.steps.append(
            {
                "label": label,
                # The first item is the exe's full path; noise on screen.
                "cmd": " ".join([Path(cmd[0]).name, *cmd[1:]]),
                "output": out.strip(),
                "seconds": round(seconds, 2),
            }
        )


def _run(label: str, args: list[str], log: BuildLog, timeout: int = 300) -> str:
    cmd = [str(ORBIS_CMD), *args]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise PackageError(f"{label}: the tool hung (more than {timeout}s).") from exc

    out = proc.stdout.decode("utf-8", "replace")
    # The tool's banner repeats on every call and adds nothing.
    out = "\n".join(
        line for line in out.splitlines()
        if not re.match(r"^(Fake PKG Tools|orbis-pub-cmd|Custom Patches|-|CyB1K)", line.strip())
    )
    log.add(label, cmd, out, time.time() - started)

    errors = [ln for ln in out.splitlines() if ln.strip().startswith("[Error]")]
    if errors or proc.returncode != 0:
        detail = "\n".join(e.replace("[Error]", "").strip() for e in errors)
        raise PackageError(f"{label}: {detail or f'code {proc.returncode}'}")
    return out


# --- validation -----------------------------------------------------------

def sanitize_target(path: str, *, field_name: str) -> str:
    """Validates a destination path inside the package.

    These paths come from the interface and end up creating files on disk, so
    anything that could escape the content folder is rejected.
    """
    cleaned = (path or "").strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise PackageError(f"{field_name}: the path cannot be empty.")
    if len(cleaned) > 200:
        raise PackageError(f"{field_name}: the path is too long.")
    if ":" in cleaned:
        raise PackageError(f"{field_name}: absolute paths are not allowed.")

    parts = [p for p in cleaned.split("/") if p]
    for part in parts:
        if part in (".", ".."):
            raise PackageError(f"{field_name}: '..' is not allowed in the path.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", part):
            raise PackageError(
                f"{field_name}: '{part}' has characters that are not allowed "
                "(use letters, digits, dot, dash and underscore)."
            )
    return "/".join(parts)


def default_content_id(title_id: str, label: str) -> str:
    """Builds a valid Content ID from the title id and a name.

    Format: PPPPPP-TITLEID00_00-LABEL of 16 characters. Padded with zeros
    because the length is fixed.
    """
    clean = re.sub(r"[^A-Z0-9]", "", (label or "").upper())[:16]
    clean = (clean or "THEME").ljust(16, "0")
    return f"{PKG_DEFAULTS['content_prefix']}-{title_id}_00-{clean}"


@dataclass
class PackageMeta:
    title: str
    title_id: str
    content_id: str
    category: str
    passcode: str
    mode: str = PKG_DEFAULTS["mode"]
    #: Real loudness of sound/bgm_home.at9 in LKFS, or None when the theme has
    #: no music. The console levels the theme's music against the rest of the
    #: system with it: the reference theme declares bgmhome_loud=-32.00, and we
    #: used to measure it, show it in the interface and then throw it away.
    bgm_loudness: float | None = None

    @property
    def profile(self) -> dict:
        return PKG_MODES[self.mode]

    def validate(self) -> None:
        if self.mode not in PKG_MODES:
            raise PackageError(
                f"Unknown package type: '{self.mode}'. "
                f"Options: {', '.join(PKG_MODES)}."
            )
        if not self.title.strip():
            raise PackageError("The title cannot be empty.")
        if len(self.title) > 128:
            raise PackageError("The title cannot exceed 128 characters.")
        if not TITLE_ID_RE.match(self.title_id):
            raise PackageError(
                f"Invalid Title ID: '{self.title_id}'. It must be 4 letters and 5 digits, e.g. CUSA00000."
            )
        if not CONTENT_ID_RE.match(self.content_id):
            raise PackageError(
                f"Invalid Content ID: '{self.content_id}'. "
                "Format: XXXXXX-CUSA00000_00-ABCDEFGHIJKLMNOP (36 characters)."
            )
        permitidas = self.profile.get("categories") or PKG_CATEGORIES
        if self.category not in permitidas:
            raise PackageError(
                f"Invalid category: '{self.category}'. A '{self.mode}' package only "
                f"works with: {', '.join(permitidas)}. The others build fine, "
                "but the console fails to apply them."
            )
        if not PASSCODE_RE.match(self.passcode):
            raise PackageError("The passcode must be exactly 32 alphanumeric characters.")
        if self.title_id not in self.content_id:
            raise PackageError("The Content ID must contain the Title ID.")


# --- file generation ------------------------------------------------------

def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


def build_param_sfx(meta: PackageMeta) -> str:
    """Description of the param.sfo.

    In theme mode IRO_TAG goes in and the application fields (APP_TYPE,
    APP_VER...) stay out, which is how the tool itself writes them.
    """
    iro_tag = meta.profile["iro_tag"]
    rows = [
        '  <param key="ATTRIBUTE" fmt="int32">0</param>',
        f'  <param key="CATEGORY" fmt="utf8" max_len="4">{_esc(meta.category)}</param>',
        f'  <param key="CONTENT_ID" fmt="utf8" max_len="48">{_esc(meta.content_id)}</param>',
        '  <param key="FORMAT" fmt="utf8" max_len="4">obs</param>',
        f'  <param key="TITLE" fmt="utf8" max_len="128">{_esc(meta.title)}</param>',
        f'  <param key="TITLE_ID" fmt="utf8" max_len="12">{_esc(meta.title_id)}</param>',
        '  <param key="VERSION" fmt="utf8" max_len="8">01.00</param>',
    ]
    if iro_tag is not None:
        # CONTENT_VER is present in the retail theme used as reference.
        rows += [
            f'  <param key="IRO_TAG" fmt="int32">{iro_tag}</param>',
            '  <param key="CONTENT_VER" fmt="utf8" max_len="8">01.00</param>',
        ]

    else:
        rows += [
            '  <param key="APP_TYPE" fmt="int32">1</param>',
            '  <param key="APP_VER" fmt="utf8" max_len="8">01.00</param>',
            '  <param key="DOWNLOAD_DATA_SIZE" fmt="int32">0</param>',
            '  <param key="PARENTAL_LEVEL" fmt="int32">1</param>',
            '  <param key="SYSTEM_VER" fmt="int32">0</param>',
        ]

    # PUBTOOLINFO: the date always, the loudness only when the theme has music.
    # With bgmhome_loud the console levels the theme's music against the rest of
    # the system; the reference theme that sounds right declares -32.00, and
    # ours did not carry the field at all. Careful: the value has to be the REAL
    # one for the file. Faking it to raise the volume would be lying to the
    # console; the gain control is what actually changes the audio.
    info = [f"c_date={_dt.date.today():%Y%m%d}"]
    if meta.bgm_loudness is not None:
        info.append(f"bgmhome_loud={meta.bgm_loudness:.2f}")
    rows.append(
        f'  <param key="PUBTOOLINFO" fmt="utf8" max_len="512">{_esc(",".join(info))}</param>'
    )

    # param.sfo is ordered by key, the same way the tool writes it.
    return "\n".join([
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        "<paramsfo>",
        *sorted(rows),
        "</paramsfo>",
        "",
    ])


def build_gp4(meta: PackageMeta, targets: list[str], content_dir_name: str,
              *, source_paths: dict[str, str] | None = None) -> str:
    """Builds the .gp4 by hand.

    It is plain XML, so generating it here avoids calling gp4_file_add once per
    file (with 30+ frames that is 30 processes and several seconds for nothing).
    The format is copied from what the tool produces: the theme one carries no
    chunk_info and no storage_type, the app one does.
    """
    profile = meta.profile

    # Directories are declared separately from the file listing.
    dirs: dict[str, dict] = {}
    for target in targets:
        node = dirs
        for part in target.split("/")[:-1]:
            node = node.setdefault(part, {})

    def render_dirs(node: dict, indent: int) -> list[str]:
        out = []
        pad = " " * indent
        for name, children in node.items():
            if children:
                out.append(f'{pad}<dir targ_name="{_esc(name)}">')
                out += render_dirs(children, indent + 2)
                out.append(f"{pad}</dir>")
            else:
                out.append(f'{pad}<dir targ_name="{_esc(name)}"/>')
        return out

    sources = source_paths or {t: f"{content_dir_name}/{t}" for t in targets}
    files = [
        f'    <file targ_path="{_esc(t)}" '
        f'orig_path="{_esc(sources[t].replace("/", chr(92)))}"/>'
        for t in targets
    ]

    extra = "".join(
        f' {k}="{_esc(v)}"' for k, v in profile["extra_package_attrs"].items()
    )
    chunk_block = [
        '    <chunk_info chunk_count="1" scenario_count="1">',
        "      <chunks>",
        '        <chunk id="0" layer_no="0" label="Chunk #0"/>',
        "      </chunks>",
        '      <scenarios default_id="0">',
        '        <scenario id="0" type="sp" initial_chunk_count="1" label="Scenario #0">0</scenario>',
        "      </scenarios>",
        "    </chunk_info>",
    ] if profile["chunk_info"] else []

    return "﻿" + "\n".join([
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        '<psproject fmt="gp4" version="1000">',
        "  <volume>",
        f"    <volume_type>{profile['volume_type']}</volume_type>",
        "    <volume_id></volume_id>",
        f"    <volume_ts>{time.strftime('%Y-%m-%d %H:%M:%S')}</volume_ts>",
        f'    <package content_id="{_esc(meta.content_id)}" '
        f'passcode="{_esc(meta.passcode)}"{extra}/>',
        *chunk_block,
        "  </volume>",
        '  <files img_no="0">',
        *files,
        "  </files>",
        "  <rootdir>",
        *render_dirs(dirs, 4),
        "  </rootdir>",
        "</psproject>",
        "",
    ])


def build_package(work_dir: Path, content_dir: Path, meta: PackageMeta) -> tuple[Path, BuildLog]:
    """Builds the .pkg from an already assembled content folder.

    `content_dir` must hold everything except param.sfo and keystone, which are
    generated here inside its sce_sys.
    """
    if not tools_available():
        raise PackageError(
            f"No se encontro orbis-pub-cmd.exe en {PKG_TOOLS_DIR}. "
            "Revisa que la carpeta de las Fake PKG Tools siga en el proyecto."
        )
    meta.validate()

    log = BuildLog()
    sce_sys = content_dir / "sce_sys"
    sce_sys.mkdir(parents=True, exist_ok=True)

    # 1. param.sfo
    sfx_path = work_dir / "param.sfx"
    sfx_path.write_text(build_param_sfx(meta), encoding="utf-8")
    _run("param.sfo", ["sfo_create", str(sfx_path), str(sce_sys / "param.sfo")], log)

    # 2. keystone: application mode only. A theme stores no data and the tool
    #    itself leaves it out when the volume is a theme.
    if meta.profile["keystone"]:
        _run("keystone", ["ks_create", "--passcode", meta.passcode, str(sce_sys / "keystone")], log)

    # 3. gp4 with every file left in the content folder
    targets = sorted(
        p.relative_to(content_dir).as_posix()
        for p in content_dir.rglob("*") if p.is_file()
    )
    if not targets:
        raise PackageError("There is nothing to package.")

    gp4_path = work_dir / "theme.gp4"
    gp4_path.write_text(build_gp4(meta, targets, content_dir.name), encoding="utf-8")
    log.add("proyecto .gp4", ["(generado)", gp4_path.name], f"{len(targets)} archivos", 0.0)

    # 4. the package. The full digest costs tenths of a second at this size,
    #    so --skip_digest is not used.
    out_dir = work_dir / "pkg"
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    tmp_dir = work_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    _run(
        "img_create",
        [
            "img_create", "--no_progress_bar",
            "--tmp_path", str(tmp_dir),
            "--oformat", "pkg", str(gp4_path), str(out_dir),
        ],
        log,
        timeout=BUILD_TIMEOUT,
    )

    built = sorted(out_dir.glob("*.pkg"))
    if not built:
        raise PackageError("img_create finished without errors but produced no .pkg.")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return built[0], log
