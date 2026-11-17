#!/bin/bash
# Builds builds/DuplicatePhotoFinder-<VERSION>-windows-x64.zip on a Mac or Linux machine, no Windows needed:
# the official portable Python for Windows + Windows builds of the libraries + the program + start.bat.
#   bash packaging/build_windows.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PYV=3.13.17
VENV=packaging/.build-venv
CACHE=packaging/.cache
WORK=packaging/.build/win
APP="$WORK/DuplicatePhotoFinder"
VER=$(cat VERSION)
OUT="builds/DuplicatePhotoFinder-$VER-windows-x64.zip"
mkdir -p "$CACHE" builds
# any local Python 3 with pip can download Windows builds of the libraries
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
EMBED="$CACHE/python-$PYV-embed-amd64.zip"
[ -f "$EMBED" ] || curl -fsSL -o "$EMBED" "https://www.python.org/ftp/python/$PYV/python-$PYV-embed-amd64.zip"
rm -rf "$WORK"; mkdir -p "$APP/python"
unzip -q "$EMBED" -d "$APP/python"
# let the portable Python see the libraries and the program next to it
PTH=$(ls "$APP"/python/python3*._pth)
printf 'python313.zip\r\n.\r\nLib\\site-packages\r\n..\r\nimport site\r\n' > "$PTH"
"$VENV/bin/python" -m pip install -q --target "$APP/python/Lib/site-packages" \
  --platform win_amd64 --python-version 3.13 --implementation cp --only-binary=:all: \
  -r requirements.txt
rm -rf "$APP"/python/Lib/site-packages/{sympy,mpmath,isympy.py,bin}
# OpenCV's video module (ffmpeg, ~28 MB): the program reads only still images
rm -f "$APP"/python/Lib/site-packages/cv2/opencv_videoio_ffmpeg*.dll
# what the program never loads: compiled caches, library tests, type stubs, onnxruntime developer tools.
# Checked on a pruned copy of the same libraries: the full pipeline runs. Keeps the archive under 100 MB.
SP="$APP/python/Lib/site-packages"
find "$SP" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$SP" -name tests -type d -prune -exec rm -rf {} +
find "$SP" -name "*.pyi" -delete
rm -rf "$SP"/onnxruntime/{transformers,quantization,tools} "$SP"/numpy/_core/include
# onnxruntime needs msvcp140.dll and msvcp140_1.dll; a clean Windows may lack them. Microsoft's redistributable
# files, taken from the msvc-runtime wheel, go next to python.exe, where Windows looks for them first.
MSVC="$CACHE/msvc"
ls "$MSVC"/msvc_runtime-*-cp313-cp313-win_amd64.whl >/dev/null 2>&1 || "$VENV/bin/python" -m pip download -q \
  msvc-runtime==14.51.36231 --platform win_amd64 --python-version 3.13 --implementation cp --only-binary=:all: --no-deps -d "$MSVC"
unzip -q -j -o "$MSVC"/msvc_runtime-*-cp313-cp313-win_amd64.whl \
  'msvc_runtime-*.data/data/msvcp140.dll' 'msvc_runtime-*.data/data/msvcp140_1.dll' -d "$APP/python"
mkdir -p "$APP/dedup"
rsync -a --exclude __pycache__ dedup/ "$APP/dedup/"
cp packaging/launcher.py "$APP/"
cp packaging/windows/start.bat "$APP/"
perl -pe 's/\r?\n/\r\n/' packaging/readme/windows.txt > "$APP/README.txt"
cp LICENSE THIRD_PARTY_NOTICES.md "$APP/"
rm -f "$OUT"
(cd "$WORK" && zip -qr -9 -X "../../../$OUT" DuplicatePhotoFinder)
ls -lh "$OUT"
# GitHub refuses files over 100 MB in a repository
[ $(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT") -lt 100000000 ] || { echo "Архив больше 100 МБ, GitHub его не примет"; exit 1; }
