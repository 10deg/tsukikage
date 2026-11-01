#!/usr/bin/env bash
# Build the native macOS app (Swift/AppKit shell + bundled Python backend)
# and wrap it into a DMG.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="build/macos-service-venv"
OUT="out/macos-app"
VERSION="$("$PYTHON" -c "import re,pathlib; print(re.search(r'__version__\\s*=\\s*\"([^\"]+)\"', pathlib.Path('src/moonlib/_version.py').read_text()).group(1))")"
ARCH="$(uname -m)"
TARGET="${ARCH}-apple-macos12.0"
DMG="$OUT/Tsukikage-$VERSION-macos-$ARCH.dmg"

echo "[1/6] Python build environment"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV/bin/python" -m pip install -e . \
  "Pillow==11.3.0" "numpy==2.0.2" "pyinstaller>=6.11"

echo "[2/6] bundling the Python backend"
rm -rf "$OUT/backend" "$OUT/build/backend"
mkdir -p "$OUT/backend" "$OUT/build"
PYINSTALLER_CONFIG_DIR="$ROOT/build/pyinstaller-config" \
  "$VENV/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$OUT/backend" --workpath "$OUT/build/backend" \
  packaging/tsukikage-backend-macos.spec

BACKEND="$OUT/backend/TsukikageBackend"
if [ ! -x "$BACKEND/TsukikageBackend" ]; then
  echo "[error] backend executable missing" >&2
  exit 1
fi

echo "[3/6] compiling the Swift app shell"
rm -rf "$OUT/build/swift-module-cache" "$OUT/dist"
mkdir -p "$OUT/build/swift-module-cache" "$OUT/dist"
swiftc -O -swift-version 5 -target "$TARGET" \
  -module-cache-path "$OUT/build/swift-module-cache" \
  -framework Cocoa -framework WebKit \
  -o "$OUT/build/Tsukikage" macapp/*.swift

echo "[4/6] assembling Tsukikage.app"
APP="$OUT/dist/Tsukikage.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/backend"
cp "$OUT/build/Tsukikage" "$APP/Contents/MacOS/Tsukikage"
cp macapp/Info.plist "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
cp packaging/tsukikage.icns "$APP/Contents/Resources/AppIcon.icns"
cp -R "$BACKEND/." "$APP/Contents/Resources/backend/"
chmod +x "$APP/Contents/MacOS/Tsukikage" "$APP/Contents/Resources/backend/TsukikageBackend"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

echo "[5/6] ad-hoc signing"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "[6/6] creating DMG"
STAGE="$OUT/dmg-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "$ROOT/macapp/DMG-README.txt" "$STAGE/安装说明.txt"
rm -f "$DMG"
hdiutil create -volname "Tsukikage $VERSION" -srcfolder "$STAGE" \
  -ov -format UDZO -fs HFS+ "$DMG"
rm -rf "$STAGE"

echo
echo "app: $APP"
echo "dmg: $DMG"
shasum -a 256 "$DMG" | tee "$DMG.sha256"
du -sh "$APP" "$DMG"
