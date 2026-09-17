import Foundation
import Testing

@testable import NOVA

/// The app icon is the launch screen's mark, and has to stay that way.
///
/// `Tools/make-appicon.swift` draws the icon from a copy of `NovaMark`'s
/// proportions, because it runs as a standalone script and cannot import the
/// app. That copy is the risk: change the mark, and the icon silently keeps
/// the old shape until somebody notices the home screen no longer matches
/// what the app shows while it loads.
///
/// These pin the numbers the script was written against. A failure here is
/// not a bug -- it means the mark moved and the icon needs regenerating:
///
///     cd apps/ios
///     swift Tools/make-appicon.swift NOVA/Assets.xcassets/AppIcon.appiconset/icon-1024.png
struct IconTests {
    @Test("The mark's proportions are the ones the icon was drawn from")
    func proportionsMatchTheGenerator() {
        #expect(NovaMark.Proportions.barWidth == 0.28)
        #expect(NovaMark.Proportions.barHeight == 0.52)
        #expect(NovaMark.Proportions.spacing == 0.22)
        #expect(NovaMark.Proportions.cornerRadius == 0.16)
    }

    @Test("The bars read as a pair rather than one wide block")
    func theGapIsVisible() {
        // Two eyes, not a letterbox. The gap is most of a bar's width, which
        // is what stops them merging at 60pt on a home screen.
        #expect(NovaMark.Proportions.spacing > NovaMark.Proportions.barWidth * 0.5)
    }

    @Test("The corner radius rounds the bars into capsules")
    func barsAreCapsules() {
        // A radius past half the width clamps, which is what gives the mark
        // its eye shape rather than a rounded rectangle. The icon generator
        // depends on this landing the same way in CoreGraphics as in SwiftUI.
        #expect(NovaMark.Proportions.cornerRadius > NovaMark.Proportions.barWidth / 2)
    }

    // Deliberately not tested here: that the icon is actually in the bundle.
    // `CFBundleIconName` lives in the app's Info.plist, and swift-testing
    // does not resolve `Bundle.main` to the host app, so the assertion fails
    // on a correct build -- a test that cannot see the thing it checks.
    //
    // It is a packaging fact, verified where packaging happens:
    //
    //     xcodebuild build -configuration Release -destination generic/platform=iOS
    //     ls "$APP"            # Assets.car, AppIcon60x60@2x.png
    //     plutil -p "$APP/Info.plist" | grep CFBundleIconName
}
