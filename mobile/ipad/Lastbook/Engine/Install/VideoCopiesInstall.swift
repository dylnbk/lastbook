import Foundation

/// The H.264 copies of a library's AV1 videos (Engine/VideoCompat.swift; README.md "Video compatibility"): a zip holding
/// only derived/video-compat/h264-v1/ (manifest.json and <video id>.mp4 files), with or without a LastbookData/ top folder.
/// Installed only on an iPad that cannot decode AV1 (VideoCompat.needsH264); one that can is never given them.
enum VideoCopiesInstall {
    static var folder: String { "derived/video-compat/\(VideoCompat.version)" }
    static var dirs: Set<String> { ["", "derived", "derived/video-compat", folder] }

    static func allowed(_ rel: String, _ isDir: Bool) -> Bool {
        if isDir { return dirs.contains(rel) }
        guard rel.hasPrefix(folder + "/") else { return false }
        let name = String(rel.dropFirst(folder.count + 1))
        guard !name.hasPrefix("."), InstallCatalog.isName(name) else { return false }
        return name == "manifest.json" || name.lowercased().hasSuffix(".mp4")
    }

    /// Returns (copies installed, their bytes).
    @discardableResult
    static func install(zip: URL, root: URL, needed: Bool = VideoCompat.shared.needsH264,
                        progress: (Int64, Int64) -> Void = { _, _ in }, isCancelled: () -> Bool = { false }) throws -> (Int, Int64) {
        guard needed else { throw InstallError.notNeeded("This \(Device.name) plays the original AV1 videos, so it doesn’t need the H.264 copies.") }
        let z = try ZipInstall.open(zip, what: "video copies download")
        let (members, total) = try ZipInstall.checkZip(z, installedBytes: nil, allowed: allowed,
                                                       notAllowed: { InstallError.notAMap("The video copies download holds a file that is not a video copy (\($0)).") })
        guard let mf = members.first(where: { $0.parts.dropFirst().joined(separator: "/") == folder + "/manifest.json" })?.entry,
              let m = try? JSONSerialization.jsonObject(with: ZipInstall.readSmall(z, mf)) as? [String: Any],
              m["version"] as? String == VideoCompat.version, let items = m["items"] as? [String: [String: Any]] else {
            throw InstallError.notAMap("This zip is not a Lastbook video copies download (no \(VideoCompat.version) manifest).")
        }
        let work = try ZipInstall.ensureWorkFolder(root)
        let into = work.appendingPathComponent("unpack-video")
        let lib = try ZipInstall.unpack(z, members, total: total, into: into, margin: 512 << 20, what: "video copies",
                                        progress: progress, isCancelled: isCancelled)
        let staged = lib.appendingPathComponent(folder)
        var count = 0, bytes: Int64 = 0
        for (_, it) in items {
            guard let file = it["file"] as? String, InstallCatalog.isName(file), let want = (it["bytes"] as? NSNumber)?.int64Value,
                  ZipInstall.size(staged.appendingPathComponent(file)) == want else {
                throw InstallError.damaged("A video copy the download lists is missing or the wrong size.")
            }
            count += 1; bytes += want
        }
        // One step: the copies there now go aside, the new ones come in, the old ones go.
        let dest = root.appendingPathComponent(folder), aside = work.appendingPathComponent("video-old")
        try? FileManager.default.removeItem(at: aside)
        if FileManager.default.fileExists(atPath: dest.path) { try ZipInstall.move(dest, aside) }
        do { try ZipInstall.move(staged, dest) } catch {
            if FileManager.default.fileExists(atPath: aside.path) { try? ZipInstall.move(aside, dest) }
            throw InstallError.folder("Lastbook could not put the video copies in place.")
        }
        try? FileManager.default.removeItem(at: aside)
        try? FileManager.default.removeItem(at: into)
        VideoCompat.shared.reset()
        return (count, bytes)
    }
}
