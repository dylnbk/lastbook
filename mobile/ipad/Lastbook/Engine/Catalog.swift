import Foundation

typealias Rec = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func s(_ k: String) -> String { (self[k] as? String) ?? (self[k].map { "\($0)" }.flatMap { $0 == "<null>" ? nil : $0 } ?? "") }
    func str(_ k: String) -> String? { let v = self[k]; if v == nil || v is NSNull { return nil }; return v as? String ?? "\(v!)" }
    func d(_ k: String) -> Double { (self[k] as? NSNumber)?.doubleValue ?? Double(self[k] as? String ?? "") ?? 0 }
    func truthy(_ k: String) -> Bool {
        guard let v = self[k], !(v is NSNull) else { return false }
        if let b = v as? Bool { return b }; if let n = v as? NSNumber { return n != 0 }
        if let s = v as? String { return !s.isEmpty }; if let a = v as? [Any] { return !a.isEmpty }
        return true
    }
}

/// Port of server.py's catalog(), trust(), public() and friends. The catalog is library-data/catalog.json.
final class Catalog: @unchecked Sendable {
    static let shared = Catalog()
    private let lock = NSLock()
    private var loadedFor: String?
    private(set) var byId: [String: Rec] = [:]
    private(set) var byPath: [String: Rec] = [:]
    private(set) var videoTx: [String: Rec] = [:]
    private(set) var thumbs: [String: String] = [:]
    private(set) var order: [Rec] = []

    func reset() { lock.lock(); loadedFor = nil; lock.unlock() }

    /// Loads once per library. Desktop re-reads on mtime change because indexers rewrite it; a pack on iPad never changes.
    func ensure() {
        lock.lock(); defer { lock.unlock() }
        guard let lib = Library.shared.lib else { byId = [:]; order = []; loadedFor = nil; return }
        if loadedFor == lib.path { return }
        let recs = (try? JSONSerialization.jsonObject(with: Data(contentsOf: lib.appendingPathComponent("catalog.json")))) as? [Rec] ?? []
        var excl = Set<String>()
        if let d = try? Data(contentsOf: lib.appendingPathComponent("video-exclusions.json")),
           let o = try? JSONSerialization.jsonObject(with: d) as? Rec, let v = o["videos"] as? Rec {
            for k in v.keys { excl.insert(k); excl.insert("metadata-" + k) }
        }
        var kept = recs
        if !excl.isEmpty {
            let bad = Set(recs.filter { excl.contains($0.s("id")) }.map { $0.s("path") })
            kept = recs.filter { r in !excl.contains(r.s("id")) && !excl.contains(r.s("video_id")) && !(r.str("video_path").map(bad.contains) ?? false) }
        }
        var bi: [String: Rec] = [:], bp: [String: Rec] = [:], tx: [String: Rec] = [:], th: [String: String] = [:]
        for r in kept {
            bi[r.s("id")] = r; bp[r.s("path")] = r
            if r.s("kind") == "transcript", let vp = r.str("video_path") { tx[vp] = r }
            if r.s("kind") == "metadata", let vp = r.str("video_path"), let t = r.str("thumbnail_path") { th[vp] = t }
        }
        byId = bi; byPath = bp; videoTx = tx; thumbs = th; order = kept
        loadedFor = lib.path
    }

    var records: [Rec] { ensure(); return order }
    func get(_ id: String) -> Rec? { ensure(); return byId[id] }

    // ---------------------------------------------------------------- trust ----
    static let TRUST: [(NSRegularExpression, String, Double)] = [
        (#"World Health|WHO\b|ICRC|IFRC|Red Cross|Médecins|MSF|UNICEF|Sphere|St John Ambulance|CAWST"#, "International health & humanitarian guidance", 1.5),
        (#"Hesperian|Where There Is No|Where Women|Book for Midwives|Community Guide to Environmental|Health Actions|Workers' Guide"#, "Hesperian community health guide", 1.45),
        (#"FEMA|Ready\.gov|CDC|Centers for Disease|EPA|Environmental Protection|NOAA|National Weather|USDA|Food Safety|National Park|USGS|Department|Agency|U\.S\.|US Army|Army|Navy|Census|NIOSH|OSHA|NASA|FAA|Cal OES|California|County of|City of"#, "Government guidance", 1.4),
        (#"National Center for Home Food Preservation|Extension|University|UC |College|Agricultural|Food Hero|UWyo"#, "University / extension service", 1.35),
        (#"MIT|OpenStax|OpenCourseWare|NIST"#, "Course or textbook", 1.1),
        (#"Kiwix|Wikipedia|Wikibooks|Appropedia|MDWiki"#, "Community-written encyclopedia", 1.0),
        (#"Coalcracker|Corporals Corner|Canterbury|Primitive Technology|creator-"#, "Hobbyist demonstration video", 0.8),
    ].map { (try! NSRegularExpression(pattern: $0.0), $0.1, $0.2) }

    static func trust(_ r: Rec) -> (String, Double) {
        let s = "\(r.s("publisher")) \(r.s("collection")) \(r.s("category"))"
        for (rx, label, w) in TRUST where rx.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil { return (label, w) }
        if r.truthy("equipment_class") || r.truthy("model") { return ("Manufacturer manual (model-specific)", 1.05) }
        return ("Reference", 1.0)
    }

    static func kindLabel(_ r: Rec) -> String {
        let k = r.s("kind")
        return ["pdf": "PDF", "html": "Saved web page", "video": "Video", "transcript": "Transcript", "pptx": "Slides",
                "zim": "Encyclopedia", "metadata": "Video details"][k] ?? k
    }

    static func yearOf(_ r: Rec) -> String? {
        for k in ["edition", "publication_date", "upload_date"] {
            if let m = r.s(k).firstMatch(#"(19|20)\d\d"#) { return m }
        }
        return r.s("title").firstMatch(#"\b(19[5-9]\d|20[0-2]\d)\b"#)
    }

    static func cleanTitle(_ r: Rec) -> String {
        var t = r.s("title")
        t = t.replacing(#"^(\d{3}(\s\d+)?)\s+"#, with: "")
        t = t.replacing(#"(?i)\.pdf$"#, with: "")
        if t.fullMatch(#"[\w\-]+"#) && t.contains("_") { t = t.replacingOccurrences(of: "_", with: " ") }
        return t
    }

    func publicRec(_ r: Rec?, full: Bool = false) -> Rec? {
        guard let r else { return nil }
        let (t, _) = Self.trust(r)
        var out: Rec = ["id": r.s("id"), "title": Self.cleanTitle(r), "kind": r.s("kind"), "kindLabel": Self.kindLabel(r),
                        "publisher": r["publisher"] ?? NSNull(), "collection": r["collection"] ?? NSNull(), "path": r.s("path"),
                        "pages": (r.truthy("pages") ? r["pages"] : r["slides"]) ?? NSNull(), "duration": r["duration"] ?? NSNull(),
                        "year": Self.yearOf(r) ?? NSNull(), "trust": t, "bytes": r["bytes"] ?? NSNull()]
        if r.s("kind") == "video" {
            out["transcript"] = videoTx[r.s("path")] != nil || r.truthy("captions")
            if let th = thumbs[r.s("path")], let lib = Library.shared.lib, FileManager.default.fileExists(atPath: lib.appendingPathComponent(th).path) {
                out["thumb"] = th
            } else { out["thumb"] = NSNull() }
            // A library made of chosen parts (engine/wizard on the desktop) may hold a video's words without its picture:
            // left out to save room, or (pending) not yet made smaller by a build that is still running. As server.py's public().
            if r.truthy("words_only") {
                out["wordsOnly"] = true
                out["pending"] = r.truthy("pending")
            }
        }
        if r.s("searchability").hasPrefix("image-only") { out["imageOnly"] = true }
        if full {
            for k in ["notes", "edition", "retrieved_at", "source_url", "license", "license_url", "searchability",
                      "upload_date", "publication_date", "model", "course", "level", "caption_type"] where r.truthy(k) { out[k] = r[k] }
            if r.s("kind") == "transcript", let vp = r.str("video_path"), let v = byPath[vp] { out["videoId"] = v.s("id") }
            if r.s("kind") == "video", let tx = videoTx[r.s("path")] {
                out["transcriptId"] = tx.s("id"); out["caption_type"] = tx["caption_type"] ?? NSNull()
            }
        }
        return out
    }

    /// content/topics/*.json, bundled with the app (desktop: APP/content/topics).
    lazy var topics: [Rec] = {
        let dir = Resources.url("content").appendingPathComponent("topics")
        guard
              let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return [] }
        return files.filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }.compactMap { f in
            guard let d = try? Data(contentsOf: f) else { return nil }
            return (try? JSONSerialization.jsonObject(with: d) as? Rec) ?? ["id": f.deletingPathExtension().lastPathComponent, "error": "Could not read"]
        }
    }()
}

extension String {
    func firstMatch(_ pattern: String) -> String? {
        guard let rx = try? NSRegularExpression(pattern: pattern), let m = rx.firstMatch(in: self, range: NSRange(startIndex..., in: self)),
              let r = Range(m.range, in: self) else { return nil }
        return String(self[r])
    }
    func fullMatch(_ pattern: String) -> Bool { firstMatch("^(?:" + pattern + ")$") == self }
    func replacing(_ pattern: String, with t: String) -> String {
        guard let rx = try? NSRegularExpression(pattern: pattern) else { return self }
        return rx.stringByReplacingMatches(in: self, range: NSRange(startIndex..., in: self), withTemplate: t)
    }
    func allMatches(_ pattern: String) -> [String] {
        guard let rx = try? NSRegularExpression(pattern: pattern) else { return [] }
        return rx.matches(in: self, range: NSRange(startIndex..., in: self)).compactMap { Range($0.range, in: self).map { String(self[$0]) } }
    }
}
