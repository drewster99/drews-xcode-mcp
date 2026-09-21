#!/usr/bin/swift
import Foundation

/// Decode Xcode recent documents and get currently open projects.
/// Reads bookmark data for recents and queries Xcode's open workspace documents
/// via AppleScript. Outputs paths to .xcodeproj and .xcworkspace files, one per
/// line, prefixed "OPEN:" for currently open projects and "RECENT:" for
/// projects from Xcode's recents list that are not currently open.
/// Usage: swift decode_xcode_recents.swift [--include-open]

let arguments = CommandLine.arguments.dropFirst()
let includeOpen = arguments.contains("--include-open")

var recentPaths: Set<String> = []

// Try .sfl4 first (newer macOS), then .sfl3 (older macOS)
func decodePlistRecents() -> Set<String> {
    var paths: Set<String> = []
    let basePath = "~/Library/Application Support/com.apple.sharedfilelist/com.apple.LSSharedFileList.ApplicationRecentDocuments/com.apple.dt.xcode"

    for ext in ["sfl4", "sfl3"] {
        let plistPath = NSString(string: "\(basePath).\(ext)").expandingTildeInPath

        guard FileManager.default.fileExists(atPath: plistPath) else {
            continue
        }

        guard let plistData = try? Data(contentsOf: URL(fileURLWithPath: plistPath)),
              let plist = try? PropertyListSerialization.propertyList(from: plistData, format: nil) as? [String: Any],
              let objects = plist["$objects"] as? [Any] else {
            continue
        }

        for obj in objects {
            if let bookmarkData = obj as? Data {
                do {
                    var isStale = false
                    let url = try URL(resolvingBookmarkData: bookmarkData, bookmarkDataIsStale: &isStale)
                    let path = url.path

                    // Only include .xcodeproj and .xcworkspace files
                    if (path.hasSuffix(".xcodeproj") || path.hasSuffix(".xcworkspace")) &&
                       FileManager.default.fileExists(atPath: path) {
                        paths.insert(path)
                    }
                } catch {
                    // Not a valid bookmark or file doesn't exist, skip
                }
            }
        }

        // Found the file, don't try the other format
        break
    }

    return paths
}

// Get currently open projects via AppleScript. `workspace documents` (plural,
// top-level) is the correct vocabulary -- Xcode's dictionary has no `open
// documents` property, and includes .xcfilescontainer entries alongside real
// projects, so filter to just .xcodeproj/.xcworkspace paths.
//
// Guarded by an `is running` check first: a bare `tell application "Xcode"`
// launches Xcode via Launch Services if it isn't already running, which is
// not an acceptable side effect for what is otherwise a read-only listing.
func getOpenProjects() -> Set<String> {
    var openPaths: Set<String> = []

    let script = """
    tell application "System Events"
        set xcodeRunning to (name of processes) contains "Xcode"
    end tell
    if not xcodeRunning then return ""
    tell application "Xcode"
        set docPaths to path of workspace documents
        set AppleScript's text item delimiters to "\\n"
        set output to docPaths as string
        set AppleScript's text item delimiters to ""
        return output
    end tell
    """

    let task = Process()
    task.launchPath = "/usr/bin/osascript"
    task.arguments = ["-e", script]

    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = Pipe()

    do {
        try task.run()
        task.waitUntilExit()

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        if let output = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !output.isEmpty {
            let paths = output.split(separator: "\n").map { String($0).trimmingCharacters(in: .whitespaces) }
            for path in paths {
                if (path.hasSuffix(".xcodeproj") || path.hasSuffix(".xcworkspace")) &&
                   FileManager.default.fileExists(atPath: path) {
                    openPaths.insert(path)
                }
            }
        }
    } catch {
        // AppleScript failed, skip
    }

    return openPaths
}

recentPaths = decodePlistRecents()

var openPaths: Set<String> = []
if includeOpen {
    openPaths = getOpenProjects()
}

// Recents that are also open are reported only under OPEN.
for path in openPaths.sorted() {
    print("OPEN:\(path)")
}
for path in recentPaths.subtracting(openPaths).sorted() {
    print("RECENT:\(path)")
}
