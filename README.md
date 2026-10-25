# PS Audio Bridge

Turn a Raspberry Pi 4 into a USB sound card for your PlayStation 5 that sits between the
console and your headset, and adds what the console can't do:

- **Soundboard** — fire clips into party chat *and* hear them in your headset.
- **Voice effects** — radio, robot, pitch up/down, live on your mic.
- **Mutes and volume** you can drive from a phone, a stream deck, Home Assistant or a script.
- **Your headset keeps working** — game and chat audio pass straight through.

```
 PS5 ──USB-C──▶ Raspberry Pi 4 ──USB-A──▶ USB headset (e.g. Logitech A50 X)
     (Pi = "PS Audio Bridge"   PipeWire: console audio → headset
      USB sound card)          headset mic → effects → console
                               soundboard clips → both
```

Built on Raspberry Pi OS Lite (64-bit), PipeWire and the Linux USB gadget (UAC1) driver.
No custom kernel.

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 4 Model B (2 GB+) | The USB-C port is the one that can act as a USB device. A Pi 5 is untested. |
| microSD card, 8 GB+ | |
| USB-C ↔ USB cable to the PS5 | Data **and** power: the Pi runs off the console's USB port. A Pi 4 with an A50 X base station draws fine from a PS5 port (no under-voltage). If `vcgencmd get_throttled` isn't `0x0`, use a USB-C power+data splitter. |
| A USB headset or USB DAC | Plugs into a Pi USB-A port. Tested: Logitech A50 X (PC/USB mode). Any USB Audio Class device should work. |

On the PS5: **Settings → Sound → Audio Output → Output Device: "PS Audio Bridge"**, and the
same for the microphone input.

## Install

### Option A — flash the ready image (easiest)

1. Download `psaudio-<version>-arm64.img.xz` from [Releases](https://github.com/moiz-qureshi/psaudio/releases).
2. Raspberry Pi Imager → **Choose OS → Use custom** → the `.img.xz`.
3. In Imager's **OS customisation** set: hostname `psaudio`, username **`psaudio`** with a
   password, your Wi-Fi, and **enable SSH** (paste your public key).
4. Flash, put the card in the Pi, plug the Pi into the PS5 and the headset into the Pi.
   First boot takes a couple of minutes while it expands the filesystem.

### Option B — install onto Raspberry Pi OS Lite yourself

Flash **Raspberry Pi OS Lite (64-bit)** with Imager (same customisation as above), SSH in, then:

```bash
curl -fsSL https://raw.githubusercontent.com/moiz-qureshi/psaudio/main/install.sh | sudo bash
sudo reboot
```

or from a clone: `git clone https://github.com/moiz-qureshi/psaudio && sudo psaudio/install.sh`.
The installer is safe to re-run: it upgrades the scripts and keeps your config, clips and keys.

### Check it

```bash
ssh psaudio@psaudio.local psaudio-ctl status     # gadget bound, PipeWire active, devices found
ssh psaudio@psaudio.local psaudio-ctl play coin  # you and your party hear a coin
```

## Remote control (phone, stream deck, Home Assistant…)

Give your controller its **own** SSH key that can only run `psaudio-ctl` commands —
it can't open a shell or forward ports:

```bash
# on the controller
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_psaudio -N ''
cat ~/.ssh/id_ed25519_psaudio.pub          # copy this line

# on the Pi (paste the line between the quotes)
sudo /opt/psaudio/install.sh --automation-key "ssh-ed25519 AAAA… controller"

# back on the controller
ssh -i ~/.ssh/id_ed25519_psaudio psaudio@psaudio.local "play airhorn"
```

### Commands

| Command | |
|---|---|
| `play <clip>` · `stop` | Play a clip into chat + headset / stop all clips |
| `volume <0-100>` · `volume_up [n]` · `volume_down [n]` · `get_volume` | Soundboard volume |
| `mic_mute` · `mic_unmute` | Your mic to the console |
| `mute` · `unmute` | Everything the console hears from the Pi |
| `effect normal\|radio\|pitch_high\|pitch_low\|robot` | Live voice effect |
| `sounds` | List clips |
| `receive_sound <name>` · `delete_sound <name>` | Add a clip (WAV on stdin) / remove one |
| `status` · `devices` · `version` | Health, detected audio devices, version |
| `reboot_audio` | Restart the audio stack |

Add a clip from any computer (convert to 48 kHz mono WAV first):

```bash
ffmpeg -i airhorn.mp3 -ar 48000 -ac 1 -sample_fmt s16 -f wav - \
  | ssh -i ~/.ssh/id_ed25519_psaudio psaudio@psaudio.local "receive_sound airhorn"
```

### Stream deck

[`integrations/ragnaros-deck`](integrations/ragnaros-deck) adds the soundboard, mutes, effects
and a volume knob to Deck Manager for a Ragnaros/Ajazz deck:
copy `psaudio.yaml` to `~/.config/ragnaros/integrations/` and `bin/psaudio` to
`~/.config/ragnaros/bin/`.

## Configure

`/etc/psaudio/psaudio.conf`:

```bash
PSAUDIO_USER=psaudio
SOUNDS_DIR=/home/psaudio/sounds
HEADSET_SINK=auto     # or a PipeWire node name — see: psaudio-ctl devices
HEADSET_SOURCE=auto
GADGET_SINK=auto
GADGET_SOURCE=auto
GADGET_PRODUCT="PS Audio Bridge"   # the name the PS5 shows
DEFAULT_VOLUME=80
```

`auto` picks the first USB headset and the Pi's own gadget. After changing headsets run
`psaudio-ctl configure` (it re-points the console → headset route and restarts PipeWire).

## Update

```bash
curl -fsSL https://raw.githubusercontent.com/moiz-qureshi/psaudio/main/install.sh | sudo bash
```

It fetches the latest version, upgrades in place and keeps your config, clips and keys
(a copy of the installed version stays in `/opt/psaudio`).

## Back up and restore

From any Linux/macOS machine that can SSH to the Pi:

```bash
scripts/backup.sh psaudio@psaudio.local ~/backups/psaudio        # config + clips + keys, ~5 MB, no root
scripts/backup-image.sh psaudio@psaudio.local ~/backups/psaudio  # exact SD-card clone (.img.xz), asks for sudo password
```

- **New card, same Pi setup:** flash the `-sd.img.xz` with Imager (*Use custom*). It's an exact
  clone — hostname, users, keys, Wi-Fi — so keep it private.
- **Fresh install + your stuff:** install (A or B), then on the Pi:
  `sudo tar xzf psaudio-*.tgz -C / && sudo reboot`.

## Build the image yourself

Images are built by GitHub Actions on every `v*` tag ([`image.yml`](.github/workflows/image.yml)).
Locally it needs an arm64 Linux machine (a Pi 4/5 works):

```bash
sudo apt install parted e2fsprogs xz-utils curl
sudo image/build-image.sh dist          # → dist/psaudio-<version>-arm64.img.xz
```

It downloads the official Raspberry Pi OS Lite (64-bit), runs `install.sh --image` inside it
and compresses the result. Release a new version: bump `VERSION`, then
`git tag v0.2.0 && git push --tags`.

## Troubleshooting

| Symptom | Check |
|---|---|
| PS5 doesn't list "PS Audio Bridge" | `psaudio-ctl status` → *Gadget UDC* must say `fe980000.usb`. Use the Pi's **USB-C** port; check `dtoverlay=dwc2,dr_mode=peripheral` is in `/boot/firmware/config.txt`; reboot. |
| No game audio in the headset | `psaudio-ctl devices` — both `GADGET_SOURCE` and `HEADSET_SINK` found? Then `psaudio-ctl configure`. |
| Party can't hear you | `systemctl --user status psaudio-mic-fx`; `psaudio-ctl effect normal`. |
| Crackles | `psaudio-ctl status` → *ALSA xruns*; avoid USB hubs; check power (`vcgencmd get_throttled`). |
| Logs | `journalctl -u psaudio-gadget` · `journalctl --user -u psaudio-mic-fx -u psaudio-configure` |

## Layout

```
bin/            psaudio-ctl (control), psaudio-ctl-ssh (restricted SSH entry),
                psaudio-gadget-setup (USB gadget), psaudio-mic-fx-run (mic effects)
lib/            common.sh — config + device discovery
config/         psaudio.conf defaults → /etc/psaudio/
systemd/        psaudio-gadget (system) · psaudio-mic-fx, psaudio-configure (user)
pipewire/ wireplumber/   audio graph config
sounds/         generate.py — the built-in clips (original, generated)
image/          build-image.sh — flashable image
scripts/        backup.sh, backup-image.sh
integrations/   stream deck (Deck Manager) actions
```

## License

PS Audio Bridge is free software under the [GNU AGPL-3.0](LICENSE): use it, change it,
self-host it. If you distribute a modified version — or run one as a service for others —
you must publish your changes under the same license. For a commercial license (ship it
in a closed product without the AGPL terms), open an issue or contact the author.

PlayStation and PS5 are trademarks of Sony Interactive Entertainment; this project is not
affiliated with or endorsed by Sony.
