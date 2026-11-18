import Foundation

/// Maps of regions of the world, built on a computer from open data and kept side by side in the library:
/// <LastbookData>/derived/geo/regions/<id>/ holding region.json (what it is), map.sqlite (what is drawn, and the help
/// points), places.sqlite (the place finder) and terrain.sqlite (elevation). The desktop reads them in engine/regions.py;
/// this is that file's reading side, for the same folders. docs/WORLD-MAPS.md has the format.
///
/// What this does: which regions are here, which of them a box or a point touches, and the databases to ask. The map engine
/// (API/MapAPI.swift) draws a region's features with the code it draws a pack's map with: the rows have the same shape.
///
/// What it does not do yet, that the desktop does: lay several regions' elevation together below the zoom their tiles are
/// whole at (the first region's tile is used), and leave an outline region's help points out where a detailed region covers
/// the same ground (both are shown; regions of different countries do not overlap).
final class RegionAtlas: @unchecked Sendable {
    static let shared = RegionAtlas()
    static let CELL = 0.05
    static let rescan: TimeInterval = 4
    /// As TILE_SQL in engine/regions.py: a region's features carry the zooms they are drawn between.
    static let tileSQL = "SELECT f.layer AS layer,f.cls AS cls,f.name AS name,f.geom AS geom FROM feat_idx i JOIN feat f ON f.id=i.id "
        + "WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=? AND i.maxz>=? ORDER BY f.id"

    final class Region: @unchecked Sendable {
        let folder: URL
        let info: [String: Any]
        let id: String, name: String, level: String
        let west: Double, south: Double, east: Double, north: Double
        /// Polygons of rings of (lon, lat), as GeoJSON has them. Empty: the box is the outline.
        let outline: [[[(Double, Double)]]]
        let hasPlaces: Bool, hasTerrain: Bool
        private var cells: Set<Int>?
        private let lock = NSLock()

        init(folder: URL, info: [String: Any]) {
            self.folder = folder
            self.info = info
            id = (info["id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? folder.lastPathComponent
            name = (info["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? id
            level = (info["level"] as? String) == "overview" ? "overview" : "detail"
            let b = ((info["box"] as? [Any]) ?? []).compactMap { ($0 as? NSNumber)?.doubleValue }
            let box = b.count >= 4 ? b : [-180, -85, 180, 85]
            west = box[0]; south = box[1]; east = box[2]; north = box[3]
            var polys: [[[(Double, Double)]]] = []
            for poly in (info["outline"] as? [Any]) ?? [] {
                var rings: [[(Double, Double)]] = []
                for ring in (poly as? [Any]) ?? [] {
                    let pts: [(Double, Double)] = ((ring as? [Any]) ?? []).compactMap { p in
                        guard let a = p as? [Any], a.count >= 2, let x = (a[0] as? NSNumber)?.doubleValue, let y = (a[1] as? NSNumber)?.doubleValue else { return nil }
                        return (x, y)
                    }
                    if !pts.isEmpty { rings.append(pts) }
                }
                if !rings.isEmpty { polys.append(rings) }
            }
            outline = polys
            let fm = FileManager.default
            hasPlaces = fm.fileExists(atPath: folder.appendingPathComponent("places.sqlite").path)
            hasTerrain = fm.fileExists(atPath: folder.appendingPathComponent("terrain.sqlite").path)
        }

        var box: [Double] { [west, south, east, north] }
        var map: SQLiteDB? { DBCache.shared.get(folder.appendingPathComponent("map.sqlite")) }
        var places: SQLiteDB? { hasPlaces ? DBCache.shared.get(folder.appendingPathComponent("places.sqlite")) : nil }
        var terrain: SQLiteDB? { hasTerrain ? DBCache.shared.get(folder.appendingPathComponent("terrain.sqlite")) : nil }
        var dataAsOf: String { (info["data_as_of"] as? String) ?? "" }

        func touches(_ w: Double, _ s: Double, _ e: Double, _ n: Double) -> Bool {
            east >= w && west <= e && north >= s && south <= n
        }

        /// In the region's outline (its box, when it has no outline). Even-odd, as the desktop's.
        func inside(_ lon: Double, _ lat: Double) -> Bool {
            guard west <= lon, lon <= east, south <= lat, lat <= north else { return false }
            if outline.isEmpty { return true }
            for poly in outline {
                var hit = false
                for ring in poly {
                    var j = ring.count - 1
                    for i in 0..<ring.count {
                        let (xi, yi) = ring[i], (xj, yj) = ring[j]
                        if (yi > lat) != (yj > lat) && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi { hit.toggle() }
                        j = i
                    }
                }
                if hit { return true }
            }
            return false
        }

        /// The 0.05 degree cells this region maps in detail (none for an outline region).
        func cover() -> Set<Int> {
            lock.lock(); defer { lock.unlock() }
            if let c = cells { return c }
            var out = Set<Int>()
            if level == "detail", let db = map {
                db.each("SELECT cell FROM cover") { r in if let c = r["cell"]?.int { out.insert(Int(c)) } }
            }
            cells = out
            return out
        }

        func summary() -> [String: Any] {
            let counts = (info["counts"] as? [String: Any]) ?? [:]
            let sizes = (info["bytes"] as? [String: Any]) ?? [:]
            let total = sizes.values.reduce(0) { $0 + (($1 as? NSNumber)?.intValue ?? 0) }
            return ["id": id, "name": name, "level": level, "box": box, "data_as_of": dataAsOf, "built": (info["built"] as? String) ?? "",
                    "features": (counts["features"] as? NSNumber)?.intValue ?? 0, "help_points": (counts["pois"] as? NSNumber)?.intValue ?? 0,
                    "terrain": hasTerrain, "bytes": total]
        }
    }

    private let lock = NSLock()
    private var found: [Region] = []
    private var signature: String?
    private var lookedAt = Date.distantPast
    private var root: String?
    private var allCells: Set<Int>?
    private var allClasses: Set<String>?

    /// The regions in the open library, detailed ones first, then by name: the order the desktop draws and asks them in.
    func regions() -> [Region] {
        lock.lock(); defer { lock.unlock() }
        let base = Library.shared.geo?.appendingPathComponent("regions")
        if base?.path == root, signature != nil, Date().timeIntervalSince(lookedAt) < Self.rescan { return found }
        root = base?.path
        lookedAt = Date()
        var sig = "", folders: [URL] = []
        let fm = FileManager.default
        let names = base.flatMap { try? fm.contentsOfDirectory(atPath: $0.path) } ?? []
        for name in names.sorted(by: { $0.unicodeScalars.lexicographicallyPrecedes($1.unicodeScalars) }) where !name.hasPrefix(".") {
            guard let folder = base?.appendingPathComponent(name) else { continue }
            let note = folder.appendingPathComponent("region.json")
            guard fm.fileExists(atPath: folder.appendingPathComponent("map.sqlite").path),
                  let a = try? fm.attributesOfItem(atPath: note.path) else { continue }
            let stamp = (a[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
            sig += "\(name)\u{1}\(stamp)\u{1}\((a[.size] as? NSNumber)?.intValue ?? 0)\u{2}"
            folders.append(folder)
        }
        if sig == signature { return found }
        var out: [Region] = []
        for folder in folders {
            guard let d = try? Data(contentsOf: folder.appendingPathComponent("region.json")),
                  let info = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { continue }
            out.append(Region(folder: folder, info: info))
        }
        out.sort { a, b in
            if (a.level != "detail") != (b.level != "detail") { return a.level == "detail" }
            return a.name.unicodeScalars.lexicographicallyPrecedes(b.name.unicodeScalars)
        }
        found = out
        signature = sig
        allCells = nil; allClasses = nil
        return out
    }

    var isEmpty: Bool { regions().isEmpty }

    /// Changes when a region is added, replaced or removed: part of the address of every map tile, so none outlives its data.
    func version() -> String {
        _ = regions()
        lock.lock(); defer { lock.unlock() }
        guard let s = signature, !s.isEmpty else { return "r0" }
        var h: UInt32 = 2166136261      // FNV-1a: a short name for the list, not a checksum of anything
        for b in s.utf8 { h = (h ^ UInt32(b)) &* 16777619 }
        return "r" + String(h, radix: 16)
    }

    // ---------------------------------------------------------------- cover ----
    /// Every cell some detailed region maps.
    func cells() -> Set<Int> {
        let rs = regions()
        lock.lock()
        if let c = allCells { lock.unlock(); return c }
        lock.unlock()
        var out = Set<Int>()
        for r in rs { out.formUnion(r.cover()) }
        lock.lock(); allCells = out; lock.unlock()
        return out
    }

    func covered(_ lon: Double, _ lat: Double) -> Bool {
        cells().contains(Int((lon + 180) / Self.CELL) * 100000 + Int((lat + 90) / Self.CELL))
    }

    /// Is every cell of this box mapped in detail?
    func boxCovered(_ w: Double, _ s: Double, _ e: Double, _ n: Double) -> Bool {
        let c = cells()
        if c.isEmpty { return false }
        for i in Int((w + 180) / Self.CELL)...Int((e + 180) / Self.CELL) {
            for j in Int((s + 90) / Self.CELL)...Int((n + 90) / Self.CELL) where !c.contains(i * 100000 + j) { return false }
        }
        return true
    }

    /// The region a point is on, a detailed one before an outline, or nil.
    func at(_ lon: Double, _ lat: Double) -> Region? { regions().first { $0.inside(lon, lat) } }
    func contains(_ lon: Double, _ lat: Double) -> Bool { at(lon, lat) != nil }

    func bounds() -> [Double]? {
        let rs = regions()
        guard !rs.isEmpty else { return nil }
        return [rs.map(\.west).min()!, rs.map(\.south).min()!, rs.map(\.east).max()!, rs.map(\.north).max()!]
    }

    // ---------------------------------------------------------------- what to ask ----
    /// The map databases to draw from for this box at this zoom. An outline region is left out where the whole box is
    /// mapped in detail and the zoom is close enough to show it.
    func tileSources(_ z: Int, _ w: Double, _ s: Double, _ e: Double, _ n: Double) -> [SQLiteDB] {
        let skipOverview = z >= 11 && boxCovered(w, s, e, n)
        return regions().compactMap { r in
            guard r.touches(w, s, e, n), !(skipOverview && r.level == "overview") else { return nil }
            return r.map
        }
    }

    /// The databases with help points in the box.
    func poiSources(_ w: Double, _ s: Double, _ e: Double, _ n: Double) -> [SQLiteDB] {
        regions().compactMap { $0.touches(w, s, e, n) ? $0.map : nil }
    }

    /// Every kind of help point any region holds.
    func classes() -> Set<String> {
        let rs = regions()
        lock.lock()
        if let c = allClasses { lock.unlock(); return c }
        lock.unlock()
        var out = Set<String>()
        for r in rs { r.map?.each("SELECT DISTINCT cls FROM poi") { row in if let c = row["cls"]?.text { out.insert(c) } } }
        lock.lock(); allClasses = out; lock.unlock()
        return out
    }

    func placeDBs() -> [SQLiteDB] { regions().compactMap(\.places) }

    /// The elevation tile from the first region that has one there, or nil.
    func terrain(_ z: Int, _ x: Int, _ y: Int) -> Data? {
        let n = Double(1 << z)
        let west = Double(x) / n * 360 - 180, east = Double(x + 1) / n * 360 - 180
        let north = atan(sinh(Double.pi * (1 - 2 * Double(y) / n))) * 180 / Double.pi
        let south = atan(sinh(Double.pi * (1 - 2 * Double(y + 1) / n))) * 180 / Double.pi
        var first: Data?
        for r in regions() where r.hasTerrain && r.touches(west, south, east, north) {
            guard let png = r.terrain?.one("SELECT png FROM tile WHERE z=? AND x=? AND y=?", [z, x, y])?["png"]?.blob else { continue }
            if z >= (r.level == "detail" ? 8 : 6) { return png }       // whole at this zoom, whichever region made it
            if first == nil { first = png }
        }
        return first
    }

    var hasTerrain: Bool { regions().contains { $0.hasTerrain } }
    var terrainDetail: Bool { regions().contains { $0.hasTerrain && $0.level == "detail" } }

    func summary() -> [[String: Any]] { regions().map { $0.summary() } }

    func credits() -> [String] {
        var out: [String] = []
        for r in regions() { for c in (r.info["credits"] as? [String]) ?? [] where !out.contains(c) { out.append(c) } }
        return out
    }

    /// (oldest, newest) date of the map data, as YYYY-MM-DD, or nil.
    func dataAsOf() -> [String]? {
        let days = regions().map(\.dataAsOf).filter { $0.count >= 10 }.map { String($0.prefix(10)) }.sorted()
        guard let a = days.first, let b = days.last else { return nil }
        return [a, b]
    }
}
