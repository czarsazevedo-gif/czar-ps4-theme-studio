"""Checkpoint 2 tests — Unified OuroborosEngine orchestrator & clean self-contained architecture."""

from __future__ import annotations

import ast
from pathlib import Path

from app.ouroboros_engine import INTERNAL_STRATEGIES, OUROBOROS_VERSION, PROPOSAL_PROFILES, OuroborosEngine

REPO = Path(__file__).resolve().parents[1]
SAMPLE_MP4 = REPO / "tests" / "sample.mp4"


def test_ouroboros_engine_has_zero_legacy_imports():
    """app/ouroboros_engine must never import app.deluxe, app.sentinela, app.circulo, app.genesis, or app.ouroboros_media."""
    forbidden = {"deluxe", "sentinela", "circulo", "genesis", "ouroboros_media"}
    engine_dir = REPO / "app" / "ouroboros_engine"
    files = list(engine_dir.glob("*.py"))
    assert len(files) >= 12
    for py_file in files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")
                names = [a.name for a in node.names]
                assert not (set(mod) & forbidden), f"{py_file.name} imports forbidden module {node.module}"
                assert not (set(names) & forbidden), f"{py_file.name} imports forbidden symbol {names}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert not (set(a.name.split(".")) & forbidden), f"{py_file.name} imports {a.name}"


def test_unified_orchestrator_inspect_analyze_render_post_dds(tmp_path):
    engine = OuroborosEngine()
    assert engine.version == OUROBOROS_VERSION
    assert set(INTERNAL_STRATEGIES.keys()) == {"A", "B", "C", "D", "E", "F"}
    assert [p["size"] for p in PROPOSAL_PROFILES] == [(1280, 720), (960, 540), (640, 360)]

    info = engine.inspect_source(SAMPLE_MP4, 10.0)
    assert info["width"] > 0 and info["height"] > 0
    assert info["pyav"] is not None and info["pyav"]["available"] is True

    an_dir = tmp_path / "an"
    an_dir.mkdir()
    analysis = engine.analyze_and_search(SAMPLE_MP4, float(info["duration"]), an_dir, filename="sample.mp4")
    assert analysis["version"] == OUROBOROS_VERSION
    assert len(analysis["groups"]) == 3
    assert "mediaDiagnostic" in analysis and len(analysis["mediaDiagnostic"]["fields"]) == 21

    rend_dir = tmp_path / "rend"
    rend = engine.render_loop(
        SAMPLE_MP4,
        float(info["duration"]),
        rend_dir,
        start=0.5,
        end=2.0,
        frames=8,
        size=(640, 360),
        wait=0.1,
        mode="auto",
        finish_enabled=True,
    )
    assert rend["type"] == "ouroboros_render"
    assert rend["strategyCode"] in {"A", "B", "C", "D", "E"}
    assert "issueAttribution" in rend
    assert rend["tenCycleAudit"]["cycles"] == 10
    assert (rend_dir / "background.raf.zip").is_file()
    assert (rend_dir / "diff.png").is_file()
    assert len(rend["ddsHashes"]) == 8
