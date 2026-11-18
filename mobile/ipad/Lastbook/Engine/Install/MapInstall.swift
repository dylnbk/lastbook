import Foundation

/// Installing a state map set from its zip (desktop: engine/setup/install.py, the map parts of api.py; docs/SETUP-API.md,
/// "State maps"). A map set replaces the library's map set; it never merges with it.
///
///   <parent>/LastbookData                                  the library
///   <parent>/LastbookData.setup/unpack-map/LastbookData    the new map set, built here from the zip (stage)
///   <parent>/LastbookData.setup/map-old/                   the old set's files (with -wal/-shm) after the swap (activate)
///   <settings>/map-pending.json                            the switch in progress: never inside LastbookData.setup
///
/// Every member is checked before anything is written. The swap is renames only, on one disk; if the result does not
/// check out (every file map-set.json lists, at its size, and the library's catalog) the old set is put back.
/// The work folder goes once the library has opened with the new set (cleanupAfterOpen).
enum InstallError: Error, CustomStringConvertible {
    case notAMap(String)                    // a file that is not part of a map, or no map-set.json
    case wrongState(found: String, expected: String)
    case refused(String)                    // an unsafe zip: names, links, encryption, duplicates, overlap, too large
    case damaged(String)                    // not a zip, CRC or size wrong, truncated
    case noSpace(need: Int64, available: Int64)
    case activate(String)                   // the swap could not be done; what was there is back
    case folder(String)                     // Lastbook cannot write where it needs to
    case unknownDownload(String)            // not a file Lastbook's download list knows, or its SHA-256 differs
    case notNeeded(String)                  // nothing to do (for example H.264 copies on an iPad that plays AV1)
    case network(String)                    // offline, the server refused, or the connection could not be trusted
    case needsConfirmation(String)          // ask the person first (a state map would replace All's whole-US map)
    case cancelled

    /// Desktop's error code for the same failure (Refused.code / the job's error code).
    var code: String {
        switch self {
        case .notAMap, .wrongState, .refused, .damaged: return "unpack"
        case .noSpace: return "space"
        case .activate: return "activate"
        case .folder: return "folder"
        case .unknownDownload: return "checksum"
        case .notNeeded: return "nothing"
        case .network: return "network"
        case .needsConfirmation: return "confirm"
        case .cancelled: return "cancelled"
        }
    }

    var description: String {
        switch self {
        case .notAMap(let m), .refused(let m), .damaged(let m), .activate(let m), .folder(let m), .unknownDownload(let m), .notNeeded(let m), .network(let m), .needsConfirmation(let m): return m
        case .wrongState(let f, let e): return "The map file says it is \(f), not \(e)."
        case .noSpace(let n, let a):
            let f = ByteCountFormatter()
            return "There is not enough room: this needs \(f.string(fromByteCount: n)) free and the \(Device.name) has \(f.string(fromByteCount: a)). Free some space, then try again."
        case .cancelled: return "Cancelled."
        }
    }
}

enum MapInstall {
    static let dataName = ZipInstall.dataName
    static let spaceMargin: Int64 = 2_000_000_000   // install.SPACE_MARGIN
    static let mapFixed: Set<String> = ["map-set.json", "derived/geo/national-status.json", "derived/geo/socal-cells.json",
                                        "library-data/geography/resources/atlas.sqlite"]
    static let mapDirs: Set<String> = ["", "derived", "derived/geo", "library-data", "library-data/geography", "library-data/geography/resources"]
    static let sqliteSides = ["-wal", "-shm", "-journal"]
    /// Posted after a map set was switched in (or put back). Route groups holding per-library caches should drop them.
    static let didChange = Notification.Name("LastbookMapSetChanged")

    struct MapSet { let name: String?; let files: [String: Int64] }

    struct Staged {
        let root: URL           // the library (LastbookData)
        let work: URL           // LastbookData.setup
        let newLib: URL         // LastbookData.setup/unpack-map/LastbookData
        let name: String        // map-set.json's name
        let bytes: Int64        // unpacked size
    }

    enum Outcome: Equatable {
        case switched           // the new set is in place and checks out
        case restored(String)   // the new set did not check out (or the swap was interrupted): the old one is back
    }

    /// For the Mac test tool only: make the swap fail after `after` renames in `phase` ("out" or "in"). With `crash`,
    /// the swap stops dead as if the app were killed (no rollback; recoverIfNeeded must repair it).
    struct FailPoint { let phase: String; let after: Int; let crash: Bool }
    nonisolated(unsafe) static var testFailPoint: FailPoint?
    struct SimulatedCrash: Error {}

    // ------------------------------------------------------------------ places ----
    static func workFolder(_ root: URL) -> URL { ZipInstall.workFolder(root) }

    /// The app's own settings folder: Application Support/Lastbook/setup (never inside the library or its work folder).
    static var defaultSettings: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Lastbook/setup")
    }

    static func pendingFile(_ settings: URL) -> URL { settings.appendingPathComponent("map-pending.json") }

    // ------------------------------------------------------------------ rules ----
    /// Is this path (under LastbookData) one a state map zip may hold? install.map_member.
    static func mapMember(_ rel0: String, isDir: Bool = false) -> Bool {
        let rel = rel0.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        if isDir { return mapDirs.contains(rel) }
        if mapFixed.contains(rel) { return true }
        guard rel.hasPrefix("derived/geo/"), rel.hasSuffix(".sqlite") else { return false }
        let name = rel.dropFirst("derived/geo/".count).dropLast(".sqlite".count)
        return !name.isEmpty && name.unicodeScalars.allSatisfy { $0.isASCII && (CharacterSet.alphanumerics.contains($0) || "._-".unicodeScalars.contains($0)) }
    }

    static func isLibrary(_ lib: URL) -> Bool {
        var d: ObjCBool = false
        return FileManager.default.fileExists(atPath: lib.appendingPathComponent("library-data/catalog.json").path, isDirectory: &d) && !d.boolValue
    }

    // ------------------------------------------------------------------ map-set.json ----
    static func mapSet(_ lib: URL) -> MapSet? {
        guard let d = FileManager.default.contents(atPath: lib.appendingPathComponent("map-set.json").path) else { return nil }
        return parseMapSet(d)
    }

    static func parseMapSet(_ d: Data) -> MapSet? {
        guard let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let f = o["files"] as? [String: Any] else { return nil }
        var files: [String: Int64] = [:]
        for (k, v) in f { files[k] = (v as? NSNumber)?.int64Value ?? -1 }
        let name: String? = o["name"].flatMap { v -> String? in
            if v is NSNull { return nil }
            if let s = v as? String { return s.isEmpty ? nil : s }
            if let n = v as? NSNumber { return n == 0 ? nil : n.stringValue }
            return "\(v)"
        }
        return MapSet(name: name, files: files)
    }

    /// 'base', 'us' or a state code: map-set.json's name, else pack.json's map, else 'base'. install.map_name.
    static func mapName(_ lib: URL) -> String {
        if let m = mapSet(lib), let n = m.name { return n }
        if let d = FileManager.default.contents(atPath: lib.appendingPathComponent("pack.json").path),
           let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let m = o["map"], !(m is NSNull) {
            let s = m as? String ?? "\(m)"
            if !s.isEmpty { return s }
        }
        return "base"
    }

    /// Every file map-set.json lists is there at its size, and each is a map file. install.check_map.
    @discardableResult
    static func checkMap(_ lib: URL) throws -> MapSet {
        guard let d = mapSet(lib) else { throw InstallError.notAMap("The map has no map-set.json in it.") }
        for (rel, size) in d.files {
            if !mapMember(rel) || rel.split(separator: "/").contains("..") {
                throw InstallError.notAMap("The map lists a file that is not part of a map (\(rel.prefix(80))).")
            }
            let p = lib.appendingPathComponent(rel).path
            guard let a = try? FileManager.default.attributesOfItem(atPath: p), (a[.type] as? FileAttributeType) == .typeRegular,
                  (a[.size] as? NSNumber)?.int64Value == size else {
                throw InstallError.damaged("The map’s \(rel) is missing or the wrong size.")
            }
        }
        return d
    }

    static func isFile(_ u: URL) -> Bool { ZipInstall.isFile(u) }

    /// The files of a library's map set as they are on disk, with SQLite's side files and map-set.json. install.map_files.
    static func mapFiles(_ lib: URL) -> [String] {
        var rels = Set<String>()
        if let d = mapSet(lib) { rels.formUnion(d.files.keys.filter { mapMember($0) && !$0.split(separator: "/").contains("..") }) }
        let geo = lib.appendingPathComponent("derived/geo")
        for n in (try? FileManager.default.contentsOfDirectory(atPath: geo.path)) ?? [] {
            let rel = "derived/geo/" + n
            if isFile(geo.appendingPathComponent(n)) && mapMember(rel) { rels.insert(rel) }
        }
        rels.formUnion(mapFixed.filter { isFile(lib.appendingPathComponent($0)) })
        var out = Set<String>()
        for r in rels {
            for side in [""] + (r.hasSuffix(".sqlite") ? sqliteSides : []) where isFile(lib.appendingPathComponent(r + side)) { out.insert(r + side) }
        }
        return out.sorted { $0.unicodeScalars.lexicographicallyPrecedes($1.unicodeScalars) }
    }

    // ------------------------------------------------------------------ stage ----
    /// Checks the zip, then unpacks it into LastbookData.setup/unpack-map/LastbookData. Resumable: a file already whole
    /// from an earlier run is kept. Throws InstallError; on failure the library itself has not been touched.
    static func stage(zip: URL, root: URL, expectedID: String?, installedBytes: Int64? = nil,
                      progress: (Int64, Int64) -> Void = { _, _ in }, isCancelled: () -> Bool = { false }) throws -> Staged {
        let z = try ZipInstall.open(zip, what: "map")
        let (members, total) = try ZipInstall.checkZip(z, installedBytes: installedBytes, allowed: { mapMember($0, isDir: $1) })
        // map-set.json first (small): a wrong state is refused before gigabytes are written
        guard let msEntry = members.first(where: { $0.parts.dropFirst().joined(separator: "/") == "map-set.json" && !$0.entry.isDirectory })?.entry else {
            throw InstallError.notAMap("The map has no map-set.json in it.")
        }
        guard let ms = parseMapSet(try ZipInstall.readSmall(z, msEntry)) else { throw InstallError.notAMap("The map has no map-set.json in it.") }
        if let exp = expectedID {
            let found = ms.name ?? "base"
            if found != exp { throw InstallError.wrongState(found: found, expected: exp) }
        }
        let work = try ZipInstall.ensureWorkFolder(root)
        let lib = try ZipInstall.unpack(z, members, total: total, into: work.appendingPathComponent("unpack-map"), margin: spaceMargin, what: "map",
                                        progress: progress, isCancelled: isCancelled)
        try checkMap(lib)
        let name = mapName(lib)
        if let exp = expectedID, name != exp { throw InstallError.wrongState(found: name, expected: exp) }
        return Staged(root: root, work: work, newLib: lib, name: name, bytes: total)
    }

    // ------------------------------------------------------------------ the switch ----
    private static func writeJSON(_ u: URL, _ d: [String: Any]) throws { try ZipInstall.writeJSON(u, d) }
    private static func move(_ a: URL, _ b: URL) throws { try ZipInstall.move(a, b) }

    private static func failPoint(_ phase: String, _ n: Int) throws {
        guard let f = testFailPoint, f.phase == phase, n >= f.after else { return }
        if f.crash { throw SimulatedCrash() }
        throw CocoaError(.fileWriteUnknown)
    }

    /// The old set's files go to map-old, the new set's come in. Renames only. On a failure what moved is put back.
    /// install.swap_map, with the phase recorded in the pending file so an interrupted swap can be undone at launch.
    static func swapMap(_ lib: URL, _ new: URL, _ oldDir: URL, pending: URL, record: [String: Any]) throws {
        try checkMap(new)
        var movedOut: [String] = [], movedIn: [String] = []
        var rec = record
        do {
            rec["phase"] = "out"; try writeJSON(pending, rec)
            for rel in mapFiles(lib) {
                try failPoint("out", movedOut.count)
                try move(lib.appendingPathComponent(rel), oldDir.appendingPathComponent(rel)); movedOut.append(rel)
            }
            rec["phase"] = "in"; try writeJSON(pending, rec)
            for rel in mapFiles(new) {
                try failPoint("in", movedIn.count)
                try move(new.appendingPathComponent(rel), lib.appendingPathComponent(rel)); movedIn.append(rel)
            }
        } catch is SimulatedCrash {
            throw SimulatedCrash()
        } catch {
            for rel in movedIn { try? move(lib.appendingPathComponent(rel), new.appendingPathComponent(rel)) }
            for rel in movedOut { try? move(oldDir.appendingPathComponent(rel), lib.appendingPathComponent(rel)) }
            throw InstallError.activate("Lastbook could not put the new map in place (\(type(of: error))).")
        }
    }

    /// Puts every file under map-old back into the library, the failed new set's files going to map-failed first.
    private static func putOldBack(_ lib: URL, work: URL) {
        let fm = FileManager.default
        let oldDir = work.appendingPathComponent("map-old"), broken = work.appendingPathComponent("map-failed")
        guard fm.fileExists(atPath: oldDir.path) else { return }
        try? fm.removeItem(at: broken)
        for rel in mapFiles(lib) { try? move(lib.appendingPathComponent(rel), broken.appendingPathComponent(rel)) }
        if let en = fm.enumerator(at: oldDir, includingPropertiesForKeys: nil) {
            var files: [URL] = []
            for case let u as URL in en where isFile(u) { files.append(u) }
            let base = oldDir.standardizedFileURL.path
            for u in files {
                let rel = String(u.standardizedFileURL.path.dropFirst(base.count + 1))
                try? move(u, lib.appendingPathComponent(rel))
            }
        }
    }

    private static func libraryChanged() {
        DBCache.shared.reset()
        Catalog.shared.reset()
        NotificationCenter.default.post(name: didChange, object: nil)
    }

    /// Switches the staged set in now (the iPad closes its databases first rather than restarting). install._apply_map:
    /// swap, then check the result; if it does not check out, the old set goes back. The pending file stays until
    /// cleanupAfterOpen. Throws InstallError.activate when the swap itself could not be done (nothing changed).
    @discardableResult
    static func activate(_ s: Staged, settings: URL = defaultSettings) throws -> Outcome {
        let pending = pendingFile(settings)
        let oldDir = s.work.appendingPathComponent("map-old")
        if FileManager.default.fileExists(atPath: oldDir.path) {
            // a set from an earlier switch that never confirmed: the library opened since, so it is no longer needed
            try? FileManager.default.removeItem(at: oldDir)
        }
        let rec: [String: Any] = ["kind": "map", "new": s.newLib.path, "target": s.root.path, "written": Date().timeIntervalSince1970, "switched": false]
        DBCache.shared.reset()       // nothing keeps the old files open while they move
        do {
            try swapMap(s.root, s.newLib, oldDir, pending: pending, record: rec)
        } catch let e as InstallError {
            try? FileManager.default.removeItem(at: pending)
            libraryChanged()
            throw e
        }
        var done = rec; done["switched"] = true; done["phase"] = "done"
        try? writeJSON(pending, done)
        let outcome = verifyOrRestore(s.root, work: s.work)
        libraryChanged()
        return outcome
    }

    private static func verifyOrRestore(_ root: URL, work: URL) -> Outcome {
        let ok = ((try? checkMap(root)) != nil) && isLibrary(root)
        if ok { return .switched }
        putOldBack(root, work: work)
        return .restored("The new map did not open, so Lastbook put the old one back.")
    }

    /// Called at launch before the library is opened. If a switch was interrupted (the app was stopped mid-swap), the old
    /// set is put back; if it finished but the library does not check out, the old set is put back too. A pending file
    /// that is not exactly what activate writes for this library is removed, not obeyed. Returns what happened, or nil.
    @discardableResult
    static func recoverIfNeeded(root: URL, settings: URL = defaultSettings) -> Outcome? {
        let pending = pendingFile(settings)
        guard let data = FileManager.default.contents(atPath: pending.path) else { return nil }
        let work = workFolder(root)
        func real(_ p: String) -> String { URL(fileURLWithPath: p).standardizedFileURL.resolvingSymlinksInPath().path }
        guard let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any], d["kind"] as? String == "map",
              let target = d["target"] as? String, let new = d["new"] as? String,
              real(target) == real(root.path), real(new) == real(work.appendingPathComponent("unpack-map/\(dataName)").path) else {
            try? FileManager.default.removeItem(at: pending)
            return nil
        }
        let newLib = URL(fileURLWithPath: new)
        let oldDir = work.appendingPathComponent("map-old")
        let outcome: Outcome
        if d["switched"] as? Bool == true {
            outcome = verifyOrRestore(root, work: work)
        } else {
            // Interrupted mid-swap. "out": old files partly in map-old, none of the new moved. "in": every old file is in
            // map-old, some new ones in the library: those go back to the staged set first.
            if d["phase"] as? String == "in" {
                for rel in mapFiles(root) where !FileManager.default.fileExists(atPath: newLib.appendingPathComponent(rel).path) {
                    try? move(root.appendingPathComponent(rel), newLib.appendingPathComponent(rel))
                }
            }
            if let en = FileManager.default.enumerator(at: oldDir, includingPropertiesForKeys: nil) {
                var files: [URL] = []
                for case let u as URL in en where isFile(u) { files.append(u) }
                let base = oldDir.standardizedFileURL.path
                for u in files {
                    let rel = String(u.standardizedFileURL.path.dropFirst(base.count + 1))
                    if !FileManager.default.fileExists(atPath: root.appendingPathComponent(rel).path) { try? move(u, root.appendingPathComponent(rel)) }
                }
            }
            try? FileManager.default.removeItem(at: oldDir)
            try? FileManager.default.removeItem(at: pending)
            outcome = .restored("The map switch was interrupted, so Lastbook put the old one back.")
        }
        libraryChanged()
        return outcome
    }

    /// The library opened: the switch is done. The pending file and the work folder (map-old, map-failed, the staging) go.
    /// (api.py after apply_pending: confirm(), then the work folder is removed.)
    static func cleanupAfterOpen(root: URL, settings: URL = defaultSettings) {
        let pending = pendingFile(settings)
        guard FileManager.default.fileExists(atPath: pending.path) else { return }
        try? FileManager.default.removeItem(at: pending)
        let work = workFolder(root)
        for sub in ["map-old", "map-failed", "unpack-map"] { try? FileManager.default.removeItem(at: work.appendingPathComponent(sub)) }
        if let rest = try? FileManager.default.contentsOfDirectory(atPath: work.path), rest.isEmpty { try? FileManager.default.removeItem(at: work) }
    }

    /// The installed set's name, for the setup screen (state.current.map).
    static func currentMap(_ root: URL) -> String { mapName(root) }
}
