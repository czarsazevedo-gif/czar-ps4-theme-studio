#!/usr/bin/env bash
# Isolate the CPU extension and the editor's Python packages in this checkout.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
venv="$PWD/.venv-motion"
if [[ ! -x "$venv/bin/python" ]]; then python -m venv --system-site-packages "$venv"; fi
py="$venv/bin/python"
"$py" -m pip install -r requirements.txt
check_opencv() {
  "$py" - <<'PY'
import re
import sys
try:
    import cv2
except ImportError:
    sys.exit(1)
info = cv2.getBuildInformation()
ipp = re.search(r"^ *Intel IPP: *(.*)$", info, re.M)
modules = re.search(r"^ *To be built: *(.*)$", info, re.M)
valid = (cv2.__version__ == "4.11.0" and (ipp is None or ipp.group(1).strip() == "NO")
         and bool(modules and set(modules.group(1).split()) == {"core", "imgproc", "video", "python3"}))
sys.exit(0 if valid else 1)
PY
}
if check_opencv; then
  echo 'OpenCV de CPU já disponível para o laboratório.'
  exit 0
fi
for tool in cmake ninja c++ curl tar sha256sum; do
  command -v "$tool" >/dev/null || { echo "Dependência de compilação ausente: $tool" >&2; exit 1; }
done
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
source_url='https://files.pythonhosted.org/packages/36/2f/5b2b3ba52c864848885ba988f24b7f105052f68da9ab0e693cc7c25b0b30/opencv-python-headless-4.11.0.86.tar.gz'
sha256='996eb282ca4b43ec6a3972414de0e2331f5d9cda2b41091a49739c19fb843798'
curl --fail --silent --show-error --location --retry 2 --max-time 360 "$source_url" -o "$work_dir/sources.tar.gz"
printf '%s  %s\n' "$sha256" "$work_dir/sources.tar.gz" | sha256sum --check --status
mkdir "$work_dir/source"
# Only the upstream OpenCV source tree is needed from this fixed PyPI source distribution.
tar -xzf "$work_dir/sources.tar.gz" --strip-components=2 -C "$work_dir/source" --wildcards '*/opencv/*'
include_dir=$("$py" -c 'import sysconfig; print(sysconfig.get_path("include"))')
python_lib=$("$py" - <<'PYLIB'
import sys, sysconfig
from pathlib import Path
name = sysconfig.get_config_var("LDLIBRARY")
for directory in (Path(sys.base_prefix) / "lib", Path(sysconfig.get_config_var("LIBDIR"))):
    if (directory / name).is_file():
        print(directory / name)
        break
else:
    raise SystemExit("Biblioteca de desenvolvimento do Python ausente.")
PYLIB
)
numpy_dir=$("$py" -c 'import numpy; print(numpy.get_include())')
site_dir=$("$py" -c 'import sysconfig; print(sysconfig.get_path("platlib"))')
cmake -S "$work_dir/source" -B "$work_dir/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_LIST=core,imgproc,video,python3 -DBUILD_SHARED_LIBS=OFF \
  -DBUILD_opencv_python3=ON -DPYTHON3_EXECUTABLE="$py" -DPYTHON3_INCLUDE_DIR="$include_dir" \
  -DPYTHON3_NUMPY_INCLUDE_DIRS="$numpy_dir" -DPYTHON_DEFAULT_EXECUTABLE="$py" -DPYTHON3_LIBRARY="$python_lib" -DOPENCV_PYTHON3_INSTALL_PATH=python \
  -DWITH_IPP=OFF -DWITH_ITT=OFF -DWITH_OPENCL=OFF -DWITH_LAPACK=OFF -DWITH_FFMPEG=OFF \
  -DWITH_ADE=OFF -DWITH_PROTOBUF=OFF -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF \
  -DBUILD_EXAMPLES=OFF -DBUILD_JAVA=OFF -DCPU_DISPATCH=
cmake --build "$work_dir/build" --target opencv_python3 --parallel 2
mapfile -t extensions < <(find "$work_dir/build/lib" -type f -name 'cv2*.so')
[[ ${#extensions[@]} == 1 ]] || { echo 'A compilação não produziu uma extensão cv2 única.' >&2; exit 1; }
# Publish the complete extension atomically. Python packages in other worktrees remain independent.
install -m 755 "${extensions[0]}" "$site_dir/.motion-cv2.tmp"
mv -f -- "$site_dir/.motion-cv2.tmp" "$site_dir/$(basename -- "${extensions[0]}")"
check_opencv || { echo 'Falha ao verificar a compilação livre do OpenCV.' >&2; exit 1; }
echo 'OpenCV 4.11.0 de CPU pronto.'
