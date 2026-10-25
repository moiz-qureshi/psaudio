// HasaanVoice: serves your macOS Personal Voice (or any system voice) as a tiny
// LAN TTS endpoint for psaudio-buddy. Must run as an app in your login session:
// Apple only lets an app you approved use a Personal Voice.
//
//   GET  /health                      -> {"authorized":..,"voice":..,"voices":[..]}   (no auth)
//   POST /speak  {"text":"..","rate":0.5}  -> audio/wav, 24 kHz mono 16-bit       (Bearer token)
//
// Token: ~/Library/Application Support/HasaanVoice/token (created on first run).
import AVFoundation
import Foundation
import Network

let port: UInt16 = 8891
let supportDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/HasaanVoice")
try? FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
let tokenURL = supportDir.appendingPathComponent("token")
if !FileManager.default.fileExists(atPath: tokenURL.path) {
    var bytes = [UInt8](repeating: 0, count: 24)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    try? Data(bytes).base64EncodedString().replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "=", with: "")
        .write(to: tokenURL, atomically: true, encoding: .utf8)
    try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tokenURL.path)
}
let token = (try? String(contentsOf: tokenURL, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
let logURL = supportDir.appendingPathComponent("log.txt")
func log(_ s: String) {
    let line = "\(Date()) \(s)\n"
    if let h = try? FileHandle(forWritingTo: logURL) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close() }
    else { try? line.write(to: logURL, atomically: true, encoding: .utf8) }
}

var authorized = AVSpeechSynthesizer.personalVoiceAuthorizationStatus == .authorized
func personalVoices() -> [AVSpeechSynthesisVoice] {
    AVSpeechSynthesisVoice.speechVoices().filter { $0.voiceTraits.contains(.isPersonalVoice) }
}
func chosenVoice() -> AVSpeechSynthesisVoice? {
    personalVoices().first ?? AVSpeechSynthesisVoice(language: "en-US")
}

// -- synthesis to 24 kHz mono Int16 ------------------------------------------
let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: true)!
let synthQueue = DispatchQueue(label: "synth")
var keepAlive: [AVSpeechSynthesizer] = []

func synthesize(_ text: String, rate: Float, done: @escaping (Data?) -> Void) {
    synthQueue.async {
        let synth = AVSpeechSynthesizer()
        keepAlive.append(synth)
        let u = AVSpeechUtterance(string: text)
        u.voice = chosenVoice()
        u.rate = rate
        var pcm = Data()
        var converter: AVAudioConverter?
        var finished = false
        synth.write(u) { buffer in
            guard let src = buffer as? AVAudioPCMBuffer else { return }
            if src.frameLength == 0 {
                if !finished { finished = true; done(pcm); synthQueue.async { keepAlive.removeAll { $0 === synth } } }
                return
            }
            if converter == nil { converter = AVAudioConverter(from: src.format, to: outFormat) }
            guard let conv = converter else { return }
            let cap = AVAudioFrameCount(Double(src.frameLength) * 24000 / src.format.sampleRate) + 64
            guard let dst = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: cap) else { return }
            var fed = false
            var err: NSError?
            conv.convert(to: dst, error: &err) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true; status.pointee = .haveData; return src
            }
            if let p = dst.int16ChannelData { pcm.append(Data(bytes: p[0], count: Int(dst.frameLength) * 2)) }
        }
        // a voice that never calls back (e.g. not allowed to render) must not hang the caller
        DispatchQueue.global().asyncAfter(deadline: .now() + 20) { if !finished { finished = true; done(nil) } }
    }
}

func wav(_ pcm: Data) -> Data {
    var d = Data()
    func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
    func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
    d.append("RIFF".data(using: .ascii)!); u32(UInt32(36 + pcm.count)); d.append("WAVEfmt ".data(using: .ascii)!)
    u32(16); u16(1); u16(1); u32(24000); u32(48000); u16(2); u16(16)
    d.append("data".data(using: .ascii)!); u32(UInt32(pcm.count)); d.append(pcm)
    return d
}

// -- minimal HTTP ---------------------------------------------------------------
func reply(_ c: NWConnection, _ code: Int, _ type: String, _ body: Data) {
    let head = "HTTP/1.1 \(code) OK\r\nContent-Type: \(type)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
    c.send(content: head.data(using: .utf8)! + body, completion: .contentProcessed { _ in c.cancel() })
}
func json(_ c: NWConnection, _ code: Int, _ obj: [String: Any]) {
    reply(c, code, "application/json", (try? JSONSerialization.data(withJSONObject: obj)) ?? Data())
}

func handle(_ c: NWConnection, _ buf: Data) {
    guard let sep = buf.range(of: Data("\r\n\r\n".utf8)) else {
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { d, _, done, _ in
            if let d = d, !d.isEmpty, buf.count < 200_000 { handle(c, buf + d) } else { c.cancel() }
        }
        return
    }
    let head = String(decoding: buf[..<sep.lowerBound], as: UTF8.self)
    let lines = head.components(separatedBy: "\r\n")
    let parts = (lines.first ?? "").split(separator: " ")
    let method = parts.first.map(String.init) ?? "", path = parts.count > 1 ? String(parts[1]) : "/"
    var headers: [String: String] = [:]
    for l in lines.dropFirst() { if let i = l.firstIndex(of: ":") { headers[l[..<i].lowercased()] = l[l.index(after: i)...].trimmingCharacters(in: .whitespaces) } }
    let want = Int(headers["content-length"] ?? "0") ?? 0
    let body = buf[sep.upperBound...]
    if body.count < want {
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { d, _, _, _ in
            if let d = d, !d.isEmpty { handle(c, buf + d) } else { c.cancel() }
        }
        return
    }
    if method == "GET" && path == "/health" {
        json(c, 200, ["ok": true, "authorized": authorized, "voice": chosenVoice()?.name ?? "",
                      "personal_voice": !personalVoices().isEmpty,
                      "voices": personalVoices().map { $0.name }])
        return
    }
    guard headers["authorization"] == "Bearer \(token)", !token.isEmpty else { json(c, 401, ["ok": false, "error": "unauthorised"]); return }
    if method == "POST" && path == "/speak" {
        let o = (try? JSONSerialization.jsonObject(with: Data(body))) as? [String: Any] ?? [:]
        let text = (o["text"] as? String ?? "").prefix(1000)
        let rate = Float((o["rate"] as? Double) ?? Double(AVSpeechUtteranceDefaultSpeechRate))
        guard !text.isEmpty else { json(c, 400, ["ok": false, "error": "no text"]); return }
        let t0 = Date()
        synthesize(String(text), rate: rate) { pcm in
            guard let pcm = pcm, !pcm.isEmpty else { json(c, 503, ["ok": false, "error": "voice produced no audio (Personal Voice allowed?)"]); return }
            log("speak \(text.count) chars -> \(pcm.count / 48000 * 1000 / 1000)s audio in \(Int(Date().timeIntervalSince(t0) * 1000)) ms")
            reply(c, 200, "audio/wav", wav(pcm))
        }
        return
    }
    json(c, 404, ["ok": false, "error": "not found"])
}

func selfTest() {
    synthesize("Hey, this is your Personal Voice test.", rate: AVSpeechUtteranceDefaultSpeechRate) { pcm in
        let n = pcm?.count ?? 0
        log("self-test: authorized=\(authorized) voice=\(chosenVoice()?.name ?? "-") personal=\(!personalVoices().isEmpty) bytes=\(n)")
        if let pcm = pcm, n > 0 { try? wav(pcm).write(to: supportDir.appendingPathComponent("selftest.wav")) }
    }
}

let listener = try! NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: port)!)
listener.newConnectionHandler = { c in
    c.start(queue: .global())
    c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { d, _, _, _ in handle(c, d ?? Data()) }
}
listener.start(queue: .global())
log("listening on :\(port); personal voice status=\(AVSpeechSynthesizer.personalVoiceAuthorizationStatus.rawValue)")

if !authorized {
    AVSpeechSynthesizer.requestPersonalVoiceAuthorization { status in
        authorized = status == .authorized
        log("authorization answer: \(status.rawValue)")
        selfTest()
    }
} else {
    selfTest()
}
RunLoop.main.run()
