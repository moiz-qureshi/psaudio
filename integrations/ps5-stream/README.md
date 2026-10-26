# PS5 Stream: let Hasaan see your PS5 screen

The PS5 can broadcast to Twitch over plain RTMP. It looks the ingest server up with DNS
every time, so a DNS server that answers those names with the MacBook's IP sends the
broadcast to the Mac instead (method: https://yashgarg.dev/posts/hijacking-ps5-rtmp-stream/,
reference app: github.com/yash-garg/PS5Streamer). Nothing reaches Twitch.

```
PS5 --DNS?--> opti3 ps5-stream-dns (192.168.5.54:53)  "contribute.live-video.net -> 192.168.4.38"
PS5 --RTMP--> MacBook "PS5 Stream" app :1935 (mediamtx) -> ffmpeg -> latest frame (4 fps)
Pi (squad mode) --GET /frame.jpg, /status--> MacBook :8898 (bearer token) -> Qwen3-VL on oMLX
```

| Piece | Where | Notes |
|---|---|---|
| DNS | opti3 `~/services/ps5-stream-dns` (copy in `opti3-dns/`) | `docker compose up -d`. Everything else is forwarded to the eero. |
| Receiver app | MacBook `~/Applications/PS5 Stream.app`, LaunchAgent `me.crcmz.ps5stream` | build: `./build.sh` on the Mac. Menu bar `PS5 ●` = live. Needs `brew install mediamtx ffmpeg`. |
| Frames | `http://192.168.4.38:8898/frame.jpg`, `/status` (token in `~/Library/Application Support/PS5Stream/token`, on the Pi as `PS5_STREAM_TOKEN`) | `/health` is open; the stream key never leaves the Mac. |

## One-time setup

1. **eero app**: reserve the MacBook's IP (192.168.4.38) so the DNS answer stays right.
2. **PS5**: Settings > Network > Settings > Set Up Internet Connection > (your connection) >
   Advanced Settings > DNS Settings: **Manual**, Primary **192.168.5.54**, Secondary **192.168.5.54**.
   (Don't put the eero or 1.1.1.1 as secondary: the PS5 would sometimes skip the redirect and
   go live on your real Twitch.)
3. **PS5**: link Twitch once (Settings > Users and Accounts > Linked Services > Twitch).
4. **Go live**: Create button > Broadcast > Twitch > Go Live. The Mac's menu bar turns `PS5 ●`.

## Check

```bash
dig +short @192.168.5.54 ingest.global-contribute.live-video.net   # -> 192.168.4.38
curl -s http://192.168.4.38:8898/health                              # {"live": true, ...}
```

Fake PS5 for testing (from opti3): `ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 -c:v libx264
-tune zerolatency -pix_fmt yuv420p -f flv rtmp://192.168.4.38:1935/app/live_test`.

To undo: set the PS5's DNS back to Automatic.
