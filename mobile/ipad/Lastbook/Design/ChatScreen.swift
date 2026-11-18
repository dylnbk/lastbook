import SwiftUI

/// ChatGPT's layout on iPad: chat history (with search and New chat) in the side column; the conversation in a readable
/// centered column; the composer pinned at the bottom. Everything stays on this iPad.
struct ChatScreen: View {
    @EnvironmentObject var nav: AppNav
    @EnvironmentObject var store: ChatStore
    @State private var visibility = NavigationSplitViewVisibility.automatic
    @State private var compactColumn = NavigationSplitViewColumn.detail   // narrow: open on the conversation, history one tap away
    @State private var find = ""

    var body: some View {
        NavigationSplitView(columnVisibility: $visibility, preferredCompactColumn: $compactColumn) {
            List(selection: $store.selection) {
                ForEach(filtered) { c in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(c.title).font(.body).lineLimit(1)
                        Text(c.updated.formatted(date: .abbreviated, time: .omitted) + (c.fixture ? " · visual fixture" : ""))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                    .tag(c.id)
                    .contextMenu { Button("Delete", systemImage: "trash", role: .destructive) { store.delete(c.id) } }
                    .swipeActions { Button("Delete", systemImage: "trash", role: .destructive) { store.delete(c.id) } }
                }
            }
            .overlay {
                if store.chats.isEmpty { ContentUnavailableView("No chats yet", systemImage: "bubble.left.and.text.bubble.right", description: Text("Chats stay on this \(Device.name).")) }
                else if filtered.isEmpty { ContentUnavailableView.search(text: find) }
            }
            .searchable(text: $find, placement: .sidebar, prompt: "Search chats")
            .navigationTitle("Chats")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { store.newChat() } label: { Label("New chat", systemImage: "square.and.pencil") }.help("New chat (⌘N)")
                }
            }
        } detail: {
            ConversationView()
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) { LibraryToolbarButton() }
                }
                .navigationBarTitleDisplayMode(.inline)
        }
        .task { await store.refreshStatus() }
        .onChange(of: nav.chatAbout) { _, a in
            guard let a else { return }
            store.newChat(); store.pendingAbout = a; nav.chatAbout = nil
        }
    }

    /// Search titles and every message, as ChatGPT's chat search does.
    var filtered: [Conversation] {
        let q = find.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return store.chats }
        return store.chats.filter { c in c.title.localizedCaseInsensitiveContains(q) || c.messages.contains { $0.content.localizedCaseInsensitiveContains(q) } }
    }
}

/// The model's own state (ready / starting / missing), shown once, just above the composer, separate from any answer's
/// progress. Tapping it explains what that means and offers what can be done.
struct AIStatusLine: View {
    @EnvironmentObject var store: ChatStore
    @EnvironmentObject var nav: AppNav
    @State private var open = false
    var dot: Color {
        switch store.status.tone { case .ready: return .green; case .starting: return .secondary; case .unavailable: return .gray; case .incompatible: return .orange; case .failed: return .red }
    }
    var body: some View {
        Button { open = true } label: {
            HStack(spacing: 7) {
                if store.status.busy { ProgressView().controlSize(.mini) }
                else { Circle().fill(dot).frame(width: 8, height: 8) }
                Text(store.status.line + ([.unavailable, .incompatible, .failed].contains(store.status.tone) ? " · library pages only" : ""))
                    .font(.footnote.weight(.medium)).foregroundStyle(.secondary).lineLimit(1)
                Image(systemName: "info.circle").font(.footnote).foregroundStyle(.tertiary)
            }
            .frame(minHeight: 30).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Local AI: \(store.status.line)").accessibilityHint("Shows details")
        .popover(isPresented: $open) {
            VStack(alignment: .leading, spacing: 12) {
                Text(store.status.line).font(.headline)
                Text(store.status.tone == .ready ? "Questions are answered by the local AI on this \(Device.name), from the library only. Nothing is sent anywhere."
                     : store.status.tone == .starting ? "The local AI is starting, as it does each time Lastbook opens. You can keep using Videos and Map, and type your question meanwhile."
                     : "Without the local AI, a question shows the library pages that match it. Written answers need the local AI file on this \(Device.name).")
                    .font(.body).fixedSize(horizontal: false, vertical: true)
                HStack {
                    Button("Library and downloads") { open = false; nav.showLibrary = true }.buttonStyle(.bordered)
                    if store.status.state == "off" { Button("Turn on") { Task { await store.setAI("on") } }.buttonStyle(.bordered) }
                    if ["absent", "error", "wrong"].contains(store.status.state) { Button("Look again") { Task { await store.setAI("rescan") } }.buttonStyle(.bordered) }
                }
            }
            .padding(20).frame(width: 380)
        }
    }
}

struct ConversationView: View {
    @EnvironmentObject var store: ChatStore
    @EnvironmentObject var nav: AppNav
    @State private var draft = ""
    @FocusState private var focused: Bool
    static let readable: CGFloat = 760

    var body: some View {
        let c = store.current
        ScrollViewReader { proxy in
            ScrollView {
                if let c, !c.messages.isEmpty {
                    LazyVStack(alignment: .leading, spacing: 26) {
                        if c.fixture { FixtureBanner() }
                        ForEach(c.messages) { m in
                            if m.role == "user" { UserBubble(text: m.content) }
                            else { AnswerView(m: m, convo: c, isLast: m.id == c.messages.last?.id) }
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .frame(maxWidth: Self.readable)
                    .padding(.horizontal, 24).padding(.vertical, 24)
                    .frame(maxWidth: .infinity)
                } else {
                    EmptyChat(send: { send($0) }).frame(maxWidth: Self.readable).padding(24).frame(maxWidth: .infinity)
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .defaultScrollAnchor(c?.messages.isEmpty == false ? .bottom : .top)
            .onChange(of: c?.messages.last?.content) { _, _ in proxy.scrollTo("end", anchor: .bottom) }
            .onChange(of: c?.messages.count) { _, _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                Composer(draft: $draft, focused: $focused, running: store.isRunning(c?.id), about: aboutChip,
                         clearAbout: { store.pendingAbout = nil },
                         send: { send(draft) }, stop: { store.stop(c?.id) })
                    .frame(maxWidth: Self.readable + 48).frame(maxWidth: .infinity)
                    .background(Color(.systemBackground).opacity(0.94))
            }
        }
    }

    var aboutChip: String? {
        if let c = store.current, let l = c.aboutLabel { return l }
        return store.pendingAbout.map { $0.label + ($0.sub.isEmpty ? "" : " · \($0.sub)") }
    }

    func send(_ text: String) {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !store.isRunning(store.selection) else { return }
        store.send(text, about: store.current == nil ? store.pendingAbout : nil)
        store.pendingAbout = nil
        draft = ""
    }
}

struct FixtureBanner: View {
    var body: some View {
        Label("Visual fixture for design review: sample text, not an AI answer.", systemImage: "paintbrush")
            .font(.subheadline.weight(.semibold)).foregroundStyle(.white)
            .padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.purple, in: RoundedRectangle(cornerRadius: 10))
            .accessibilityAddTraits(.isHeader)
    }
}

/// The start state: what Chat does, honest about the AI, and starter questions.
struct EmptyChat: View {
    @EnvironmentObject var store: ChatStore
    @EnvironmentObject var nav: AppNav
    var send: (String) -> Void
    static let starters: [(String, String)] = [
        ("drop", "The tap water is off. How do I make water safe to drink?"),
        ("cross.case", "Someone has a deep cut that won’t stop bleeding. What do I do?"),
        ("bolt", "How do I run a generator safely during a power outage?"),
        ("refrigerator", "The fridge has been off for 6 hours. What food is still safe?"),
        ("toilet", "The toilets don’t work. How do we make an emergency toilet?"),
        ("antenna.radiowaves.left.and.right", "Phones are down. How can our household stay in contact?"),
    ]
    var body: some View {
        VStack(spacing: 24) {
            Spacer(minLength: 40)
            Image("Book").resizable().scaledToFit().frame(width: 64, height: 64).clipShape(RoundedRectangle(cornerRadius: 14)).accessibilityHidden(true)
            Text("Ask the library").font(.largeTitle.bold())
            Text("Answers from the manuals and videos on this \(Device.name), with the exact page or moment for each point.")
                .font(.title3).foregroundStyle(.secondary).multilineTextAlignment(.center)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 300), spacing: 12)], spacing: 12) {
                ForEach(Self.starters, id: \.1) { icon, q in
                    Button { send(q) } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            Image(systemName: icon).font(.body).foregroundStyle(.tint).frame(width: 28)
                            Text(q).font(.body).multilineTextAlignment(.leading).foregroundStyle(.primary)
                            Spacer(minLength: 0)
                        }
                        .padding(14).frame(maxWidth: .infinity, minHeight: 64, alignment: .leading)
                        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .contentShape(.hoverEffect, RoundedRectangle(cornerRadius: 14, style: .continuous)).hoverEffect(.highlight)
                    }.buttonStyle(.plain)
                }
            }
        }
    }
}

/// ChatGPT's composer: a rounded field that grows to a few lines, with a round send button that becomes Stop.
struct Composer: View {
    @Binding var draft: String
    var focused: FocusState<Bool>.Binding
    var running: Bool
    var about: String?
    var clearAbout: () -> Void
    var send: () -> Void
    var stop: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            AIStatusLine().padding(.leading, 14)
            if let about {
                HStack(spacing: 8) {
                    Image(systemName: "text.bubble")
                    Text("Asking about: ").foregroundStyle(.secondary) + Text(about).bold()
                    Button { clearAbout() } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                        .buttonStyle(.plain).accessibilityLabel("Don’t ask about this").frame(minWidth: 44, minHeight: 44)
                }
                .font(.subheadline).padding(.horizontal, 14)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Ask a question", text: $draft, axis: .vertical)
                    .lineLimit(1...6)
                    .font(.body)
                    .focused(focused)
                    .padding(.vertical, 12).padding(.leading, 18)
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) { return .ignored }
                        send(); return .handled
                    }
                    .accessibilityLabel("Your question")
                Button { running ? stop() : send() } label: {
                    Image(systemName: running ? "stop.fill" : "arrow.up")
                        .font(.system(size: 17, weight: .bold))
                        .frame(width: 36, height: 36)
                        .foregroundStyle(Color(.systemBackground))
                        .background(Circle().fill(running || !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.primary : Color.secondary.opacity(0.5)))
                        .frame(width: 44, height: 44)
                }
                .buttonStyle(.plain)
                .disabled(!running && draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .keyboardShortcut(running ? .cancelAction : .defaultAction)
                .accessibilityLabel(running ? "Stop the answer" : "Send")
                .padding(.trailing, 6).padding(.bottom, 4)
            }
            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 26, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).strokeBorder(Color(.separator), lineWidth: 0.5))
            Text("Answers come from the library on this \(Device.name) and can be wrong. Check the linked pages.")
                .font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity)
        }
        .padding(.horizontal, 24).padding(.top, 8).padding(.bottom, 10)
    }
}

struct UserBubble: View {
    let text: String
    var body: some View {
        HStack {
            Spacer(minLength: 120)
            Text(text).font(.body).textSelection(.enabled)
                .padding(.horizontal, 16).padding(.vertical, 11)
                .background(Color(.secondarySystemFill), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .accessibilityElement(children: .combine).accessibilityLabel("You: \(text)")
    }
}

/// One answer: progress while it runs, the text with citation numbers, source cards, and Copy / Ask again.
struct AnswerView: View {
    let m: Message
    let convo: Conversation
    let isLast: Bool
    @EnvironmentObject var store: ChatStore
    @EnvironmentObject var nav: AppNav
    @State private var copied = false
    static let states = ["starting": "Starting the local AI…", "thinking": "Thinking…", "searching": "Searching the library…", "reading": "Reading the pages it found…", "writing": "Writing the answer…"]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if m.running {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text((Self.states[m.state ?? ""] ?? "Working…") + (m.state == "starting" ? m.pct.map { " \($0)%" } ?? "" : "")).font(.subheadline.weight(.semibold))
                    }
                    ForEach(Array(m.steps.enumerated()), id: \.offset) { _, s in
                        Label(s.text, systemImage: s.done ? "checkmark" : "ellipsis").font(.footnote).foregroundStyle(.secondary)
                    }
                }
                .accessibilityElement(children: .combine)
            }
            if let e = m.error {
                Label(e, systemImage: m.offline ? "info.circle" : "exclamationmark.triangle")
                    .font(.body).padding(14).frame(maxWidth: .infinity, alignment: .leading)
                    .background(m.offline ? Color(.secondarySystemFill) : Color.red.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
            }
            if !m.content.isEmpty {
                AnswerText(text: m.content, sources: m.cited.isEmpty ? m.found : m.cited, markUncited: !m.running && !m.cited.isEmpty) { s in open(s) }
            }
            if m.running && !m.found.isEmpty {
                Text("Pages found so far. You can open them while the answer is written.").font(.footnote).foregroundStyle(.secondary)
                SourceList(sources: Array(m.found.prefix(6)), compact: true, open: open)
            }
            if !m.fallback.isEmpty { SourceList(sources: m.fallback, compact: false, open: open) }
            if !m.running && !m.content.isEmpty {
                if !m.invalid.isEmpty {
                    Text("The model referred to sources that don’t exist (\(m.invalid.joined(separator: ", "))); those markers were removed.").font(.footnote).foregroundStyle(.red)
                }
                if m.cited.isEmpty {
                    Label("This answer has no linked sources. Treat it as unverified.", systemImage: "exclamationmark.circle").font(.footnote).foregroundStyle(.orange)
                } else {
                    Text("Sources").font(.headline).accessibilityAddTraits(.isHeader).padding(.top, 4)
                    SourceList(sources: m.cited, compact: false, open: open)
                    Text("Dotted underline: the model’s own wording, not linked to a source.").font(.caption).foregroundStyle(.secondary)
                }
                actions
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var actions: some View {
        HStack(spacing: 4) {
            Button { UIPasteboard.general.string = ChatStore.plain(m); copied = true } label: {
                Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
            }
            if isLast && !convo.fixture {
                Button { store.retry(convo.id) } label: { Label("Ask again", systemImage: "arrow.clockwise") }
            }
        }
        .labelStyle(.iconOnly).buttonStyle(.borderless).font(.body).foregroundStyle(.secondary)
        .frame(minHeight: 44)
        .controlSize(.large)
    }

    func open(_ s: Source) {
        if convo.fixture { return }   // fixture sources point nowhere
        nav.open(route: s.route)
    }
}

/// Markdown-lite answer text (headings, lists, bold) with tappable citation numbers that open the exact passage.
struct AnswerText: View {
    let text: String
    let sources: [Source]
    let markUncited: Bool
    let open: (Source) -> Void

    enum Block: Hashable { case h(String), p(String), li(String, String), note(String, String) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in
                switch b {
                case .h(let t): Text(inline(t, mark: false)).font(.title3.bold()).padding(.top, 6).accessibilityAddTraits(.isHeader)
                case .p(let t): Text(inline(t, mark: markUncited)).font(.body).lineSpacing(3)
                case .li(let bullet, let t):
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(bullet).font(.body.monospacedDigit()).foregroundStyle(.secondary).frame(minWidth: 18, alignment: .trailing)
                        Text(inline(t, mark: markUncited)).font(.body).lineSpacing(3)
                    }
                case .note(let title, let t):
                    VStack(alignment: .leading, spacing: 6) {
                        Text(title).font(.headline)
                        if !t.isEmpty { Text(inline(t, mark: false)).font(.body) }
                    }
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 10))
                }
            }
        }
        .textSelection(.enabled)
        .environment(\.openURL, OpenURLAction { url in
            if url.scheme == "lbcite", let s = sources.first(where: { $0.key == url.host }) { open(s) }
            return .handled
        })
    }

    var blocks: [Block] {
        var out: [Block] = []
        for raw in text.replacingOccurrences(of: "\r", with: "").components(separatedBy: "\n") {
            let t = raw.trimmingCharacters(in: .whitespaces)
            if t.isEmpty { continue }
            let low = t.lowercased()
            let notes = [("not found in the library", "Not found in the library"), ("sources disagree", "Sources disagree"), ("own reasoning", "The model’s own reasoning, not from the library")]
            if let (k, title) = notes.first(where: { low.contains($0.0) }), t.hasPrefix("#") || t.hasPrefix("**") || low.hasPrefix(k) || low.hasPrefix("my " + k) {
                var rest = t
                if let r = rest.range(of: #"^[#*\s]*(my )?(not found in the library|sources disagree|own reasoning( \(not from the library\))?)[:*\s]*"#, options: [.regularExpression, .caseInsensitive]) { rest = String(rest[r.upperBound...]) }
                _ = k; out.append(.note(title, rest)); continue
            }
            if let r = t.range(of: #"^#{1,4}\s+"#, options: .regularExpression) { out.append(.h(String(t[r.upperBound...]))); continue }
            if t.range(of: #"^\*\*[^*]+\*\*:?$"#, options: .regularExpression) != nil {
                out.append(.h(t.replacingOccurrences(of: "*", with: "").trimmingCharacters(in: CharacterSet(charactersIn: ": ")))); continue
            }
            if let r = t.range(of: #"^[-*•]\s+"#, options: .regularExpression) { out.append(.li("•", String(t[r.upperBound...]))); continue }
            if let r = t.range(of: #"^\d+[.)]\s+"#, options: .regularExpression) { out.append(.li(String(t[..<r.upperBound]).trimmingCharacters(in: .whitespaces), String(t[r.upperBound...]))); continue }
            out.append(.p(t))
        }
        return out
    }

    /// **bold**, *italic*, [S1] → a tappable number; sentences without a citation get a dotted underline (the model's own words).
    func inline(_ s: String, mark: Bool) -> AttributedString {
        let sentences = mark ? s.split(separator: ". ", omittingEmptySubsequences: false).map(String.init) : [s]
        var out = AttributedString()
        for (i, sn) in sentences.enumerated() {
            let piece = sentences.count > 1 && i < sentences.count - 1 ? sn + ". " : sn
            let cited = piece.range(of: #"\[[SM]\d+\]"#, options: .regularExpression) != nil
            var a = parse(piece)
            if mark && !cited && piece.replacingOccurrences(of: "*", with: "").count >= 28 && !piece.hasSuffix(":") {
                a.underlineStyle = Text.LineStyle(pattern: .dot, color: .secondary)
            }
            out += a
        }
        return out
    }

    private func parse(_ s: String) -> AttributedString {
        var out = AttributedString()
        var rest = Substring(s)
        let pattern = #/\*\*(.+?)\*\*|\*(\S.*?)\*|\[([SM]\d+)\]/#
        while let m = rest.firstMatch(of: pattern) {
            out += AttributedString(String(rest[..<m.range.lowerBound]))
            if let b = m.output.1 { var a = AttributedString(String(b)); a.inlinePresentationIntent = .stronglyEmphasized; out += a }
            else if let e = m.output.2 { var a = AttributedString(String(e)); a.inlinePresentationIntent = .emphasized; out += a }
            else if let k = m.output.3 {
                let key = String(k)
                if sources.contains(where: { $0.key == key }) {
                    var a = AttributedString(" \(key.replacingOccurrences(of: "S", with: "")) ")
                    a.link = URL(string: "lbcite://\(key)")
                    a.font = .footnote.bold()
                    a.foregroundColor = .accentColor
                    a.backgroundColor = Color.accentColor.opacity(0.14)
                    out += a
                }
            }
            rest = rest[m.range.upperBound...]
        }
        out += AttributedString(String(rest))
        return out
    }
}

/// Source cards: a picture of the page or the video frame, the number, title, publisher and where. Opens the exact passage.
struct SourceList: View {
    let sources: [Source]
    let compact: Bool
    let open: (Source) -> Void
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: compact ? 260 : 320), spacing: 12)], spacing: 12) {
            ForEach(sources) { s in
                Button { open(s) } label: { SourceCard(s: s, compact: compact) }.buttonStyle(.plain)
            }
        }
    }
}

struct SourceCard: View {
    let s: Source
    let compact: Bool
    @State private var page: UIImage?
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack(alignment: .bottomTrailing) {
                Rectangle().fill(Color(.tertiarySystemFill))
                if s.kind == "video" { ThumbView(path: s.thumb, maxPixel: 300) }
                else if let page { Image(uiImage: page).resizable().scaledToFill() }
                else { Image(systemName: s.kind == "place" ? "mappin.and.ellipse" : "doc.text").font(.title2).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity) }
                if !s.whereText.isEmpty && s.kind != "place" {
                    Text(s.kind == "video" ? (s.t.map(Fmt.clock) ?? "video") : "p. \(s.page ?? 0)")
                        .font(.caption2.weight(.semibold).monospacedDigit()).padding(.horizontal, 5).padding(.vertical, 2)
                        .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 4)).foregroundStyle(.white).padding(4)
                }
            }
            .frame(width: s.kind == "video" ? 112 : 72, height: s.kind == "video" ? 63 : 92)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if !s.key.isEmpty {
                        Text(s.key.replacingOccurrences(of: "S", with: "")).font(.caption.bold()).padding(.horizontal, 6).padding(.vertical, 1)
                            .background(Color.accentColor.opacity(0.16), in: Capsule()).foregroundStyle(.tint)
                    }
                    Text(s.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                }
                Text([s.publisher, s.year, s.whereText].filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                if !compact, let e = s.excerpt, !e.isEmpty {
                    Text("“\(String(e.prefix(200)))\(e.count > 200 ? "…" : "")”").font(.caption).foregroundStyle(.secondary).lineLimit(3)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .contentShape(.hoverEffect, RoundedRectangle(cornerRadius: 12, style: .continuous)).hoverEffect(.highlight)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Source \(s.key.replacingOccurrences(of: "S", with: "")): \(s.title), \(s.publisher), \(s.whereText)")
        .accessibilityHint("Opens this exact passage")
        .accessibilityAddTraits(.isButton)
        .task(id: s.id) { if s.kind == "pdf", let p = s.page { page = await LocalAPI.pageImage(doc: s.docId, page: p, width: 240) } }
    }
}
