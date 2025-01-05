import XCTest
import SwiftTreeSitter
import TreeSitterMah

final class TreeSitterMahTests: XCTestCase {
    func testCanLoadGrammar() throws {
        let parser = Parser()
        let language = Language(language: tree_sitter_mah())
        XCTAssertNoThrow(try parser.setLanguage(language),
                         "Error loading Mah grammar")
    }
}
