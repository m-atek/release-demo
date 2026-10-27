// swift-tools-version: 6.0
// Store Assistant for macOS: a small SwiftUI app that shows its version in
// large type and today's orders from orders-service. The release pipeline
// builds it with xcodebuild (or swift build where only the Command Line Tools
// are installed), wraps it in an app bundle, signs, packages and notarizes it:
// scripts/demos/desktop/mac_build.py.
import PackageDescription

let package = Package(
    name: "StoreAssistant",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "StoreAssistant", targets: ["StoreAssistant"]),
    ],
    targets: [
        // Everything the tests reach: the orders model, its decoding and the settings.
        .target(name: "StoreAssistantCore"),
        .executableTarget(name: "StoreAssistant", dependencies: ["StoreAssistantCore"]),
        .testTarget(
            name: "StoreAssistantCoreTests",
            dependencies: ["StoreAssistantCore"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
