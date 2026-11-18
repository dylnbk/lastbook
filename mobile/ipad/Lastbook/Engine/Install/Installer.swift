import Foundation

/// One entry point for the install screen: a zip the person chose in Files is recognised by what it holds (pack, state map,
/// address index, video copies), checked against the download list (name, size, SHA-256, as desktop setup checks a found
/// file), then installed. Also the launch hooks that finish or undo an interrupted switch.
enum Installer {
    enum Kind: String { case pack, map, addresses, videoCopies }
    enum Phase: String { case checking = "Checking the download", unpacking = "Unpacking", switching = "Putting it in place" }
    struct Report { let kind: Kind; let title: String; let detail: String }

    /// The library an imported pack becomes (On My iPad › Lastbook › LastbookData).
    static var packTarget: URL { packTargetOverride ?? Library.inApp }
    /// Mac test tool only: never ~/Documents on a Mac.
    nonisolated(unsafe) static var packTargetOverride: URL?
    /// Mac test tool only: pretend this device does (not) need the H.264 copies.
    nonisolated(unsafe) static var needsH264Override: Bool?
    static var needsH264: Bool { needsH264Override ?? VideoCompat.shared.needsH264 }

    /// What a zip is, from its own directory.
    static func kind(of z: ZipReader) -> (Kind, String?)? {
        var names = z.entries.map { e -> String in
            let p = e.name.split(separator: "/").filter { $0 != "." }.map(String.init)
            return (p.first == ZipInstall.dataName ? p.dropFirst() : p[...]).joined(separator: "/")
        }
        names = names.filter { !$0.isEmpty }
        if z.entries.contains(where: { $0.name == "\(ZipInstall.dataName)/pack.json" }) { return (.pack, nil) }
        if names.contains("map-set.json") { return (.map, nil) }
        if let a = names.first(where: { $0.hasPrefix("derived/geo/addresses/") && $0.hasSuffix(".sqlite") }) {
            return (.addresses, String(a.dropFirst("derived/geo/addresses/".count).dropLast(7)))
        }
        if names.contains(where: { $0.hasPrefix("derived/video-compat/") }) { return (.videoCopies, nil) }
        return nil
    }

    /// Installs one chosen zip. `state`: the state the person picked (required for a state map). `allowUnlisted` is for the
    /// Mac test tool only: a zip not in the download list is then accepted without a SHA-256 check.
    static func install(file: URL, state: String?, allowUnlisted: Bool = false, verifiedSHA: String? = nil, replaceWholeUS: Bool = false,
                        settings: URL = MapInstall.defaultSettings,
                        progress: @escaping (Phase, Int64, Int64) -> Void = { _, _, _ in }, isCancelled: @escaping () -> Bool = { false }) throws -> Report {
        let scoped = file.startAccessingSecurityScopedResource()
        defer { if scoped { file.stopAccessingSecurityScopedResource() } }
        let z = try ZipInstall.open(file, what: "download")
        guard let found = Installer.kind(of: z) else {
            throw InstallError.notAMap("This zip is not a Lastbook download (no library, state map, address index or video copies in it).")
        }
        let (kind, addrID) = found
        // The download list: name and size, then the SHA-256 of the whole file.
        var entry: CatalogEntry?, cfile: CatalogFile?
        if kind != .videoCopies {
            let size = ZipInstall.size(file) ?? -1
            if let (e, f) = InstallCatalog.match(fileName: file.lastPathComponent, size: size) {
                entry = e; cfile = f
                let want: CatalogEntry.Kind = kind == .pack ? .pack : kind == .map ? .map : .addresses
                if e.kind != want { throw InstallError.unknownDownload("“\(file.lastPathComponent)” is listed as a different kind of download.") }
                // a download made here was hashed as it arrived; a file from Files is hashed now
                let sha = try verifiedSHA ?? ZipInstall.sha256(file, progress: { progress(.checking, $0, $1) }, isCancelled: isCancelled)
                if sha != f.sha256 {
                    throw InstallError.unknownDownload("“\(file.lastPathComponent)” is not the file Lastbook published (its SHA-256 differs). It may be damaged or changed; download it again.")
                }
            } else if !allowUnlisted {
                throw InstallError.unknownDownload("Lastbook doesn’t know “\(file.lastPathComponent)” (\(ByteCountFormatter().string(fromByteCount: size))). Use a file from downloads.lastbook.org, with its name unchanged.")
            }
        }
        let up: (Int64, Int64) -> Void = { progress(.unpacking, $0, $1) }
        switch kind {
        case .pack:
            let staged = try PackInstall.stage(zip: file, target: packTarget, entry: entry, includeVideoCopies: needsH264,
                                               progress: up, isCancelled: isCancelled)
            progress(.switching, 0, 0)
            switch try PackInstall.activate(staged, settings: settings) {
            case .switched(let old):
                return Report(kind: .pack, title: "Library installed: \(staged.pack.capitalized)",
                              detail: old == nil ? "Ready to use." : "Your earlier library is kept as “\(old!.lastPathComponent)” until you delete it.")
            case .restored(let m): throw InstallError.activate(m)
            }
        case .map:
            guard let state else { throw InstallError.refused("Choose your state first, then add its map.") }
            if let e = entry, e.id != state { throw InstallError.wrongState(found: e.id, expected: state) }
            guard let root = Library.shared.root else { throw InstallError.refused("Install a library first, then its state map.") }
            if MapInstall.mapName(root) == "us" && !replaceWholeUS {
                // All carries the whole US; a state map would replace it (desktop: All needs no state map). Ask first.
                throw InstallError.needsConfirmation("This library already has the whole-US map. The \(title(state)) map would replace it with \(title(state)) only. Replace it?")
            }
            let staged = try MapInstall.stage(zip: file, root: root, expectedID: state, installedBytes: entry?.installedBytes,
                                              progress: up, isCancelled: isCancelled)
            progress(.switching, 0, 0)
            switch try MapInstall.activate(staged, settings: settings) {
            case .switched: return Report(kind: .map, title: "State map installed: \(title(state))", detail: "Streets, buildings, help points and terrain for \(title(state)).")
            case .restored(let m): throw InstallError.activate(m)
            }
        case .addresses:
            guard let id = addrID, let root = Library.shared.root else { throw InstallError.refused("Install a library first, then its addresses.") }
            if let e = entry, e.id != id { throw InstallError.unknownDownload("The address download’s contents don’t match its name.") }
            try AddressInstall.install(zip: file, root: root, id: id, installedBytes: entry?.installedBytes,
                                       receipt: cfile.map { [$0.name: $0.sha256] }, progress: up, isCancelled: isCancelled)
            return Report(kind: .addresses, title: "Street addresses installed: \(title(id))", detail: "Search the map for a street address in \(title(id)).")
        case .videoCopies:
            guard let root = Library.shared.root else { throw InstallError.refused("Install a library first, then its video copies.") }
            let (n, bytes) = try VideoCopiesInstall.install(zip: file, root: root, needed: needsH264, progress: up, isCancelled: isCancelled)
            return Report(kind: .videoCopies, title: "Video copies installed", detail: "\(n) videos (\(ByteCountFormatter().string(fromByteCount: bytes))) now play on this \(Device.name).")
        }
    }

    static func title(_ state: String) -> String { InstallCatalog.entry(.map, state)?.title ?? state }

    // ------------------------------------------------------------------ launch hooks ----
    /// Call at launch BEFORE Library.shared.locate(): finishes or undoes a switch that was interrupted.
    /// Returns messages to show (for example "The new map did not open, so Lastbook put the old one back.").
    @discardableResult
    static func atLaunch(settings: URL = MapInstall.defaultSettings) -> [String] {
        var notes: [String] = []
        if case .restored(let m)? = PackInstall.recoverIfNeeded(target: packTarget, settings: settings) { notes.append(m) }
        if let t = ZipInstall.readJSON(MapInstall.pendingFile(settings))?["target"] as? String,
           case .restored(let m)? = MapInstall.recoverIfNeeded(root: URL(fileURLWithPath: t), settings: settings) { notes.append(m) }
        return notes
    }

    /// Call once the library has opened: the switch is done, the work folders go.
    static func afterOpen(settings: URL = MapInstall.defaultSettings) {
        guard let root = Library.shared.root else { return }
        PackInstall.cleanupAfterOpen(target: packTarget, settings: settings)
        MapInstall.cleanupAfterOpen(root: root, settings: settings)
    }

    // ------------------------------------------------------------------ what is installed ----
    struct Status {
        var library: String?            // pack name, or nil when there is no library
        var map: String                 // "base", "us" or a state code
        var addresses: [String]         // present index files
        var addressesChecked: [String]  // those matching the download list
        var needsH264: Bool
        var videoCopies: Int
        var av1Missing: Int
        var oldLibraries: [(url: URL, bytes: Int64)]
        var free: Int64?
    }

    static func status() -> Status {
        let root = Library.shared.root
        let pack = root.flatMap { ZipInstall.readJSON($0.appendingPathComponent("pack.json")) }
        let vc = VideoCompat.shared
        let needs = vc.needsH264
        return Status(library: root.map { _ in (pack?["name"] as? String) ?? "custom" },
                      map: root.map(MapInstall.mapName) ?? "base",
                      addresses: root.map(AddressInstall.present) ?? [],
                      addressesChecked: root.map(AddressInstall.installed) ?? [],
                      needsH264: needs,
                      videoCopies: root == nil ? 0 : vc.variantCount().count,
                      av1Missing: root == nil || !needs ? 0 : vc.coverage().missing,
                      oldLibraries: PackInstall.oldLibraries(packTarget),
                      free: ZipInstall.freeSpace(Library.documents))
    }
}
