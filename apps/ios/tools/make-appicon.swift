#!/usr/bin/env swift
//
// Render NOVA's app icon.
//
// The icon is the same mark the launch screen shows -- `NovaMark` in
// RootView.swift -- at the same proportions, so what somebody taps and what
// they then see are recognisably one thing. Those proportions are copied
// below rather than imported because this runs as a standalone script, and
// the test in NOVATests/IconTests.swift is what keeps the two in step.
//
// A dark ground rather than the launch screen's system background: two small
// blue bars on white reads as a blank tile on a home screen, and the mark is
// meant to read as attention -- a pair of eyes open in the dark.
//
//   swift Tools/make-appicon.swift NOVA/Assets.xcassets/AppIcon.appiconset/icon-1024.png

import AppKit
import CoreGraphics
import Foundation

// -- the mark, as NovaMark declares it ---------------------------------------

/// Fractions of `size`, lifted from `NovaMark`.
enum Mark {
    static let barWidth = 0.28
    static let barHeight = 0.52
    static let spacing = 0.22
    static let cornerRadius = 0.16

    /// Both bars and the gap between them.
    static var totalWidth: Double { barWidth * 2 + spacing }
}

/// How much of the canvas the mark spans. Icons are read at 60pt on a home
/// screen, so the mark is given room rather than filling the tile.
let markSpan = 0.52

let side = 1024.0
let scale = side * markSpan / Mark.totalWidth

let barW = Mark.barWidth * scale
let barH = Mark.barHeight * scale
let gap = Mark.spacing * scale
let radius = Mark.cornerRadius * scale

// -- colours ------------------------------------------------------------------

/// The system blue `Color.accentColor` resolves to, which is what the launch
/// screen draws the bars in.
let accent = CGColor(red: 0.0, green: 0.478, blue: 1.0, alpha: 1.0)
let groundTop = CGColor(red: 0.075, green: 0.094, blue: 0.145, alpha: 1.0)
let groundBottom = CGColor(red: 0.027, green: 0.035, blue: 0.063, alpha: 1.0)

// -- draw ----------------------------------------------------------------------

guard
    let context = CGContext(
        data: nil,
        width: Int(side),
        height: Int(side),
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpace(name: CGColorSpace.sRGB)!,
        // No alpha: the App Store rejects an icon with transparency, and a
        // home screen composites it onto nothing anyway.
        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    )
else {
    FileHandle.standardError.write(Data("could not create a bitmap context\n".utf8))
    exit(1)
}

// A very slight vertical gradient, so the tile has depth without reading as
// a photograph of anything.
let space = CGColorSpace(name: CGColorSpace.sRGB)!
if let ground = CGGradient(
    colorsSpace: space, colors: [groundTop, groundBottom] as CFArray, locations: [0, 1]
) {
    context.drawLinearGradient(
        ground,
        start: CGPoint(x: 0, y: side),
        end: CGPoint(x: 0, y: 0),
        options: []
    )
}

let totalW = barW * 2 + gap
let left = (side - totalW) / 2
let bottom = (side - barH) / 2

for x in [left, left + barW + gap] {
    let rect = CGRect(x: x, y: bottom, width: barW, height: barH)
    let path = CGPath(
        roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil
    )

    // The glow is what makes them read as lit rather than painted. Drawn as
    // a shadow under the same path, so it follows the rounding.
    context.saveGState()
    context.setShadow(offset: .zero, blur: barW * 0.42, color: accent.copy(alpha: 0.55))
    context.setFillColor(accent)
    context.addPath(path)
    context.fillPath()
    context.restoreGState()
}

// -- write --------------------------------------------------------------------

let output = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "NOVA/Assets.xcassets/AppIcon.appiconset/icon-1024.png"

guard let image = context.makeImage() else {
    FileHandle.standardError.write(Data("could not render\n".utf8))
    exit(1)
}

let rep = NSBitmapImageRep(cgImage: image)
guard let png = rep.representation(using: .png, properties: [:]) else {
    FileHandle.standardError.write(Data("could not encode a PNG\n".utf8))
    exit(1)
}

try FileManager.default.createDirectory(
    atPath: (output as NSString).deletingLastPathComponent,
    withIntermediateDirectories: true
)
try png.write(to: URL(fileURLWithPath: output))
print("wrote \(output) — \(Int(side))x\(Int(side))")
