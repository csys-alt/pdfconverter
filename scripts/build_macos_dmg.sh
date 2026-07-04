#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_NAME="PDFConverter"
DISPLAY_NAME="PDFConverter Desktop"
BUNDLE_ID="com.pdfconverter.desktop"
VERSION="${VERSION:-2026.07.04}"
ARCH="$(uname -m)"
VENV_DIR="$ROOT_DIR/.venv-release-macos"
BUILD_ROOT="$ROOT_DIR/release/macos"
DIST_DIR="$BUILD_ROOT/dist"
WORK_DIR="$BUILD_ROOT/build"
SPEC_DIR="$BUILD_ROOT/spec"
ASSET_DIR="$BUILD_ROOT/assets"
DMG_STAGING="$BUILD_ROOT/dmg-staging"
DMG_PATH="$ROOT_DIR/release/${APP_NAME}-${VERSION}-macos-${ARCH}.dmg"
ICONSET_DIR="$ASSET_DIR/${APP_NAME}.iconset"
ICNS_PATH="$ASSET_DIR/${APP_NAME}.icns"

mkdir -p "$DIST_DIR" "$WORK_DIR" "$SPEC_DIR" "$ASSET_DIR" "$ICONSET_DIR" "$DMG_STAGING"

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r requirements.txt pyinstaller
"$VENV_DIR/bin/python" -m pip uninstall -y pillow >/dev/null 2>&1 || true

sips -z 16 16 assets/icon.png --out "$ICONSET_DIR/icon_16x16.png" >/dev/null
sips -z 32 32 assets/icon.png --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
sips -z 32 32 assets/icon.png --out "$ICONSET_DIR/icon_32x32.png" >/dev/null
sips -z 64 64 assets/icon.png --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
sips -z 128 128 assets/icon.png --out "$ICONSET_DIR/icon_128x128.png" >/dev/null
sips -z 256 256 assets/icon.png --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
sips -z 256 256 assets/icon.png --out "$ICONSET_DIR/icon_256x256.png" >/dev/null
sips -z 512 512 assets/icon.png --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
sips -z 512 512 assets/icon.png --out "$ICONSET_DIR/icon_512x512.png" >/dev/null
cp assets/icon.png "$ICONSET_DIR/icon_512x512@2x.png"
iconutil -c icns "$ICONSET_DIR" -o "$ICNS_PATH"

"$VENV_DIR/bin/pyinstaller" \
  --name "$APP_NAME" \
  --windowed \
  --clean \
  --noconfirm \
  --icon "$ICNS_PATH" \
  --add-data "$ROOT_DIR/assets:assets" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR" \
  --specpath "$SPEC_DIR" \
  --target-architecture "$ARCH" \
  --exclude-module tkinter \
  --exclude-module matplotlib \
  --exclude-module numpy \
  --exclude-module pandas \
  pdfbro.py

APP_PATH="$DIST_DIR/${APP_NAME}.app"
PLIST_PATH="$APP_PATH/Contents/Info.plist"

/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $DISPLAY_NAME" "$PLIST_PATH" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string $DISPLAY_NAME" "$PLIST_PATH"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST_PATH" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST_PATH"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST_PATH" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST_PATH"
/usr/libexec/PlistBuddy -c "Add :NSLocalNetworkUsageDescription string PDFConverter uses the local network to pair with your mobile device and receive files over Wi-Fi." "$PLIST_PATH" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :NSLocalNetworkUsageDescription PDFConverter uses the local network to pair with your mobile device and receive files over Wi-Fi." "$PLIST_PATH"

codesign --force --deep --sign - "$APP_PATH"

rm -rf "$DMG_STAGING/${APP_NAME}.app"
ditto "$APP_PATH" "$DMG_STAGING/${APP_NAME}.app"
ln -sfn /Applications "$DMG_STAGING/Applications"

hdiutil create \
  -volname "$DISPLAY_NAME" \
  -srcfolder "$DMG_STAGING" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

spctl --assess --type execute "$APP_PATH" || true

echo "$DMG_PATH"
