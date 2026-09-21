#!/usr/bin/swift
import Foundation

/// Decode Xcode recent documents and get currently open projects
/// Reads bookmark data and outputs paths to .xcodeproj and .xcworkspace files
/// Also queries Xcode for currently open projects
/// Usage: swift decode_xcode_recents.swift [--include-open]

let arguments = CommandLine.arguments.dropFirst()
let includeOpen = arguments.contains("--include-open")

var allPaths: Set<String> = []

// Try .sfl4 first (newer macOS), then .sfl3 (older macOS)
func decodePlistRecents() {
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
                        allPaths.insert(path)
                    }
                } catch {
                    // Not a valid bookmark or file doesn't exist, skip
                }
            }
        }

        // Found the file, don't try the other format
        break
    }
}

// Get currently open projects via AppleScript
func getOpenProjects() -> Set<String> {
    var openPaths: Set<String> = []

    let script = """
    tell application "Xcode"
        set openProjects to {}
        repeat with doc in open documents
            try
                set docPath to path of doc
                if docPath ends with ".xcodeproj" or docPath ends with ".xcworkspace" then
                    copy docPath to the end of openProjects
                end if
            end try
        end repeat
        return openProjects
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
            // AppleScript returns items separated by commas or newlines
            let paths = output.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
            for path in paths {
                if !path.isEmpty && FileManager.default.fileExists(atPath: path) {
                    openPaths.insert(path)
                }
            }
        }
    } catch {
        // AppleScript failed, skip
    }

    return openPaths
}


// Decode recent projects from plist
decodePlistRecents()

// Add currently open projects if requested
if includeOpen {
    let openProjects = getOpenProjects()
    allPaths.formUnion(openProjects)
}

// Output paths one per line, sorted
for path in allPaths.sorted() {
    print(path)
}
