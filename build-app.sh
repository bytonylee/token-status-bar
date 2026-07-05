#!/bin/bash
# Build and install the TokenStatusBar menu-bar app.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="TokenStatusBar"
APP_DIR="$DIR/$APP_NAME.app"
BUILD_DIR="$DIR/build"
INSTALL_DIR="/Applications/$APP_NAME.app"

# ─── Bundled Python (python-build-standalone) ────────────────────────────
# Embeds a standalone Python so the app works without a system python3.
# Cached under build/python-standalone to avoid re-downloading.
PY_VERSION="3.12.13"
PY_RELEASE="20260623"
PY_ARCH="aarch64-apple-darwin"
PY_TARBALL="cpython-${PY_VERSION}+${PY_RELEASE}-${PY_ARCH}-install_only_stripped.tar.gz"
PY_URL="https://github.com/indygreg/python-build-standalone/releases/download/${PY_RELEASE}/${PY_TARBALL}"
PY_CACHE="$BUILD_DIR/python-standalone"

echo "Building $APP_NAME..."
swiftc -framework Cocoa -framework SwiftUI \
  "$DIR/app/TokenStatusBar.swift" \
  -o "$BUILD_DIR/$APP_NAME" \
  -parse-as-library \
  2>&1

echo "Bundling .app..."
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"
cp "$BUILD_DIR/$APP_NAME" "$APP_DIR/Contents/MacOS/$APP_NAME"
cp "$DIR/public/assets/icon/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"

# Bundle backend Python scripts (stdlib-only, no third-party packages).
mkdir -p "$APP_DIR/Contents/Resources/backend"
for f in "$DIR/backend/"*.py; do
  [[ "$(basename "$f")" == test_* ]] && continue
  cp "$f" "$APP_DIR/Contents/Resources/backend/"
done

# Bundle standalone Python if not already cached.
if [[ ! -d "$PY_CACHE/python/bin" ]]; then
  echo "Fetching standalone Python ${PY_VERSION} (${PY_ARCH})..."
  mkdir -p "$PY_CACHE"
  curl -sL -o "$BUILD_DIR/$PY_TARBALL" "$PY_URL"
  tar xzf "$BUILD_DIR/$PY_TARBALL" -C "$PY_CACHE"
  rm -f "$BUILD_DIR/$PY_TARBALL"
fi
rm -rf "$APP_DIR/Contents/Resources/python"
cp -R "$PY_CACHE/python" "$APP_DIR/Contents/Resources/python"

cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>TokenStatusBar</string>
    <key>CFBundleDisplayName</key>
    <string>TokenStatusBar</string>
    <key>CFBundleIdentifier</key>
    <string>com.tonye.tokenstatusbar</string>
    <key>CFBundleVersion</key>
    <string>0.0.1</string>
    <key>CFBundleShortVersionString</key>
    <string>0.0.1</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleExecutable</key>
    <string>TokenStatusBar</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

echo "Installing to $INSTALL_DIR..."
# Remove any previous install (kill it first if running).
if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
    pkill -x "$APP_NAME" && sleep 1
fi
rm -rf "$INSTALL_DIR"
cp -R "$APP_DIR" "$INSTALL_DIR"

if [[ "${1:-}" == "--dmg" ]]; then
  echo "Packaging DMG..."
  DMG="$BUILD_DIR/$APP_NAME.dmg"
  STAGE="$BUILD_DIR/dmg-stage"
  RWDMG="$BUILD_DIR/rw.dmg"
  VOLNAME="$APP_NAME"
  BG="$DIR/public/assets/dmg/dmg-background.png"
  WIN_W=660; WIN_H=440
  APP_X=160; APP_Y=220
  APPS_X=500; APPS_Y=220

  rm -rf "$STAGE" "$DMG" "$RWDMG"
  mkdir -p "$STAGE/.background"
  cp -R "$APP_DIR" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  cp "$BG" "$STAGE/.background/background.png"

  hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -ov -format UDRW "$RWDMG" >/dev/null
  rm -rf "$STAGE"

  ATTACH_OUTPUT="$(hdiutil attach -readwrite -nobrowse "$RWDMG")"
  VOL="$(printf '%s\n' "$ATTACH_OUTPUT" | sed -n 's#^.*\(/Volumes/.*\)$#\1#p' | tail -n 1)"
  if [[ -z "$VOL" ]]; then
    echo "Unable to find mounted DMG volume." >&2
    exit 1
  fi
  osascript \
    -e "tell application \"Finder\"" \
    -e "set dmg to disk \"$VOLNAME\"" \
    -e "open dmg" \
    -e "set current view of container window of dmg to icon view" \
    -e "set toolbar visible of container window of dmg to false" \
    -e "set statusbar visible of container window of dmg to false" \
    -e "set the bounds of container window of dmg to {100, 100, $((WIN_W + 100)), $((WIN_H + 100))}" \
    -e "set theViewOptions to the icon view options of container window of dmg" \
    -e "set arrangement of theViewOptions to not arranged" \
    -e "set icon size of theViewOptions to 80" \
    -e "set background picture of theViewOptions to POSIX file \"$VOL/.background/background.png\" as alias" \
    -e "set position of item \"$APP_NAME\" of dmg to {$APP_X, $APP_Y}" \
    -e "set position of item \"Applications\" of dmg to {$APPS_X, $APPS_Y}" \
    -e "set the bounds of container window of dmg to {100, 100, $((WIN_W + 100)), $((WIN_H + 100))}" \
    -e "close dmg" \
    -e "end tell"

  sleep 1
  hdiutil detach "$VOL" -force >/dev/null
  hdiutil convert "$RWDMG" -format UDZO -ov -o "$DMG" >/dev/null
  rm -f "$RWDMG"
  echo "Built $DMG"
fi

echo "Done: $INSTALL_DIR"
echo "Run: open '$INSTALL_DIR'"
