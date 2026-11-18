import Foundation
import Compression

/// Port of server.py's map section: vector tiles, elevation / crop / aerial images, 3D buildings, clustered pins,
/// cluster members, nearest help, resources, place search and the map's own description (/api/map/info).
/// Answers match the desktop byte for byte where they are binary, and as data where they are JSON.
struct MapAPI: RouteGroup {
    func handle(_ r: APIRequest) -> APIResponse? {
        let p = r.path
        guard p.hasPrefix("/api/map/") || p == "/api/places" else { return nil }
        do { return try MapEngine.shared.route(r) } catch let e as PyError {
            return .json(["error": "Server error (\(e.name))."], status: 500)
        } catch {
            return .json(["error": "Server error (Exception)."], status: 500)
        }
    }
}

/// A Python exception the desktop would raise for this request (it answers 500 with the exception's name).
fileprivate struct PyError: Error { let name: String }

fileprivate let degToRad = Double.pi / 180.0
fileprivate let radToDeg = 180.0 / Double.pi

// ------------------------------------------------------------------ Python number semantics ----

/// Python round(x, n): the double nearest the correctly rounded decimal (half-even on exact ties).
fileprivate func pyRound(_ x: Double, _ nd: Int) -> Double {
    guard x.isFinite else { return x }
    let m: Double = nd == 1 ? 10 : nd == 2 ? 100 : nd == 3 ? 1000 : nd == 5 ? 100000 : pow(10, Double(nd))
    let t = x * m
    if abs(t) < 1e9 {
        let fl = t.rounded(.down), f = t - fl
        if abs(f - 0.5) > 1e-6 { return (f < 0.5 ? fl : fl + 1) / m }
    }
    return Double(String(format: "%.\(nd)f", x)) ?? x   // near a tie: let the exact decimal conversion decide
}

/// Python round(x) to an int (banker's rounding).
fileprivate func pyRoundInt(_ x: Double) -> Int { Int(x.rounded(.toNearestOrEven)) }

/// Python float // float.
fileprivate func pyFloorDiv(_ vx: Double, _ wx: Double) -> Double {
    var mod = fmod(vx, wx)
    var div = (vx - mod) / wx
    if mod != 0, (wx < 0) != (mod < 0) { mod += wx; div -= 1 }
    if div != 0 {
        var f = floor(div)
        if div - f > 0.5 { f += 1 }
        return f
    }
    return copysign(0, vx / wx)
}

/// Python int(float): truncation toward zero (guarded against values Int cannot hold).
fileprivate func pyInt(_ x: Double) -> Int {
    guard x.isFinite, abs(x) < 9e18 else { return x > 0 ? Int.max : Int.min }
    return Int(x)
}

/// Python float(str) for query values.
fileprivate func pyFloat(_ s: String) throws -> Double {
    let t = s.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "_", with: "")
    let l = t.lowercased()
    if ["inf", "+inf", "infinity", "+infinity"].contains(l) { return .infinity }
    if ["-inf", "-infinity"].contains(l) { return -.infinity }
    if ["nan", "+nan", "-nan"].contains(l) { return .nan }
    guard !t.isEmpty, let d = Double(t), !l.hasPrefix("0x") else { throw PyError(name: "ValueError") }
    return d
}

/// Python int(str).
fileprivate func pyIntStr(_ s: String) throws -> Int {
    guard let i = Int(s.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "_", with: "")) else { throw PyError(name: "ValueError") }
    return i
}

/// Python repr(float) / str(float): the shortest round-trip form, like Swift's.
fileprivate func pyFloatRepr(_ d: Double) -> String {
    if d.isNaN { return "nan" }
    if d.isInfinite { return d > 0 ? "inf" : "-inf" }
    return "\(d)"
}

fileprivate func groupThousands(_ digits: String) -> String {
    var neg = false, s = Substring(digits)
    if s.hasPrefix("-") { neg = true; s = s.dropFirst() }
    var out: [Character] = []
    for (i, c) in s.reversed().enumerated() { if i > 0 && i % 3 == 0 { out.append(",") }; out.append(c) }
    return (neg ? "-" : "") + String(out.reversed())
}

/// Python f"{n:,}" for an int.
fileprivate func commas(_ n: Int) -> String { groupThousands(String(n)) }

/// Python f"{v:,}" for an int or float value from JSON.
fileprivate func commasAny(_ v: Any) -> String {
    if let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() {
        if CFNumberIsFloatType(n) {
            let r = pyFloatRepr(n.doubleValue)
            if r.contains("e") || r.contains("n") { return r }
            let parts = r.split(separator: ".", maxSplits: 1)
            return groupThousands(String(parts[0])) + (parts.count > 1 ? "." + parts[1] : "")
        }
        return commas(n.intValue)
    }
    if let n = v as? NSNumber { return n.boolValue ? "1" : "0" }
    return pyStr(v)
}

/// Python str(value) for a value from json.loads.
fileprivate func pyStr(_ v: Any?) -> String {
    guard let v, !(v is NSNull) else { return "None" }
    if let s = v as? String { return s }
    if let n = v as? NSNumber {
        if CFGetTypeID(n) == CFBooleanGetTypeID() { return n.boolValue ? "True" : "False" }
        return CFNumberIsFloatType(n) ? pyFloatRepr(n.doubleValue) : String(n.int64Value)
    }
    if let a = v as? [Any] { return "[" + a.map(pyRepr).joined(separator: ", ") + "]" }
    if let o = v as? [String: Any] { return "{" + o.map { "'\($0.key)': \(pyRepr($0.value))" }.joined(separator: ", ") + "}" }
    return "\(v)"
}

fileprivate func pyRepr(_ v: Any) -> String { if let s = v as? String { return "'\(s)'" }; return pyStr(v) }

/// Python truthiness of a value from json.loads (None for a missing key).
fileprivate func truthy(_ v: Any?) -> Bool {
    guard let v, !(v is NSNull) else { return false }
    if let n = v as? NSNumber { return n.doubleValue != 0 }
    if let s = v as? String { return !s.isEmpty }
    if let a = v as? [Any] { return !a.isEmpty }
    if let o = v as? [String: Any] { return !o.isEmpty }
    return true
}

/// Python float(value) for a JSON value (number or numeric string).
fileprivate func pyFloatAny(_ v: Any) throws -> Double {
    if let n = v as? NSNumber { return n.doubleValue }
    if let s = v as? String { return try pyFloat(s) }
    throw PyError(name: "TypeError")
}

/// Python string ordering (by code point).
fileprivate func pyLess(_ a: String, _ b: String) -> Bool { a.unicodeScalars.lexicographicallyPrecedes(b.unicodeScalars) }

fileprivate func jsonLoads(_ s: String?) -> [String: Any] {
    guard let s, !s.isEmpty, let o = try? JSONSerialization.jsonObject(with: Data(s.utf8), options: [.fragmentsAllowed]) as? [String: Any] else { return [:] }
    return o
}

fileprivate func jsonLoadsAny(_ s: String?) -> Any {
    guard let s, !s.isEmpty, let o = try? JSONSerialization.jsonObject(with: Data(s.utf8), options: [.fragmentsAllowed]) else { return [String: Any]() }
    return o
}

/// Python re.findall(r'\w+', s).
fileprivate func words(_ s: String) -> [String] { s.allMatches(#"\w+"#) }

fileprivate func nn(_ v: Any?) -> Any { v ?? NSNull() }

// ------------------------------------------------------------------ the engine ----

fileprivate final class MapEngine: @unchecked Sendable {
    static let shared = MapEngine()

    private var observer: NSObjectProtocol?
    init() {
        // A map set switched in (Engine/Install) keeps the library's path: drop everything read from the old one.
        observer = NotificationCenter.default.addObserver(forName: MapInstall.didChange, object: nil, queue: nil) { [weak self] _ in
            guard let self else { return }
            self.lock.lock(); self.stateFor = "\u{0}changed"; self.lock.unlock()
        }
    }

    static let SOCAL_CELL = 0.05
    static let EXT = 512
    static let CLUSTER_PX = 46.0

    static let HELP: [String: String] = [
        "hospital": "Hospital", "clinic": "Clinic", "doctors": "Doctor", "pharmacy": "Pharmacy", "chemist": "Pharmacy",
        "dentist": "Dentist", "fire_station": "Fire station", "police": "Police", "drinking_water": "Drinking water tap",
        "fountain": "Fountain (not necessarily drinkable)", "water_tower": "Water tower", "water_works": "Water works",
        "spring": "Spring (untested)", "toilet": "Public toilet", "shelter": "Shelter (structure)",
        "community_centre": "Community centre", "town_hall": "Town hall", "school": "School", "library": "Library",
        "supermarket": "Supermarket", "convenience": "Convenience store", "doityourself": "Hardware / DIY store",
        "hardware": "Hardware store", "garden_centre": "Garden centre", "post_office": "Post office",
        "camp_site": "Campsite", "veterinary": "Vet", "nursing_home": "Care home", "fuel": "Fuel station",
        "car_repair": "Car repair", "bicycle_shop": "Bicycle shop", "wastewater_plant": "Wastewater plant",
        "comms_tower": "Communications tower", "park": "Park", "courthouse": "Courthouse",
        "college": "College", "university": "University", "kindergarten": "Kindergarten", "beach": "Beach",
        "embassy": "Embassy", "mineral": "Recorded mineral / mine", "oil_gas_well": "Recorded oil / gas well",
        "boat_launch": "Boating facility", "fish_stocking": "Fish-stocking record",
        "dam": "Dam (National Inventory of Dams)", "reservoir": "Reservoir", "lake": "Lake or pond", "pond": "Pond",
        "orchard": "Orchard", "vineyard": "Vineyard", "berries": "Berry field", "perennial_veg": "Asparagus field",
        // in regions built from OpenStreetMap for anywhere in the world (server.py HELP)
        "water_well": "Well or water pump (untested)", "defibrillator": "Defibrillator (AED)", "ambulance_station": "Ambulance station", "assembly_point": "Emergency assembly point"]
    static let CROP_KINDS: Set<String> = ["orchard", "vineyard", "berries", "perennial_veg"]
    static let WATER_KINDS: Set<String> = ["dam", "reservoir", "lake", "pond"]
    static let RESOURCE_KINDS: Set<String> = Set(["mineral", "oil_gas_well", "boat_launch", "fish_stocking", "dam", "reservoir", "lake", "pond"]).union(CROP_KINDS)
    static let WATER_WORDS: Set<String> = ["lake", "lakes", "reservoir", "res", "dam", "pond", "ponds"]
    static let DEFAULT_NEAR = "hospital,clinic,pharmacy,fire_station,drinking_water"

    // Per-library state (a pack never changes while it is open). Reset when the library changes.
    private let lock = NSLock()
    private var stateFor: String?
    private var _socalCells: Set<Int>?
    private var _socalClasses: Set<String>?
    private var _contents: [String: Any]?
    private var _contentsFor: String?
    private var _pack: Any??
    private var _national: [String: Any]?
    private var tileCache: [String: Data] = [:]
    private var tileOrder: [String] = []
    private var bucketCache: [Int: [Footprint]] = [:]
    private var bucketOrder: [Int] = []
    private var pinCache: [String: [String: Any]] = [:]
    private var pinOrder: [String] = []

    private func fresh() {
        let root = Library.shared.root?.path
        if stateFor != root {
            stateFor = root
            _socalCells = nil; _socalClasses = nil; _contents = nil; _pack = nil; _national = nil
            tileCache = [:]; tileOrder = []; bucketCache = [:]; bucketOrder = []; pinCache = [:]; pinOrder = []
        }
    }

    // ---------------------------------------------------------------- files ----
    var geo: URL? { Library.shared.geo }
    func geoPath(_ name: String) -> String? { geo?.appendingPathComponent(name).path }
    func geoExists(_ name: String) -> Bool { geoPath(name).map { FileManager.default.fileExists(atPath: $0) } ?? false }
    func geoDB(_ name: String) -> SQLiteDB? { Library.shared.db(name, geo: true) }
    var mapdb: SQLiteDB? { geoDB("map.sqlite") }
    var nationalDB: SQLiteDB? { geoDB("national.sqlite") }
    var waterDB: SQLiteDB? { geoDB("water.sqlite") }
    var cropDB: SQLiteDB? { geoDB("crops.sqlite") }
    var atlasDB: SQLiteDB? { Library.shared.db("geography/resources/atlas.sqlite") }
    var placeDB: SQLiteDB? { geoDB("places.sqlite") }

    func nationalStatus() -> [String: Any] {
        lock.lock(); defer { lock.unlock() }
        fresh()
        if let n = _national { return n }
        var out: [String: Any] = ["state": "not-built"]
        if let p = geoPath("national-status.json"), let d = FileManager.default.contents(atPath: p),
           let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { out = o }
        if (out["state"] as? String) != "building" { _national = out }   // while building, re-read like the desktop
        return out
    }

    func packInfo() -> Any? {
        lock.lock(); defer { lock.unlock() }
        fresh()
        if let p = _pack { return p }
        var v: Any? = nil
        if let root = Library.shared.root, let d = FileManager.default.contents(atPath: root.appendingPathComponent("pack.json").path),
           let o = try? JSONSerialization.jsonObject(with: d, options: [.fragmentsAllowed]), !(o is NSNull) { v = o }
        _pack = .some(v)
        return v
    }

    func packTruthy() -> Bool { truthy(packInfo()) }

    func mtimeMinutes(_ name: String) -> Int? {
        guard let p = geoPath(name) else { return nil }
        var st = stat()
        guard stat(p, &st) == 0 else { return nil }
        let t = Double(st.st_mtimespec.tv_sec) + Double(st.st_mtimespec.tv_nsec) / 1e9
        return Int(floor(t)) / 60
    }

    func dataVersion() -> String {
        let status = nationalStatus()
        var v: String
        if (status["state"] as? String) == "building" { v = "n\(Int(floor(Date().timeIntervalSince1970 / 20)))" }
        else { v = "n" + pyStr(status["features"] ?? 0) }
        for f in ["water.sqlite", "water.sqlite-wal", "crops.sqlite", "terrain.sqlite", "buildings.sqlite", "buildings.sqlite-wal"] {
            if let m = mtimeMinutes(f) { v += ".\(m)" }
        }
        let atlas = RegionAtlas.shared
        return atlas.isEmpty ? v : v + "." + atlas.version()     // as server.py: a region added or replaced gives every tile a new address
    }

    func socalCells() -> Set<Int> {
        lock.lock(); defer { lock.unlock() }
        fresh()
        if let c = _socalCells { return c }
        var cells = Set<Int>()
        if let p = geoPath("socal-cells.json"), let d = FileManager.default.contents(atPath: p),
           let a = try? JSONSerialization.jsonObject(with: d) as? [NSNumber] {
            cells = Set(a.map { $0.intValue })
        } else if let db = mapdb {
            let c = Self.SOCAL_CELL
            db.each("SELECT DISTINCT CAST((minx+180)/? AS INT)*100000+CAST((miny+90)/? AS INT) AS c FROM feat_idx WHERE maxx-minx<?", [c, c, c]) {
                if let v = $0["c"]?.int { cells.insert(Int(v)) }
            }
        }
        _socalCells = cells
        return cells
    }

    func inSocal(_ lon: Double, _ lat: Double) -> Bool {
        socalCells().contains(pyInt((lon + 180) / Self.SOCAL_CELL) &* 100000 &+ pyInt((lat + 90) / Self.SOCAL_CELL))
    }

    func socalClasses() -> Set<String> {
        lock.lock()
        fresh()
        if let c = _socalClasses { lock.unlock(); return c }
        lock.unlock()
        let c = Set(mapdb?.query("SELECT DISTINCT cls FROM poi").compactMap { $0["cls"]?.text } ?? [])
        lock.lock(); _socalClasses = c; lock.unlock()
        return c
    }

    // ---------------------------------------------------------------- routing ----
    func route(_ r: APIRequest) throws -> APIResponse? {
        let p = r.path
        // parse_qs drops blank values
        func qs(_ k: String) -> String? { if let v = r.query[k], !v.isEmpty { return v }; return nil }
        func need(_ k: String) throws -> String { guard let v = qs(k) else { throw PyError(name: "KeyError") }; return v }

        let parts = p.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        // ["", "api", "map", kind, ...]
        func nums(_ a: [String]) -> [Int]? {
            var out: [Int] = []
            for s in a {
                guard !s.isEmpty, s.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
                out.append(Int(s) ?? Int.max)
            }
            return out
        }
        if parts.count == 7, parts[1] == "api", parts[2] == "map", parts[3] == "terrain" || parts[3] == "crops", parts[6].hasSuffix(".png"),
           let n = nums([parts[4], parts[5], String(parts[6].dropLast(4))]) {
            let terrain = parts[3] == "terrain"
            if let png = terrain ? terrainTile(n[0], n[1], n[2]) : cropTile(n[0], n[1], n[2]) {
                return .bytes(png, mime: "image/png", cache: "max-age=86400")
            }
            return APIResponse(status: 204, mime: "text/plain", headers: ["Cache-Control": terrain && n[0] >= 13 ? "no-store" : "max-age=3600"], body: .data(Data()))
        }
        if parts.count == 7, parts[1] == "api", parts[2] == "map", parts[3] == "aerial", let n = nums(Array(parts[4...6])) {
            guard let img = aerialTile(n[0], n[1], n[2]) else { return .noContent }
            let jpeg = img.count >= 3 && img[img.startIndex] == 0xFF && img[img.startIndex + 1] == 0xD8 && img[img.startIndex + 2] == 0xFF
            let age = n[0] >= 17 && aerialCoastBuilding() ? 60 : 604800
            return .bytes(img, mime: jpeg ? "image/jpeg" : "image/png", cache: "max-age=\(age)")
        }
        if parts.count == 6, parts[1] == "api", parts[2] == "map", parts[3] == "bldg3d", let n = nums(Array(parts[4...5])) {
            return .bytes(bldg3d(n[0], n[1]), mime: "application/octet-stream", cache: "max-age=86400")
        }
        if parts.count == 7, parts[1] == "api", parts[2] == "map", parts[3] == "tile", let n = nums(Array(parts[4...6])) {
            let (z, x, y) = (n[0], n[1], n[2])
            if !(3...18).contains(z) { return .json(["z": z, "layers": [String: Any]()], cache: "no-store") }
            let data = mapTile(z, x, y)
            return APIResponse(status: 200, mime: "application/json", headers: ["Cache-Control": qs("v") != nil ? "max-age=86400" : "max-age=60"], body: .data(data))
        }
        if p == "/api/map/resources" {
            let box = try ["west", "south", "east", "north"].map { try pyFloat(try need($0)) }
            let zoom = try pyFloat(qs("zoom") ?? "10")
            let kinds = (qs("kinds") ?? Self.RESOURCE_KINDS.sorted().joined(separator: ",")).components(separatedBy: ",")
            return .json(resourceView(box[0], box[1], box[2], box[3], zoom, kinds))
        }
        if p == "/api/map/pins" || p == "/api/map/members" {
            let box = try ["west", "south", "east", "north"].map { try pyFloat(try need($0)) }
            let kinds = (qs("kinds") ?? "").components(separatedBy: ",").filter { Self.HELP[$0] != nil }
            if p == "/api/map/pins" { return .json(pins(box[0], box[1], box[2], box[3], try pyFloat(qs("zoom") ?? "10"), kinds)) }
            return .json(members(box[0], box[1], box[2], box[3], kinds, try pyFloat(try need("lat")), try pyFloat(try need("lon"))))
        }
        if p == "/api/map/near" {
            let cls = (qs("cls") ?? Self.DEFAULT_NEAR).components(separatedBy: ",").filter { Self.HELP[$0] != nil }
            return .json(try near(try pyFloat(try need("lat")), try pyFloat(try need("lon")), cls, try pyIntStr(qs("limit") ?? "5")))
        }
        if p == "/api/map/info" { return .json(mapInfo()) }
        if p == "/api/map/where" {      // which map a point is on: a region in full detail, one in outline, a pack's map, or none
            let lat = try pyFloat(try need("lat")), lon = try pyFloat(try need("lon"))
            if let r = RegionAtlas.shared.at(lon, lat) { return .json(["level": r.level, "name": r.name, "id": r.id]) }
            return .json(["level": mapdb != nil || nationalDB != nil ? "other" as Any : NSNull()])
        }
        if p == "/api/places" {
            var nearPt: (Double, Double)? = nil
            if let s = qs("near") {
                let v = try? s.components(separatedBy: ",").map { try pyFloat($0) }
                if let v, v.count >= 2 { nearPt = (v[0], v[1]) } else if let v, v.count == 1 { _ = v; throw PyError(name: "IndexError") }
            }
            let q = String(String.UnicodeScalarView((qs("q") ?? "").unicodeScalars.prefix(100)))
            return .json(try places(q, near: nearPt))
        }
        if p.hasPrefix("/api/map/") { return .json(["error": "not found"], status: 404) }
        return nil
    }

    // ---------------------------------------------------------------- image tiles ----
    func tileBlob(_ db: SQLiteDB?, _ col: String, _ z: Int, _ x: Int, _ y: Int) -> Data? {
        guard let db, z != Int.max, x != Int.max, y != Int.max else { return nil }
        return db.one("SELECT \(col) AS b FROM tile WHERE z=? AND x=? AND y=?", [z, x, y])?["b"]?.blob
    }

    func terrainTile(_ z: Int, _ x: Int, _ y: Int) -> Data? {
        if let png = tileBlob(geoDB(z >= 13 ? "terrain-detail.sqlite" : "terrain.sqlite"), "png", z, x, y) { return png }
        guard z != Int.max, x != Int.max, y != Int.max, z <= 12 else { return nil }
        return RegionAtlas.shared.terrain(z, x, y)      // outside the US elevation: a region built for somewhere else may have it
    }

    func cropTile(_ z: Int, _ x: Int, _ y: Int) -> Data? { tileBlob(cropDB, "png", z, x, y) }

    func aerialTile(_ z: Int, _ x: Int, _ y: Int) -> Data? {
        for name in z >= 17 ? ["aerial-coast.sqlite", "aerial-detail.sqlite"] : ["aerial.sqlite"] {
            if let b = tileBlob(geoDB(name), "jpg", z, x, y) { return b }
        }
        return nil
    }

    func aerialCoastBuilding() -> Bool {
        guard let db = geoDB("aerial-coast.sqlite"), db.tables().contains("meta") else { return false }
        return db.one("SELECT 1 AS one FROM meta WHERE key='built'") == nil
    }

    // ---------------------------------------------------------------- vector tiles ----
    static func unpack(_ blob: Data) -> [[(Double, Double)]] {
        let cnt = blob.count / 4
        guard cnt > 0 else { return [] }
        var a = [Int32](repeating: 0, count: cnt)
        _ = a.withUnsafeMutableBytes { blob.copyBytes(to: $0, count: cnt * 4) }
        var parts: [[(Double, Double)]] = []
        var i = 1
        let np = Int(a[0])
        parts.reserveCapacity(max(0, np))
        for _ in 0..<max(0, np) {
            guard i < cnt else { break }
            let n = Int(a[i]); i += 1
            var x = 0, y = 0
            var pts: [(Double, Double)] = []
            pts.reserveCapacity(max(0, n))
            for _ in 0..<max(0, n) {
                guard i + 1 < cnt else { break }
                x += Int(a[i]); y += Int(a[i + 1]); i += 2
                pts.append((Double(x) / 1e6, Double(y) / 1e6))
            }
            parts.append(pts)
        }
        return parts
    }

    static func tileBounds(_ z: Int, _ x: Int, _ y: Int) -> (Double, Double, Double, Double) {
        let n = Double(1 << z)
        let lon0 = Double(x) / n * 360 - 180, lon1 = Double(x + 1) / n * 360 - 180
        let lat0 = atan(sinh(Double.pi * (1 - Double(2 * (y + 1)) / n))) * radToDeg
        let lat1 = atan(sinh(Double.pi * (1 - Double(2 * y) / n))) * radToDeg
        return (lon0, lat0, lon1, lat1)
    }

    /// Sutherland-Hodgman clip of a ring (tile pixel space) to a box.
    static func clipRing(_ input: [(Double, Double)], _ x0: Double, _ y0: Double, _ x1: Double, _ y1: Double) -> [(Double, Double)] {
        func orz(_ v: Double) -> Double { v == 0 ? 1e-9 : v }
        func clip(_ pts: [(Double, Double)], _ inside: ((Double, Double)) -> Bool, _ inter: ((Double, Double), (Double, Double)) -> (Double, Double)) -> [(Double, Double)] {
            var out: [(Double, Double)] = []
            for i in 0..<pts.count {
                let a = pts[i == 0 ? pts.count - 1 : i - 1], b = pts[i]
                if inside(b) {
                    if !inside(a) { out.append(inter(a, b)) }
                    out.append(b)
                } else if inside(a) { out.append(inter(a, b)) }
            }
            return out
        }
        func ix(_ xv: Double) -> ((Double, Double), (Double, Double)) -> (Double, Double) { { a, b in (xv, a.1 + (b.1 - a.1) * (xv - a.0) / orz(b.0 - a.0)) } }
        func iy(_ yv: Double) -> ((Double, Double), (Double, Double)) -> (Double, Double) { { a, b in (a.0 + (b.0 - a.0) * (yv - a.1) / orz(b.1 - a.1), yv) } }
        var pts = input
        let steps: [(((Double, Double)) -> Bool, ((Double, Double), (Double, Double)) -> (Double, Double))] = [
            ({ $0.0 >= x0 }, ix(x0)), ({ $0.0 <= x1 }, ix(x1)), ({ $0.1 >= y0 }, iy(y0)), ({ $0.1 <= y1 }, iy(y1))]
        for (inside, inter) in steps {
            pts = clip(pts, inside, inter)
            if pts.isEmpty { break }
        }
        return pts
    }

    struct Feature { let layer: String?; let cls: String?; let name: String?; let geom: [[Double]] }

    func mapTile(_ z: Int, _ x: Int, _ y: Int) -> Data {
        let version = dataVersion()
        let key = "\(version)/\(z)/\(x)/\(y)"
        lock.lock(); fresh()
        if let d = tileCache[key] { lock.unlock(); return d }
        lock.unlock()
        let d = buildTile(z, x, y, version)
        lock.lock()
        tileCache[key] = d; tileOrder.append(key)
        if tileOrder.count > 256 { tileCache[tileOrder.removeFirst()] = nil }
        lock.unlock()
        return d
    }

    func buildTile(_ z: Int, _ x: Int, _ y: Int, _ version: String) -> Data {
        let atlas = RegionAtlas.shared
        let db = mapdb
        if db == nil && atlas.isEmpty { return Data(#"{"error":"map not built"}"#.utf8) }
        guard z <= 30, x >= 0, y >= 0, x < Int(Int32.max), y < Int(Int32.max) else { return Data(#"{"z":\#(z),"layers":{}}"#.utf8) }
        let (lon0, lat0, lon1, lat1) = Self.tileBounds(z, x, y)
        let pad = (lon1 - lon0) * 0.04
        let n = Double(1 << z)
        let ext = Double(Self.EXT)
        let fx = Double(x), fy = Double(y)
        let fourPi = 4 * Double.pi
        @inline(__always) func proj(_ lon: Double, _ lat: Double) -> (Double, Double) {
            let px = ((lon + 180) / 360 * n - fx) * ext
            let s = sin(max(-85, min(85, lat)) * degToRad)
            let py = ((0.5 - log((1 + s) / (1 - s)) / fourPi) * n - fy) * ext
            return (px, py)
        }
        let box: [Any?] = [lon0 - pad, lon1 + pad, lat0 - pad, lat1 + pad, z]
        let sqlOrdered = "SELECT f.layer AS layer,f.cls AS cls,f.name AS name,f.geom AS geom FROM feat_idx i JOIN feat f ON f.id=i.id WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=? ORDER BY f.id"
        var sources: [(SQLiteDB, String, [Any?])] = []
        if let db { sources.append((db, sqlOrdered, box)) }
        // The regions built for anywhere in the world, drawn with the same code as the map that came with a pack
        for region in atlas.tileSources(z, lon0 - pad, lat0 - pad, lon1 + pad, lat1 + pad) {
            sources.append((region, RegionAtlas.tileSQL, [lon0 - pad, lon1 + pad, lat0 - pad, lat1 + pad, z, z]))
        }
        if let national = nationalDB {
            var covered = false   // fully covered by the detailed SoCal map
            if z >= 11 {
                let c = Self.SOCAL_CELL, cells = socalCells()
                covered = true
                outer: for i in pyInt((lon0 + 180) / c)...pyInt((lon1 + 180) / c) {
                    for j in pyInt((lat0 + 90) / c)...pyInt((lat1 + 90) / c) where !cells.contains(i * 100000 + j) { covered = false; break outer }
                }
            }
            if !covered { sources.append((national, sqlOrdered, box)) }
        }
        if let w = waterDB {
            sources.append((w, "SELECT f.layer AS layer,f.cls AS cls,f.name AS name,f.geom AS geom FROM feat_idx i JOIN feat f ON f.id=i.id WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=?", box))
        }
        let tol = z <= 6 ? 2.0 : z <= 9 ? 1.2 : z < 15 ? 0.7 : 0.4
        var layerOrder: [String] = []
        var layers: [String: [Feature]] = [:]
        let polyLayers: Set<String> = ["water", "wet", "land", "beach", "bldg", "ocean", "island"]
        for (src, sql, args) in sources {
            src.each(sql, args) { row in
                let layer = row["layer"]?.text
                if layer == "bldg" && z < 15 { return }
                guard let geom = row["geom"]?.blob else { return }
                let parts = Self.unpack(geom)
                let polygon = layer.map(polyLayers.contains) ?? false
                var geo: [[Double]] = []
                for p in parts {
                    var pts = p.map { proj($0.0, $0.1) }
                    if polygon && pts.count > 200 { pts = Self.clipRing(pts, -16, -16, ext + 16, ext + 16) }
                    var q: [Double] = []
                    q.reserveCapacity(pts.count * 2)
                    var last: (Double, Double)? = nil
                    for (px, py) in pts {
                        if let l = last, !(abs(px - l.0) > tol || abs(py - l.1) > tol) { continue }
                        q.append(pyRound(px, 1)); q.append(pyRound(py, 1))
                        last = (px, py)
                    }
                    if polygon && q.count < 6 { continue }
                    if !polygon && layer != "place" && q.count < 4 && z < 12 { continue }
                    if layer == "place", let f = pts.first { q = [pyRound(f.0, 1), pyRound(f.1, 1)] }
                    if !q.isEmpty { geo.append(q) }
                }
                if !geo.isEmpty {
                    let key = layer ?? "\u{0}null"
                    if layers[key] == nil { layerOrder.append(key); layers[key] = [] }
                    layers[key]!.append(Feature(layer: layer, cls: row["cls"]?.text, name: row["name"]?.text, geom: geo))
                }
            }
        }
        if z >= 15 { msBuildings(&layers, &layerOrder, z, x, y, tol) }
        // Service symbols are not baked into tiles; /api/map/pins serves them clustered and filterable.
        var s = "{\"z\":\(z),\"layers\":{"
        s.reserveCapacity(1 << 16)
        for (li, key) in layerOrder.enumerated() {
            if li > 0 { s += "," }
            s += key == "\u{0}null" ? "\"null\"" : Self.jstr(key)
            s += ":["
            for (fi, f) in (layers[key] ?? []).enumerated() {
                if fi > 0 { s += "," }
                s += "[" + (f.cls.map(Self.jstr) ?? "null") + "," + (f.name.map(Self.jstr) ?? "null") + ",["
                for (gi, g) in f.geom.enumerated() {
                    if gi > 0 { s += "," }
                    s += "["
                    for (vi, v) in g.enumerated() {
                        if vi > 0 { s += "," }
                        s += pyFloatRepr(v)
                    }
                    s += "]"
                }
                s += "]]"
            }
            s += "]"
        }
        s += "}}"
        return Data(s.utf8)
    }

    static func jstr(_ t: String) -> String {
        var o = "\""
        for u in t.unicodeScalars {
            switch u {
            case "\"": o += "\\\""
            case "\\": o += "\\\\"
            case "\n": o += "\\n"
            case "\r": o += "\\r"
            case "\t": o += "\\t"
            default:
                if u.value < 0x20 { o += String(format: "\\u%04x", u.value) } else { o.unicodeScalars.append(u) }
            }
        }
        return o + "\""
    }

    // ---------------------------------------------------------------- buildings ----
    struct Footprint { let minx: Int; let maxx: Int; let miny: Int; let maxy: Int; let h: Int; let pts: [(Int, Int)] }

    static func inflate(_ d: Data) -> Data? {
        // zlib stream: 2-byte header, raw deflate, 4-byte checksum. Compression's ZLIB is raw deflate.
        guard d.count > 2 else { return nil }
        let raw = d.subdata(in: (d.startIndex + 2)..<d.endIndex)
        var out = Data()
        let bufSize = 1 << 16
        let dst = UnsafeMutablePointer<UInt8>.allocate(capacity: bufSize)
        defer { dst.deallocate() }
        let sp = UnsafeMutablePointer<compression_stream>.allocate(capacity: 1)
        defer { sp.deallocate() }
        guard compression_stream_init(sp, COMPRESSION_STREAM_DECODE, COMPRESSION_ZLIB) == COMPRESSION_STATUS_OK else { return nil }
        defer { compression_stream_destroy(sp) }
        return raw.withUnsafeBytes { (src: UnsafeRawBufferPointer) -> Data? in
            sp.pointee.src_ptr = src.bindMemory(to: UInt8.self).baseAddress!
            sp.pointee.src_size = raw.count
            while true {
                sp.pointee.dst_ptr = dst
                sp.pointee.dst_size = bufSize
                let st = compression_stream_process(sp, Int32(COMPRESSION_STREAM_FINALIZE.rawValue))
                out.append(dst, count: bufSize - sp.pointee.dst_size)
                if st == COMPRESSION_STATUS_END { return out }
                if st != COMPRESSION_STATUS_OK { return out.isEmpty ? nil : out }
            }
        }
    }

    static func int32s(_ d: Data) -> [Int32] {
        let cnt = d.count / 4
        var a = [Int32](repeating: 0, count: cnt)
        _ = a.withUnsafeMutableBytes { d.copyBytes(to: $0, count: cnt * 4) }
        return a
    }

    func msBucket(_ x14: Int, _ y14: Int) -> [Footprint] {
        let key = x14 &* 1_000_003 &+ y14
        lock.lock(); fresh()
        if let b = bucketCache[key] { lock.unlock(); return b }
        lock.unlock()
        var out: [Footprint] = []
        geoDB("buildings.sqlite")?.each("SELECT data FROM bldg WHERE x=? AND y=?", [x14, y14]) { row in
            guard let z = row["data"]?.blob, let raw = Self.inflate(z) else { return }
            let a = Self.int32s(raw)
            var i = 0
            while i + 3 < a.count {
                let n = Int(a[i]), h = Int(a[i + 1])
                var X = Int(a[i + 2]), Y = Int(a[i + 3]); i += 4
                var pts = [(X, Y)]
                var mnx = X, mxx = X, mny = Y, mxy = Y
                if n > 1 {
                    for _ in 0..<(n - 1) where i + 1 < a.count {
                        X += Int(a[i]); Y += Int(a[i + 1]); i += 2; pts.append((X, Y))
                        mnx = min(mnx, X); mxx = max(mxx, X); mny = min(mny, Y); mxy = max(mxy, Y)
                    }
                }
                out.append(Footprint(minx: mnx, maxx: mxx, miny: mny, maxy: mxy, h: h, pts: pts))
            }
        }
        lock.lock()
        bucketCache[key] = out; bucketOrder.append(key)
        if bucketOrder.count > 192 { bucketCache[bucketOrder.removeFirst()] = nil }
        lock.unlock()
        return out
    }

    func msBuildings(_ layers: inout [String: [Feature]], _ order: inout [String], _ z: Int, _ x: Int, _ y: Int, _ tol: Double) {
        guard let db = geoDB("buildings.sqlite") else { return }
        let span = 1 << (30 - z), pad = span / 25
        let X0 = x * span - pad, Y0 = y * span - pad
        let X1 = X0 + span + 2 * pad, Y1 = Y0 + span + 2 * pad
        let k = Double(Self.EXT) / Double(span)
        if layers["bldg"] == nil { order.append("bldg"); layers["bldg"] = [] }
        guard db.tables().contains("bldg") else { return }
        let reach = 1 << 12
        var seen = Set<Int>()
        for bx in ((X0 - reach) >> 16)...((X1 + reach) >> 16) {
            for by in ((Y0 - reach) >> 16)...((Y1 + reach) >> 16) {
                for f in msBucket(bx, by) {
                    let p0 = f.pts[0].0 &* 4_294_967_311 &+ f.pts[0].1
                    if f.maxx < X0 || f.minx > X1 || f.maxy < Y0 || f.miny > Y1 || seen.contains(p0) { continue }
                    seen.insert(p0)
                    var q: [Double] = []
                    var last: (Double, Double)? = nil
                    for (X, Y) in f.pts {
                        let px = Double(X - x * span) * k, py = Double(Y - y * span) * k
                        if let l = last, !(abs(px - l.0) > tol || abs(py - l.1) > tol) { continue }
                        q.append(pyRound(px, 1)); q.append(pyRound(py, 1)); last = (px, py)
                    }
                    if q.count >= 6 { layers["bldg"]!.append(Feature(layer: "bldg", cls: "building", name: nil, geom: [q])) }
                }
            }
        }
    }

    func bldg3d(_ x14: Int, _ y14: Int) -> Data {
        guard let db = geoDB("buildings.sqlite") else { return Data() }
        let tables = db.tables()
        func rows(_ t: String) -> [Data] {
            guard tables.contains(t) else { return [] }
            return db.query("SELECT data FROM \(t) WHERE x=? AND y=?", [x14, y14]).compactMap { $0["data"]?.blob.flatMap(Self.inflate) }
        }
        var ms = Data()
        for d in rows("bldg") + rows("bldg_socal") { ms.append(d) }
        var n = Int32(truncatingIfNeeded: ms.count / 4).littleEndian
        var out = Data(bytes: &n, count: 4)
        out.append(ms)
        for d in rows("bldg_osm") { out.append(d) }
        return out
    }

    // ---------------------------------------------------------------- resources ----
    /// Point databases holding these resource kinds: the library's resource atlas, the water inventory, crops.
    func resourceDBs(_ kinds: [String]) -> [(SQLiteDB, Bool)] {
        let k = Set(kinds)
        var out: [(SQLiteDB, Bool)] = []
        if !k.subtracting(Self.WATER_KINDS).subtracting(Self.CROP_KINDS).isEmpty, let a = atlasDB { out.append((a, false)) }
        if !k.intersection(Self.WATER_KINDS).isEmpty, let w = waterDB { out.append((w, false)) }
        if !k.intersection(Self.CROP_KINDS).isEmpty, let c = cropDB { out.append((c, true)) }
        return out
    }

    static func resourceSummary(_ cls: String, _ detail: [String: Any]) -> String {
        let keys: [String] = [
            "mineral": ["commod1", "commod2", "dev_stat"], "oil_gas_well": ["WellTypeLabel", "FieldName", "API"],
            "fish_stocking": ["FishType", "Counties"], "boat_launch": ["Water_Body", "Facility_Type"],
            "dam": ["purpose", "river", "county"], "reservoir": ["reservoir_type", "water_type", "state"],
            "orchard": ["crop"], "vineyard": ["crop"], "berries": ["crop"], "perennial_veg": ["crop"],
            "lake": ["water_type", "state"], "pond": ["water_type", "state"]][cls] ?? []
        var out = keys.filter { truthy(detail[$0]) }.map { pyStr(detail[$0]) }
        if truthy(detail["not_drinking_water"]) { out.insert("NOT drinking water", at: 0) }
        if truthy(detail["area_acres"]) { out.insert("\(commasAny(detail["area_acres"]!)) acres", at: 0) }
        if truthy(detail["crop_acres"]), let a = try? pyFloatAny(detail["crop_acres"]!) { out.append("about \(commas(pyRoundInt(a))) acres in this block") }
        if truthy(detail["normal_storage_af"]), let a = try? pyFloatAny(detail["normal_storage_af"]!) { out.append("\(commas(pyInt(a))) acre-ft stored") }
        return out.joined(separator: " · ")
    }

    func resourceNear(_ lat: Double, _ lon: Double, _ cls: String, _ limit: Int) -> [[String: Any]] {
        var out: [[String: Any]] = []
        for (db, _) in resourceDBs([cls]) {
            var radius = 0.15
            var rows: [SQLiteDB.Row] = []
            while radius <= 19.2 {
                rows = db.query("SELECT r.name AS name,r.lat AS lat,r.lon AS lon,r.detail AS detail,r.source AS source FROM spatial s CROSS JOIN resource r ON r.id=s.id WHERE r.kind=? AND s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=?",
                                [cls, lon - radius, lon + radius, lat - radius, lat + radius])
                if cls == "fish_stocking" {
                    var order: [String] = [], byKey: [String: SQLiteDB.Row] = [:]
                    for r in rows {
                        let ft = jsonLoads(r["detail"]?.text)["FishType"]
                        let k = [r["name"]?.text ?? "\u{0}", pyFloatRepr(pyRound(r["lat"]?.double ?? 0, 3)), pyFloatRepr(pyRound(r["lon"]?.double ?? 0, 3)), ft.map { pyRepr($0) } ?? "None"].joined(separator: "\u{1}")
                        if byKey[k] == nil { order.append(k) }
                        byKey[k] = r
                    }
                    rows = order.map { byKey[$0]! }
                }
                if rows.count >= limit { break }
                radius *= 2
            }
            for r in rows {
                let y = r["lat"]?.double ?? 0, x = r["lon"]?.double ?? 0
                let detail = jsonLoads(r["detail"]?.text)
                let summary = Self.resourceSummary(cls, detail) + (cls == "fish_stocking" ? " · historical stocking record" : "")
                let (dist, brg) = Self.haversine(lat, lon, y, x)
                out.append(["cls": cls, "label": Self.HELP[cls]!, "name": nn(r["name"]?.json), "lat": y, "lon": x, "km": pyRound(dist, 2),
                            "dir": brg, "detail": summary, "source": nn(r["source"]?.json)])
            }
        }
        out = stableSort(out) { ($0["km"] as! Double) < ($1["km"] as! Double) }
        return Array(out.prefix(max(0, limit)))
    }

    func resourceView(_ west0: Double, _ south0: Double, _ east0: Double, _ north0: Double, _ zoom: Double, _ kinds0: [String]) -> [String: Any] {
        guard let db = atlasDB else { return ["points": [Any](), "records": 0] }
        let kinds = kinds0.filter { Self.RESOURCE_KINDS.contains($0) }
        if kinds.isEmpty { return ["points": [Any](), "records": 0] }
        let west = max(-180, west0), east = min(180, east0), south = max(-85, south0), north = min(85, north0)
        let cell = 180 / pow(2, max(3, min(18, zoom)))
        let sql = """
            SELECT r.kind AS k,AVG(r.lat) AS lat,AVG(r.lon) AS lon,COUNT(*) AS n,r.name AS name,r.detail AS detail,r.source AS source,COUNT(DISTINCT CASE WHEN r.kind='fish_stocking' THEN r.name ELSE r.source_id END) AS sites
                FROM spatial s CROSS JOIN resource r ON r.id=s.id
                WHERE s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=?
                AND r.kind IN (\(kinds.map { _ in "?" }.joined(separator: ",")))
                GROUP BY r.kind, CAST((r.lon+180)/? AS INTEGER), CAST((r.lat+90)/? AS INTEGER)
                ORDER BY COUNT(*) DESC LIMIT 2001
            """
        let rows = db.query(sql, [west, east, south, north] + kinds.map { $0 as Any? } + [cell, cell])
        var points: [[String: Any]] = []
        var records = 0
        for r in rows.prefix(2000) {
            let k = r["k"]?.text ?? "", sites = Int(r["sites"]?.int ?? 0), n = Int(r["n"]?.int ?? 0)
            records += n
            points.append(["cls": k, "lat": nn(r["lat"]?.json), "lon": nn(r["lon"]?.json), "count": sites, "record_count": n,
                           "name": sites == 1 ? nn(r["name"]?.json) : "\(commas(sites)) locations",
                           "label": Self.HELP[k] ?? k, "detail": sites == 1 ? jsonLoadsAny(r["detail"]?.text) : [String: Any](),
                           "source": nn(r["source"]?.json)])
        }
        return ["points": points, "records": records, "truncated": rows.count > 2000]
    }

    // ---------------------------------------------------------------- pins ----
    /// SQL standing in for the desktop's socal(cls,lon,lat)=0 function: skip national rows the detailed SoCal map already has.
    func socalFilter() -> (String, [Any?]) {
        let classes = socalClasses(), cells = socalCells()
        if classes.isEmpty || cells.isEmpty { return ("", []) }
        let cj = String(data: try! JSONSerialization.data(withJSONObject: classes.sorted()), encoding: .utf8)!
        let nj = "[" + cells.sorted().map(String.init).joined(separator: ",") + "]"
        return (" AND NOT (cls IN (SELECT value FROM json_each(?)) AND CAST((lon+180)/? AS INT)*100000+CAST((lat+90)/? AS INT) IN (SELECT value FROM json_each(?)))",
                [cj, Self.SOCAL_CELL, Self.SOCAL_CELL, nj])
    }

    /// (database, skip-SoCal-duplicates) pairs for service points.
    func poiSources(_ west: Double, _ south: Double, _ east: Double, _ north: Double) -> [(SQLiteDB, Bool)] {
        var out: [(SQLiteDB, Bool)] = []
        let socalBox = east >= -121.4 && west <= -114.1 && north >= 32.5 && south <= 35.9
        let m = mapdb
        if let m, socalBox { out.append((m, false)) }
        if let nat = nationalDB { out.append((nat, socalBox && m != nil)) }
        for region in RegionAtlas.shared.poiSources(west, south, east, north) { out.append((region, false)) }
        return out
    }

    final class Cell {
        var kinds: [(String, Int)] = []
        var count = 0, records = 0
        var lat = 0.0, lon = 0.0
        var bbox: [Double]
        var name: Any, detail: Any, source: Any, cls: String
        init(_ bbox: [Double], _ name: Any, _ detail: Any, _ source: Any, _ cls: String) {
            self.bbox = bbox; self.name = name; self.detail = detail; self.source = source; self.cls = cls
        }
    }

    func pins(_ west0: Double, _ south0: Double, _ east0: Double, _ north0: Double, _ zoom0: Double, _ kinds: [String]) -> [String: Any] {
        let zoom = pyRoundInt(max(3, min(18, zoom0)))
        var cx = 360 / Double(1 << zoom) * (zoom <= 5 ? 72 : zoom <= 8 ? 58 : Self.CLUSTER_PX) / 256
        var cy = cx * max(0.2, cos((south0 + north0) / 2 * degToRad))
        var west = west0, east = east0, south = south0, north = north0
        if zoom >= 17 { cx = 1e-6; cy = 1e-6 } else {
            cy = Double(pyRoundInt(cy / cx * 8)) / 8 * cx
            west = floor((west + 180) / cx) * cx - 180; east = ceil((east + 180) / cx) * cx - 180
            south = floor((south + 90) / cy) * cy - 90; north = ceil((north + 90) / cy) * cy - 90
        }
        let ks = Set(kinds).sorted()
        let args = (max(-180, west), max(-85, south), min(180, east), min(85, north))
        // snapped boxes repeat while panning: keep recent answers (desktop: lru_cache on _pins, keyed with the data version)
        let key = "\(args.0),\(args.1),\(args.2),\(args.3),\(cx),\(cy),\(ks.joined(separator: ",")),\(dataVersion())"
        lock.lock(); fresh()
        if let c = pinCache[key] { lock.unlock(); return c }
        lock.unlock()
        let out = pinsCore(args.0, args.1, args.2, args.3, cx, cy, ks)
        lock.lock()
        pinCache[key] = out; pinOrder.append(key)
        if pinOrder.count > 256 { pinCache[pinOrder.removeFirst()] = nil }
        lock.unlock()
        return out
    }

    func pinsCore(_ west: Double, _ south: Double, _ east: Double, _ north: Double, _ cx: Double, _ cy: Double, _ kinds: [String]) -> [String: Any] {
        let services = kinds.filter { Self.HELP[$0] != nil && !Self.RESOURCE_KINDS.contains($0) }
        let resources = kinds.filter { Self.RESOURCE_KINDS.contains($0) }
        var order: [Cell] = []
        var keyOf: [String: Cell] = [:]   // keyed "ix,iy", in insertion order through `order`
        func add(_ kind: String, _ n: Int, _ sites: Int, _ lat: Double, _ lon: Double, _ x0: Double, _ x1: Double, _ y0: Double, _ y1: Double,
                 _ name: String, _ detail: Any, _ source: String) {
            let key = "\(pyInt((lon + 180) / cx)),\(pyInt((lat + 90) / cy))"
            let c: Cell
            if let e = keyOf[key] { c = e } else {
                c = Cell([x0, y0, x1, y1], name, detail, source, kind); keyOf[key] = c; order.append(c)
            }
            c.lat += lat * Double(sites); c.lon += lon * Double(sites)
            c.count += sites; c.records += n
            if let i = c.kinds.firstIndex(where: { $0.0 == kind }) { c.kinds[i].1 += sites } else { c.kinds.append((kind, sites)) }
            let b = c.bbox; c.bbox = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]
        }
        if !services.isEmpty {
            let q = services.map { _ in "?" }.joined(separator: ",")
            for (db, dedupe) in poiSources(west, south, east, north) {
                let (clause, cargs) = dedupe ? socalFilter() : ("", [])
                let sql = "SELECT cls AS k,COUNT(*) AS n,AVG(lat) AS la,AVG(lon) AS lo,MIN(lon) AS x0,MAX(lon) AS x1,MIN(lat) AS y0,MAX(lat) AS y1,MIN(name) AS name FROM poi "
                    + "WHERE cls IN (\(q)) AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?\(clause) "
                    + "GROUP BY cls, CAST((lon+180)/? AS INT), CAST((lat+90)/? AS INT)"
                db.each(sql, services.map { $0 as Any? } + [west, east, south, north] + cargs + [cx, cy]) { r in
                    let n = Int(r["n"]?.int ?? 0)
                    add(r["k"]?.text ?? "", n, n, r["la"]?.double ?? 0, r["lo"]?.double ?? 0, r["x0"]?.double ?? 0, r["x1"]?.double ?? 0,
                        r["y0"]?.double ?? 0, r["y1"]?.double ?? 0, r["name"]?.text ?? "", [String: Any](), "")
                }
            }
        }
        for (db, isCrop) in resourceDBs(resources) {
            let sql = "SELECT r.kind AS k,COUNT(*) AS n,AVG(r.lat) AS la,AVG(r.lon) AS lo,MIN(r.lon) AS x0,MAX(r.lon) AS x1,MIN(r.lat) AS y0,MAX(r.lat) AS y1,r.name AS name,r.detail AS detail,r.source AS source,"
                + "COUNT(DISTINCT CASE WHEN r.kind='fish_stocking' THEN r.name ELSE r.source_id END) AS sites "
                + "FROM spatial s CROSS JOIN resource r ON r.id=s.id WHERE s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=? "
                + "AND r.kind IN (\(resources.map { _ in "?" }.joined(separator: ","))) GROUP BY r.kind, CAST((r.lon+180)/? AS INT), CAST((r.lat+90)/? AS INT)"
            let g = isCrop && cx > 1e-5 ? 3.0 : 1.0
            db.each(sql, [west, east, south, north] + resources.map { $0 as Any? } + [cx * g, cy * g]) { r in
                let sites = Int(r["sites"]?.int ?? 0)
                let detail: Any = sites == 1 ? jsonLoadsAny((r["detail"]?.text).flatMap { $0.isEmpty ? nil : $0 } ?? "{}") : [String: Any]()
                add(r["k"]?.text ?? "", Int(r["n"]?.int ?? 0), sites, r["la"]?.double ?? 0, r["lo"]?.double ?? 0, r["x0"]?.double ?? 0, r["x1"]?.double ?? 0,
                    r["y0"]?.double ?? 0, r["y1"]?.double ?? 0, r["name"]?.text ?? "", detail, r["source"]?.text ?? "")
            }
        }
        var out: [[String: Any]] = []
        for c in order {
            var d: [String: Any] = [:]
            let lat = c.lat / Double(c.count), lon = c.lon / Double(c.count)
            var kd: [String: Any] = [:]
            for (k, v) in c.kinds { kd[k] = v }
            d["kinds"] = kd; d["count"] = c.count; d["records"] = c.records; d["lat"] = lat; d["lon"] = lon; d["bbox"] = c.bbox
            if c.count == 1 {
                d["cls"] = c.cls; d["name"] = c.name; d["detail"] = c.detail; d["source"] = c.source
                d["label"] = Self.HELP[c.cls] ?? c.cls
                d["summary"] = Self.resourceSummary(c.cls, c.detail as? [String: Any] ?? [:])
            } else {
                var best = c.kinds[0]
                for kv in c.kinds where kv.1 > best.1 { best = kv }
                d["cls"] = best.0; d["name"] = NSNull(); d["detail"] = NSNull(); d["source"] = NSNull()
            }
            d["spot"] = c.count > 1 && c.bbox[2] - c.bbox[0] < 1e-4 && c.bbox[3] - c.bbox[1] < 1e-4
            out.append(d)
        }
        out = stableSort(out) { ($0["count"] as! Int) > ($1["count"] as! Int) }
        let total = out.reduce(0) { $0 + ($1["count"] as! Int) }
        return ["points": Array(out.prefix(2500)), "sites": total, "truncated": out.count > 2500, "cell": [cx, cy]]
    }

    /// The same shop is often mapped twice: drop same-name points within ~80 m.
    static func dedupeNamed(_ rows: [[String: Any]]) -> [[String: Any]] {
        var kept: [String: [(Double, Double)]] = [:]
        var out: [[String: Any]] = []
        for r in rows {
            if let name = r["name"] as? String, !name.isEmpty {
                let k = name.lowercased()
                let la = r["lat"] as? Double ?? 0, lo = r["lon"] as? Double ?? 0
                if (kept[k] ?? []).contains(where: { abs(la - $0.0) < 0.0007 && abs(lo - $0.1) < 0.0009 }) { continue }
                kept[k, default: []].append((la, lo))
            }
            out.append(r)
        }
        return out
    }

    func members(_ west: Double, _ south: Double, _ east: Double, _ north: Double, _ kinds: [String], _ lat: Double, _ lon: Double, limit: Int = 60) -> [String: Any] {
        let services = kinds.filter { Self.HELP[$0] != nil && !Self.RESOURCE_KINDS.contains($0) }
        let resources = kinds.filter { Self.RESOURCE_KINDS.contains($0) }
        var rows: [[String: Any]] = []
        let eps = 1e-4
        if !services.isEmpty {
            let q = services.map { _ in "?" }.joined(separator: ",")
            for (db, dedupe) in poiSources(west, south, east, north) {
                let (clause, cargs) = dedupe ? socalFilter() : ("", [])
                db.each("SELECT cls,name,lon,lat FROM poi WHERE cls IN (\(q)) AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?\(clause) LIMIT 2000",
                        services.map { $0 as Any? } + [west - eps, east + eps, south - eps, north + eps] + cargs) { r in
                    let c = r["cls"]?.text ?? ""
                    rows.append(["cls": c, "label": Self.HELP[c] ?? c, "name": r["name"]?.text ?? "", "lon": r["lon"]?.double ?? 0,
                                 "lat": r["lat"]?.double ?? 0, "summary": "", "detail": [String: Any](), "source": ""])
                }
            }
        }
        for (db, _) in resourceDBs(resources) {
            db.each("SELECT r.kind AS k,r.name AS n,r.lat AS y,r.lon AS x,r.detail AS d,r.source AS src FROM spatial s CROSS JOIN resource r ON r.id=s.id "
                    + "WHERE s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=? AND r.kind IN (\(resources.map { _ in "?" }.joined(separator: ","))) LIMIT 2000",
                    [west - eps, east + eps, south - eps, north + eps] + resources.map { $0 as Any? }) { r in
                let k = r["k"]?.text ?? ""
                let detail = jsonLoads(r["d"]?.text)
                rows.append(["cls": k, "label": Self.HELP[k] ?? k, "name": r["n"]?.text ?? "", "lat": r["y"]?.double ?? 0, "lon": r["x"]?.double ?? 0,
                             "detail": detail, "summary": Self.resourceSummary(k, detail), "source": r["src"]?.text ?? ""])
            }
        }
        rows = rows.filter { Self.RESOURCE_KINDS.contains($0["cls"] as! String) } + Self.dedupeNamed(rows.filter { !Self.RESOURCE_KINDS.contains($0["cls"] as! String) })
        for i in rows.indices {
            let (d, b) = Self.haversine(lat, lon, rows[i]["lat"] as! Double, rows[i]["lon"] as! Double)
            rows[i]["km"] = d; rows[i]["dir"] = b
        }
        func week(_ r: [String: Any]) -> String {
            let v = (r["detail"] as? [String: Any])?["WeekOfPlantStart"]
            return truthy(v) ? pyStr(v) : ""
        }
        rows = stableSort(rows) { pyLess(week($1), week($0)) }   // reverse=True keeps equal items in order
        rows = stableSort(rows) { pyRound($0["km"] as! Double, 3) < pyRound($1["km"] as! Double, 3) }
        return ["items": Array(rows.prefix(limit)), "total": rows.count, "capped": rows.count >= 2000]
    }

    func near(_ lat: Double, _ lon: Double, _ classes: [String], _ limit0: Int) throws -> [[String: Any]] {
        let db = mapdb
        let limit = max(1, min(limit0, 50))
        var out: [[String: Any]] = []
        for cls in classes {
            if Self.RESOURCE_KINDS.contains(cls) { out += resourceNear(lat, lon, cls, limit); continue }
            var sources: [SQLiteDB] = []
            if let db { sources.append(db) }
            if let n = nationalDB { sources.append(n) }
            let packSources = sources.count
            var d = 0.02
            var rows: [(String?, String?, Double, Double)] = []
            while d < 1.5 && rows.count < limit {
                rows = []
                let all = sources + RegionAtlas.shared.poiSources(lon - d, lat - d, lon + d, lat + d)
                for (si, source) in all.enumerated() {
                    var found: [(String?, String?, Double, Double)] = []
                    source.each("SELECT cls,name,lon,lat FROM poi WHERE cls=? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?", [cls, lon - d, lon + d, lat - d, lat + d]) { r in
                        found.append((r["cls"]?.text, r["name"]?.text, r["lon"]?.double ?? 0, r["lat"]?.double ?? 0))
                    }
                    let isMain = db != nil && si == 0
                    if !isMain && si < packSources && db != nil && socalClasses().contains(cls) { found = found.filter { !inSocal($0.2, $0.3) } }
                    rows += found
                }
                var keys: [String] = [], byKey: [String: (String?, String?, Double, Double)] = [:]
                for r in rows {
                    let k = "\(r.0 ?? "\u{0}")\u{1}\(r.1 ?? "\u{0}")\u{1}\(pyRound(r.2, 5))\u{1}\(pyRound(r.3, 5))"
                    if byKey[k] == nil { keys.append(k) }
                    byKey[k] = r
                }
                rows = keys.map { byKey[$0]! }
                d *= 2
            }
            for (c, name, x, y) in rows {
                let (dist, brg) = Self.haversine(lat, lon, y, x)
                out.append(["cls": nn(c), "label": (c.flatMap { Self.HELP[$0] } as Any?) ?? nn(c), "name": nn(name), "lat": y, "lon": x, "km": pyRound(dist, 2), "dir": brg])
            }
        }
        let km: ([String: Any]) -> Double = { $0["km"] as! Double }
        let isRes: ([String: Any]) -> Bool = { Self.RESOURCE_KINDS.contains($0["cls"] as? String ?? "") }
        out = stableSort(out) { km($0) < km($1) }
        out = out.filter(isRes) + Self.dedupeNamed(out.filter { !isRes($0) })
        out = stableSort(out) { km($0) < km($1) }
        var per: [String: Int] = [:]
        var res: [[String: Any]] = []
        for p in out {
            let c = p["cls"] as? String ?? ""
            if per[c, default: 0] < limit { per[c, default: 0] += 1; res.append(p) }
        }
        return res
    }

    static func haversine(_ la1: Double, _ lo1: Double, _ la2: Double, _ lo2: Double) -> (Double, String) {
        let R = 6371.0
        let p1 = la1 * degToRad, p2 = la2 * degToRad
        let dp = p2 - p1, dl = (lo2 - lo1) * degToRad
        let a = pow(sin(dp / 2), 2) + cos(p1) * cos(p2) * pow(sin(dl / 2), 2)
        let d = 2 * R * asin(sqrt(a))
        let deg = atan2(sin(dl) * cos(p2), cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)) * radToDeg
        var b = fmod(deg + 360, 360)
        if b < 0 { b += 360 }
        let dirs = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
        var i = pyInt(pyFloorDiv(b + 22.5, 45)) % 8
        if i < 0 { i += 8 }
        return (d, dirs[i])
    }

    // ---------------------------------------------------------------- places ----
    static func nearBonus(_ near: (Double, Double)?, _ lat: Double, _ lon: Double) -> Double {
        guard let near else { return 0 }
        let km = haversine(near.0, near.1, lat, lon).0
        return 6 * exp(-km / 60) + 2 * exp(-km / 600)
    }

    static func wordSets(_ w: [String]) -> [[String]] {
        var out = [w]
        if w.count > 1 { for i in w.indices { var c = w; c.remove(at: i); out.append(c) } }
        return out
    }

    static func ftsQuery(_ ws: [String]) -> String { ws.map { "\"\($0.replacingOccurrences(of: "\"", with: ""))\"*" }.joined(separator: " ") }

    func homeArea() -> (Double, Double)? {
        let u = Resources.url("web").appendingPathComponent("home-area.json")
        guard let d = FileManager.default.contents(atPath: u.path), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let la = o["lat"], let lo = o["lon"], let a = try? pyFloatAny(la), let b = try? pyFloatAny(lo) else { return nil }
        return (a, b)
    }

    func waterPlaces(_ q: String, limit: Int = 10, near: (Double, Double)?) throws -> [[String: Any]] {
        guard let db = waterDB else { return [] }
        let ws = words(q).map { $0.lowercased() }
        let core = ws.filter { !Self.WATER_WORDS.contains($0) }
        if core.isEmpty { return [] }
        typealias WRow = (String, String?, Double, Double, String?)
        var rows: [WRow] = []
        func conv(_ r: SQLiteDB.Row) -> WRow { (r["kind"]?.text ?? "", r["name"]?.text, r["lat"]?.double ?? 0, r["lon"]?.double ?? 0, r["detail"]?.text) }
        if db.tables().contains("water_fts") {
            var full = false
            for w in Self.wordSets(core) {
                if !rows.isEmpty && w.count < core.count && full { break }
                if !w.isEmpty {
                    rows += db.query("SELECT r.kind AS kind,r.name AS name,r.lat AS lat,r.lon AS lon,r.detail AS detail FROM water_fts f JOIN resource r ON r.id=f.rowid WHERE water_fts MATCH ? LIMIT 6000",
                                     [Self.ftsQuery(w)]).map(conv)
                    if w.count == core.count { full = !rows.isEmpty }
                }
            }
            var seen = Set<String>(), uniq: [WRow] = []
            for r in rows {
                let k = "\(r.0)\u{1}\(r.1 ?? "\u{0}")\u{1}\(r.2)\u{1}\(r.3)\u{1}\(r.4 ?? "\u{0}")"
                if seen.insert(k).inserted { uniq.append(r) }
            }
            rows = uniq
        } else {
            let cond = core.map { _ in "(r.name LIKE ? OR (r.kind='dam' AND r.detail LIKE ?))" }.joined(separator: " AND ")
            rows = db.query("SELECT r.kind AS kind,r.name AS name,r.lat AS lat,r.lon AS lon,r.detail AS detail FROM resource r WHERE r.kind IN ('dam','reservoir','lake') AND r.name!='' AND \(cond) LIMIT 300",
                            core.flatMap { w -> [Any?] in ["%\(w)%", "%\(w)%"] }).map(conv)
        }
        var out: [(Double, [String: Any])] = []
        let kindName = ["dam": "Dam", "reservoir": "Reservoir", "lake": "Lake"]
        for (kind, name0, lat, lon, raw) in rows {
            guard let name = name0 else { throw PyError(name: "TypeError") }
            let d = jsonLoads(raw)
            let other = truthy(d["other_names"]) ? pyStr(d["other_names"]) : ""
            let text = (name + " " + other).lowercased()
            var score = Double(ws.filter { text.contains($0) }.count * 2)
            score += text.contains(core[0]) ? 1 : 0
            score += kind != "dam" ? 1 : 0
            score += name.lowercased() == q.lowercased().trimmingCharacters(in: .whitespacesAndNewlines) ? 3 : 0
            let acres: Any = truthy(d["area_acres"]) ? d["area_acres"]! : truthy(d["surface_acres"]) ? d["surface_acres"]! : 1
            score += log10(try pyFloatAny(acres) + 1) / 3 + Self.nearBonus(near, lat, lon)
            let county: String? = truthy(d["county"]) ? pyStr(d["county"]) + " County" : nil
            let state: String? = truthy(d["state"]) ? pyStr(d["state"]) : nil
            let whereS = [county, state].compactMap { $0 }.joined(separator: ", ")
            let extra = truthy(d["other_names"]) ? " · also called \(pyStr(d["other_names"]))" : ""
            var away = ""
            if let near {
                let km = Self.haversine(near.0, near.1, lat, lon).0
                away = km >= 1.6 ? " · \(groupThousands(String(format: "%.0f", km / 1.609))) mi away" : " · nearby"
            }
            guard let kn = kindName[kind] else { throw PyError(name: "KeyError") }
            out.append((score, ["name": name, "kind": kn, "detail": (whereS.isEmpty ? "United States" : whereS) + extra + away,
                                "lat": lat, "lon": lon, "source": "water", "onMap": true]))
        }
        out = stableSort(out) { $0.0 > $1.0 }
        return out.prefix(limit).map { $0.1 }
    }

    /// 'sw1a2aa' -> 'SW1A 2AA'. nil when the words are not a UK postcode. As postcode_form in engine/address_search.py.
    static func postcodeForm(_ q: String) -> String? {
        guard let re = try? NSRegularExpression(pattern: #"\A\s*([A-Za-z]{1,2}\d[A-Za-z\d]?)\s*(\d[A-Za-z]{2})\s*\z"#),
              let m = re.firstMatch(in: q, range: NSRange(q.startIndex..., in: q)),
              let a = Range(m.range(at: 1), in: q), let b = Range(m.range(at: 2), in: q) else { return nil }
        return q[a].uppercased() + " " + q[b].uppercased()
    }

    /// The words of an address with the house number taken out: '10 Downing Street, London' -> 'Downing Street London'.
    /// The number is written first in Britain, Ireland, France and the US, and after the street in most of Europe
    /// ('Hauptstraße 12, Berlin'): both are read. nil when nothing but a number was written, or when there is no number.
    /// As street_only in engine/address_search.py.
    static func streetOnly(_ q: String) -> String? {
        let house = #"\d+\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?"#       // 10, 10a, 10-12, 12/3
        let parts = q.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        func without(_ pattern: String, _ s: String) -> String {
            guard let re = try? NSRegularExpression(pattern: pattern), let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)),
                  let r = Range(m.range, in: s) else { return s }
            return s.replacingCharacters(in: r, with: "")
        }
        var first = without("\\A" + house + #"\s+(?=\S)"#, parts[0])
        if first == parts[0] { first = without(#"(?<=\S)\s+"# + house + "\\z", parts[0]) }
        if first == parts[0] || first.range(of: #"[^\W\d]"#, options: .regularExpression) == nil { return nil }
        return ([first] + parts.dropFirst()).filter { !$0.isEmpty }.joined(separator: " ")
    }

    static func placeNorm(_ t: String) -> String {
        t.lowercased().replacing(#"[^\w ]+"#, with: " ").replacing(#"\bsaint\b"#, with: "st")
            .split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    func places(_ asked: String, limit: Int = 20, near: (Double, Double)?) throws -> [[String: Any]] {
        let atlas = RegionAtlas.shared
        if AddressSearch.isAddressQuery(asked) {
            let found = AddressSearch.find(asked, limit: min(limit, 8), near: near)
            if !found.isEmpty || atlas.isEmpty { return found }     // no index of house numbers has it: a region's place finder may still have the street
        }
        var q = Self.postcodeForm(asked) ?? asked
        let placeDBs = (placeDB.map { [$0] } ?? []) + atlas.placeDBs()
        if placeDBs.isEmpty { return [] }
        typealias PRow = (name: String, kind: SQLiteDB.Value, detail: SQLiteDB.Value, lat: Double, lon: Double, rank: Int64, source: SQLiteDB.Value)
        func vk(_ v: SQLiteDB.Value) -> String { "\(v)" }
        /// (the places whose names hold the words, whether every word was found). As the nested search() of server.py's places().
        func search(_ text: String) -> ([PRow], Bool) {
            let ws = words(text)
            var rows: [PRow] = []
            var full = false
            for w in ws.isEmpty ? [] : Self.wordSets(ws) {
                if full && w.count < ws.count { break }     // every word matched somewhere: no need to loosen
                if w.isEmpty { continue }
                for db in placeDBs {
                    db.each("SELECT p.name AS name,p.kind AS kind,p.detail AS detail,p.lat AS lat,p.lon AS lon,p.rank AS rank,p.source AS source FROM place_fts f JOIN place p ON p.id=f.rowid "
                            + "WHERE place_fts MATCH ? ORDER BY p.rank, bm25(place_fts) LIMIT 200", [Self.ftsQuery(w)]) { r in
                        rows.append((r["name"]?.text ?? "", r["kind"] ?? .null, r["detail"] ?? .null, r["lat"]?.double ?? 0, r["lon"]?.double ?? 0,
                                     r["rank"]?.int ?? 0, r["source"] ?? .null))
                    }
                }
                if w.count == ws.count { full = !rows.isEmpty }
            }
            var seenRow = Set<String>(), uniq: [PRow] = []
            for r in rows where seenRow.insert("\(r.name)\u{1}\(vk(r.kind))\u{1}\(vk(r.detail))\u{1}\(r.lat)\u{1}\(r.lon)\u{1}\(r.rank)\u{1}\(vk(r.source))").inserted { uniq.append(r) }
            return (uniq, full)
        }
        var (rows, full) = search(q)
        if rows.isEmpty && words(q).isEmpty { return [] }
        if !full, let street = Self.streetOnly(q) {
            // No index of house numbers has this address, and no place is called all of it. Where the map is made from OpenStreetMap
            // alone (anywhere outside the US) the street itself can still be found: show that, and say the house was not.
            let (found, whole) = search(street)
            let streets = found.filter { $0.kind.text == "street" }
            if whole && !streets.isEmpty {
                q = street
                rows = streets.map { r in
                    (name: r.name, kind: r.kind, detail: .text((r.detail.text ?? "") + " · the street: house numbers are not in this map’s data"),
                     lat: r.lat, lon: r.lon, rank: r.rank, source: r.source)
                }
            }
        }
        let ql = Self.placeNorm(q)
        let nat = nationalDB
        var size: [String: Int] = [:]
        func importance(_ r: PRow) -> Int {
            guard let nat, Self.placeNorm(r.name) == ql, size.count <= 80 else { return 3 }
            let k = "\(pyRound(r.lat, 2)),\(pyRound(r.lon, 2))"
            if size[k] == nil {
                let hit = nat.one("SELECT MIN(CASE f.cls WHEN 'city' THEN 0 WHEN 'town' THEN 1 ELSE 2 END) AS m FROM feat_idx i JOIN feat f ON f.id=i.id "
                                  + "WHERE i.minx BETWEEN ? AND ? AND i.miny BETWEEN ? AND ? AND i.minz<=10 AND f.layer='place' AND f.name=?",
                                  [r.lon - 0.08, r.lon + 0.08, r.lat - 0.08, r.lat + 0.08, r.name])
                size[k] = hit?["m"]?.int.map { Int($0) } ?? 2
            }
            return size[k]!
        }
        let home = near ?? homeArea()
        // keys are computed in list order, as Python's sort does
        let keyed: [(PRow, (Int, Int, Int, Int, Int64))] = rows.map { r in
            let nr = Self.placeNorm(r.name)
            let k0 = nr != ql ? 1 : 0
            let k1 = !nr.hasPrefix(ql) ? 1 : 0
            let k2 = (r.rank > 2 && nr == ql) ? importance(r) : 0
            let k3 = -pyRoundInt(Self.nearBonus(home, r.lat, r.lon))
            return (r, (k0, k1, k2, k3, r.rank))
        }
        let sorted = stableSort(keyed) { $0.1 < $1.1 }.map { $0.0 }
        var seen = Set<String>(), out: [[String: Any]] = []
        let pack = packTruthy()
        for r in sorted {
            let k = "\(r.name.lowercased())\u{1}\(pyRound(r.lat, 2))\u{1}\(pyRound(r.lon, 2))"
            if !seen.insert(k).inserted { continue }
            out.append(["name": r.name, "kind": r.kind.json, "detail": r.detail.json, "lat": r.lat, "lon": r.lon, "source": r.source.json,
                        "onMap": pack || (32.5 < r.lat && r.lat < 35.9 && -121.4 < r.lon && r.lon < -114.1) || RegionAtlas.shared.contains(r.lon, r.lat)])
            if out.count >= limit { break }
        }
        let water = try waterPlaces(q, near: near)
        if !water.isEmpty {
            if !Set(words(q).map { $0.lowercased() }).intersection(Self.WATER_WORDS).isEmpty { out = water + out }
            else { out = Array(out.prefix(8)) + water + Array(out.dropFirst(8)) }
        }
        return Array(out.prefix(limit + water.count))
    }

    // ---------------------------------------------------------------- map info ----
    func distinct(_ db: SQLiteDB?, _ sql: String) -> [SQLiteDB.Value] {
        guard let db else { return [] }
        return db.query(sql).compactMap { $0.values.first }
    }

    func mapContents() -> [String: Any] {
        let regionsNow = RegionAtlas.shared.version()       // a region added or taken away is seen without the app being closed
        lock.lock(); fresh()
        if let c = _contents, _contentsFor == regionsNow { lock.unlock(); return c }
        lock.unlock()
        let atlas = RegionAtlas.shared
        var kinds = atlas.classes()
        var boxes: [[Double]] = atlas.regions().map(\.box)
        for db in [mapdb, nationalDB] {
            kinds.formUnion(distinct(db, "SELECT DISTINCT cls FROM poi").compactMap { $0.text })
            if let db, let b = db.one("SELECT MIN(lon) AS a,MIN(lat) AS b,MAX(lon) AS c,MAX(lat) AS d FROM poi"),
               let a = b["a"]?.double, let bb = b["b"]?.double, let c = b["c"]?.double, let d = b["d"]?.double {
                boxes.append([a, bb, c, d])
            }
        }
        for db in [atlasDB, waterDB, cropDB] { kinds.formUnion(distinct(db, "SELECT DISTINCT kind FROM resource").compactMap { $0.text }) }
        let contours = distinct(geoDB("terrain.sqlite"), "SELECT 1 FROM tile WHERE z>=11 LIMIT 1").contains { ($0.int ?? 0) != 0 } || atlas.terrainDetail
        var bounds: Any = NSNull()
        if !boxes.isEmpty {
            let b = [boxes.map { $0[0] }.min()!, boxes.map { $0[1] }.min()!, boxes.map { $0[2] }.max()!, boxes.map { $0[3] }.max()!]
            if !(b[2] - b[0] > 90) { bounds = b }
        }
        let c: [String: Any] = ["layers": kinds.sorted(by: pyLess), "bounds": bounds, "detail": !boxes.isEmpty, "contours": contours]
        lock.lock(); _contents = c; _contentsFor = regionsNow; lock.unlock()
        return c
    }

    func meta(_ db: SQLiteDB?) -> [String: String]? {
        guard let db, db.tables().contains("meta") else { return nil }
        var m: [String: String] = [:]
        db.each("SELECT key,value FROM meta") { r in if let k = r["key"]?.text { m[k] = r["value"]?.text ?? (r["value"].map { "\($0)" } ?? "") } }
        return m
    }

    func hasAerial() -> Bool { ["aerial.sqlite", "aerial-detail.sqlite", "aerial-coast.sqlite"].contains(where: geoExists) }

    func mapInfo() -> [String: Any] {
        var metaOut: [String: Any] = [:]
        if let db = mapdb { db.each("SELECT key,value FROM meta") { r in if let k = r["key"]?.json as? String { metaOut[k] = nn(r["value"]?.json) } } }
        let status = nationalStatus()
        var geoList: [[String: Any]] = []
        let lib = Library.shared.lib
        let feats = commasAny(status["features"] ?? 0)
        let rels: [(String, String, String)] = [
            ("geography/openstreetmap/socal-latest-free.gpkg.zip", "Southern California streets, buildings, water and places (OpenStreetMap)", "Shown on this map"),
            ("geography/openstreetmap/socal-latest.osm.pbf", "Southern California raw OpenStreetMap data", "Coastline taken from it for the map; the rest needs conversion"),
            ("geography/openstreetmap/california-latest.osm.pbf", "All of California raw OpenStreetMap data", "Covered by the national map"),
            ("geography/openstreetmap/us-latest.osm.pbf", "United States roads, waterways and places (OpenStreetMap)", ((status["state"] as? String) == "complete" ? "Shown on this map — " : "Converting — ") + feats + " features (buildings come from Microsoft, below)"),
            ("geography/openstreetmap/us-virgin-islands-latest.osm.pbf", "US Virgin Islands raw OpenStreetMap data", "Raw data — needs conversion before it can be viewed"),
            ("geography/census-gazetteers/2025_Gaz_zcta_national_.zip", "", ""),
        ]
        for (rel, label, st) in rels {
            guard let lib, !label.isEmpty else { continue }
            let p = lib.appendingPathComponent(rel).path
            if let a = try? FileManager.default.attributesOfItem(atPath: p) {
                geoList.append(["label": label, "status": st, "path": rel, "bytes": (a[.size] as? NSNumber)?.intValue ?? 0])
            }
        }
        geoList.append(["label": "US place names (USGS GNIS) and Census 2025 ZIP-code and county centres", "status": "Used by \"Find a place\", together with named lakes, reservoirs and dams", "path": "geography/geographic-names/"])
        if let w = meta(waterDB) {
            let nid = jsonLoads(w["nid"] ?? "{}")
            let states = w.keys.filter { $0.hasPrefix("nhd:") }
            if !nid.isEmpty {
                geoList.append(["label": "Dams (US Army Corps of Engineers National Inventory of Dams)", "status": "\(commasAny(nid["records"] ?? 0)) dams · data updated \(pyStr(nid["data_last_updated"]))"])
            }
            geoList.append(["label": "Lakes, reservoirs and ponds (USGS National Hydrography Dataset, final 2023 edition)",
                            "status": states.isEmpty ? "Downloading / importing" : "\(states.count) of 56 states and territories imported"])
        }
        if geoExists("terrain.sqlite") {
            geoList.append(["label": "Elevation: shaded relief, contour lines, spot heights and 3D (USGS 3DEP, 30 m)", "status": "Whole US and territories"])
        }
        if let m = meta(cropDB) {
            let c = jsonLoads(m["built"] ?? "{}")
            geoList.append(["label": "Orchards, vineyards & berry fields (USDA Cropland Data Layer \(c["year"].map { pyStr($0) } ?? ""))",
                            "status": "\(commasAny(c["cells"] ?? 0)) mapped blocks in \(pyStr(c["states"] ?? 0)) states (lower 48)"])
        }
        if let m = meta(geoDB("buildings.sqlite")) {
            let n = m["buildings"].flatMap { Int($0) }, h = m["osm_heights"].flatMap { Int($0) }
            geoList.append(["label": "Building outlines for the whole US, plus heights for 3D (Microsoft Global ML Building Footprints, 2026 release, ODbL; OpenStreetMap is used where its outlines are complete)",
                            "status": n.map { "\(commas($0)) buildings, shown from street level zoom" + (h.map { " · \(commas($0)) OpenStreetMap buildings with measured heights in 3D" } ?? "") } ?? "Converting"])
        }
        var detail: Any = NSNull()
        if let m = meta(geoDB("aerial-detail.sqlite")) {
            if let b = m["bbox"], !b.isEmpty { detail = b.components(separatedBy: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) } }
            geoList.append(["label": "Full-detail aerial photos, 30-60 cm (USGS NAIP Plus: USDA NAIP and high-resolution orthoimagery)",
                            "status": (m["area"] ?? "") + ((m["built"] ?? "").isEmpty ? " · downloading" : "")])
        }
        var rev: Any = NSNull()
        if let m = meta(geoDB("aerial-coast.sqlite")) {
            rev = (m["rev"] ?? "") + ((m["built"] ?? "").isEmpty ? ".building" : "")
            geoList.append(["label": "Sharpest aerial photos, 25 cm, 2024-25 (NOAA coastal mapping, public domain)",
                            "status": (m["area"] ?? "") + ((m["built"] ?? "").isEmpty ? " · building" : "")])
        }
        var tdetail: Any = NSNull()
        if let m = meta(geoDB("terrain-detail.sqlite")) {
            if let c = m["circle"], !c.isEmpty { tdetail = c.components(separatedBy: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) } }
            geoList.append(["label": "Detailed elevation from 1 m lidar: relief, 5 ft contours, exact heights (USGS 3DEP)",
                            "status": (m["area"] ?? "") + ((m["built"] ?? "").isEmpty ? " · building" : "")])
        }
        if geoExists("aerial.sqlite") {
            geoList.append(["label": "Aerial photos, whole US (USGS The National Map: USDA NAIP and other orthoimagery)", "status": "Zoom 0-15 nationwide, 16 in California and towns · downloading"])
        }
        var out: [String: Any] = ["aerial": hasAerial()]
        let atlas = RegionAtlas.shared
        let pack = packTruthy(), regions = !atlas.isEmpty
        if pack || regions {
            let c = mapContents()
            for (k, v) in c { out[k] = v }
            let legacy = mapdb != nil || nationalDB != nil
            geoList = (regions ? regionDatasets() : []) + (pack && (legacy || !regions) ? packDatasets(c) : legacy && !pack ? geoList : [])
        }
        if regions {
            // Regions built for anywhere in the world: the page lists them, measures in kilometres unless told otherwise, and credits their sources.
            out["regions"] = atlas.summary(); out["units"] = "metric"
            out["data_as_of"] = atlas.dataAsOf().map { $0 as Any } ?? NSNull()
            out["credits"] = atlas.credits()
            out["terrain_levels"] = atlas.hasTerrain ? ["detail": 12, "overview": 9] as Any : NSNull()
        }
        var nat: [String: Any] = [:]
        for (k, v) in status where k != "source" && k != "database" { nat[k] = v }
        out["meta"] = metaOut; out["datasets"] = geoList; out["national"] = nat; out["version"] = dataVersion()
        out["aerial_detail"] = detail; out["aerial_rev"] = rev; out["terrain_circle"] = tdetail
        return out
    }

    /// The panel beside the map, for regions built from open data: each detailed region by name, the outlines together. As server.py's region_datasets.
    func regionDatasets() -> [[String: Any]] {
        var out: [[String: Any]] = []
        let rs = RegionAtlas.shared.regions()
        func num(_ v: Any?) -> Int { (v as? NSNumber)?.intValue ?? 0 }
        func bytes(_ r: RegionAtlas.Region) -> Int { ((r.info["bytes"] as? [String: Any]) ?? [:]).values.reduce(0) { $0 + num($1) } }
        for r in rs where r.level == "detail" {
            let c = (r.info["counts"] as? [String: Any]) ?? [:]
            let finder = (c["finder"] as? [String: Any]) ?? [:]
            let day = String(r.dataAsOf.prefix(10))
            var bits = [day.isEmpty ? "" : "mapped by \(day)", "\(commas(num(c["features"]))) features", "\(commas(num(c["pois"]))) help points"]
            if num(finder["streets"]) != 0 {
                bits.append("\(commas(num(finder["streets"]))) streets" + (num(finder["postcodes"]) != 0 ? " and \(commas(num(finder["postcodes"]))) postcodes" : "") + " in Find a place")
            }
            if r.hasTerrain { bits.append("elevation: relief, contours, 3D") }
            out.append(["label": "\(r.name): every street and path, buildings, land, water, places and help points (OpenStreetMap)",
                        "status": bits.filter { !$0.isEmpty }.joined(separator: " · "), "bytes": bytes(r), "region": r.id])
        }
        let over = rs.filter { $0.level == "overview" }
        if !over.isEmpty {
            let days = over.map { String($0.dataAsOf.prefix(10)) }.filter { !$0.isEmpty }.sorted()
            out.append(["label": "Main roads, towns, rivers, lakes and every help point for \(over.count) more region\(over.count == 1 ? "" : "s") (OpenStreetMap)",
                        "status": over.map(\.name).joined(separator: ", ") + (days.first.map { " · mapped by \($0)" } ?? "") + ". Side streets and buildings are not in these.",
                        "bytes": over.reduce(0) { $0 + bytes($1) }])
        }
        if rs.contains(where: { $0.hasTerrain }) {
            out.append(["label": "Elevation (Copernicus DEM, European Space Agency)",
                        "status": "About 30 m in the detailed regions, 90 m elsewhere. A surface model: it includes treetops and roofs."])
        }
        return out
    }

    func packDatasets(_ have: [String: Any]) -> [[String: Any]] {
        let hasBounds = !(have["bounds"] is NSNull) && have["bounds"] != nil
        let area = hasBounds ? "the area this map details" : "the whole US"
        var out: [[String: Any]] = [["label": "Main roads, towns and water for the whole US (OpenStreetMap, USGS)", "status": "Shown at every zoom level"]]
        let detail = have["detail"] as? Bool ?? false
        if detail {
            out.append(["label": "Streets, places and help points (OpenStreetMap) for \(area)", "status": hasBounds ? "Street detail stops at the edge of this area." : "Shown when you zoom in"])
        } else {
            out.append(["label": "No street detail or help points", "status": "This download has only the base map. Hospitals, water taps and side streets are not in it."])
        }
        if geoExists("places.sqlite") { out.append(["label": "Place names (USGS GNIS, Census 2025 ZIP codes and counties)", "status": "Used by \"Find a town, ZIP or place\""]) }
        if geoExists("terrain.sqlite") {
            out.append(["label": "Elevation (USGS 3DEP)", "status": detail ? "Shaded relief. Contour lines only where this map has fine detail." : "Shaded relief at broad zoom levels"])
        }
        if geoExists("buildings.sqlite") { out.append(["label": "Building outlines (Microsoft Global ML Building Footprints, ODbL; OpenStreetMap)", "status": "For \(area), shown from street level zoom"]) }
        if geoExists("crops.sqlite") { out.append(["label": "Orchards, vineyards and berry fields (USDA Cropland Data Layer)", "status": "For \(area)"]) }
        return out
    }
}

// ------------------------------------------------------------------ addresses (engine/address_search.py) ----

fileprivate enum AddressSearch {
    static let WORDS: [String: String] = [
        "boulevard": "blvd", "avenue": "ave", "av": "ave", "street": "st", "drive": "dr", "road": "rd", "place": "pl", "court": "ct", "lane": "ln",
        "terrace": "ter", "circle": "cir", "highway": "hwy", "parkway": "pkwy", "square": "sq", "north": "n", "south": "s", "east": "e", "west": "w",
        "mount": "mt", "saint": "st"]
    static let WORD_VALUES = Set(WORDS.values)
    static let STATE_NAMES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|District of Columbia|Florida|Georgia|Hawaii|Idaho|"
        + "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
        + "New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Puerto Rico|Rhode Island|"
        + "South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming").components(separatedBy: "|")
    static let STATE_CODES = "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY".components(separatedBy: " ")
    static let STATES: [String: String] = Dictionary(uniqueKeysWithValues: zip(STATE_NAMES.map { $0.lowercased() }, STATE_CODES))
    static let ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".components(separatedBy: " ")
    static let ORDINALS = "zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".components(separatedBy: " ")
    static let TO_DIGITS: [String: String] = {
        var d: [String: String] = [:]
        for (n, w) in ONES.enumerated() { d[w] = String(n) }
        for (n, w) in ORDINALS.enumerated() {
            let suffix = (10...20).contains(n % 100) ? "th" : ([1: "st", 2: "nd", 3: "rd"][n % 10] ?? "th")
            d[w] = "\(n)\(suffix)"
        }
        return d
    }()
    static let TO_WORDS: [String: String] = { var d: [String: String] = [:]; for (k, v) in TO_DIGITS { d[v] = k }; return d }()

    static func normalize(_ text: String) -> String {
        text.lowercased().replacing("[^a-z0-9 ]+", with: " ").split(separator: " ").map { WORDS[String($0)] ?? String($0) }.joined(separator: " ")
    }

    static func isAddressQuery(_ q: String) -> Bool { q.firstMatch(#"^\s*\d[\w-]*\s+\S"#) != nil }

    static func streetForms(_ street: String) -> [String] {
        let words = street.split(separator: " ").map(String.init)
        var forms = [street, words.map { TO_DIGITS[$0] ?? $0 }.joined(separator: " "), words.map { TO_WORDS[$0] ?? $0 }.joined(separator: " ")]
        forms += forms.map { $0.replacing(#"\b(\d+)(st|nd|rd|th)\b"#, with: "$1") }
        if !["n", "s", "e", "w", "ne", "nw", "se", "sw"].contains(words.first ?? "") {
            forms += forms.flatMap { f in ["n", "s", "e", "w"].map { "\($0) \(f)" } }
        }
        var seen = Set<String>()
        return forms.filter { seen.insert($0).inserted }
    }

    static func parts(_ query: String) -> (String, [String], String, String) {
        let t = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let sp = t.split(maxSplits: 1, whereSeparator: { $0.isWhitespace })
        let number = String(sp[0]).uppercased()
        var rem = (sp.count > 1 ? String(sp[1]) : "").lowercased().replacing("[^a-z0-9 ]+", with: " ").split(whereSeparator: { $0.isWhitespace }).map(String.init)
        var postcode = ""
        if let last = rem.last, last.fullMatch(#"\d{5}"#) { postcode = rem.removeLast() }
        var state = ""
        for count in [3, 2, 1] {
            if rem.count <= count { continue }
            let name = rem.suffix(count).joined(separator: " ")
            let code = STATES[name] ?? (count == 1 && STATE_CODES.contains(name.uppercased()) ? name.uppercased() : "")
            if !code.isEmpty { state = code; rem = Array(rem.dropLast(count)); break }
        }
        return (number, normalize(rem.joined(separator: " ")).split(separator: " ").map(String.init), state, postcode)
    }

    static func find(_ query: String, limit: Int = 8, near: (Double, Double)?) -> [[String: Any]] {
        guard isAddressQuery(query), let geo = Library.shared.geo else { return [] }
        let (number, words, state, postcode) = parts(query)
        if words.isEmpty { return [] }
        let dir = geo.appendingPathComponent("addresses")
        var names = ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []).filter { $0.hasSuffix(".sqlite") }.sorted(by: pyLess)
        if !state.isEmpty { names = names.filter { String($0.dropLast(7)).uppercased() == state } }
        var dbs = names.compactMap { Library.shared.db("addresses/" + $0, geo: true) }
        if names.isEmpty, let legacy = Library.shared.db("addresses.sqlite", geo: true) { dbs = [legacy] }
        for count in stride(from: words.count, through: 1, by: -1) {
            let streetWords = Array(words[..<count])
            if streetWords.allSatisfy(WORD_VALUES.contains) { continue }
            let street = streetWords.joined(separator: " ")
            let city = words[count...].joined(separator: " ")
            var rows: [SQLiteDB.Row] = []
            for form in streetForms(street) {
                var conds = ["num IN (?, ?)", "norm >= ?", "norm < ?"]
                var params: [Any?] = [number, number.lowercased(), form, form + "\u{FFFF}"]
                if !state.isEmpty { conds.append("state = ?"); params.append(state) }
                if !postcode.isEmpty { conds.append("zip = ?"); params.append(postcode) }
                if !city.isEmpty { conds.append("lower(city) LIKE ?"); params.append(city + "%") }
                let sql = "SELECT num,street,city,state,zip,lat,lon,norm FROM addr WHERE " + conds.joined(separator: " AND ") + " ORDER BY length(norm),norm LIMIT ?"
                for db in dbs { rows += db.query(sql, params + [limit * 3]) }
            }
            if rows.isEmpty { continue }
            func s(_ r: SQLiteDB.Row, _ k: String) -> String {
                guard let v = r[k] else { return "" }
                switch v { case .text(let t): return t; case .int(let i): return String(i); case .double(let d): return pyFloatRepr(d); default: return "" }
            }
            func rank(_ r: SQLiteDB.Row) -> (Int, Double, String, String, String) {
                var dist = 0.0
                if let near {
                    let la = r["lat"]?.double ?? 0, lo = r["lon"]?.double ?? 0
                    dist = pow(la - near.0, 2) + pow((lo - near.1) * cos(near.0 * degToRad), 2)
                }
                return (s(r, "norm").unicodeScalars.count, dist, s(r, "state"), s(r, "city"), s(r, "street"))
            }
            let ranked = stableSort(rows.map { ($0, rank($0)) }) { a, b in
                let x = a.1, y = b.1
                if x.0 != y.0 { return x.0 < y.0 }
                if x.1 != y.1 { return x.1 < y.1 }
                if x.2 != y.2 { return pyLess(x.2, y.2) }
                if x.3 != y.3 { return pyLess(x.3, y.3) }
                return pyLess(x.4, y.4)
            }
            var results: [[String: Any]] = [], seen = Set<String>()
            for (r, _) in ranked {
                let la = r["lat"]?.double ?? 0, lo = r["lon"]?.double ?? 0
                let key = "\(s(r, "num"))\u{1}\(s(r, "norm"))\u{1}\(la)\u{1}\(lo)"
                if !seen.insert(key).inserted { continue }
                let detail = [s(r, "city"), s(r, "state"), s(r, "zip")].filter { !$0.isEmpty }.joined(separator: ", ")
                results.append(["name": "\(s(r, "num")) \(s(r, "street"))", "kind": "Street address", "detail": detail,
                                "lat": nn(r["lat"]?.json), "lon": nn(r["lon"]?.json), "source": "Overture Maps", "onMap": true])
                if results.count == limit { break }
            }
            return results
        }
        return []
    }
}
