import Foundation

/// What this iPad's library can actually do right now. A partial install must say what is missing instead of looking
/// complete. Shown as a notice above the library and served at /api/ipad/readiness (for tests).
struct Readiness {
    /// `busy`: on its way (the local AI checking its file, measuring or loading). Neither ready nor missing: shown neutrally.
    struct Item { let id: String; let ok: Bool; let title: String; let detail: String; var busy = false; var pct: Int? = nil }
    let items: [Item]
    var missing: [Item] { items.filter { !$0.ok && !$0.busy } }
    var preparing: [Item] { items.filter(\.busy) }

    static func check() -> Readiness {
        guard let root = Library.shared.root else {
            return Readiness(items: [Item(id: "library", ok: false, title: "No library", detail: "Add a LastbookData folder to use Lastbook.")])
        }
        var out: [Item] = []
        let pack = (try? JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("pack.json")))) as? Rec
        let packName = pack?.s("name") ?? "custom"
        out.append(Item(id: "library", ok: true, title: "Library: \(packName.capitalized)",
                        detail: "\(Catalog.shared.records.filter { $0.s("kind") == "video" }.count) videos, reading and search work offline."))

        // State map: required for street detail, help points and address search (desktop SETUP-API "State maps").
        let mapSet = (try? JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("map-set.json")))) as? Rec
        let mapName = mapSet?.s("name") ?? pack?.s("map") ?? "base"
        let state = mapName.count == 2 && mapName == mapName.uppercased()
        out.append(state || mapName == "us"
            ? Item(id: "map", ok: true, title: "Map: \(mapName == "us" ? "whole US" : mapName) detail", detail: "Streets, help points and address search.")
            : Item(id: "map", ok: false, title: "No state map yet",
                   detail: "The map shows only main roads and towns. Add your state's map for streets, help points and address search."))

        // Street addresses: a separate per-state index (derived/geo/addresses/<ST>.sqlite).
        // Installed through the download list (size + receipt, desktop address_indexes()), or placed by hand (index file present).
        let addr = Array(Set(AddressInstall.installed(root: root) + AddressInstall.present(root: root))).sorted()
        out.append(addr.isEmpty
            ? Item(id: "addresses", ok: false, title: "No street addresses yet", detail: "Town, ZIP and place search works. Add your state's addresses to find a street address.")
            : Item(id: "addresses", ok: true, title: "Street addresses: \(addr.joined(separator: ", "))", detail: "Find a street address on the map."))

        // Videos: AV1 originals need H.264 copies on iPads whose decoder can't play AV1.
        let vc = VideoCompat.shared
        if vc.needsH264 {
            let (have, _) = vc.variantCount()
            let (av1, missingCopies) = vc.coverage()
            if missingCopies > 0 {
                out.append(Item(id: "videos", ok: false, title: "\(missingCopies) of \(av1) videos can't play on this \(Device.name) yet",
                                detail: "This \(Device.name) can't decode the format these videos use. Add the library's H.264 video copies to play them."))
            } else {
                out.append(Item(id: "videos", ok: true, title: "Videos play on this \(Device.name)", detail: "\(have) videos use H.264 copies."))
            }
        } else {
            out.append(Item(id: "videos", ok: true, title: "Videos play on this \(Device.name)", detail: "This \(Device.name) plays the original videos."))
        }

        // Encyclopedias (.zim): no reader on iPad yet.
        let zims = ((try? FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("library-data/collections").path)) ?? []).filter { $0.hasSuffix(".zim") }
        if !zims.isEmpty {
            out.append(Item(id: "encyclopedias", ok: false, title: "Encyclopedias don't open on \(Device.name) yet",
                            detail: "\(zims.count) encyclopedia\(zims.count == 1 ? "" : "s") in this library need a reader the \(Device.name) app doesn't have yet."))
        }

        out.append(Self.aiItem(LocalAI.shared.status))
        return Readiness(items: out)
    }

    /// The local AI, by its real state (desktop llm_local state names). Preparing is never shown as missing or failed, and
    /// "ready" is said only when it is: loaded, or verified and fitting so it starts when asked.
    static func aiItem(_ ai: Rec) -> Item {
        let pct = (ai["pct"] as? NSNumber)?.intValue
        let pctText = pct.map { " \($0)%" } ?? ""
        switch ai.s("state") {
        case "ready":
            return Item(id: "ai", ok: true, title: "Local AI ready", detail: "Chat writes answers from the library, on this \(Device.name) only.")
        case "stopped":
            return Item(id: "ai", ok: true, title: "Local AI ready", detail: "It starts when you ask a question. Chat writes answers from the library, on this \(Device.name) only.")
        case "checking", "found":
            return Item(id: "ai", ok: false, title: "Preparing local AI…\(pctText)", detail: "Checking the local AI file once. Videos, Map and search work meanwhile.", busy: true, pct: pct)
        case "measuring":
            return Item(id: "ai", ok: false, title: "Preparing local AI…", detail: "Checking it fits this \(Device.name)'s memory. Videos, Map and search work meanwhile.", busy: true)
        case "loading":
            return Item(id: "ai", ok: false, title: "Loading local AI…\(pctText)", detail: "Questions asked now wait for it. Videos, Map and search work meanwhile.", busy: true, pct: pct)
        case "too_big":
            return Item(id: "ai", ok: false, title: "The local AI doesn't fit this \(Device.name)", detail: ai.str("message") ?? "This \(Device.name) doesn't have enough memory for it. Chat shows the library pages that match.")
        case "wrong":
            return Item(id: "ai", ok: false, title: "The local AI file isn't the right one", detail: ai.str("message") ?? "Lastbook checks the file and this one doesn't match. Chat shows the library pages that match.")
        case "error":
            return Item(id: "ai", ok: false, title: "The local AI couldn't start", detail: ai.str("message") ?? "Chat shows the library pages that match. Try again from Chat.")
        case "off":
            return Item(id: "ai", ok: false, title: "Local AI is off", detail: "Turn it on in Chat for written answers. Chat shows the library pages that match.")
        default:   // absent, or no runtime
            return Item(id: "ai", ok: false, title: "No written answers yet", detail: "Chat shows the library pages that match. Written answers need the local AI file on this \(Device.name).")
        }
    }

    var json: Rec {
        ["complete": missing.isEmpty && preparing.isEmpty, "preparing": !preparing.isEmpty,
         "items": items.map { i -> Rec in
            var o: Rec = ["id": i.id, "ok": i.ok, "busy": i.busy, "title": i.title, "detail": i.detail]
            if let p = i.pct { o["pct"] = p }
            return o
         }]
    }
}

struct ReadinessAPI: RouteGroup {
    func handle(_ r: APIRequest) -> APIResponse? {
        r.path == "/api/ipad/readiness" ? .json(Readiness.check().json) : nil
    }
}
