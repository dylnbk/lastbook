import Foundation

/// /api/assistant/status, /api/assistant/local and /api/chat, as desktop server.py answers them with its local AI.
///
/// The local AI is Gemma 4 26B A4B in this process (LocalAI, Engine/AI). Status reports it as `local` with the desktop's
/// state names, so chat.js shows the same lines ("Local AI ready · it starts when you ask", "Checking the local AI file 40%",
/// "No local AI added"...). When it can't answer (no file, wrong file, too big, off, error), status says so plainly, /api/chat
/// answers `model_unavailable`, and chat.js shows the library pages that match the question (/api/retrieve), as on desktop.
struct ChatAPI: RouteGroup {
    /// The routes are installed as the app starts (LastbookApp.init): making the local AI then lets it prewarm at launch.
    init() { _ = LocalAI.shared }

    func handle(_ r: APIRequest) -> APIResponse? {
        switch r.path {
        case "/api/assistant/status":
            return .json(Self.status())
        case "/api/assistant/local":
            guard r.method == "POST" else { return .json(Self.local()) }
            guard let m = LocalAI.shared.model else { return .error("no local AI in this version", status: 404) }
            switch Self.body(r).s("action") {
            case "rescan": m.prewarm("look again"); m.discover()   // Look again: find it, and load it if it is all there
            case "on": m.setEnabled(true)
            case "off": m.setEnabled(false)
            case "stop": m.stop("asked")
            default: return .error("action is rescan, on, off or stop", status: 400)
            }
            return .json(m.status())
        case "/api/chat":
            guard r.method == "POST" else { return .error("POST only.", status: 405) }
            if let b = r.body, b.count > 200_000 { return .error("request too large", status: 413) }
            var req: Rec = [:]
            if let b = r.body, !b.isEmpty {
                guard let o = (try? JSONSerialization.jsonObject(with: b)) as? Rec else { return .error("bad request", status: 400) }
                req = o
            }
            let msgs = Array(((req["messages"] as? [Any]) ?? []).compactMap { $0 as? Rec }.suffix(12))
            let about = pyPrefix(pyTruthy(req["about"]) ? "\(req["about"]!)" : "", 200)
            return Self.chatStream(msgs, about: about.isEmpty ? nil : about)
        default:
            if r.path.hasPrefix("/api/update/") {
                // Desktop checks the website only when asked. The iPad app has no such check: a new build replaces it.
                let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
                return .json(["state": "error", "current": (v as Any?) ?? NSNull(),
                              "message": "On \(Device.name), a newer Lastbook comes as a new app build. Your library stays where it is."])
            }
            return nil
        }
    }

    static func body(_ r: APIRequest) -> Rec {
        guard let b = r.body, let o = try? JSONSerialization.jsonObject(with: b) as? Rec else { return [:] }
        return o
    }

    static func local() -> Rec {
        LocalAI.shared.status
    }

    /// Desktop model_status() with the local AI, plus coverage (server.py adds it to /api/assistant/status).
    static func status() -> Rec {
        let local = local()
        // Busy but coming (checking, measuring, loading) counts as online: a question then waits for it (/api/chat).
        let online = ["in_progress", "ready"].contains(local.s("phase"))
        var coverage: Rec = [:]
        let recs = Catalog.shared.records
        let videos = recs.filter { $0.s("kind") == "video" }
        let pdfs = recs.filter { $0.s("kind") == "pdf" }
        coverage["videos"] = videos.count
        coverage["videos_with_text"] = videos.filter { v in
            Catalog.shared.videoTx[v.s("path")] != nil || ((v["captions"] as? [String]) ?? []).contains { $0.hasSuffix(".vtt") }
        }.count
        coverage["docs_without_text"] = 0   // desktop counts semantic-index gaps; there is no semantic index on iPad
        coverage["pdfs"] = pdfs.count
        coverage["pdfs_image_only"] = pdfs.filter { $0.s("searchability").hasPrefix("image-only") }.count
        coverage["remote_videos"] = 0
        coverage["videos_excluded"] = 0
        return ["model": local["name"] ?? "Gemma 4 26B A4B", "online": online,
                "error": online ? NSNull() : (local.s("message").isEmpty ? local.s("state") : local.s("message")),
                "local": local, "vision": false, "index": LibraryAPI.semanticStatus, "coverage": coverage]
    }

    /// Key under which SchemeHandler tells a stream whether the web view still wants it (false after Stop or leaving).
    static let aliveKey = "lastbook.stream.alive"

    /// Server-sent events, as desktop /api/chat streams them ("data: {...}\n\n"), written while the turn runs.
    static func chatStream(_ msgs: [Rec], about: String?) -> APIResponse {
        APIResponse(status: 200, mime: "text/event-stream", headers: ["Cache-Control": "no-cache"], body: .stream({ send in
            // Called by SchemeHandler on its own thread, which carries the task's "still wanted" check.
            let alive = Thread.current.threadDictionary[aliveKey] as? () -> Bool ?? { true }
            let t = Thread {
                let gone = Locked(false)
                let cancelled: () -> Bool = { gone.value || !alive() }
                let emit: (Rec) -> Void = { ev in
                    if cancelled() { gone.value = true; return }
                    var d = Data("data: ".utf8)
                    d.append((try? JSONSerialization.data(withJSONObject: ev)) ?? Data("{}".utf8))
                    d.append(Data("\n\n".utf8))
                    send(d)
                }
                if let m = LocalAI.shared.model, m.available {
                    Assistant.chat(msgs, about: about, model: m, emit: emit, cancelled: cancelled)
                } else {
                    let l = local()
                    emit(["type": "error", "error": "model_unavailable",
                          "text": l.s("message").isEmpty ? LlamaLocal.unavailable(l.s("state")) : l.s("message")])
                }
                send(nil)
            }
            t.stackSize = 8 << 20
            t.name = "chat"
            t.start()
        }))
    }
}

/// A value read and written from more than one thread.
final class Locked<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var v: T
    init(_ v: T) { self.v = v }
    var value: T {
        get { lock.lock(); defer { lock.unlock() }; return v }
        set { lock.lock(); v = newValue; lock.unlock() }
    }
}
