import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// Install and manage (minimal wiring; the design task owns the look). One Install action runs the whole plan in order:
/// the Small library, the chosen state's map (required), its street addresses, the H.264 video copies where this iPad needs
/// them, and the local AI when supported and wanted (Engine/Install/InstallPlan.swift, InstallRun.swift). The state is chosen
/// by the person first; there is no location lookup. Importing a zip from Files stays as a second way in.
///
/// Entry point: `InstallView(onDone:)`. Done is enabled only when the install is complete: the library and the state map are
/// installed and checked (and the state's addresses when published).
struct InstallView: View {
    var onDone: () -> Void
    @StateObject private var run = InstallRun()
    @StateObject private var m = InstallModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    if let note = m.note { noteView(note.ok, note.text, retry: nil) }
                    if let e = run.error, !run.running { noteView(false, e.code == "cancelled" ? "Stopped. What was already downloaded is kept." : "\(e)", retry: { run.resume() }) }
                    packSection
                    stateSection
                    planSection
                    if run.running { progressPanel }
                    filesSection
                    if let old = m.status?.oldLibraries, !old.isEmpty { oldSection(old) }
                }
                .padding(28).frame(maxWidth: 760, alignment: .leading).frame(maxWidth: .infinity)
            }
            .navigationTitle("Install Lastbook")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done", action: onDone).font(.title3.bold()).disabled(!run.complete || run.running || m.busy)
                }
            }
        }
        .fileImporter(isPresented: $m.picking, allowedContentTypes: m.pickFolder ? [.folder] : [.zip]) { result in
            if case .success(let url) = result { m.picked(url, state: run.state) { run.replan() } }
        }
        .task { run.prepare(); await m.refresh() }
        .alert("Replace the whole-US map?", isPresented: Binding(get: { m.confirm != nil }, set: { if !$0 { m.confirm = nil } })) {
            Button("Replace it", role: .destructive) { m.confirmReplace(state: run.state) { run.replan() } }
            Button("Keep the whole-US map", role: .cancel) { m.confirm = nil }
        } message: { Text(m.confirm?.text ?? "") }
    }

    // ------------------------------------------------------------------ sections ----
    private func noteView(_ ok: Bool, _ text: String, retry: (() -> Void)?) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(text, systemImage: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .font(.title3).foregroundStyle(ok ? .green : .red)
            if let retry { Button("Retry", action: retry).buttonStyle(.borderedProminent).controlSize(.large) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16).background((ok ? Color.green : Color.red).opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
    }

    private var packSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Library").font(.title2.bold())
            Picker("Library", selection: $run.pack) {
                ForEach(InstallPlan.packChoices, id: \.entry.id) { c in
                    Text("\(c.entry.title): \(bytes(c.download)) download, \(bytes(c.installed)) installed, \(bytes(c.peak)) free needed while installing")
                        .tag(c.entry.id)
                }
            }
            .pickerStyle(.inline).disabled(run.running)
        }
    }

    private var stateSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Your state").font(.title2.bold())
            Text("Lastbook installs your state’s detailed map. It doesn’t look up your location; you choose.").font(.body)
            Picker("State", selection: Binding(get: { run.state ?? "" }, set: { run.state = $0.isEmpty ? nil : $0 })) {
                Text("Choose a state…").tag("")
                ForEach(InstallCatalog.states, id: \.id) { s in Text(s.title).tag(s.id) }
            }
            .pickerStyle(.menu).font(.title3).disabled(run.running)
        }
    }

    private var planSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("What will be installed").font(.title2.bold())
            if run.planning && run.plan == nil {
                HStack { ProgressView(); Text("Checking this \(Device.name)’s videos…") }
            }
            if let p = run.plan {
                if run.planning { HStack { ProgressView(); Text("Checking this \(Device.name)’s videos…") } }
                ForEach(p.items) { i in planRow(i) }
                if InstallCatalog.model != nil, p.items.contains(where: { $0.kind == .model && $0.status != .installed }),
                   !p.items.contains(where: { if case .unsupported = $0.status { return $0.kind == .model }; return false }) {
                    Toggle("Include the local AI", isOn: $run.wantAI).font(.title3).disabled(run.running)
                }
                Text("To download: \(bytes(p.downloadBytes)). Room needed: \(bytes(p.spaceNeeded))"
                     + (p.free.map { ". Free on this \(Device.name): \(bytes($0))." } ?? ".")).font(.body)
                if p.tooBigForDevice, let c = p.capacity {
                    Text("This \(Device.name) holds \(bytes(c)) in all. This install needs \(bytes(p.spaceNeeded)) free at its fullest, so it can’t fit here even on an empty \(Device.name). Choose a smaller library.")
                        .foregroundStyle(.red)
                } else if !p.fits, let free = p.free {
                    Text("Not enough room: this needs \(bytes(p.spaceNeeded)) free and the \(Device.name) has \(bytes(free)). Free some space, then try again.")
                        .foregroundStyle(.red)
                }
                ForEach(p.blockers.filter { !p.tooBigForDevice || !$0.hasPrefix("This \(Device.name) holds") }, id: \.self) { Text($0).foregroundStyle(.orange) }
                if p.complete && p.toDo.isEmpty {
                    Label("Everything required is installed.", systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.title3)
                } else {
                    Button(p.items.contains { $0.partialBytes > 0 } || run.error != nil ? "Resume install" : "Install") { run.start() }
                        .buttonStyle(.borderedProminent).controlSize(.large).font(.title3.bold())
                        .disabled(run.running || p.state == nil || !p.fits || p.toDo.isEmpty)
                }
            } else {
                ProgressView()
            }
        }
    }

    private func planRow(_ i: PlanItem) -> some View {
        let status: (String, Color) = {
            switch i.status {
            case .installed: return ("Installed", .green)
            case .toDownload: return (i.partialBytes > 0 ? "\(bytes(i.partialBytes)) of \(bytes(i.downloadBytes)) downloaded" : "To download: \(bytes(i.downloadBytes))", .primary)
            case .unavailable(let why): return (why, .orange)
            case .unsupported(let why): return (why, .secondary)
            case .optedOut: return ("Turned off", .secondary)
            case .included(let why): return (why, .secondary)
            }
        }()
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Image(systemName: i.status == .installed ? "checkmark.circle.fill" : run.current?.id == i.id ? "arrow.down.circle" : "circle")
                    .foregroundStyle(i.status == .installed ? .green : .primary)
                Text(i.title + (i.required ? "" : " (optional)")).font(.title3)
                Spacer()
                if i.installedBytes > 0 { Text(bytes(i.installedBytes) + " installed").foregroundStyle(.secondary) }
            }
            Text(status.0).foregroundStyle(status.1)
            if let n = i.note { Text(n).foregroundStyle(.secondary) }
        }
    }

    private var progressPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(run.current?.title ?? "Starting…").font(.title3.weight(.semibold))
            Text(run.phase).font(.body)
            if run.itemTotal > 0 { ProgressView(value: Double(min(run.itemDone, run.itemTotal)), total: Double(run.itemTotal)) }
            else { ProgressView() }
            if run.overallTotal > 0 {
                Text("Downloaded \(bytes(run.overallDone)) of \(bytes(run.overallTotal))" + (run.rateText.map { " · \($0)" } ?? ""))
                    .font(.body).foregroundStyle(.secondary)
            }
            Text("Keep Lastbook open: a download pauses when you leave the app, and carries on when you tap Resume install.")
                .font(.body).foregroundStyle(.secondary)
            Button("Cancel", role: .cancel) { run.cancel() }.buttonStyle(.bordered).controlSize(.large)
        }
        .padding(18).background(Color.blue.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
    }

    private var filesSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Already have the files?").font(.title2.bold())
            Text("Choose a Lastbook zip from Files (a pack, a state map, street addresses or video copies), or a LastbookData folder.")
            HStack(spacing: 14) {
                Button("Choose a zip") { m.pick(folder: false) }.buttonStyle(.bordered)
                Button("Use a LastbookData folder") { m.pick(folder: true) }.buttonStyle(.bordered)
            }.controlSize(.large).disabled(run.running || m.busy)
            if m.busy { HStack { ProgressView(); Text("\(m.phase) \(m.total > 0 ? "\(bytes(m.done)) of \(bytes(m.total))" : "")") } }
        }
    }

    private func oldSection(_ old: [(url: URL, bytes: Int64)]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Earlier libraries").font(.title2.bold())
            ForEach(old, id: \.url) { o in
                HStack {
                    Text("\(o.url.lastPathComponent) (\(bytes(o.bytes)))")
                    Spacer()
                    Button("Delete", role: .destructive) { m.deleteOld(o.url) { run.replan() } }.buttonStyle(.bordered).disabled(run.running)
                }
            }
        }
    }

    private func bytes(_ n: Int64) -> String { ByteCountFormatter.string(fromByteCount: n, countStyle: .file) }
}

/// The Files path (one zip or a folder) and the earlier-libraries list.
@MainActor
final class InstallModel: ObservableObject {
    struct Note { let ok: Bool; let text: String }
    @Published var status: Installer.Status?
    @Published var busy = false
    @Published var phase = ""
    @Published var done: Int64 = 0
    @Published var total: Int64 = 0
    @Published var note: Note?
    @Published var picking = false
    @Published var pickFolder = false
    /// A map zip chosen for a library with the whole-US map: asked before it replaces it.
    @Published var confirm: (url: URL, text: String)?
    static var stateKey: String { InstallRun.stateKey }

    func confirmReplace(state: String?, then: @escaping () -> Void) {
        guard let c = confirm else { return }
        confirm = nil
        install(c.url, state: state, replaceWholeUS: true, then: then)
    }

    func refresh() async {
        status = await Task.detached(priority: .userInitiated) { Installer.status() }.value
    }

    func pick(folder: Bool) { pickFolder = folder; picking = true }

    func deleteOld(_ u: URL, then: @escaping () -> Void) {
        do { try PackInstall.deleteOld(u, target: Installer.packTarget); note = Note(ok: true, text: "Deleted \(u.lastPathComponent).") }
        catch { note = Note(ok: false, text: "\(error)") }
        Task { await refresh(); then() }
    }

    func picked(_ url: URL, state: String?, then: @escaping () -> Void) {
        note = nil
        if pickFolder {
            if Library.shared.choose(url) { note = Note(ok: true, text: "Using the library in “\(url.lastPathComponent)”.") }
            else { note = Note(ok: false, text: "That folder has no Lastbook library in it. Pick the LastbookData folder (it holds library-data).") }
            Task { await refresh(); then() }
            return
        }
        install(url, state: state, replaceWholeUS: false, then: then)
    }

    private func install(_ url: URL, state: String?, replaceWholeUS: Bool, then: @escaping () -> Void) {
        note = nil
        busy = true; phase = Installer.Phase.checking.rawValue; done = 0; total = 0
        let throttle = Throttle()
        Task.detached(priority: .userInitiated) {
            let result = Result { try Installer.install(file: url, state: state, replaceWholeUS: replaceWholeUS, progress: { p, d, t in
                if throttle.due(final: d == t) { Task { @MainActor in self.phase = p.rawValue; self.done = d; self.total = t } }
            }) }
            await MainActor.run {
                switch result {
                case .success(let r):
                    if r.kind == .pack { InstallRunner.useNewLibrary() } else { Installer.afterOpen() }
                    self.note = Note(ok: true, text: r.title + ". " + r.detail)
                case .failure(InstallError.needsConfirmation(let text)): self.confirm = (url, text)
                case .failure(let e): self.note = Note(ok: false, text: "\(e)")
                }
                self.busy = false
            }
            await self.refresh()
            await MainActor.run { then() }
        }
    }
}
