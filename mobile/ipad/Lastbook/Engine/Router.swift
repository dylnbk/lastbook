import Foundation

/// One request from the web view, already parsed. Mirrors what server.py's Handler sees after unquoting.
struct APIRequest {
    var method: String
    var path: String                 // percent-decoded path, e.g. /api/videos/health
    var query: [String: String]      // first value of each query parameter
    var headers: [String: String]
    var body: Data?

    /// Form decoding as Python's parse_qs (server.py): '+' is a space, first value wins. URLSearchParams sends '+'.
    static func parseQuery(_ raw: String?) -> [String: String] {
        var q: [String: String] = [:]
        for pair in (raw ?? "").split(separator: "&", omittingEmptySubsequences: true) {
            let kv = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            let dec: (Substring) -> String = { s in
                let t = s.replacingOccurrences(of: "+", with: " ")
                return t.removingPercentEncoding ?? t
            }
            let k = dec(kv[0]), v = kv.count > 1 ? dec(kv[1]) : ""
            if q[k] == nil { q[k] = v }
        }
        return q
    }

    func q(_ k: String, _ d: String = "") -> String { query[k] ?? d }
    func qd(_ k: String, _ d: Double) -> Double { Double(query[k] ?? "") ?? d }
    func qi(_ k: String, _ d: Int) -> Int { Int(query[k] ?? "") ?? d }
}

/// An answer: a body in memory, or a file (served in pieces with Range, for video), or a stream (chat).
struct APIResponse {
    enum Body { case data(Data), file(URL), stream((@escaping (Data?) -> Void) -> Void) }
    var status: Int = 200
    var mime: String = "application/json"
    var headers: [String: String] = [:]
    var body: Body = .data(Data())

    static func json(_ obj: Any, status: Int = 200, cache: String = "no-store") -> APIResponse {
        let d = (try? JSONSerialization.data(withJSONObject: obj, options: [.fragmentsAllowed, .withoutEscapingSlashes])) ?? Data("null".utf8)
        return APIResponse(status: status, mime: "application/json", headers: ["Cache-Control": cache], body: .data(d))
    }
    static func error(_ msg: String, status: Int) -> APIResponse { json(["error": msg], status: status) }
    static func notFound() -> APIResponse { error("Not found.", status: 404) }
    static func bytes(_ d: Data, mime: String, cache: String = "max-age=86400") -> APIResponse {
        APIResponse(status: 200, mime: mime, headers: ["Cache-Control": cache], body: .data(d))
    }
    static func file(_ url: URL, mime: String? = nil) -> APIResponse {
        APIResponse(status: 200, mime: mime ?? MIME.of(url.pathExtension), headers: [:], body: .file(url))
    }
    static let noContent = APIResponse(status: 204, mime: "text/plain", headers: ["Cache-Control": "no-store"], body: .data(Data()))
}

/// A group of routes (one file per area, like server.py's sections). Returns nil when the path is not its own.
protocol RouteGroup {
    func handle(_ r: APIRequest) -> APIResponse?
}

enum MIME {
    static func of(_ ext: String) -> String {
        switch ext.lowercased() {
        case "html", "htm": return "text/html; charset=utf-8"
        case "js", "mjs": return "text/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json", "webmanifest": return "application/json"
        case "svg": return "image/svg+xml"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "webp": return "image/webp"
        case "gif": return "image/gif"
        case "mp4", "m4v": return "video/mp4"
        case "webm": return "video/webm"
        case "mkv": return "video/x-matroska"
        case "mp3": return "audio/mpeg"
        case "m4a": return "audio/mp4"
        case "vtt": return "text/vtt; charset=utf-8"
        case "txt": return "text/plain; charset=utf-8"
        case "pdf": return "application/pdf"
        case "woff2": return "font/woff2"
        case "woff": return "font/woff"
        case "ttf": return "font/ttf"
        default: return "application/octet-stream"
        }
    }
}

/// Routes every lastbook://app request: the bundled UI (/ui/, /), library files, and /api/ routes.
final class Router: @unchecked Sendable {
    static let shared = Router()
    private(set) var groups: [RouteGroup] = []
    var web: URL { Resources.url("web") }

    func install(_ gs: [RouteGroup]) { groups = gs }

    /// Logs any request slower than 300 ms, so a device log shows which route is slow (path only: no query, no library text).
    func route(_ r: APIRequest) -> APIResponse {
        let t0 = DispatchTime.now().uptimeNanoseconds
        let a = route0(r)
        let ms = Int((DispatchTime.now().uptimeNanoseconds - t0) / 1_000_000)
        if ms >= 300 { NSLog("Lastbook slow: %d ms %@ (AI %@)", ms, r.path, LocalAI.shared.model?.state ?? "none") }
        return a
    }

    private func route0(_ r: APIRequest) -> APIResponse {
        let p = r.path
        if p == "/" || p == "/index.html" { return .file(web.appendingPathComponent("index.html")) }
        if p == "/sw.js" { return .notFound() }   // no service worker inside the app
        if p.hasPrefix("/ui/") { return staticFile(web, String(p.dropFirst(4))) }
        for g in groups { if let a = g.handle(r) { return a } }
        if p.hasPrefix("/api/") { return .error("Not available in the \(Device.name) app yet.", status: 404) }
        // Library files (videos, thumbnails, transcripts, saved pages), only below library-data and only allowed prefixes.
        if let lib = Library.shared.lib, LibraryFiles.allowed(p) {
            let rel = String(p.dropFirst())
            // An AV1 original on an iPad that can't decode AV1: the same address answers with its H.264 copy (VideoCompat).
            if let sub = VideoCompat.shared.substitute(rel) { return .file(sub, mime: "video/mp4") }
            var a = SavedPages.answer(p, lib: lib) ?? staticFile(lib, rel)   // a saved web page is rewritten as the desktop serves it
            if case .file(let f) = a.body, p.lowercased().hasSuffix(".pdf") { return PDFViewer.answer(rel, f, r.query) }
            if LibraryFiles.active(p) { a.headers["Content-Security-Policy"] = PDFViewer.savedPageCSP }
            return a
        }
        return .notFound()
    }

    /// A file below `root`, refusing anything that escapes it (.., symlinks out).
    func staticFile(_ root: URL, _ rel: String) -> APIResponse {
        guard !rel.isEmpty, !rel.split(separator: "/").contains(where: { $0 == ".." || $0.hasPrefix(".") }) else { return .notFound() }
        let url = root.appendingPathComponent(rel).standardizedFileURL.resolvingSymlinksInPath()
        let base = root.standardizedFileURL.resolvingSymlinksInPath().path
        guard url.path.hasPrefix(base + "/") else { return .notFound() }
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue else { return .notFound() }
        if url.lastPathComponent.lowercased().hasSuffix(".info.json") { return .notFound() }
        return .file(url)
    }
}

enum LibraryFiles {
    /// Same served prefixes as the desktop (server.py SERVED_PREFIXES): library folders the UI links into.
    static let prefixes = ["/documents/", "/pages/", "/videos/", "/videos-external/", "/transcripts/", "/metadata/videos/", "/courses/", "/collections/", "/assets/"]
    static func allowed(_ p: String) -> Bool {
        if p == "/reader.css" { return true }
        if p.lowercased().hasSuffix(".zim") { return false }
        return prefixes.contains { p.hasPrefix($0) }   // a .pdf is answered by PDFViewer, never as the file itself
    }
    /// server.py ACTIVE_SUFFIXES: what a browser would run scripts in. Served with the saved-page policy: shown, never run.
    static let activeSuffixes: Set<String> = ["", "html", "htm", "shtml", "xhtml", "xht", "svg", "svgz", "xml", "xsl", "xslt", "rdf", "mht", "mhtml"]
    static func active(_ p: String) -> Bool {
        let last = p.split(separator: "/").last.map(String.init) ?? ""
        let ext = last.contains(".") ? String(last.split(separator: ".").last!).lowercased() : ""
        return activeSuffixes.contains(ext)
    }
}
