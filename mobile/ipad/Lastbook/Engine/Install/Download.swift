import Foundation
import CommonCrypto
import Security

/// The iPad's one way out: a download the person started (desktop engine/setup/download.py, mirrored).
///
///   - HTTPS to downloads.lastbook.org only, port 443; a redirect is followed only to that host, at most 5 times.
///   - No proxy, no cookies, no cache, no stored credentials (an ephemeral session with all of them off).
///   - Trust: the system's own check must pass AND the chain must end in one of the desktop's pinned roots (DownloadRoots).
///   - <dest>.part grows, with <dest>.part.json beside it (url, size, sha256, bytes done, ETag). Resuming uses Range; the
///     server's answer is checked here, not trusted to If-Range: another ETag on a 206, a 200 instead, or another total size
///     starts the file over (one 200 to a Range is first asked again). After a resume, the bytes already on disk are read
///     once to rebuild the SHA-256; otherwise it is computed as the bytes arrive.
///   - The whole file must match the download list's SHA-256, or it is deleted and the error says so.
/// Nothing here runs unless the person tapped a download button.
final class DownloadControl: @unchecked Sendable {
    private let lock = NSLock()
    private var stopped = false
    func stop() { lock.withLock { stopped = true } }
    func reset() { lock.withLock { stopped = false } }
    var isStopped: Bool { lock.withLock { stopped } }
    func check() throws { if isStopped { throw InstallError.cancelled } }
    /// Waits, but wakes within a quarter second of a stop.
    func sleep(_ s: Double) throws {
        let until = Date().addingTimeInterval(s)
        while Date() < until { try check(); Thread.sleep(forTimeInterval: 0.25) }
        try check()
    }
}

/// SHA-256 whose running state can be saved and restored (CommonCrypto's CC_SHA256_CTX is a plain struct), so a resume of a
/// very large download (All: 388 GB) need not read the whole part file again. The state is saved only at checkpoints taken
/// after the part file is flushed to disk, together with the byte offset it covers; a resume cuts the part file back to that
/// offset. Bytes on disk are then not re-read, so damage to them after the checkpoint would not show in this hash; it shows
/// in the zip's own CRC-32 checks on unpacking (and LocalAI re-checks the model file itself).
struct ResumableSHA256 {
    private var ctx = CC_SHA256_CTX()
    init() { CC_SHA256_Init(&ctx) }
    mutating func update(data: Data) {
        data.withUnsafeBytes { b in _ = CC_SHA256_Update(&ctx, b.baseAddress, CC_LONG(b.count)) }
    }
    func finalize() -> [UInt8] {
        var c = ctx
        var out = [UInt8](repeating: 0, count: Int(CC_SHA256_DIGEST_LENGTH))
        CC_SHA256_Final(&out, &c)
        return out
    }
    static let layout = "CC_SHA256_CTX/\(MemoryLayout<CC_SHA256_CTX>.size)"
    var snapshot: Data { var c = ctx; return withUnsafeBytes(of: &c) { Data($0) } }
    init?(snapshot d: Data) {
        guard d.count == MemoryLayout<CC_SHA256_CTX>.size else { return nil }
        var c = CC_SHA256_CTX()
        withUnsafeMutableBytes(of: &c) { d.copyBytes(to: $0) }
        ctx = c
    }
}

final class Download: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    enum Phase: String { case downloading, verifying }

    /// Where a download may go: the hosts (and host suffixes) its first address and every redirect must be on.
    /// Desktop download.py ALLOWED_HOSTS / ALLOWED_SUFFIXES, narrowed per kind as desktop manifest.py does for the first address:
    /// library files from downloads.lastbook.org only (and no redirect elsewhere); the AI model from huggingface.co, which
    /// redirects to its own download hosts (cas-bridge.xethub.hf.co and siblings under .hf.co).
    struct Hosts: Sendable {
        let first: Set<String>       // the address in the download list
        let hosts: Set<String>       // any address on the way (redirects)
        let suffixes: [String]
        static let library = Hosts(first: [InstallCatalog.host], hosts: [InstallCatalog.host], suffixes: [])
        static let model = Hosts(first: ["huggingface.co"], hosts: ["huggingface.co", "cas-bridge.xethub.hf.co"], suffixes: [".hf.co"])
        func allows(_ host: String?) -> Bool {
            guard let h = host?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")), !h.isEmpty else { return false }
            return hosts.contains(h) || suffixes.contains { h.hasSuffix($0) && h.count > $0.count }
        }
    }
    let rule: Hosts
    static let maxRedirects = 5
    static let timeout: TimeInterval = 30           // seconds without a byte before the connection counts as broken
    static let saveEvery: TimeInterval = 2
    static let networkTries = 6
    static let backoff: [Double] = [2, 4, 8, 15, 30, 30]
    static let userAgent = "Lastbook-iPad-setup/1"

    let url: URL, dest: URL, size: Int64, sha256: String
    var part: URL { dest.deletingLastPathComponent().appendingPathComponent(dest.lastPathComponent + ".part") }
    var meta: URL { dest.deletingLastPathComponent().appendingPathComponent(dest.lastPathComponent + ".part.json") }
    let control: DownloadControl
    let progress: (Int64, Int64, Phase) -> Void
    /// Mac test tool only: replace the pinned roots (an empty list must make every download fail).
    nonisolated(unsafe) static var pinsOverride: [SecCertificate]?

    init(url: URL, dest: URL, size: Int64, sha256: String, control: DownloadControl, hosts: Hosts = .library,
         progress: @escaping (Int64, Int64, Phase) -> Void = { _, _, _ in }) {
        self.rule = hosts
        self.url = url; self.dest = dest; self.size = size; self.sha256 = sha256.lowercased()
        self.control = control; self.progress = progress
    }

    /// An https address on an allowed host, with no user name or password, on port 443. download.url_allowed.
    static func urlAllowed(_ u: URL?, _ rule: Hosts = .library, first: Bool = true) -> Bool {
        guard let u, let c = URLComponents(url: u, resolvingAgainstBaseURL: false) else { return false }
        guard c.scheme == "https", c.user == nil, c.password == nil, c.port == nil || c.port == 443 else { return false }
        let h = c.host?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
        return first ? (h.map(rule.first.contains) ?? false) : rule.allows(h)
    }

    static let pins: [SecCertificate] = DownloadRoots.der.compactMap { Data(base64Encoded: $0).flatMap { SecCertificateCreateWithData(nil, $0 as CFData) } }

    // ------------------------------------------------------------------ files ----
    func doneBytes() -> Int64 { min(ZipInstall.size(part) ?? 0, size) }

    var shaFile: URL { dest.deletingLastPathComponent().appendingPathComponent(dest.lastPathComponent + ".part.sha") }

    func discard() { for u in [part, meta, meta.appendingPathExtension("tmp"), shaFile] { try? FileManager.default.removeItem(at: u) } }

    /// Saves the hash state covering the first `offset` bytes. Call only after the part file is flushed.
    /// Saved hash states are used only for downloads this large (Medium 67 GB, All 388 GB); a smaller one is re-read on resume,
    /// as desktop does, which also notices a part file damaged on disk. Changeable by the Mac test tool.
    nonisolated(unsafe) static var hashStateFrom: Int64 = 20_000_000_000
    var keepsHashState: Bool { size >= Self.hashStateFrom }

    fileprivate func saveHash(_ h: ResumableSHA256, _ offset: Int64) {
        guard keepsHashState else { return }
        try? ZipInstall.writeJSON(shaFile, ["offset": offset, "layout": ResumableSHA256.layout, "ctx": h.snapshot.base64EncodedString(),
                                            "sha256": sha256, "size": size])
    }

    /// A saved hash state for this file covering at most `available` bytes, or nil.
    private func loadHash(_ available: Int64) -> (ResumableSHA256, Int64)? {
        guard let d = ZipInstall.readJSON(shaFile), d["layout"] as? String == ResumableSHA256.layout, d["sha256"] as? String == sha256,
              (d["size"] as? NSNumber)?.int64Value == size, let off = (d["offset"] as? NSNumber)?.int64Value, off > 0, off <= available,
              let b = (d["ctx"] as? String).flatMap({ Data(base64Encoded: $0) }), let h = ResumableSHA256(snapshot: b) else { return nil }
        return (h, off)
    }

    private func loadMeta() -> [String: Any] { ZipInstall.readJSON(meta) ?? [:] }
    private func saveMeta(_ d: [String: Any]) { try? ZipInstall.writeJSON(meta, d) }

    // ------------------------------------------------------------------ run ----
    /// Returns the whole, checked file at `dest`. Throws InstallError: .cancelled, .network, .unknownDownload (checksum),
    /// .noSpace (the disk filled up).
    func run() throws -> URL {
        let fm = FileManager.default
        if ZipInstall.size(dest) == size && !fm.fileExists(atPath: part.path) { return dest }   // finished and checked before
        try fm.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
        var m = loadMeta()
        if !m.isEmpty && ((m["sha256"] as? String) != sha256 || (m["size"] as? NSNumber)?.int64Value != size) { discard(); m = [:] }
        var start = doneBytes()
        if start > 0 && (m["etag"] as? String) == nil { start = 0 }     // nothing tells the server which file those bytes came from
        if !fm.fileExists(atPath: part.path) { fm.createFile(atPath: part.path, contents: nil) }
        var h = ResumableSHA256()
        var restored = false
        if start > 0, keepsHashState, let (sh, off) = loadHash(start) {
            h = sh; start = off; restored = true     // the bytes up to the checkpoint were hashed as they arrived: no re-read
            NSLog("Lastbook download: resuming %@ at %lld from the saved hash state", dest.lastPathComponent, off)
        }
        guard let th = try? FileHandle(forWritingTo: part) else { throw InstallError.folder("Lastbook cannot save the download.") }
        try? th.truncate(atOffset: UInt64(start)); try? th.close()
        if start > 0 && !restored { (h, start) = try rehash(start) }
        var etag = m["etag"] as? String
        var tries = 0
        var ignoredOnce = false
        while start < size {
            try control.check()
            do {
                (start, etag) = try fetch(start: start, etag: etag, hash: &h)
                tries = 0
            } catch let r as Restart {
                if r.whole && !ignoredOnce {    // one 200 to a Range can be a passing hiccup at the edge: ask once more first
                    ignoredOnce = true
                    NSLog("Lastbook download: the server sent the whole of %@ to a resume; asking again", dest.lastPathComponent)
                    try control.sleep(1)
                    continue
                }
                NSLog("Lastbook download: the file on the server changed or ignores Range; starting %@ over", dest.lastPathComponent)
                if let f = try? FileHandle(forWritingTo: part) { try? f.truncate(atOffset: 0); try? f.close() }
                try? FileManager.default.removeItem(at: shaFile)
                h = ResumableSHA256(); start = 0; etag = nil
            } catch let t as Transient {
                if let f = t.diskFull, f { throw InstallError.noSpace(need: size - doneBytes(), available: ZipInstall.freeSpace(dest) ?? 0) }
                let now = doneBytes()           // h already holds every byte written: it is updated just after each write
                tries = now > start ? 0 : tries + 1
                start = now
                etag = (loadMeta()["etag"] as? String) ?? etag
                if tries >= Self.networkTries { throw InstallError.network(t.giveUp) }
                NSLog("Lastbook download: broke at %lld of %lld: %@; trying again", start, size, t.why)
                try control.sleep(Self.backoff[min(tries, Self.backoff.count - 1)])
            }
        }
        let digest = h.finalize().map { String(format: "%02x", $0) }.joined()
        if digest != sha256 {
            discard()
            throw InstallError.unknownDownload("The downloaded file did not match its published checksum, so it was deleted. Tap Retry to download it again.")
        }
        try? fm.removeItem(at: dest)
        try ZipInstall.move(part, dest)
        try? fm.removeItem(at: meta)
        try? fm.removeItem(at: shaFile)
        return dest
    }

    private func rehash(_ n: Int64) throws -> (ResumableSHA256, Int64) {
        var h = ResumableSHA256()
        guard let f = try? FileHandle(forReadingFrom: part) else { return (h, 0) }
        defer { try? f.close() }
        var done: Int64 = 0
        while done < n {
            try control.check()
            let ok: Bool = autoreleasepool {
                guard let d = try? f.read(upToCount: Int(min(8 << 20, n - done))), !d.isEmpty else { return false }
                h.update(data: d); done += Int64(d.count)
                progress(done, n, .verifying)
                return true
            }
            if !ok { break }
        }
        return (h, done)
    }

    // ------------------------------------------------------------------ one request ----
    struct Restart: Error { let etag: String?; let whole: Bool }
    struct Transient: Error {
        let why: String; var diskFull: Bool? = nil
        var giveUp: String {
            "The connection kept breaking (\(why)). Check the internet connection, then tap Retry; the download carries on from where it stopped."
        }
    }

    // state of the request in flight, touched on the session's serial queue and read after it completes
    private var cur: (start: Int64, etag: String?, hash: ResumableSHA256, file: FileHandle?, meta: [String: Any], saved: Date)?
    private var failure: Error?
    private var redirects = 0
    private var tlsRefused = false

    private func fetch(start: Int64, etag: String?, hash: inout ResumableSHA256) throws -> (Int64, String?) {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.urlCache = nil
        cfg.httpCookieStorage = nil
        cfg.httpShouldSetCookies = false
        cfg.httpCookieAcceptPolicy = .never
        cfg.urlCredentialStorage = nil
        cfg.connectionProxyDictionary = [:]
        cfg.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        cfg.timeoutIntervalForRequest = Self.timeout
        cfg.timeoutIntervalForResource = 7 * 24 * 3600
        cfg.waitsForConnectivity = false
        cfg.tlsMinimumSupportedProtocolVersion = .TLSv12
        cfg.httpAdditionalHeaders = nil
        let q = OperationQueue(); q.maxConcurrentOperationCount = 1
        let session = URLSession(configuration: cfg, delegate: self, delegateQueue: q)
        defer { session.invalidateAndCancel() }
        guard Self.urlAllowed(url, rule) else { throw InstallError.network("Lastbook refused to download from an address it does not know.") }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        req.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        if start > 0 {
            req.setValue("bytes=\(start)-", forHTTPHeaderField: "Range")
            if let etag { req.setValue(etag, forHTTPHeaderField: "If-Range") }
        }
        let done = DispatchSemaphore(value: 0)
        let h0 = hash
        q.addOperation {
            self.cur = (start, etag, h0, nil, [:], Date())
            self.failure = nil; self.redirects = 0; self.tlsRefused = false
        }
        q.waitUntilAllOperationsAreFinished()
        let task = session.dataTask(with: req)
        finished = { done.signal() }
        task.resume()
        while done.wait(timeout: .now() + 0.25) == .timedOut {
            if control.isStopped { task.cancel() }
        }
        var result: (Int64, String?, ResumableSHA256)!
        var err: Error?
        q.addOperation {
            try? self.cur?.file?.synchronizeFile()
            try? self.cur?.file?.close()
            if let c = self.cur {
                result = (c.start, c.etag, c.hash)
                if c.file != nil && c.start > 0 { self.saveHash(c.hash, c.start) }   // flushed just above
                if !c.meta.isEmpty { var md = c.meta; md["bytes_done"] = c.start; self.saveMeta(md) }   // only once the server answered
            }
            err = self.failure
        }
        q.waitUntilAllOperationsAreFinished()
        hash = result.2
        if control.isStopped { throw InstallError.cancelled }
        if let err { throw err }
        if result.0 < size { throw Transient(why: "the connection closed early") }
        return (result.0, result.1)
    }

    private var finished: () -> Void = {}

    func urlSession(_ s: URLSession, task: URLSessionTask, willPerformHTTPRedirection r: HTTPURLResponse, newRequest: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        redirects += 1
        guard redirects <= Self.maxRedirects, Self.urlAllowed(newRequest.url, rule, first: false) else {
            failure = InstallError.network(redirects > Self.maxRedirects ? "The download server sent Lastbook round in circles."
                                                                           : "Lastbook refused to download from an address it does not know.")
            completionHandler(nil); task.cancel(); return
        }
        completionHandler(newRequest)
    }

    func urlSession(_ s: URLSession, task: URLSessionTask, didReceive ch: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard ch.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust, let trust = ch.protectionSpace.serverTrust,
              rule.allows(ch.protectionSpace.host) else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        if Self.trusted(trust, host: ch.protectionSpace.host) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            tlsRefused = true
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    /// The system's own check, then the same chain against the pinned roots only.
    static func trusted(_ trust: SecTrust, host: String) -> Bool {
        SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString))
        guard SecTrustEvaluateWithError(trust, nil) else { return false }
        let pins = pinsOverride ?? Self.pins
        guard !pins.isEmpty else { return false }
        SecTrustSetAnchorCertificates(trust, pins as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        return SecTrustEvaluateWithError(trust, nil)
    }

    func urlSession(_ s: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard var c = cur, let r = response as? HTTPURLResponse else { completionHandler(.cancel); return }
        func refuse(_ e: Error) { failure = e; completionHandler(.cancel) }
        guard Self.urlAllowed(r.url, rule, first: false) else { return refuse(InstallError.network("Lastbook refused to download from an address it does not know.")) }
        let st = r.statusCode
        if (300..<400).contains(st) { return refuse(InstallError.network("The download server answered with a move Lastbook does not follow.")) }
        if st >= 500 { return refuse(Transient(why: "HTTP \(st)")) }
        if st >= 400 || st == 416 { return refuse(InstallError.network("The download server refused the file (HTTP \(st)).")) }
        let newEtag = r.value(forHTTPHeaderField: "ETag")
        if st == 206 {
            let cr = Self.contentRange(r.value(forHTTPHeaderField: "Content-Range"))
            if cr == nil || cr!.0 != c.start || cr!.1 != size || (c.etag != nil && newEtag != nil && newEtag != c.etag) {
                return refuse(Restart(etag: newEtag, whole: false))
            }
        } else if st == 200 {
            if c.start > 0 { return refuse(Restart(etag: newEtag, whole: true)) }
            if let l = r.value(forHTTPHeaderField: "Content-Length"), let n = Int64(l), n != size {
                return refuse(InstallError.unknownDownload("The file on the server is \(n) bytes, not the \(size) Lastbook expects."))
            }
        } else {
            return refuse(Transient(why: "HTTP \(st)"))
        }
        c.etag = newEtag ?? c.etag
        guard let f = try? FileHandle(forWritingTo: part) else { return refuse(InstallError.folder("Lastbook cannot save the download.")) }
        do { try f.seek(toOffset: UInt64(c.start)) } catch { return refuse(InstallError.folder("Lastbook cannot save the download.")) }
        c.file = f
        c.meta = ["url": url.absoluteString, "size": size, "sha256": sha256, "bytes_done": c.start]
        if let e = c.etag { c.meta["etag"] = e }
        saveMeta(c.meta)
        c.saved = Date()
        cur = c
        completionHandler(.allow)
    }

    func urlSession(_ s: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard var c = cur, let f = c.file, failure == nil else { return }
        if control.isStopped { dataTask.cancel(); return }
        let room = Int(min(Int64(data.count), size - c.start))
        let piece = room < data.count ? data.prefix(room) : data
        do { try f.write(contentsOf: piece) } catch {
            let full = (error as NSError).code == Int(ENOSPC) || (error as NSError).code == NSFileWriteOutOfSpaceError
                || ((error as NSError).userInfo[NSUnderlyingErrorKey] as? NSError)?.code == Int(ENOSPC)
            failure = Transient(why: "a write failed", diskFull: full || (ZipInstall.freeSpace(dest) ?? 1 << 40) < 16 << 20)
            dataTask.cancel(); return
        }
        c.hash.update(data: piece)
        c.start += Int64(piece.count)
        if Date().timeIntervalSince(c.saved) > Self.saveEvery {
            try? f.synchronizeFile()
            saveHash(c.hash, c.start)
            c.meta["bytes_done"] = c.start
            saveMeta(c.meta)
            c.saved = Date()
        }
        cur = c
        progress(c.start, size, .downloading)
        if c.start >= size { dataTask.cancel() }   // anything past the declared size is not read
    }

    func urlSession(_ s: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        defer { finished() }
        if failure != nil { return }
        if let c = cur, c.start >= size { return }
        guard let e = error as? URLError else { if let error { failure = Transient(why: error.localizedDescription) }; return }
        if e.code == .cancelled {
            if tlsRefused { failure = Self.tlsError }
            return    // a stop: run() sees the control
        }
        switch e.code {
        case .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot, .serverCertificateNotYetValid,
             .clientCertificateRejected, .secureConnectionFailed:
            failure = Self.tlsError
        case .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff:
            failure = Transient(why: "this \(Device.name) is offline")
        default:
            failure = Transient(why: e.localizedDescription)
        }
    }

    static let tlsError = InstallError.network("Lastbook could not confirm it is talking to the download server, so it stopped. "
                                               + "A network that intercepts secure connections (some offices, hotels) can cause this.")

    /// (start, total) from "bytes 100-199/1000".
    static func contentRange(_ v: String?) -> (Int64, Int64)? {
        guard let v, v.hasPrefix("bytes ") else { return nil }
        let rest = v.dropFirst(6)
        let parts = rest.split(separator: "/")
        guard parts.count == 2, let total = Int64(parts[1]), let s = parts[0].split(separator: "-").first, let start = Int64(s) else { return nil }
        return (start, total)
    }
}

/// Downloads a listed file for the install screen: the space check first, then Download, then Installer.install.
enum DownloadInstall {
    /// Where downloads go: <library>.setup/downloads/<file name> (desktop: the work folder's downloads/).
    static func dest(for f: CatalogFile, target: URL) throws -> URL {
        try ZipInstall.ensureWorkFolder(target).appendingPathComponent("downloads/\(f.name)")
    }

    /// Bytes already downloaded for this file (a partial or finished download), for "Resume (X of Y)".
    static func partial(_ f: CatalogFile, target: URL) -> Int64 {
        let d = ZipInstall.workFolder(target).appendingPathComponent("downloads/\(f.name)")
        return max(ZipInstall.size(d.deletingLastPathComponent().appendingPathComponent(f.name + ".part")) ?? 0, ZipInstall.size(d) ?? 0)
    }

    static func discardPartial(_ f: CatalogFile, target: URL) {
        let dir = ZipInstall.workFolder(target).appendingPathComponent("downloads")
        for n in [f.name, f.name + ".part", f.name + ".part.json", f.name + ".part.json.tmp"] { try? FileManager.default.removeItem(at: dir.appendingPathComponent(n)) }
    }

    /// Room for what is still to download, its unpacked size and the margin; InstallError.noSpace with the numbers otherwise.
    static func checkSpace(_ e: CatalogEntry, _ f: CatalogFile, target: URL) throws {
        let margin: Int64 = e.kind == .map ? MapInstall.spaceMargin : 512 << 20
        let need = (f.size - partial(f, target: target)) + (e.installedBytes ?? 0) + margin
        if let free = ZipInstall.freeSpace(target.deletingLastPathComponent()), need > free { throw InstallError.noSpace(need: need, available: free) }
    }

    enum Step { case downloading(Int64, Int64), verifying(Int64, Int64), installing(Installer.Phase, Int64, Int64) }

    /// The whole thing for one listed entry. `target`: the library a pack becomes, or the open library for the others.
    static func run(_ e: CatalogEntry, state: String?, target: URL, control: DownloadControl, settings: URL = MapInstall.defaultSettings,
                    progress: @escaping (Step) -> Void) throws -> Installer.Report {
        guard e.available, let f = e.files.first, e.files.count == 1 else {
            throw InstallError.notNeeded("\(e.title) isn’t on the download site yet.")
        }
        try checkSpace(e, f, target: target)
        guard let url = URL(string: f.url), Download.urlAllowed(url, .library) else { throw InstallError.network("Lastbook refused to download from an address it does not know.") }
        let d = Download(url: url, dest: try dest(for: f, target: target), size: f.size, sha256: f.sha256, control: control) { done, total, ph in
            progress(ph == .downloading ? .downloading(done, total) : .verifying(done, total))
        }
        let file = try d.run()
        let r = try Installer.install(file: file, state: state, verifiedSHA: f.sha256, settings: settings,
                                      progress: { progress(.installing($0, $1, $2)) }, isCancelled: { control.isStopped })
        try? FileManager.default.removeItem(at: file)       // ours: the installed copy is what stays
        return r
    }
}
