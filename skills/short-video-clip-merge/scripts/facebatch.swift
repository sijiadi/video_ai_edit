import Foundation
import Vision
import AppKit

// 用法: swift facebatch.swift <json文件路径>
// JSON 内容: ["路径1","路径2",...]
// 输出: 每行 <路径>\tFACE\tq\tyaw\tpitch\troll\tbbx\bby\tbbw\tbbh  或  <路径>\tNOFACE
let args = CommandLine.arguments
guard args.count > 1,
      let data = try? Data(contentsOf: URL(fileURLWithPath: args[1])),
      let paths = try? JSONDecoder().decode([String].self, from: data) else {
  print("ERR args"); exit(1)
}
for path in paths {
  guard let img = NSImage(contentsOfFile: path),
        let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    print("\(path)\tERRIMG"); continue
  }
  let req = VNDetectFaceCaptureQualityRequest()
  let h = VNImageRequestHandler(cgImage: cg, options: [:])
  try? h.perform([req])
  let rs = req.results ?? []
  if rs.isEmpty { print("\(path)\tNOFACE"); continue }
  // 取最大脸
  let o = rs.max { a, b in
    (a.boundingBox.width * a.boundingBox.height) < (b.boundingBox.width * b.boundingBox.height)
  }!
  print(String(format:"%@\tFACE\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f\t%.3f",
    path, o.faceCaptureQuality ?? 0,
    o.yaw?.floatValue ?? 0, o.pitch?.floatValue ?? 0, o.roll?.floatValue ?? 0,
    Float(o.boundingBox.origin.x), Float(o.boundingBox.origin.y),
    Float(o.boundingBox.width), Float(o.boundingBox.height)))
}
