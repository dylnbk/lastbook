import Foundation
import CryptoKit

/// What every Lastbook zip install shares (desktop engine/setup/install.py): the member checks done before anything is
/// written, the resumable unpack into the work folder (<name>.partial, renamed when whole), and file helpers.
enum ZipInstall {
    static let dataName = "LastbookData"
    static let slack = 1.02                     // install.SLACK

    static func workFolder(_ root: URL) -> URL {
        root.deletingLastPathComponent().appendingPathComponent(root.lastPathComponent + ".setup")
    }

    /// The work folder sits next to the library. A library picked in Files may be in a folder Lastbook cannot write to.
    static func ensureWorkFolder(_ root: URL) throws -> URL {
        let work = workFolder(root)
        do {
            try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
            let probe = work.appendingPathComponent(".write-test-\(UUID().uuidString)")
            try Data().write(to: probe)
            try FileManager.default.removeItem(at: probe)
        } catch {
            throw InstallError.folder("Lastbook can’t save next to this library (in “\(root.deletingLastPathComponent().lastPathComponent)”). "
                                      + "Put the library in \(Device.files) › Lastbook, or install the download there instead.")
        }
        return work
    }

    // ------------------------------------------------------------------ member checks ----
    /// The parts of a member's name under LastbookData, or a refusal. install._member_parts.
    static func memberParts(_ e: ZipEntry, topOptional: Bool) throws -> [String] {
        let name = e.name
        let short = String(name.prefix(80))
        if name.contains("\\") || name.contains("\u{0}") { throw InstallError.refused("The pack holds a file with a name Lastbook does not accept (\(short)).") }
        let raw = name.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        if name.hasPrefix("/") || (raw.first.map { $0.contains(":") } ?? false) {
            throw InstallError.refused("The pack holds a file with an absolute path (\(short)).")
        }
        var parts = raw.filter { $0 != "" && $0 != "." }
        if parts.contains("..") { throw InstallError.refused("The pack holds a file that would land outside the library (\(short)).") }
        if let f = parts.first, f.contains(":") { throw InstallError.refused("The pack holds a file with an absolute path (\(short)).") }
        if topOptional && !parts.isEmpty && parts[0] != dataName { parts = [dataName] + parts }
        if parts.isEmpty || parts[0] != dataName { throw InstallError.refused("The pack holds a file outside its LastbookData folder (\(short)).") }
        let t = e.unixType
        if t != 0 && t != 0o100000 && t != 0o040000 { throw InstallError.refused("The pack holds a link or a special file (\(short)).") }
        if e.isEncrypted { throw InstallError.refused("The pack is encrypted.") }
        return parts
    }

    typealias Members = [(entry: ZipEntry, parts: [String])]

    /// Every member checked before anything is written. install.check_zip. `allowed(rel, isDir)` narrows what a zip may hold
    /// (maps, address indexes, video copies); with it the LastbookData top folder is optional.
    static func checkZip(_ z: ZipReader, installedBytes: Int64?, allowed: ((String, Bool) -> Bool)? = nil,
                         notAllowed: (String) -> InstallError = { InstallError.notAMap("The map holds a file that is not part of a map (\($0)).") })
        throws -> (Members, Int64) {
        var members: Members = []
        var total: Int64 = 0
        var seen = Set<String>()
        for e in z.entries {
            let parts = try memberParts(e, topOptional: allowed != nil)
            if let allowed, !allowed(parts.dropFirst().joined(separator: "/"), e.isDirectory) { throw notAllowed(String(e.name.prefix(80))) }
            let key = parts.joined(separator: "/").lowercased()   // names differing only in case would overwrite each other
            if seen.contains(key) && !e.isDirectory { throw InstallError.refused("The pack holds the same file twice (\(e.name.prefix(80))).") }
            seen.insert(key)
            if e.method != 0 && e.method != 8 { throw InstallError.refused("The pack uses a kind of compression Lastbook does not accept (\(e.name.prefix(80))).") }
            if e.method == 0 && e.compressedSize != e.size { throw InstallError.refused("The pack holds a stored file whose sizes disagree (\(e.name.prefix(80))).") }
            total += Int64(clamping: e.size)
            members.append((e, parts))
        }
        // No two entries may share bytes: each local header (at least 30 bytes) and its data end before the next begins.
        let spans = z.entries.sorted { $0.headerOffset < $1.headerOffset }
        for (i, a) in spans.enumerated() {
            let limit = i + 1 < spans.count ? spans[i + 1].headerOffset : z.centralDirectoryOffset
            let (end, over) = a.headerOffset.addingReportingOverflow(30 &+ a.compressedSize)
            if over || a.compressedSize > UInt64.max - 30 || end > limit { throw InstallError.refused("The pack holds entries that overlap (\(a.name.prefix(80))).") }
        }
        if let ib = installedBytes, Double(total) > Double(ib) * slack + 1024 * 1024 {
            throw InstallError.refused("The pack unpacks to \(total) bytes, more than the \(ib) it should.")
        }
        return (members, total)
    }

    static func open(_ zip: URL, what: String) throws -> ZipReader {
        do { return try ZipReader(url: zip) } catch {
            throw InstallError.damaged("The \(what) could not be opened as a zip file (\(error)).")
        }
    }

    // ------------------------------------------------------------------ unpacking ----
    /// Unpacks `members` into <into>/LastbookData, resumably; `skip(rel)` leaves a member out (for example video copies an
    /// iPad that plays AV1 does not need). A staging folder holding files that are not this zip's is emptied first.
    /// Refuses before writing when the disk lacks what is still to write plus `margin`. Returns <into>/LastbookData.
    static func unpack(_ z: ZipReader, _ members: Members, total: Int64, into: URL, margin: Int64, what: String,
                       skip: (String) -> Bool = { _ in false },
                       progress: (Int64, Int64) -> Void, isCancelled: () -> Bool) throws -> URL {
        let fm = FileManager.default
        let lib = into.appendingPathComponent(dataName)
        let wanted = members.filter { !skip($0.parts.dropFirst().joined(separator: "/")) }
        let total = wanted.reduce(Int64(0)) { $0 + ($1.entry.isDirectory ? 0 : Int64(clamping: $1.entry.size)) }
        let mine = Set(wanted.map { $0.parts.dropFirst().joined(separator: "/") })
        if fm.fileExists(atPath: into.path) {
            var stale = !fm.fileExists(atPath: lib.path)
            if !stale, let en = fm.enumerator(at: lib, includingPropertiesForKeys: nil) {
                let basePath = lib.standardizedFileURL.path
                for case let u as URL in en where isFile(u) {
                    let rel = String(u.standardizedFileURL.path.dropFirst(basePath.count + 1))
                    if !mine.contains(rel.hasSuffix(".partial") ? String(rel.dropLast(8)) : rel) { stale = true; break }
                }
            }
            if stale { try? fm.removeItem(at: into) }
        }
        do { try fm.createDirectory(at: lib, withIntermediateDirectories: true) } catch {
            throw InstallError.folder("Lastbook cannot save next to the library (\(error.localizedDescription)).")
        }
        var toWrite: Int64 = 0
        for (e, parts) in wanted where !e.isDirectory {
            let dest = lib.appendingPathComponent(parts.dropFirst().joined(separator: "/"))
            if let a = try? fm.attributesOfItem(atPath: dest.path), (a[.size] as? NSNumber)?.uint64Value == e.size { continue }
            toWrite += Int64(clamping: e.size)
        }
        if let free = freeSpace(into), toWrite + margin > free { throw InstallError.noSpace(need: toWrite + margin, available: free) }

        let base = into.standardizedFileURL.resolvingSymlinksInPath().path + "/"
        var done: Int64 = 0
        for (e, parts) in wanted {
            if isCancelled() { throw InstallError.cancelled }
            let dest = lib.appendingPathComponent(parts.dropFirst().joined(separator: "/"))
            if e.isDirectory { try? fm.createDirectory(at: dest, withIntermediateDirectories: true); continue }
            try? fm.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
            guard dest.deletingLastPathComponent().standardizedFileURL.resolvingSymlinksInPath().path.appending("/").hasPrefix(base) else {
                throw InstallError.refused("The pack holds a file that would land outside the library.")
            }
            if let a = try? fm.attributesOfItem(atPath: dest.path) {
                if (a[.type] as? FileAttributeType) == .typeSymbolicLink { throw InstallError.refused("A link appeared in the unpack folder.") }
                if (a[.type] as? FileAttributeType) == .typeRegular, (a[.size] as? NSNumber)?.uint64Value == e.size {
                    done += Int64(clamping: e.size); progress(done, total)     // whole from an earlier run
                    continue
                }
            }
            let part = dest.deletingLastPathComponent().appendingPathComponent(dest.lastPathComponent + ".partial")
            try? fm.removeItem(at: part)
            guard fm.createFile(atPath: part.path, contents: nil), let out = try? FileHandle(forWritingTo: part) else {
                throw InstallError.folder("Lastbook cannot write the \(what)’s files (\(e.name.prefix(80))).")
            }
            do {
                try z.extract(e, to: out, progress: { n in done += Int64(n); progress(done, total) }, shouldContinue: { !isCancelled() })
                try out.close()
            } catch {
                try? out.close()
                try? fm.removeItem(at: part)
                switch error {
                case ZipError.cancelled: throw InstallError.cancelled
                case ZipError.unsupported(let m): throw InstallError.refused(m)
                case ZipError.writeFailed(let m):
                    if let free = freeSpace(into), free < 64 << 20 { throw InstallError.noSpace(need: total - done + margin, available: free) }
                    throw InstallError.folder(m)
                case let z as ZipError: throw InstallError.damaged(z.description)
                default: throw InstallError.damaged("A file in the \(what) could not be unpacked (\(e.name.prefix(80))).")
                }
            }
            do {
                if fm.fileExists(atPath: dest.path) { try fm.removeItem(at: dest) }
                try fm.moveItem(at: part, to: dest)
            } catch { throw InstallError.folder("Lastbook cannot write the \(what)’s files (\(error.localizedDescription)).") }
            if let d = e.date { try? fm.setAttributes([.modificationDate: d], ofItemAtPath: dest.path) }
        }
        return lib
    }

    /// A small member (map-set.json, pack.json) read before the unpack, so a wrong zip is refused before gigabytes are written.
    static func readSmall(_ z: ZipReader, _ e: ZipEntry, limit: UInt64 = 16 << 20) throws -> Data {
        guard e.size <= limit else { throw InstallError.damaged("\(e.name.prefix(80)) is too large.") }
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("lastbook-small-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: tmp) }
        guard FileManager.default.createFile(atPath: tmp.path, contents: nil), let h = try? FileHandle(forWritingTo: tmp) else {
            throw InstallError.folder("Lastbook cannot write a temporary file.")
        }
        do { try z.extract(e, to: h); try h.close() } catch {
            try? h.close()
            if let ze = error as? ZipError { throw InstallError.damaged(ze.description) }
            throw InstallError.damaged("\(e.name.prefix(80)) could not be read.")
        }
        return (try? Data(contentsOf: tmp)) ?? Data()
    }

    // ------------------------------------------------------------------ files ----
    static func isFile(_ u: URL) -> Bool {
        guard let a = try? FileManager.default.attributesOfItem(atPath: u.path) else { return false }
        return (a[.type] as? FileAttributeType) == .typeRegular
    }

    static func size(_ u: URL) -> Int64? {
        guard let a = try? FileManager.default.attributesOfItem(atPath: u.path), (a[.type] as? FileAttributeType) == .typeRegular else { return nil }
        return (a[.size] as? NSNumber)?.int64Value
    }

    static func treeSize(_ u: URL) -> Int64 {
        var n: Int64 = 0
        if let en = FileManager.default.enumerator(at: u, includingPropertiesForKeys: [.fileSizeKey]) {
            for case let f as URL in en { n += Int64((try? f.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0) }
        }
        return n
    }

    static func freeSpace(_ u: URL) -> Int64? {
        var p = u
        while !FileManager.default.fileExists(atPath: p.path) && p.pathComponents.count > 1 { p = p.deletingLastPathComponent() }
        let v = try? p.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeAvailableCapacityKey])
        if let n = v?.volumeAvailableCapacityForImportantUsage, n > 0 { return n }
        return v?.volumeAvailableCapacity.map(Int64.init)
    }

    /// The whole storage of the volume holding `u` (used or not).
    static func totalSpace(_ u: URL) -> Int64? {
        var p = u
        while !FileManager.default.fileExists(atPath: p.path) && p.pathComponents.count > 1 { p = p.deletingLastPathComponent() }
        return (try? p.resourceValues(forKeys: [.volumeTotalCapacityKey]))?.volumeTotalCapacity.map(Int64.init)
    }

    /// A rename (one disk), creating the destination's folder.
    static func move(_ a: URL, _ b: URL) throws {
        try FileManager.default.createDirectory(at: b.deletingLastPathComponent(), withIntermediateDirectories: true)
        if rename(a.path, b.path) != 0 { throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: a.path]) }
    }

    static func writeJSON(_ u: URL, _ d: [String: Any]) throws {
        try FileManager.default.createDirectory(at: u.deletingLastPathComponent(), withIntermediateDirectories: true)
        let tmp = u.deletingLastPathComponent().appendingPathComponent(u.lastPathComponent + ".tmp")
        try JSONSerialization.data(withJSONObject: d, options: [.prettyPrinted, .sortedKeys]).write(to: tmp)
        if rename(tmp.path, u.path) != 0 { throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: u.path]) }
    }

    static func readJSON(_ u: URL) -> [String: Any]? {
        guard let d = FileManager.default.contents(atPath: u.path) else { return nil }
        return try? JSONSerialization.jsonObject(with: d) as? [String: Any]
    }

    static func realPath(_ p: String) -> String { URL(fileURLWithPath: p).standardizedFileURL.resolvingSymlinksInPath().path }

    /// SHA-256 of a file, read in pieces (a pack zip is gigabytes). progress(bytes done, total).
    static func sha256(_ u: URL, progress: (Int64, Int64) -> Void = { _, _ in }, isCancelled: () -> Bool = { false }) throws -> String {
        guard let h = try? FileHandle(forReadingFrom: u) else { throw InstallError.damaged("The file could not be opened.") }
        defer { try? h.close() }
        let total = size(u) ?? 0
        var hasher = SHA256()
        var done: Int64 = 0
        while true {
            if isCancelled() { throw InstallError.cancelled }
            let ok: Bool = try autoreleasepool {
                guard let d = try h.read(upToCount: 8 << 20), !d.isEmpty else { return false }
                hasher.update(data: d)
                done += Int64(d.count)
                progress(done, total)
                return true
            }
            if !ok { break }
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
