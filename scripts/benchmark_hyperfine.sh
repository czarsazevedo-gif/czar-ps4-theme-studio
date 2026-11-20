#!/usr/bin/env bash
# Reproducible Hyperfine v2 benchmark for Czar PS4 Theme Studio (Ouroboros Absolute)
# Measures wall-clock time, CPU time, and peak RSS memory across all core Studio stages:
#   1. App cold startup & /healthz readiness
#   2. Media probe & PyAV exact PTS inspection
#   3. Canonical DXT1a DDS + RAF scene assembly
#   4. ATRAC9 144 kbps encoding + vgmstream-cli validation + internal loop check
#   5. LibOrbisPkg PS4 Theme PKG build + extraction + strict SFO & SHA-256 verification
#   6. Unified OUROBOROS 3-proposal pipeline (720p, 540p, 360p)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HYPERFINE="${ROOT_DIR}/tools/bin/hyperfine"
if [[ ! -x "${HYPERFINE}" ]]; then
  HYPERFINE="$(command -v hyperfine || true)"
fi
if [[ -z "${HYPERFINE}" ]]; then
  echo "ERRO: hyperfine não encontrado. Execute ./scripts/bootstrap_czar_full.sh --install" >&2
  exit 1
fi

PYTHON_BIN="${CZAR_PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  for cand in "/home/antigravity/.venv/bin/python" "${ROOT_DIR}/.venv/bin/python" "$(command -v python3 || true)"; do
    if [[ -n "${cand}" && -x "${cand}" ]]; then
      PYTHON_BIN="${cand}"
      break
    fi
  done
fi
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "ERRO: python3 não encontrado." >&2
  exit 1
fi

OUT_DIR="${1:-/tmp/czar-hyperfine-bench}"
RUNS="${CZAR_BENCH_RUNS:-3}"
OUROBOROS_RUNS="${CZAR_BENCH_OUROBOROS_RUNS:-1}"
mkdir -p "${OUT_DIR}"

export PYTHONPATH="${ROOT_DIR}"
cd "${ROOT_DIR}"

echo "=== CZAR HYPERFINE BENCHMARK ==="
echo "Repo: ${ROOT_DIR}"
echo "Python: ${PYTHON_BIN}"
echo "Hyperfine: ${HYPERFINE} ($("${HYPERFINE}" --version))"
echo "Output: ${OUT_DIR}"

# 1. Cold import + /healthz
"${HYPERFINE}" --warmup 1 --runs "${RUNS}" \
  --export-json "${OUT_DIR}/01_startup.json" \
  --command-name "startup_and_healthz" \
  "${PYTHON_BIN} -c 'from fastapi.testclient import TestClient; from app import main; c = TestClient(main.app); assert c.get(\"/healthz\").status_code == 200'"

# 2. Media probe + PyAV exact PTS inspection
"${HYPERFINE}" --warmup 1 --runs "${RUNS}" \
  --export-json "${OUT_DIR}/02_media_probe.json" \
  --command-name "media_probe_pyav" \
  "${PYTHON_BIN} -c 'from pathlib import Path; from app.ouroboros_engine import media_io; p = Path(\"tests/sample.mp4\"); info = media_io.source_info(p, 2.0, lambda: None); assert info[\"width\"] > 0'"

# 3. Canonical DXT1a DDS (12 frames @ 960x540) + RAF ZIP
"${HYPERFINE}" --warmup 1 --runs "${RUNS}" \
  --export-json "${OUT_DIR}/03_dds_raf.json" \
  --command-name "dds_dxt1_12x540p_raf" \
  "${PYTHON_BIN} -c 'import tempfile, numpy as np; from pathlib import Path; from app import dxt1; from app.ouroboros_engine.applier import assemble_canonical_raf; f = np.full((540, 960, 4), 180, dtype=np.uint8); blobs = [dxt1.encode_dds(f) for _ in range(12)]; td = Path(tempfile.mkdtemp()); assemble_canonical_raf(blobs, 0.1, td / \"bg.raf.zip\")'"

# 4. ATRAC9 144 kbps + vgmstream-cli + internal loop evaluation
"${HYPERFINE}" --warmup 0 --runs "${RUNS}" \
  --export-json "${OUT_DIR}/04_atrac9_vgmstream.json" \
  --command-name "atrac9_encode_vgmstream_loop" \
  "${PYTHON_BIN} -c 'import tempfile, numpy as np; from pathlib import Path; from app import at9; from app.forge.audio_validation import validate_at9; from app.forge.audio_loop import evaluate_post_at9_loop; td = Path(tempfile.mkdtemp()); p = td / \"bgm.at9\"; t = np.arange(48000 * 3, dtype=np.float64) / 48000.0; s = np.stack([0.25*np.sin(2*np.pi*220*t), 0.25*np.sin(2*np.pi*330*t)], axis=1); at9.encode_pcm(s, p, bitrate=144, loop=True, validate=False); v = validate_at9(p); assert v.valid and \"vgmstream\" in v.validator; ev = evaluate_post_at9_loop(p); assert ev[\"vgmstream_verified\"]'"

# 5. LibOrbisPkg PS4 Theme PKG build + extraction + strict SFO & hash check
"${HYPERFINE}" --warmup 0 --runs "${RUNS}" \
  --export-json "${OUT_DIR}/05_pkg_build_verify.json" \
  --command-name "pkg_build_extract_verify" \
  "${PYTHON_BIN} -c 'import tempfile; from pathlib import Path; from PIL import Image; from app import pkg, theme_xml; from app.forge import pkg_forge, sfo; td = Path(tempfile.mkdtemp()); cdir = td / \"content\"; (cdir / \"sce_sys\").mkdir(parents=True); (cdir / \"texture\" / \"background\").mkdir(parents=True); Image.new(\"RGB\", (512,512), (40,80,160)).save(cdir / \"sce_sys\" / \"icon0.png\"); Image.new(\"RGB\", (1920,1080), (20,40,90)).save(cdir / \"sce_sys\" / \"pic1.png\"); Image.new(\"RGB\", (740,416), (20,40,90)).save(cdir / \"texture\" / \"preview.png\"); Image.new(\"RGB\", (1920,1080), (20,40,90)).save(cdir / \"texture\" / \"background\" / \"homescreen.png\"); Image.new(\"RGB\", (1920,1080), (20,40,90)).save(cdir / \"texture\" / \"background\" / \"functionscreen.png\"); (cdir / \"theme.xml\").write_text(theme_xml.build_theme_xml(label=\"Bench\", homebgm_enable=False), encoding=\"utf-8\"); meta = pkg.PackageMeta(title=\"Bench\", title_id=\"CUSA12345\", content_id=\"UP0001-CUSA12345_00-0000000000000001\", category=\"ac\", passcode=\"0\"*32, mode=\"theme\", bgm_loudness=None); (cdir / \"sce_sys\" / \"param.sfo\").write_bytes(sfo.build_param_sfo_bytes(meta)); files = sorted(p.relative_to(cdir).as_posix() for p in cdir.rglob(\"*\") if p.is_file()); gp4 = td / \"t.gp4\"; gp4.write_text(pkg.build_gp4(meta, files, \"content\"), encoding=\"utf-8\"); out = td / \"out\"; out.mkdir(); _, _, prep, _ = pkg_forge.build_and_validate_theme_pkg(cdir, gp4, meta, out); assert prep.verification_level == \"full_bundle_verified\"'"

# 6. Unified OUROBOROS 3-proposal pipeline (720p, 540p, 360p)
"${HYPERFINE}" --warmup 0 --runs "${OUROBOROS_RUNS}" \
  --export-json "${OUT_DIR}/06_ouroboros_3proposals.json" \
  --command-name "ouroboros_3_proposals_sample_mp4" \
  "${PYTHON_BIN} -c 'import tempfile; from pathlib import Path; from app.ouroboros_engine import OuroborosEngine; td = Path(tempfile.mkdtemp()); rep = OuroborosEngine().generate_three_proposals(Path(\"tests/sample.mp4\"), 2.0, td); assert len(rep[\"proposals\"]) == 3'"

# Aggregate Hyperfine v2 JSON files into summary.json and summary.md
"${PYTHON_BIN}" - "${OUT_DIR}" <<'PY'
import json, sys
from pathlib import Path

out_dir = Path(sys.argv[1])
rows = []
for p in sorted(out_dir.glob("[0-9][0-9]_*.json")):
    raw = json.loads(p.read_text(encoding="utf-8"))
    for res in raw.get("results", []):
        if "summary" in res:
            s = res["summary"]
            wall = s["time_wall_clock"]["mean"]
            cpu = s.get("time_cpu", {}).get("mean")
            rss_bytes = s.get("memory_peak_resident", {}).get("mean")
            rss_mib = round(rss_bytes / (1024 * 1024), 1) if rss_bytes else None
        else:
            wall = res.get("mean")
            cpu = None
            rss_mib = None
        rows.append({
            "stage": res.get("command_name") or p.stem,
            "wall_mean_s": round(float(wall), 3) if wall is not None else None,
            "cpu_mean_s": round(float(cpu), 3) if cpu is not None else None,
            "peak_rss_mib": rss_mib,
        })

if not rows:
    print("ERRO: Nenhum resultado Hyperfine gerado.", file=sys.stderr)
    raise SystemExit(1)

(out_dir / "summary.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
lines = [
    "# CZAR PS4 Theme Studio — Hyperfine Benchmark Summary",
    "",
    "| Etapa | Tempo Wall Médio (s) | Tempo CPU Médio (s) | Pico RSS (MiB) |",
    "|---|---:|---:|---:|",
]
for r in rows:
    lines.append(
        f"| `{r['stage']}` | {r['wall_mean_s']} | {r['cpu_mean_s'] if r['cpu_mean_s'] is not None else '—'} | {r['peak_rss_mib'] if r['peak_rss_mib'] is not None else '—'} |"
    )
(out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
PY
