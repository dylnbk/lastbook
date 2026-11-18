import SwiftUI
import AVKit

/// The video page: the system player (controls, full screen, Picture in Picture, speed, captions) beside a readable,
/// synchronized transcript on wide windows; stacked in portrait. Opens paused: nothing plays until the person asks.
struct VideoWatchView: View {
    let videoId: String
    let startAt: Double?
    @EnvironmentObject var nav: AppNav
    @StateObject private var m = WatchModel()
    @Environment(\.horizontalSizeClass) private var hsc
    @State private var side = 0   // 0 transcript, 1 up next

    var body: some View {
        GeometryReader { g in
            let wide = g.size.width >= 1100 && hsc == .regular
            Group {
                if wide {
                    HStack(spacing: 0) {
                        ScrollView { main.padding(24) }
                            .frame(width: g.size.width * 0.64)
                        Divider()
                        sidePanel.frame(maxWidth: .infinity)
                    }
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 20) {
                            main
                            sidePanel.frame(height: min(560, max(360, g.size.height * 0.55)))
                                .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
                        }.padding(20)
                    }
                }
            }
        }
        .navigationTitle("")   // the title is under the player, as on YouTube
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { ask() } label: { Label("Ask about this video", systemImage: "bubble.left.and.text.bubble.right") }
                    .disabled(m.doc == nil)
            }
        }
        .task(id: videoId) { await m.load(videoId, startAt: startAt) }
        .onDisappear { m.leave() }
    }

    private var main: some View {
        VStack(alignment: .leading, spacing: 14) {
            ZStack {
                Rectangle().fill(.black)
                if let p = m.player {
                    PlayerController(player: p, pip: $m.inPiP)
                } else if let err = m.error {
                    VStack(spacing: 10) {
                        Image(systemName: "exclamationmark.triangle").font(.largeTitle)
                        Text(err).multilineTextAlignment(.center).frame(maxWidth: 420)
                    }.foregroundStyle(.white).padding()
                } else { ProgressView().tint(.white) }
            }
            .aspectRatio(16 / 9, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .accessibilityLabel("Video player")

            if let r = m.resumeAt {
                HStack(spacing: 12) {
                    Image(systemName: "clock.arrow.circlepath").foregroundStyle(.secondary)
                    Text("You stopped at \(Fmt.clock(r)).").font(.body)
                    Spacer(minLength: 8)
                    Button("Start over") { m.startOver() }.buttonStyle(.bordered).controlSize(.large)
                    Button("Resume") { m.resume() }.buttonStyle(.borderedProminent).controlSize(.large)
                }
                .padding(14).background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
            } else if let s = startAt, s > 0, m.player != nil, !m.started {
                Label("Opens at \(Fmt.clock(s)), the moment the source points to. Press play.", systemImage: "scope")
                    .font(.subheadline).foregroundStyle(.secondary)
            }

            if let d = m.doc {
                Text(d.s("title")).font(.title2.bold()).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10) {
                    Text(d.s("publisher")).font(.body.weight(.semibold))
                    if d.d("duration") > 0 { Text("·"); Text(Fmt.duration(d.d("duration"))) }
                    if !d.s("trust").isEmpty { Text("·"); Text(d.s("trust")) }
                }
                .font(.subheadline).foregroundStyle(.secondary)
                Label("A video shows how one person did something. Watching isn’t practice, and it may not match current safety guidance: check a manual for anything risky.",
                      systemImage: "info.circle")
                    .font(.footnote).foregroundStyle(.secondary)
            }

        }
    }

    /// YouTube's right column: what's said (transcript) and what to watch next, one at a time.
    private var sidePanel: some View {
        VStack(spacing: 0) {
            Picker("Show", selection: $side) {
                Text("Transcript").tag(0)
                Text("Up next").tag(1)
            }
            .pickerStyle(.segmented).padding(.horizontal, 16).padding(.top, 14)
            if side == 0 { TranscriptPanel(m: m) }
            else {
                ScrollView {
                    LazyVStack(spacing: 14) {
                        if m.related.isEmpty { Text("No related videos in this library.").foregroundStyle(.secondary).padding(.top, 20) }
                        ForEach(m.related) { v in
                            NavigationLink(value: VideoRoute.watch(id: v.id)) { UpNextRow(v: v) }.buttonStyle(.plain)
                        }
                    }.padding(16)
                }
            }
        }
    }

    private func ask() {
        guard let d = m.doc else { return }
        let t = Int(m.currentTime)
        nav.chatAbout = ChatAbout(about: "video:\(videoId):\(t)", label: d.s("title"), sub: t > 0 ? "Video at \(Fmt.clock(Double(t)))" : "Video")
        nav.opened = nil
        nav.tab = .chat
    }
}

/// A compact "up next" row: thumbnail left, title and channel right (YouTube's watch-page list).
struct UpNextRow: View {
    let v: VCard
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ThumbView(path: v.thumb, maxPixel: 360).aspectRatio(16 / 9, contentMode: .fit).frame(width: 168)
                .overlay(alignment: .bottomTrailing) {
                    if v.duration > 0 {
                        Text(Fmt.clock(v.duration)).font(.caption2.weight(.semibold).monospacedDigit())
                            .padding(.horizontal, 5).padding(.vertical, 2)
                            .background(.black.opacity(0.75), in: RoundedRectangle(cornerRadius: 4)).foregroundStyle(.white).padding(5)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            VStack(alignment: .leading, spacing: 4) {
                Text(v.title).font(.subheadline.weight(.semibold)).lineLimit(3)
                Text(v.publisher).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle()).hoverEffect(.highlight)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(v.title), \(v.publisher), \(Fmt.duration(v.duration))").accessibilityAddTraits(.isButton)
    }
}

/// Transcript beside or under the player: find words, tap a line to go there, the line being spoken is highlighted.
struct TranscriptPanel: View {
    @ObservedObject var m: WatchModel
    @State private var find = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let n = m.txNote {
                Text(n).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    .padding(.horizontal, 16).padding(.top, 10).padding(.bottom, 6)
            }
            TextField("Find words in the transcript", text: $find)
                .textFieldStyle(.roundedBorder).padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 8)
                .disabled(m.cues.isEmpty)
            Divider()
            if m.cues.isEmpty {
                Text(m.txLoaded ? "No transcript was saved for this video, so search and chat can’t see what’s said in it." : "Loading transcript…")
                    .foregroundStyle(.secondary).padding(16)
                Spacer()
            } else {
                let shown = find.isEmpty ? m.cues : m.cues.filter { $0.text.localizedCaseInsensitiveContains(find) }
                ScrollViewReader { proxy in
                    List(shown) { c in
                        Button { m.seek(c.t, play: true) } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 12) {
                                Text(c.t.map(Fmt.clock) ?? "").font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                                    .frame(minWidth: 46, alignment: .leading)
                                Text(c.text).font(.body).foregroundStyle(.primary).frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .padding(.vertical, 4)
                        }
                        .listRowBackground(c.id == m.currentCue ? Color.accentColor.opacity(0.16) : Color.clear)
                        .disabled(c.t == nil)
                        .id(c.id)
                        .accessibilityLabel("\(c.t.map(Fmt.clock) ?? ""), \(c.text)")
                        .accessibilityHint(c.t == nil ? "" : "Plays the video from here")
                    }
                    .listStyle(.plain)
                    .onChange(of: m.currentCue) { _, id in
                        guard find.isEmpty, let id else { return }
                        if reduceMotion { proxy.scrollTo(id, anchor: .center) } else { withAnimation { proxy.scrollTo(id, anchor: .center) } }
                    }
                }
                if find.isEmpty == false && shown.isEmpty { Text("No matching words.").foregroundStyle(.secondary).padding(16) }
            }
        }
    }
}

struct Cue: Identifiable { let id: Int; let t: Double?; let text: String }

@MainActor final class WatchModel: ObservableObject {
    @Published var doc: Rec?
    @Published var player: AVPlayer?
    @Published var error: String?
    @Published var cues: [Cue] = []
    @Published var txLoaded = false
    @Published var txNote: String?
    @Published var currentCue: Int?
    @Published var related: [VCard] = []
    @Published var resumeAt: Double?
    @Published var started = false
    @Published var inPiP = false
    var currentTime: Double = 0
    private var id = ""
    private var observer: Any?
    private var statusObs: NSKeyValueObservation?
    private var rateObs: NSKeyValueObservation?
    private var lastSave = Date.distantPast

    func load(_ vid: String, startAt: Double?) async {
        guard vid != id else { return }
        leave(); id = vid
        doc = nil; player = nil; error = nil; cues = []; txLoaded = false; related = []; resumeAt = nil; started = false
        let (j, status) = await LocalAPI.get("/api/doc/\(vid)")
        guard let d = j as? Rec, status == 200 else { error = "This video is not in the library on this \(Device.name)."; return }
        doc = d
        let path = d.s("path")
        let url = await Task.detached { LocalAPI.fileURL(path) }.value
        if let url {
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
            let item = AVPlayerItem(url: url)
            let p = AVPlayer(playerItem: item)
            p.actionAtItemEnd = .pause
            statusObs = item.observe(\.status) { [weak self] it, _ in
                if it.status == .failed { Task { @MainActor in self?.error = "This video file can’t be played on this \(Device.name). The transcript still works."; self?.player = nil } }
            }
            rateObs = p.observe(\.rate) { [weak self] pl, _ in if pl.rate > 0 { Task { @MainActor in self?.started = true; self?.resumeAt = nil } } }
            observer = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] t in
                MainActor.assumeIsolated { self?.tick(t.seconds) }
            }
            let saved = VideoProgress.shared.all[vid]
            if let s = startAt, s > 0 { await p.seek(to: CMTime(seconds: s, preferredTimescale: 600)) }
            else if let e = saved, e.t > 15, d.d("duration") <= 0 || e.t < d.d("duration") - 20 { resumeAt = e.t }
            player = p
        } else if d.truthy("wordsOnly") {
            // Not a fault: the library was made to hold this video's words only (or its picture is still being made smaller).
            error = d.truthy("pending")
                ? "This video’s picture is not in the library yet: it is still being made smaller on the computer that is building this library. What is said in it is below."
                : "This library holds this video’s words, not its picture, which was left out to save room. What is said in it is below, with the time each thing was said."
        } else {
            error = "This video’s file isn’t on this \(Device.name). The transcript still works."
        }
        let tx = await LocalAPI.rec("/api/transcript/\(vid)")
        let raw = (tx?["cues"] as? [Rec]) ?? []
        cues = raw.enumerated().map { Cue(id: $0.offset, t: ($0.element["t"] as? NSNumber)?.doubleValue, text: $0.element.s("text")) }
        let src = tx?.s("source") ?? ""
        txNote = cues.isEmpty ? nil : src == "authored" ? "Captions by the publisher · may contain errors" : src.contains("auto") ? "Automatic captions · may contain errors" : "Captions · may contain errors"
        txLoaded = true
        related = (await LocalAPI.recs("/api/videos/related/\(vid)")).map(VCard.init)
    }

    private func tick(_ s: Double) {
        currentTime = s
        var on: Int?
        for c in cues { if let t = c.t, t <= s + 0.25 { on = c.id } else if c.t != nil { break } }
        if on != currentCue { currentCue = on }
        if Date().timeIntervalSince(lastSave) > 4 { lastSave = Date(); save() }
    }

    func save() {
        guard let d = doc, currentTime > 3 else { return }
        let dur = d.d("duration") > 0 ? d.d("duration") : (player?.currentItem?.duration.seconds.isFinite == true ? player!.currentItem!.duration.seconds : 0)
        VideoProgress.shared.save(id, .init(t: currentTime, d: dur, title: d.s("title"), publisher: d.s("publisher"), thumb: d.str("thumb"), updated: Date()))
    }

    func seek(_ t: Double?, play: Bool) {
        guard let t, let p = player else { return }
        resumeAt = nil
        p.seek(to: CMTime(seconds: t, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
        if play { p.play() }
    }
    func resume() { seek(resumeAt, play: true) }
    func startOver() { resumeAt = nil; seek(0, play: true) }

    func leave() {
        save()
        if !inPiP { player?.pause() }
        if let o = observer { player?.removeTimeObserver(o); observer = nil }
        statusObs = nil; rateObs = nil
        if !inPiP { id = "" }
    }
}

/// AVPlayerViewController: the system controls people already know, Picture in Picture, full screen, speed and captions.
struct PlayerController: UIViewControllerRepresentable {
    let player: AVPlayer
    @Binding var pip: Bool
    func makeCoordinator() -> C { C(pip: $pip) }
    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let vc = AVPlayerViewController()
        vc.player = player
        vc.allowsPictureInPicturePlayback = true
        vc.canStartPictureInPictureAutomaticallyFromInline = false
        vc.entersFullScreenWhenPlaybackBegins = false
        vc.exitsFullScreenWhenPlaybackEnds = true
        vc.updatesNowPlayingInfoCenter = false
        vc.delegate = context.coordinator
        return vc
    }
    func updateUIViewController(_ vc: AVPlayerViewController, context: Context) { if vc.player !== player { vc.player = player } }
    final class C: NSObject, AVPlayerViewControllerDelegate {
        var pip: Binding<Bool>
        init(pip: Binding<Bool>) { self.pip = pip }
        func playerViewControllerWillStartPictureInPicture(_ c: AVPlayerViewController) { pip.wrappedValue = true }
        func playerViewControllerDidStopPictureInPicture(_ c: AVPlayerViewController) { pip.wrappedValue = false }
    }
}
