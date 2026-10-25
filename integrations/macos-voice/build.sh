#!/bin/bash
# Build + install HasaanVoice.app into ~/Applications and start it at login. Run on the Mac.
set -euo pipefail
cd "$(dirname "$0")"
APP=$HOME/Applications/HasaanVoice.app
mkdir -p "$APP/Contents/MacOS"
swiftc -O HasaanVoice.swift -o "$APP/Contents/MacOS/HasaanVoice"
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>me.qureshi.hasaanvoice</string>
<key>CFBundleName</key><string>HasaanVoice</string>
<key>CFBundleExecutable</key><string>HasaanVoice</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSUIElement</key><true/>
<key>LSMinimumSystemVersion</key><string>14.0</string>
</dict></plist>
PL
codesign --force -s - "$APP"
cat > "$HOME/Library/LaunchAgents/me.qureshi.hasaanvoice.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>me.qureshi.hasaanvoice</string>
<key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-W</string><string>$APP</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/me.qureshi.hasaanvoice" 2>/dev/null || true
pkill -x HasaanVoice 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/me.qureshi.hasaanvoice.plist"
echo "HasaanVoice running; log: ~/Library/Application Support/HasaanVoice/log.txt"
