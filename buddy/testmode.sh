#!/bin/bash
# Silent test mode: the buddy hears virtual inputs and plays into a null sink, so
# nobody (you or the squad) hears test replies.   ./testmode.sh on | off
set -e
C=~/.config/psaudio-buddy; D=~/.config/systemd/user/psaudio-buddy.service.d
if [ "$1" = on ]; then
  for n in wake_test_mic wake_test_squad buddy_test_null; do
    pactl list short sinks | grep -q "^[0-9]*\s$n\s" || pactl load-module module-null-sink sink_name=$n sink_properties=device.description=$n >> /tmp/buddy-testmods
  done
  sed -e '1i chat_url = ""' -e 's/^headset_sink .*/headset_sink = "buddy_test_null"/' -e 's/^ps5_mic_sink .*/ps5_mic_sink = "buddy_test_null"/' \
      -e 's/^mic_source .*/mic_source = "wake_test_mic.monitor"/' \
      -e 's/^squad_source .*//' -e 's/^\[wake\]/[wake]\nsquad_source = "wake_test_squad.monitor"/' $C/buddy.toml > $C/test.toml
  mkdir -p $D; printf '[Service]\nEnvironment=BUDDY_CONFIG=%%h/.config/psaudio-buddy/test.toml\nEnvironment=BUDDY_DEBUG=1\n' > $D/test.conf
else
  rm -f $D/test.conf $C/test.toml; rmdir $D 2>/dev/null || true
  for m in $(cat /tmp/buddy-testmods 2>/dev/null); do pactl unload-module $m 2>/dev/null || true; done; rm -f /tmp/buddy-testmods
fi
systemctl --user daemon-reload; systemctl --user restart psaudio-buddy; sleep 3
echo "test mode $1: $(pactl -f json list sink-inputs | python3 -c 'import json,sys;print(sorted({s["sink"] for s in json.load(sys.stdin) if s["properties"].get("application.id")=="psaudio-buddy"}))') <- buddy sinks"
