#!/bin/bash
# Builds builds/DuplicatePhotoFinder-<VERSION>-mac-arm64.dmg: a disk image with "Duplicate Photo Finder.app", a link to Applications, README.txt. Run on a Mac with Apple Silicon:
#   bash packaging/build_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=packaging/.build-venv-mac
WORK=packaging/.build/mac
NAME="Duplicate Photo Finder"
# Portable Python (python-build-standalone via uv), built for macOS 11+. The Homebrew Python of the build Mac
# targets that Mac's own macOS and would make the app refuse to start on older systems.
PYDIR=packaging/.python
if [ ! -x "$VENV/bin/python" ]; then
  command -v uv >/dev/null || { echo "Нужен uv: https://docs.astral.sh/uv/ (brew install uv)"; exit 1; }
  UV_PYTHON_INSTALL_DIR="$PWD/$PYDIR" uv python install 3.13.5
  "$PYDIR"/cpython-3.13.5-macos-aarch64-none/bin/python3.13 -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q -r requirements.txt pyinstaller
fi
VER=$(cat VERSION)
OUT="builds/DuplicatePhotoFinder-$VER-mac-arm64.dmg"
rm -rf "$WORK"; mkdir -p "$WORK/zip/$NAME" builds
"$VENV/bin/python" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$WORK/dist" --workpath "$WORK/work" packaging/mac.spec
cp -R "$WORK/dist/$NAME.app" "$WORK/zip/$NAME/"
cp packaging/readme/mac.txt "$WORK/zip/$NAME/README.txt"
cp LICENSE THIRD_PARTY_NOTICES.md "$WORK/zip/$NAME/"
# GitHub refuses files over 100 MB in a repository
# a link to Applications next to the app: the person drags the app onto it
ln -s /Applications "$WORK/zip/$NAME/Applications"
# disk image compressed with LZMA: about 15% smaller than zip, the usual way Mac programs are handed out
rm -f "$OUT"
hdiutil create -quiet -volname "$NAME" -srcfolder "$WORK/zip/$NAME" -format ULMO -o "$OUT"
ls -lh "$OUT"
[ $(stat -f%z "$OUT") -lt 100000000 ] || { echo "Архив больше 100 МБ, GitHub его не примет"; exit 1; }
