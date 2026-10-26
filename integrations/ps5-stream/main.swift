// PS5 Stream - menu bar app on the MacBook that receives the PS5's "Twitch" broadcast.
//
// The PS5 broadcasts to Twitch over plain RTMP; opti3's DNS (services/ps5-stream-dns)
// answers Twitch's ingest names with this Mac's IP, so the stream lands here instead
// (method: yashgarg.dev/posts/hijacking-ps5-rtmp-stream). This app:
//   - runs mediamtx (RTMP on :1935; its API and RTSP stay on 127.0.0.1)
//   - while a stream is live, runs ffmpeg writing the latest frame (4 fps, 960 px)
//   - serves it on :8898  GET /frame.jpg, /status (bearer token), /health
//   - shows LIVE / waiting in the menu bar, with a preview window
import AppKit
import Network

let support = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/PS5Stream")
let framePath = support.appendingPathComponent("frame.jpg").path
let tokenPath = support.appendingPathComponent("token").path
let mtxConfPath = support.appendingPathComponent("mediamtx.yml").path
let mediamtx = "/opt/homebrew/bin/mediamtx"
let ffmpeg = "/opt/homebrew/bin/ffmpeg"
let httpPort: UInt16 = 8898
let fps = 4

let mtxConf = """
logLevel: warn
api: yes
apiAddress: 127.0.0.1:9997
rtmp: yes
rtmpAddress: :1935
rtsp: yes
rtspAddress: 127.0.0.1:8554
rtspTransports: [tcp]
hls: no
webrtc: no
srt: no
metrics: no
playback: no
pprof: no
paths:
  all_others:
"""

func token() -> String {
    if let t = try? String(contentsOfFile: tokenPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty {
        return t
    }
    let t = (0..<32).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
    try? t.write(toFile: tokenPath, atomically: true, encoding: .utf8)
    try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tokenPath)
    return t
}

final class Stream {
    var mtx: Process?
    var grab: Process?
    var livePath: String?          // mediamtx path (app/<stream key>) - never shown or served
    var liveSince: Date?
    var onChange: (() -> Void)?

    func start() {
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        try? mtxConf.write(toFile: mtxConfPath, atomically: true, encoding: .utf8)
        startMediamtx()
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in self?.poll() }
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.sampleMotion() }
    }

    func startMediamtx() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: mediamtx)
        p.arguments = [mtxConfPath]
        p.terminationHandler = { [weak self] _ in      // keep it running
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self?.startMediamtx() }
        }
        try? p.run()
        mtx = p
    }

    func poll() {
        guard let url = URL(string: "http://127.0.0.1:9997/v3/paths/list") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            var ready: String?
            if let data = data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let items = obj["items"] as? [[String: Any]] {
                ready = items.first(where: { ($0["ready"] as? Bool) == true })?["name"] as? String
            }
            DispatchQueue.main.async { self?.update(ready) }
        }.resume()
    }

    func update(_ ready: String?) {
        if ready != livePath {
            stopGrab()
            livePath = ready
            liveSince = ready == nil ? nil : Date()
            if ready != nil { startGrab() }
            onChange?()
        } else if ready != nil && (grab == nil || grab?.isRunning == false) {
            startGrab()                                    // ffmpeg died: restart it
        }
    }

    func startGrab() {
        guard let path = livePath else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: ffmpeg)
        p.arguments = ["-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                       "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/\(path)", "-an",
                       "-vf", "fps=\(fps),scale=960:-2", "-q:v", "5",
                       "-f", "image2", "-update", "1", "-atomic_writing", "1", framePath]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
        grab = p
    }

    func stopGrab() {
        grab?.terminate()
        grab = nil
    }

    var frameAge: Double? {
        guard let a = try? FileManager.default.attributesOfItem(atPath: framePath),
              let d = a[.modificationDate] as? Date else { return nil }
        return Date().timeIntervalSince(d)
    }

    var live: Bool { livePath != nil && (frameAge ?? 99) < 5 }

    // How much the picture moves: a 32x18 grey thumbnail, compared twice a second.
    // static_s = seconds since it last changed visibly (AFK / pause menu / spectating idle).
    var thumb: [UInt8]?
    var motion = 0.0
    var lastMotion = Date()

    func sampleMotion() {
        guard live, let img = NSImage(contentsOfFile: framePath),
              let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return }
        let w = 32, h = 18
        var px = [UInt8](repeating: 0, count: w * h)
        guard let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w,
                                  space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)
        else { return }
        ctx.interpolationQuality = .low
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
        if let prev = thumb {
            var sum = 0
            for i in 0..<px.count { sum += abs(Int(px[i]) - Int(prev[i])) }
            motion = Double(sum) / Double(px.count * 255)
            if motion > 0.012 { lastMotion = Date() }
        } else {
            lastMotion = Date()
        }
        thumb = px
    }

    var staticSeconds: Double { live ? Date().timeIntervalSince(lastMotion) : 0 }
}

final class HTTPServer {
    let stream: Stream
    let secret = token()
    var listener: NWListener?

    init(stream: Stream) { self.stream = stream }

    func start() {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        listener = try? NWListener(using: params, on: NWEndpoint.Port(rawValue: httpPort)!)
        listener?.newConnectionHandler = { [weak self] c in self?.handle(c) }
        listener?.start(queue: .global())
    }

    func handle(_ c: NWConnection) {
        c.start(queue: .global())
        c.receive(minimumIncompleteLength: 1, maximumLength: 16384) { [weak self] data, _, _, _ in
            guard let self = self, let data = data, let req = String(data: data, encoding: .utf8) else { c.cancel(); return }
            let first = req.split(separator: "\r\n").first ?? ""
            let parts = first.split(separator: " ")
            let target = parts.count > 1 ? String(parts[1]) : "/"
            let path = String(target.split(separator: "?").first ?? "/")
            let authed = req.contains("Authorization: Bearer \(self.secret)") || req.lowercased().contains("authorization: bearer \(self.secret)")
                || target.contains("token=\(self.secret)")
            switch path {
            case "/health", "/":
                self.send(c, 200, "application/json", self.statusJSON(full: false))
            case "/status" where authed:
                self.send(c, 200, "application/json", self.statusJSON(full: true))
            case "/frame.jpg" where authed:
                if self.stream.live, let jpg = FileManager.default.contents(atPath: framePath) {
                    self.send(c, 200, "image/jpeg", jpg)
                } else {
                    self.send(c, 404, "application/json", Data("{\"live\": false}".utf8))
                }
            default:
                self.send(c, authed ? 404 : 401, "text/plain", Data((authed ? "not found" : "token required").utf8))
            }
        }
    }

    func statusJSON(full: Bool) -> Data {
        var o: [String: Any] = ["app": "PS5 Stream", "live": stream.live]
        if let s = stream.liveSince { o["live_for_s"] = Int(Date().timeIntervalSince(s)) }
        if full, let a = stream.frameAge { o["frame_age_s"] = (a * 100).rounded() / 100; o["fps"] = fps }
        if full && stream.live {
            o["motion"] = (stream.motion * 1000).rounded() / 1000
            o["static_s"] = Int(stream.staticSeconds)
        }
        return (try? JSONSerialization.data(withJSONObject: o)) ?? Data("{}".utf8)
    }

    func send(_ c: NWConnection, _ code: Int, _ type: String, _ body: Data) {
        let reason = [200: "OK", 401: "Unauthorized", 404: "Not Found"][code] ?? "OK"
        var head = "HTTP/1.1 \(code) \(reason)\r\nContent-Type: \(type)\r\nContent-Length: \(body.count)\r\n"
        head += "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        var out = Data(head.utf8)
        out.append(body)
        c.send(content: out, completion: .contentProcessed { _ in c.cancel() })
    }
}

final class App: NSObject, NSApplicationDelegate {
    let stream = Stream()
    lazy var http = HTTPServer(stream: stream)
    var item: NSStatusItem!
    var statusLine = NSMenuItem(title: "Waiting for the PS5…", action: nil, keyEquivalent: "")
    var preview: NSWindow?
    var imageView = NSImageView()
    var previewTimer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.addItem(statusLine)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Show preview", action: #selector(showPreview), keyEquivalent: "p"))
        menu.addItem(NSMenuItem(title: "How to start a stream…", action: #selector(help), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit PS5 Stream", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        item.menu = menu
        stream.onChange = { [weak self] in self?.refresh() }
        stream.start()
        http.start()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
        refresh()
    }

    func refresh() {
        let live = stream.live
        item.button?.title = live ? "PS5 ●" : "PS5 ○"
        item.button?.contentTintColor = live ? .systemGreen : .secondaryLabelColor
        if live, let s = stream.liveSince {
            let m = Int(Date().timeIntervalSince(s)) / 60
            statusLine.title = "LIVE for \(m) min · Hasaan can see it"
        } else {
            statusLine.title = stream.livePath != nil ? "Stream connected, waiting for frames…" : "Waiting for the PS5…"
        }
    }

    @objc func showPreview() {
        if preview == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 360),
                             styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            w.title = "PS5 Stream"
            w.isReleasedWhenClosed = false
            imageView.imageScaling = .scaleProportionallyUpOrDown
            w.contentView = imageView
            w.center()
            preview = w
        }
        preview?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        previewTimer?.invalidate()
        previewTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            guard let self = self, self.preview?.isVisible == true else { return }
            self.imageView.image = self.stream.live ? NSImage(contentsOfFile: framePath) : nil
        }
    }

    @objc func help() {
        let a = NSAlert()
        a.messageText = "Start a stream from the PS5"
        a.informativeText = "On the PS5: Create button → Broadcast → Twitch → Go Live.\n\nThe PS5's DNS must be 192.168.5.54 (opti3), which sends Twitch's ingest to this Mac. Nothing reaches Twitch."
        a.runModal()
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
