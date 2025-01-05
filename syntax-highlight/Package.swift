// swift-tools-version:5.3
import PackageDescription

let package = Package(
    name: "TreeSitterMah",
    products: [
        .library(name: "TreeSitterMah", targets: ["TreeSitterMah"]),
    ],
    dependencies: [
        .package(url: "https://github.com/ChimeHQ/SwiftTreeSitter", from: "0.8.0"),
    ],
    targets: [
        .target(
            name: "TreeSitterMah",
            dependencies: [],
            path: ".",
            sources: [
                "src/parser.c",
                // NOTE: if your language has an external scanner, add it here.
            ],
            resources: [
                .copy("queries")
            ],
            publicHeadersPath: "bindings/swift",
            cSettings: [.headerSearchPath("src")]
        ),
        .testTarget(
            name: "TreeSitterMahTests",
            dependencies: [
                "SwiftTreeSitter",
                "TreeSitterMah",
            ],
            path: "bindings/swift/TreeSitterMahTests"
        )
    ],
    cLanguageStandard: .c11
)
