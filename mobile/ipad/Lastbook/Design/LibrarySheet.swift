import SwiftUI

/// Settings and downloads in one sheet (outside the three destinations): what's really on this iPad, the installer
/// (owned by the install worker: InstallView), the library folder, and chat history. Nothing here claims more than
/// Readiness.check() reports.
struct LibrarySheet: View {
    var manage: () -> Void
    var chooseFolder: () -> Void
    @EnvironmentObject var nav: AppNav
    @EnvironmentObject var chats: ChatStore
    @Environment(\.dismiss) private var dismiss
    @State private var confirmDelete = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack(spacing: 14) {
                        Image("Book").resizable().scaledToFit().frame(width: 52, height: 52).clipShape(RoundedRectangle(cornerRadius: 12))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Lastbook").font(.title3.bold())
                            Text("Works offline. Nothing on this \(Device.name) is sent anywhere.").font(.subheadline).foregroundStyle(.secondary)
                        }
                    }.padding(.vertical, 4)
                }
                Section {
                    if let r = nav.readiness {
                        ForEach(r.items, id: \.id) { i in
                            let starting = i.id == "ai" && chats.status.tone == .starting
                            HStack(alignment: .top, spacing: 14) {
                                if starting { ProgressView().frame(width: 28) }
                                else {
                                    Image(systemName: i.ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                                        .font(.title2).foregroundStyle(i.ok ? .green : .orange).accessibilityHidden(true)
                                }
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(starting ? chats.status.line : i.title).font(.headline)
                                    Text(starting ? "Starting, as it does each time Lastbook opens." : AIStatus.ipad(i.detail)).font(.subheadline).foregroundStyle(.secondary)
                                }
                            }
                            .padding(.vertical, 4)
                            .accessibilityElement(children: .combine)
                            .accessibilityLabel("\(i.ok ? "Ready" : "Not ready"): \(i.title). \(i.detail)")
                        }
                    } else {
                        HStack { ProgressView(); Text("Checking this \(Device.name)…") }
                    }
                } header: { Text("On this \(Device.name)") } footer: {
                    if let r = nav.readiness { Text(r.missing.isEmpty ? "Everything in this library is ready." : "\(r.missing.count) not ready yet. The rest works now.") }
                }
                Section("Downloads") {
                    Button { manage() } label: { Label("Install or change library…", systemImage: "arrow.down.circle") }
                    Button { chooseFolder() } label: { Label("Choose a LastbookData folder in Files…", systemImage: "folder") }
                }
                Section {
                    Toggle("Keep chat history on this \(Device.name)", isOn: $chats.keep)
                    Button("Delete all chats", role: .destructive) { confirmDelete = true }.disabled(chats.chats.isEmpty)
                } header: { Text("Chat") } footer: { Text("Chats are stored in this app only and are never sent anywhere.") }
            }
            .navigationTitle("Library")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .confirmationDialog("Delete all chats on this \(Device.name)?", isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("Delete all chats", role: .destructive) { chats.deleteAll() }
            }
            .refreshable { nav.refreshReadiness() }
        }
        .onAppear { nav.refreshReadiness() }
    }
}
