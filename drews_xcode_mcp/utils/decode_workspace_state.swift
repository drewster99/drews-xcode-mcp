#!/usr/bin/env swift
/// Reads a UserInterfaceState.xcuserstate file (NSKeyedArchiver binary plist)
/// and extracts the last-used run destination identifier for each scheme.
///
/// Output: JSON object mapping scheme names to destination identifiers.
/// Example: {"MyApp": "03A7716E-4962-4F2D-9455-E27C81883D4D_iphonesimulator_arm64"}
///
/// Usage: swift decode_workspace_state.swift <path-to-xcuserstate>

import Foundation

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: decode_workspace_state.swift <path-to-xcuserstate>\n", stderr)
    exit(1)
}

let filePath = CommandLine.arguments[1]
let url = URL(fileURLWithPath: filePath)

guard let data = try? Data(contentsOf: url) else {
    fputs("Error: Cannot read file: \(filePath)\n", stderr)
    exit(1)
}

guard let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
      let objects = plist["$objects"] as? [Any] else {
    fputs("Error: Cannot parse plist\n", stderr)
    exit(1)
}

/// Extract the integer value from a CFKeyedArchiverUID object via its description.
/// These are opaque types that can't be cast directly, but their description
/// has the format: "<CFKeyedArchiverUID ...>{value = N}"
func uidValue(_ obj: Any) -> Int? {
    let desc = "\(obj)"
    guard desc.contains("CFKeyedArchiverUID"),
          let range = desc.range(of: "value = "),
          let endRange = desc[range.upperBound...].range(of: "}") else {
        return nil
    }
    return Int(desc[range.upperBound..<endRange.lowerBound])
}

/// Return the object an archived NSDictionary maps the given key-string index to.
func value(forKeyIndex keyIndex: Int) -> Any? {
    for obj in objects {
        guard let dict = obj as? [String: Any],
              let nsKeys = dict["NS.keys"] as? [Any],
              let nsObjects = dict["NS.objects"] as? [Any],
              nsKeys.count == nsObjects.count else { continue }

        for (i, key) in nsKeys.enumerated() {
            guard let uid = uidValue(key), uid == keyIndex,
                  let valueUid = uidValue(nsObjects[i]),
                  valueUid < objects.count else { continue }
            return objects[valueUid]
        }
    }
    return nil
}

func index(ofString wanted: String) -> Int? {
    for (i, obj) in objects.enumerated() {
        if let s = obj as? String, s == wanted { return i }
    }
    return nil
}

/// Resolve an archived string, which may be stored directly or wrapped.
func stringValue(_ obj: Any?) -> String? {
    if let s = obj as? String { return s }
    if let dict = obj as? [String: Any], let s = dict["NS.string"] as? String { return s }
    return nil
}

/// Read an archived dictionary that maps scheme names to strings.
func stringMap(_ obj: Any?) -> [String: String] {
    guard let dict = obj as? [String: Any],
          let keys = dict["NS.keys"] as? [Any],
          let values = dict["NS.objects"] as? [Any],
          keys.count == values.count else { return [:] }

    var result: [String: String] = [:]
    for (i, key) in keys.enumerated() {
        guard let keyUid = uidValue(key), keyUid < objects.count,
              let name = objects[keyUid] as? String,
              let valueUid = uidValue(values[i]), valueUid < objects.count,
              let value = stringValue(objects[valueUid]) else { continue }
        result[name] = value
    }
    return result
}

var destinations: [String: String] = [:]
if let keyIndex = index(ofString: "IDERunContextRecentsLastUsedRunDestinationBySchemeKey") {
    destinations = stringMap(value(forKeyIndex: keyIndex))
}

// The selected scheme is archived as an object carrying its name, so its
// IDENameString has to be read back out of it.
var activeScheme = ""
if let schemeKeyIndex = index(ofString: "ActiveScheme"),
   let nameKeyIndex = index(ofString: "IDENameString"),
   let scheme = value(forKeyIndex: schemeKeyIndex) as? [String: Any],
   let keys = scheme["NS.keys"] as? [Any],
   let values = scheme["NS.objects"] as? [Any],
   keys.count == values.count {
    for (i, key) in keys.enumerated() {
        guard let uid = uidValue(key), uid == nameKeyIndex,
              let valueUid = uidValue(values[i]), valueUid < objects.count,
              let name = stringValue(objects[valueUid]) else { continue }
        activeScheme = name
        break
    }
}

let output: [String: Any] = [
    "activeScheme": activeScheme,
    "destinationsByScheme": destinations,
]
if let jsonData = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]),
   let jsonString = String(data: jsonData, encoding: .utf8) {
    print(jsonString)
} else {
    print("{}")
}
