import Foundation
import SwiftUI

/// A library passage or video moment the answer points to (the engine's source objects).
struct Source: Codable, Hashable, Identifiable {
    var key: String = ""
    var kind: String = ""
    var docId: String = ""
    var title: String = ""
    var publisher: String = ""
    var year: String = ""
    var page: Int?
    var pages: Int?
    var t: Double?
    var thumb: String?
    var excerpt: String?
    var lat: Double?
    var lon: Double?
    var id: String { "\(key)|\(docId)|\(page ?? 0)|\(t ?? -1)" }

    init(_ r: Rec) {
        key = r.s("key"); kind = r.s("kind"); docId = r.str("doc_id") ?? r.s("id"); title = r.str("title") ?? r.s("name")
        publisher = r.s("publisher"); year = r.s("year")
        page = (r["page"] as? NSNumber)?.intValue; pages = (r["pages"] as? NSNumber)?.intValue
        t = (r["t"] as? NSNumber)?.doubleValue; thumb = r.str("thumb"); excerpt = r.str("excerpt") ?? r.str("text")
        lat = (r["lat"] as? NSNumber)?.doubleValue; lon = (r["lon"] as? NSNumber)?.doubleValue
    }
    init(key: String, kind: String, docId: String, title: String, publisher: String, page: Int? = nil, t: Double? = nil, excerpt: String? = nil) {
        self.key = key; self.kind = kind; self.docId = docId; self.title = title; self.publisher = publisher; self.page = page; self.t = t; self.excerpt = excerpt
    }

    /// The desktop route for this source: the exact page, video moment or map place.
    var route: String {
        let enc = docId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? docId
        if kind == "place", let lat, let lon { return "#/map?lat=\(lat)&lon=\(lon)&z=16&pin=\(title.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")" }
        if kind == "video" { return "#/watch/\(enc)" + (t.map { "?t=\(Int($0))" } ?? "") }
        return "#/read/\(enc)" + (page.map { "?p=\($0)" } ?? "")
    }
    var whereText: String {
        if kind == "video" { return t.map { "at \(Fmt.clock($0))" } ?? "video" }
        if let page { return "page \(page)" + (pages.map { " of \($0)" } ?? "") }
        return ""
    }
}

struct Step: Codable, Hashable { var name: String; var text: String; var done: Bool }

struct Message: Codable, Identifiable, Hashable {
    var id = UUID()
    var role: String                 // "user" | "assistant"
    var content: String = ""
    var state: String?               // starting, thinking, searching, reading, writing (while running)
    var pct: Int?
    var steps: [Step] = []
    var found: [Source] = []         // pages found so far (while writing)
    var cited: [Source] = []         // the answer's linked sources
    var consulted: [Source] = []
    var fallback: [Source] = []      // no AI: the library pages that match
    var invalid: [String] = []
    var error: String?
    var offline = false
    var stopped = false
    var running = false
}

struct Conversation: Codable, Identifiable, Hashable {
    var id = UUID()
    var title: String
    var created = Date()
    var updated = Date()
    var messages: [Message] = []
    var about: String?
    var aboutLabel: String?
    var fixture = false              // design-review sample, never an AI result
}

/// Model status from /api/assistant/status `local`, following the engine's status contract (mobile/ipad/README.md):
/// the look is chosen by `phase` (in_progress / ready / unavailable / incompatible / failed), falling back to `state` on
/// older engines. Starting up is normal and neutral; only incompatible (amber) and failed (red) use warning colour.
struct AIStatus: Equatable {
    enum Tone { case ready, starting, unavailable, incompatible, failed }
    var ok: Bool          // a question waits for the model and gets a written answer (engine `online`)
    var line: String
    var busy: Bool        // in progress: spinner, poll
    var state: String
    var tone: Tone

    static func from(_ s0: Rec?) -> AIStatus {
        var s = s0
        #if DEBUG   // simulator review of status styling only: -LBAIState loading|checking|ready|absent|error (never in release)
        if let f = UserDefaults.standard.string(forKey: "LBAIState") { s = ["online": f == "ready" || f == "loading", "local": ["state": f, "pct": 42, "message": f == "error" ? "The local AI couldn’t start." : ""]] }
        #endif
        guard let s else { return AIStatus(ok: false, line: "Local AI status unknown", busy: false, state: "error", tone: .failed) }
        let l = (s["local"] as? Rec) ?? [:]
        let st = l.s("state"), msg = ipad(l.s("message"))
        let pct = (l["pct"] as? NSNumber)?.intValue
        let phase = l.str("phase") ?? Self.phaseOf(st)
        let online = (s["online"] as? Bool) ?? (phase == "ready" || phase == "in_progress")
        switch phase {
        case "in_progress":
            let line = st == "loading" ? "Loading local AI…" + (pct.map { " \($0)%" } ?? "") : "Preparing local AI…" + (st == "checking" ? pct.map { " \($0)%" } ?? "" : "")
            return .init(ok: online, line: line, busy: true, state: st, tone: .starting)
        case "ready": return .init(ok: online, line: "Local AI ready", busy: false, state: st, tone: .ready)
        case "unavailable": return .init(ok: false, line: st == "off" ? "Local AI is off" : "No local AI on this \(Device.name)", busy: false, state: st, tone: .unavailable)
        case "incompatible":
            return .init(ok: false, line: msg.isEmpty ? (st == "too_big" ? "The local AI doesn’t fit this \(Device.name)" : "The local AI file isn’t the right one") : msg, busy: false, state: st, tone: .incompatible)
        default: return .init(ok: false, line: msg.isEmpty ? "The local AI couldn’t start" : msg, busy: false, state: st, tone: .failed)
        }
    }
    /// The 0.3 phase for engines that don't send it yet.
    static func phaseOf(_ st: String) -> String {
        switch st {
        case "checking", "measuring", "loading": return "in_progress"
        case "ready", "stopped": return "ready"
        case "absent", "off", "": return "unavailable"
        case "wrong", "too_big": return "incompatible"
        default: return "failed"
        }
    }
    /// Engine messages are shared with the desktop: say iPad, not computer.
    static func ipad(_ m: String) -> String { m.replacingOccurrences(of: "this computer", with: "this \(Device.name)").replacingOccurrences(of: "This computer", with: "This \(Device.name)") }
}

/// Conversations on this iPad only (Application Support), and the one place a question is sent: the in-process /api/chat.
@MainActor final class ChatStore: ObservableObject {
    @Published var chats: [Conversation] = []
    @Published var selection: Conversation.ID?
    @Published var status = AIStatus(ok: false, line: "Preparing local AI…", busy: true, state: "checking", tone: .starting)
    @Published var keep: Bool { didSet { UserDefaults.standard.set(keep, forKey: "lb.design.keepChats"); keep ? persist() : forgetFile() } }
    @Published var pendingAbout: ChatAbout?
    private var cancels: [UUID: Locked<Bool>] = [:]

    static var file: URL {
        let d = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Chats", isDirectory: true)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d.appendingPathComponent("chats.json")
    }

    init() {
        keep = UserDefaults.standard.object(forKey: "lb.design.keepChats") as? Bool ?? true
        if keep, let d = try? Data(contentsOf: Self.file), let c = try? JSONDecoder().decode([Conversation].self, from: d) {
            chats = c.map { var x = $0; for i in x.messages.indices where x.messages[i].running { x.messages[i].running = false; x.messages[i].error = x.messages[i].error ?? "Stopped when the app closed." }; return x }
        }
        #if DEBUG
        if UserDefaults.standard.bool(forKey: "LBResetChats") { chats = []; try? FileManager.default.removeItem(at: Self.file) }
        if UserDefaults.standard.bool(forKey: "LBChatFixture") { let f = Self.fixture(); chats.removeAll { $0.fixture }; chats.insert(f, at: 0); selection = f.id }
        #endif
    }

    var current: Conversation? { chats.first { $0.id == selection } }
    func isRunning(_ id: Conversation.ID?) -> Bool { chats.first { $0.id == id }?.messages.last?.running ?? false }

    func newChat() { selection = nil }

    func delete(_ id: Conversation.ID) {
        cancels[id]?.value = true
        chats.removeAll { $0.id == id }
        if selection == id { selection = nil }
        persist()
    }
    func deleteAll() { for c in chats { cancels[c.id]?.value = true }; chats = []; selection = nil; persist() }

    func persist() {
        guard keep else { return }
        let save = chats.filter { !$0.fixture }.prefix(100)
        if let d = try? JSONEncoder().encode(Array(save)) { try? d.write(to: Self.file, options: [.atomic, .completeFileProtection]) }
    }
    private func forgetFile() { try? FileManager.default.removeItem(at: Self.file) }

    func refreshStatus() async {
        repeat {
            let s = await LocalAPI.rec("/api/assistant/status")
            status = AIStatus.from(s)
            if status.busy { try? await Task.sleep(for: .seconds(1)) }
        } while status.busy && !Task.isCancelled
    }

    func setAI(_ action: String) async {
        _ = await Task.detached { LocalAPI.post("/api/assistant/local", json: ["action": action]) }.value
        await refreshStatus()
    }

    private func idx(_ id: UUID) -> Int? { chats.firstIndex { $0.id == id } }

    // MARK: sending

    func send(_ text: String, about: ChatAbout?) {
        let q = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        var cid = selection
        if cid == nil || idx(cid!) == nil {
            let c = Conversation(title: String(q.prefix(70)), about: about?.about, aboutLabel: about?.label)
            chats.insert(c, at: 0); cid = c.id; selection = c.id
        }
        guard let id = cid, let i = idx(id), !isRunning(id) else { return }
        let history = Self.history(chats[i].messages)
        chats[i].messages.append(Message(role: "user", content: q))
        chats[i].messages.append(Message(role: "assistant", state: "thinking", running: true))
        chats[i].updated = Date()
        persist()
        let about = chats[i].about
        let cancel = Locked(false); cancels[id] = cancel
        Task { await run(id, q, history: history, about: about, cancel: cancel) }
    }

    func stop(_ id: Conversation.ID?) { if let id { cancels[id]?.value = true } }

    func retry(_ id: Conversation.ID) {
        guard let i = idx(id), chats[i].messages.count >= 2, !isRunning(id) else { return }
        let q = chats[i].messages[chats[i].messages.count - 2].content
        chats[i].messages.removeLast(2)
        selection = id
        send(q, about: nil)
    }

    /// Earlier answers' [S#] keys mean something only in their own turn: give the model readable references instead (as chat.js).
    static func history(_ ms: [Message]) -> [Rec] {
        ms.filter { !$0.content.isEmpty }.map { m in
            guard m.role == "assistant" else { return ["role": m.role, "content": m.content] }
            var t = m.content
            for s in m.cited { t = t.replacingOccurrences(of: "[\(s.key)]", with: "(\(s.title)\(s.page.map { ", p. \($0)" } ?? "")\(s.t.map { ", " + Fmt.clock($0) } ?? ""))") }
            return ["role": "assistant", "content": t]
        }
    }

    private func update(_ id: UUID, _ f: (inout Message) -> Void) {
        guard let i = idx(id), !chats[i].messages.isEmpty else { return }
        f(&chats[i].messages[chats[i].messages.count - 1])
    }

    private func run(_ id: UUID, _ q: String, history: [Rec], about: String?, cancel: Locked<Bool>) async {
        status = AIStatus.from(await LocalAPI.rec("/api/assistant/status"))
        if !status.ok {
            let why = status.tone == .starting ? "The local AI is still being prepared, so there’s no written answer yet." : "No written answer: \(status.line.prefix(1).lowercased() + status.line.dropFirst())."
            update(id) { $0.offline = true; $0.error = why + " These library pages match your question." }
            await fallback(id, q)
            finish(id); return
        }
        var body: Rec = ["messages": history + [["role": "user", "content": q]]]
        if let about { body["about"] = about }
        let resp = await Task.detached { LocalAPI.post("/api/chat", json: body) }.value
        guard case .stream(let start) = resp.body else {
            update(id) { $0.error = "The question couldn’t be sent (\(resp.status))." }
            finish(id); return
        }
        // Drive the engine's server-sent events on a thread that carries the "still wanted" check (SchemeHandler's contract).
        let events = AsyncStream<Rec> { cont in
            let t = Thread {
                Thread.current.threadDictionary[ChatAPI.aliveKey] = { () -> Bool in !cancel.value }
                var buf = Data()
                start { chunk in
                    guard let chunk else { cont.finish(); return }
                    buf.append(chunk)
                    while let r = buf.range(of: Data("\n\n".utf8)) {
                        let line = String(decoding: buf[buf.startIndex..<r.lowerBound], as: UTF8.self)
                        buf.removeSubrange(buf.startIndex..<r.upperBound)
                        if line.hasPrefix("data:"), let o = try? JSONSerialization.jsonObject(with: Data(line.dropFirst(5).utf8)) as? Rec { cont.yield(o) }
                    }
                }
            }
            t.start()
        }
        for await ev in events {
            if cancel.value { break }
            await apply(id, ev, q)
        }
        if cancel.value { update(id) { $0.stopped = true; if $0.content.isEmpty { $0.error = "Stopped." } } }
        update(id) { if $0.running && $0.content.isEmpty && $0.error == nil { $0.error = "The answer stopped unexpectedly." } }
        finish(id)
    }

    private func apply(_ id: UUID, _ ev: Rec, _ q: String) async {
        let type = ev.s("type")
        if type == "error" && ev.s("error") == "model_unavailable" {
            update(id) { $0.offline = true; $0.error = AIStatus.ipad(ev.str("text") ?? "The local AI can’t answer right now") + " These library pages match your question." }
            await fallback(id, q); return
        }
        update(id) { m in
            switch type {
            case "state": m.state = ev.s("state"); m.pct = (ev["pct"] as? NSNumber)?.intValue
            case "found": m.steps.append(Step(name: "found", text: "Matched \(Int(ev.d("count"))) passages for your question", done: true))
            case "tool":
                let a = (ev["args"] as? Rec) ?? [:]
                if ev.s("phase") == "start" { m.steps.append(Step(name: ev.s("name"), text: Self.stepText(ev.s("name"), a), done: false)) }
                else if let k = m.steps.lastIndex(where: { $0.name == ev.s("name") && !$0.done }) { m.steps[k].done = true }
                m.state = "searching"
            case "sources": m.found = ((ev["sources"] as? [Rec]) ?? []).map(Source.init)
            case "delta": m.content += ev.s("text"); m.state = "writing"
            case "reset": m.content = ""
            case "done":
                m.invalid = (ev["invalid"] as? [String]) ?? []
                var c = ev.s("answer")
                for k in m.invalid { c = c.replacingOccurrences(of: "[\(k)]", with: "") }
                m.content = c
                m.cited = ((ev["sources"] as? [Rec]) ?? []).map(Source.init)
                m.consulted = ((ev["consulted"] as? [Rec]) ?? []).map(Source.init)
            case "error": m.error = AIStatus.ipad(ev.str("text") ?? "Something went wrong."); m.fallback = Array(((ev["sources"] as? [Rec]) ?? []).map(Source.init).prefix(8))
            case "stopped": m.stopped = true
            default: break
            }
        }
    }

    static func stepText(_ n: String, _ a: Rec) -> String {
        switch n {
        case "search_library": return "Searched “\(a.s("query"))”"
        case "read_pages": return "Read pages \(((a["pages"] as? [Any]) ?? []).map { "\($0)" }.joined(separator: ", "))"
        case "view_page": return "Looked at page \(a.s("page"))"
        case "get_timed_transcript": return "Read a video transcript"
        case "lookup_place": return "Looked up “\(a.s("query"))” on the offline map"
        default: return n
        }
    }

    private func fallback(_ id: UUID, _ q: String) async {
        let d = await LocalAPI.rec("/api/retrieve", ["q": q, "k": "8"])
        let r = ((d?["results"] as? [Rec]) ?? []).map { var s = Source($0); s.key = ""; return s }
        update(id) { $0.fallback = r; if r.isEmpty { $0.error = ($0.error ?? "") + " The library search also found nothing for this question." } }
    }

    private func finish(_ id: UUID) {
        update(id) { $0.running = false; $0.state = nil; if $0.stopped && !$0.content.isEmpty { $0.content += "\n\n(Stopped.)" } }
        if let i = idx(id) { chats[i].updated = Date() }
        cancels[id] = nil
        persist()
    }

    /// Plain text for Copy: citation numbers kept, sources listed.
    static func plain(_ m: Message) -> String {
        var t = m.content
        for s in m.cited { t = t.replacingOccurrences(of: "[\(s.key)]", with: "[\(s.key.replacingOccurrences(of: "S", with: ""))]") }
        if !m.cited.isEmpty {
            t += "\n\nSources:\n" + m.cited.map { "[\($0.key.replacingOccurrences(of: "S", with: ""))] \($0.title) — \($0.publisher)\($0.whereText.isEmpty ? "" : ", " + $0.whereText)" }.joined(separator: "\n")
        }
        return t
    }

    #if DEBUG
    /// Design review only (launch argument -LBChatFixture YES). Clearly labelled on screen; never produced by the AI.
    static func fixture() -> Conversation {
        var c = Conversation(title: "Visual fixture: safe drinking water", fixture: true)
        c.messages.append(Message(role: "user", content: "The tap water is off. How do I make water safe to drink?"))
        var m = Message(role: "assistant")
        m.content = """
        **Visual fixture — this text was written for design review, not by the AI.**

        ## Boil it first
        Bring clear water to a rolling boil for one minute [S1]. At high elevation, boil for three minutes [S1].

        ## If you can’t boil
        - Let cloudy water settle, then filter it through a clean cloth [S2].
        - Add unscented household bleach as the label directs, and wait 30 minutes [S2].

        This layout is a sample to check spacing and type.
        """
        m.cited = [Source(key: "S1", kind: "pdf", docId: "fixture-1", title: "Sample manual (fixture)", publisher: "Visual fixture", page: 12, excerpt: "Sample excerpt used only to check the card layout."),
                   Source(key: "S2", kind: "video", docId: "fixture-2", title: "Sample video (fixture)", publisher: "Visual fixture", t: 95)]
        m.steps = [Step(name: "search_library", text: "Searched “boil water”", done: true)]
        c.messages.append(m)
        return c
    }
    #endif
}
