# shellcheck shell=bash
# Shared by the psaudio scripts: config, state and device discovery.
# Installed to /usr/local/lib/psaudio/common.sh.

PSAUDIO_CONF=${PSAUDIO_CONF:-/etc/psaudio/psaudio.conf}
# shellcheck source=/dev/null
[ -r "$PSAUDIO_CONF" ] && . "$PSAUDIO_CONF"

: "${PSAUDIO_USER:=psaudio}"
: "${SOUNDS_DIR:=/home/$PSAUDIO_USER/sounds}"
: "${HEADSET_SINK:=auto}" "${HEADSET_SOURCE:=auto}" "${GADGET_SINK:=auto}" "${GADGET_SOURCE:=auto}"
: "${DEFAULT_VOLUME:=80}"
STATE=${PSAUDIO_STATE:-/run/psaudio}

# First PipeWire/Pulse node of `kind` (sinks|sources) whose name matches `pattern`.
_first_node() {
  pactl list short "$1" 2>/dev/null | awk '{print $2}' | grep -E "$2" | grep -v '\.monitor$' | head -1
}

# resolve VAR: echo the configured node, or discover it when set to "auto".
resolve() {
  local value=${!1}
  if [ -n "$value" ] && [ "$value" != auto ]; then
    echo "$value"
    return
  fi
  case "$1" in
    HEADSET_SINK)   _first_node sinks   '^alsa_output\.usb-' ;;
    HEADSET_SOURCE) _first_node sources '^alsa_input\.usb-' ;;
    GADGET_SINK)    _first_node sinks   '^alsa_output\.platform-.*\.usb\.' ;;
    GADGET_SOURCE)  _first_node sources '^alsa_input\.platform-.*\.usb\.' ;;
  esac
}

volume_pct() { cat "$STATE/soundboard-volume" 2>/dev/null || echo "$DEFAULT_VOLUME"; }

valid_name() { [[ "$1" =~ ^[a-zA-Z0-9_-]{1,64}$ ]]; }
