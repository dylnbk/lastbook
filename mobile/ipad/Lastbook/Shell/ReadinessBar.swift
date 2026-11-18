import SwiftUI

/// A plain notice above the library when something isn't installed yet, so a partial install never looks complete.
/// Orange only for what is really missing or failed. While the local AI is preparing or loading it is a neutral bar with a
/// spinner (never an error look), refreshed every 2 s until it settles. Hidden when everything is ready.
struct ReadinessBar: View {
    var manage: () -> Void = {}
    @State private var r: Readiness?
    @State private var open = false
    @State private var tick = 0      // bumps when the app comes back to the front (prewarm starts again then)

    var body: some View {
        VStack(spacing: 0) {
            Color.clear.frame(height: 0)   // always present, so .task runs before there is anything to show
            if let r, !r.missing.isEmpty || !r.preparing.isEmpty {
                Button { open = true } label: {
                    HStack(spacing: 10) {
                        if r.missing.isEmpty {
                            ProgressView().controlSize(.regular)
                            Text(r.preparing[0].title).font(.body.weight(.medium))
                        } else {
                            Image(systemName: "exclamationmark.circle").font(.title3)
                            Text(r.missing.count == 1 ? r.missing[0].title : "\(r.missing.count) things aren't ready on this \(Device.name) yet")
                                .font(.body.weight(.medium))
                            if let p = r.preparing.first { Text("· " + p.title).font(.body).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        Text("Details").font(.body)
                    }
                    .padding(.horizontal, 20).padding(.vertical, 12)
                    .frame(maxWidth: .infinity, minHeight: 48)
                    .background(r.missing.isEmpty ? Color.secondary.opacity(0.12) : Color.orange.opacity(0.15))
                    .foregroundStyle(.primary)
                }
                .accessibilityLabel(r.missing.isEmpty ? "Local AI is preparing" : "What isn't ready yet")
                .sheet(isPresented: $open) { ReadinessList(r: r, manage: { open = false; manage() }) }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)) { _ in tick += 1 }
        .task(id: tick) {
            // Check now, then follow the local AI while it prepares or loads; stop once nothing is in progress.
            repeat {
                r = await Task.detached(priority: .utility) { Readiness.check() }.value
                if r?.preparing.isEmpty ?? true { break }
                try? await Task.sleep(for: .seconds(2))
            } while !Task.isCancelled
        }
    }
}

struct ReadinessList: View {
    let r: Readiness
    var manage: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List(r.items, id: \.id) { i in
                HStack(alignment: .top, spacing: 14) {
                    Group {
                        if i.busy { ProgressView() }
                        else { Image(systemName: i.ok ? "checkmark.circle.fill" : "minus.circle").foregroundStyle(i.ok ? .green : .orange) }
                    }.font(.title2).frame(width: 28)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(i.title).font(.headline)
                        Text(i.detail).font(.body).foregroundStyle(.secondary)
                    }
                }.padding(.vertical, 6)
            }
            .navigationTitle("On this \(Device.name)")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Install or change…") { manage() } }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}
