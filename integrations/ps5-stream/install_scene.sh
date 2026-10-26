#!/bin/bash
# Install the PS5 scene watcher as an always-on LaunchAgent (run ON THE MAC from this folder).
# Needs: PS5 Stream app running, ~/Library/Application Support/PS5Stream/omlx_key (mode 600).
set -euo pipefail
cd "$(dirname "$0")"
DEST="$HOME/Library/Application Support/PS5Stream/ps5_scene.py"
cp ps5_scene.py "$DEST"
PL="$HOME/Library/LaunchAgents/me.crcmz.ps5scene.plist"
cat > "$PL" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>me.crcmz.ps5scene</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$DEST</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/ps5scene.err</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/me.crcmz.ps5scene" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PL"
echo "installed: me.crcmz.ps5scene (:8899)"
