// ps5ocr: Apple Vision text recognition for the scene watcher (kill feed, HUD, scoreboard).
//   ps5ocr <image.jpg> [x y w h]     region as fractions of the image (default: all of it)
// Prints one JSON array: [{"text", "x", "y", "w", "h", "conf", "rgb": [r, g, b]}], boxes in
// fractions of the whole image (top-left origin), rgb = the text's own colour (team colours:
// cyan = his side, red = the enemy in CoD). The local vision model can't read small, coloured,
// video-compressed HUD text; Vision can, in a fraction of a second, on the Neural Engine.
import CoreGraphics
import Foundation
import ImageIO
import Vision

let args = CommandLine.arguments
guard args.count >= 2, let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil),
      let full = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    FileHandle.standardError.write("usage: ps5ocr <image> [x y w h]\n".data(using: .utf8)!); exit(2)
}
var rx = 0.0, ry = 0.0, rw = 1.0, rh = 1.0
if args.count >= 6, let a = Double(args[2]), let b = Double(args[3]), let c = Double(args[4]), let d = Double(args[5]) {
    (rx, ry, rw, rh) = (a, b, c, d)
}
let W = Double(full.width), H = Double(full.height)
let cropRect = CGRect(x: rx * W, y: ry * H, width: rw * W, height: rh * H).integral
guard let img = full.cropping(to: cropRect) else { print("[]"); exit(0) }

// pixels, to sample each line's colour
let cw = img.width, ch = img.height
var px = [UInt8](repeating: 0, count: cw * ch * 4)
let ctx = CGContext(data: &px, width: cw, height: ch, bitsPerComponent: 8, bytesPerRow: cw * 4,
                    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.draw(img, in: CGRect(x: 0, y: 0, width: cw, height: ch))

func textColour(_ r: CGRect) -> [Int] {
    // r: Vision box (normalized, bottom-left origin) -> the brightest, most coloured pixels in it
    let x0 = max(0, Int(r.minX * Double(cw))), x1 = min(cw, Int(r.maxX * Double(cw)))
    let y0 = max(0, Int((1 - r.maxY) * Double(ch))), y1 = min(ch, Int((1 - r.minY) * Double(ch)))
    var picked: [(Int, Int, Int, Int)] = []
    if x1 > x0 && y1 > y0 {
        for y in y0..<y1 {
            for x in x0..<x1 {
                let i = (y * cw + x) * 4
                let (R, G, B) = (Int(px[i]), Int(px[i + 1]), Int(px[i + 2]))
                let mx = max(R, G, B), mn = min(R, G, B)
                picked.append((R, G, B, mx + (mx - mn)))       // bright + saturated = the glyphs
            }
        }
    }
    picked.sort { $0.3 > $1.3 }
    let top = picked.prefix(max(1, picked.count / 8))
    if top.isEmpty { return [0, 0, 0] }
    let n = top.count
    return [top.map { $0.0 }.reduce(0, +) / n, top.map { $0.1 }.reduce(0, +) / n, top.map { $0.2 }.reduce(0, +) / n]
}

let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false            // gamertags aren't words
req.recognitionLanguages = ["en-US"]
req.minimumTextHeight = 0.0
try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
var out: [[String: Any]] = []
for o in (req.results ?? []) {
    guard let c = o.topCandidates(1).first else { continue }
    let b = o.boundingBox                     // within the crop, normalized, bottom-left origin
    out.append(["text": c.string, "conf": Double(c.confidence),
                "x": rx + b.minX * rw, "y": ry + (1 - b.maxY) * rh, "w": b.width * rw, "h": b.height * rh,
                "rgb": textColour(b)])
}
out.sort { (($0["y"] as! Double), ($0["x"] as! Double)) < (($1["y"] as! Double), ($1["x"] as! Double)) }
let data = try! JSONSerialization.data(withJSONObject: out, options: [])
print(String(data: data, encoding: .utf8)!)
