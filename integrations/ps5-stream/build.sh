#!/bin/bash
# Build + install "PS5 Stream.app" into ~/Applications and keep it running (LaunchAgent).
# Run ON THE MAC from this folder:  ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
APP="$HOME/Applications/PS5 Stream.app"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/PS5Stream" main.swift -framework AppKit -framework Network
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>me.crcmz.ps5stream</string>
  <key>CFBundleName</key><string>PS5 Stream</string>
  <key>CFBundleExecutable</key><string>PS5Stream</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Receives the PS5 broadcast and shares frames with Hasaan.</string>
</dict></plist>
PL
codesign --force -s - "$APP"
PL="$HOME/Library/LaunchAgents/me.crcmz.ps5stream.plist"
cat > "$PL" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>me.crcmz.ps5stream</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/PS5Stream</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/me.crcmz.ps5stream" 2>/dev/null || true
pkill -x mediamtx 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PL"
echo "installed: $APP (LaunchAgent me.crcmz.ps5stream)"
