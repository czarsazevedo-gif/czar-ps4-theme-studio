"""Checkpoint 1 tests — CZAR Complete Toolchain bootstrap idempotency & doctor functional checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_bootstrap_and_doctor_scripts_syntax_and_security():
    for rel in (
        "scripts/bootstrap_czar_full.sh",
        "scripts/doctor_czar_full.sh",
        "scripts/setup_codespace.sh",
        "start.sh",
    ):
        p = REPO / rel
        assert p.is_file(), f"Missing {rel}"
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"Syntax error in {rel}: {r.stderr}"

    boot_txt = (REPO / "scripts" / "bootstrap_czar_full.sh").read_text(encoding="utf-8")
    # Never bypass GPG verification or download proprietary Sony tools
    assert "--allow-unauthenticated" not in boot_txt
    assert "Acquire::AllowInsecureRepositories" not in boot_txt
    assert "orbis-pub-cmd.exe" not in boot_txt
    assert "at9tool.exe" not in boot_txt
    assert "HYPERFINE_X86_64_SHA256=" in boot_txt


def test_doctor_czar_full_json_all_21_mandatory_ready():
    r = subprocess.run(
        ["bash", str(REPO / "scripts" / "doctor_czar_full.sh"), "--json"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"doctor failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert data["title"] == "CZAR COMPLETE TOOLCHAIN"
    assert data["ok"] is True
    assert data["mandatory_ready"] == data["mandatory_total"] == 21
    by_name = {item["name"]: item for item in data["mandatory"]}
    for expected in (
        "PYTHON",
        "FASTAPI",
        "FFMPEG",
        "FFPROBE",
        "GIFSICLE",
        "OPENCV",
        "SCIPY",
        "AVALIAÇÃO VISUAL",
        "PSGFX ATRAC9",
        "VGMSTREAM",
        "VAG",
        "LIBORBISPKG",
        "DOTNET",
        "ÍCONES",
        "COMPOSITOR",
        "RAF",
        "PKG",
        "PLAYWRIGHT",
        "CHROMIUM",
        "HYPERFINE",
        "OUROBOROS",
    ):
        assert expected in by_name, f"Missing mandatory entry {expected}"
        assert by_name[expected]["ok"] is True, f"{expected} not ready: {by_name[expected]}"
        assert by_name[expected]["status"] == "PRONTO"
    assert "PENDENTE" in data["physical_ps4_compatibility"]


def test_bootstrap_check_only_succeeds_idempotently():
    r = subprocess.run(
        ["bash", str(REPO / "scripts" / "bootstrap_czar_full.sh"), "--check-only"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"check-only failed:\n{r.stdout}\n{r.stderr}"
    assert "CZAR COMPLETE TOOLCHAIN" in r.stdout
    assert "21/21 obrigatórios PRONTOS" in r.stdout
