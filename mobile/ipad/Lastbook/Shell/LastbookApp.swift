import SwiftUI
import UniformTypeIdentifiers
import WebKit

@main
struct LastbookApp: App {
    init() {
        // Undo any install that was cut off (map or pack switch) BEFORE the library opens; keep what it says to show.
        AppState.launchNotes = Installer.atLaunch()
        Library.shared.locate()
        // Routes after the library is found: installing them creates LocalAI.shared, which schedules the launch prewarm
        // (asynchronous, off the main thread; only an installed, verified, fitting model) and needs the library's folders.
        Router.shared.install(Routes.all)
        _ = LocalAI.shared
        // Decide AV1 vs H.264 once, early and off the main thread, so the first video doesn't wait for the decoder probe.
        DispatchQueue.global(qos: .utility).async { VideoCompat.shared.decidesAV1() }
    }
    var body: some Scene {
        WindowGroup { RootView() }
    }
}

final class AppState: ObservableObject {
    static var launchNotes: [String] = []
    @Published var hasLibrary = Library.shared.root != nil
    @Published var message: String? = AppState.launchNotes.isEmpty ? nil : AppState.launchNotes.joined(separator: " ")
    @Published var revision = 0          // bumps after an install, so the web view and the readiness notice reload
    func refresh() { hasLibrary = Library.shared.locate(); revision += 1 }
}

struct RootView: View {
    @StateObject private var state = AppState()
    @State private var picking = false
    @State private var installing = false
    @State private var opened = false

    var body: some View {
        Group {
            if state.hasLibrary {
                Group {
                    if UserDefaults.standard.bool(forKey: "LBLegacyWeb") {   // the 0.1 web UI, for comparison
                        VStack(spacing: 0) {
                            ReadinessBar(manage: { installing = true }).id(state.revision)
                            WebHost(onChangeLibrary: { installing = true })
                                .id(state.revision)
                                .ignoresSafeArea(edges: .bottom)
                        }
                    } else {
                        AppShell(manage: { installing = true }, chooseFolder: { picking = true }).id(state.revision)   // Design/
                            .debugNarrow()
                    }
                }
                .onAppear {   // the library has opened: an install's leftovers can go now
                    if !opened { opened = true; DispatchQueue.global(qos: .utility).async { Installer.afterOpen() } }
                }
            } else {
                Welcome(message: state.message, install: { installing = true }, choose: { picking = true }, lookAgain: { state.refresh() })
            }
        }
        .sheet(isPresented: $installing) {
            InstallView(onDone: { installing = false; state.refresh() })
        }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result {
                if Library.shared.choose(url) { state.message = nil; state.hasLibrary = true }
                else { state.message = "That folder has no Lastbook library in it. Pick the LastbookData folder (it holds library-data)." }
            }
        }
    }
}

struct Welcome: View {
    var message: String?
    var install: () -> Void
    var choose: () -> Void
    var lookAgain: () -> Void
    var body: some View {
        VStack(spacing: 24) {
            Image("Book").resizable().scaledToFit().frame(width: 120, height: 120)
            Text("Lastbook").font(.largeTitle.bold())
            Text("Your offline survival library: videos, a map and a guide you can ask, with nothing sent anywhere.")
                .font(.title3).multilineTextAlignment(.center).frame(maxWidth: 560)
            VStack(alignment: .leading, spacing: 10) {
                Text("Add your library").font(.headline)
                Text("Install the Lastbook download (the library zip, then your state's map and addresses) from Files.")
                Text("Or copy a LastbookData folder to this \(Device.name) and tap Choose library, or put it in Files › \(Device.files) › Lastbook and tap Look again.")
            }.font(.body).frame(maxWidth: 560, alignment: .leading)
            if let message { Text(message).foregroundStyle(.red).frame(maxWidth: 560) }
            HStack(spacing: 16) {
                Button("Install", action: install).buttonStyle(.borderedProminent).controlSize(.large)
                Button("Choose library", action: choose).buttonStyle(.bordered).controlSize(.large)
                Button("Look again", action: lookAgain).buttonStyle(.bordered).controlSize(.large)
            }
        }
        .padding(40)
    }
}

/// The desktop UI (web/) in one WKWebView at lastbook://app/. No network: the custom scheme answers everything,
/// and any other address is refused unless the person taps a web link and agrees to leave the app.
struct WebHost: UIViewRepresentable {
    var onChangeLibrary: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onChangeLibrary: onChangeLibrary) }

    func makeUIView(context: Context) -> WKWebView {
        let c = context.coordinator
        let cfg = WKWebViewConfiguration()
        cfg.setURLSchemeHandler(c.scheme, forURLScheme: "lastbook")
        cfg.websiteDataStore = .default()   // on this iPad only: keeps chats (if the person keeps them), bookmarks and view settings, as desktop
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.allowsPictureInPictureMediaPlayback = true
        cfg.defaultWebpagePreferences.preferredContentMode = .desktop
        cfg.dataDetectorTypes = []
        cfg.userContentController.addUserScript(WKUserScript(source: c.scheme.topScript(), injectionTime: .atDocumentStart, forMainFrameOnly: true))
        cfg.userContentController.addUserScript(WKUserScript(source: "document.documentElement.classList.add('plat-ipad')", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        if let u = Bundle.main.url(forResource: "ipad-copy", withExtension: "js"), let js = try? String(contentsOf: u, encoding: .utf8) {
            // iPad wording for a few desktop sentences ("this computer", desktop folders); the shared web/ files stay as they are
            cfg.userContentController.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        }
        cfg.userContentController.add(c, name: "lastbook")
        let v = WKWebView(frame: .zero, configuration: cfg)
        v.navigationDelegate = c
        v.uiDelegate = c
        v.allowsBackForwardNavigationGestures = true
        #if DEBUG
        v.isInspectable = true
        #endif
        c.view = v
        // Tests open a page directly: simctl launch ... -LBRoute '#/map' (launch arguments land in UserDefaults).
        let route = UserDefaults.standard.string(forKey: "LBRoute").flatMap { $0.hasPrefix("#") ? $0 : nil } ?? ""
        v.load(URLRequest(url: URL(string: SchemeHandler.origin + "/" + route)!))
        return v
    }

    func updateUIView(_ v: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let scheme = SchemeHandler()
        weak var view: WKWebView?
        let onChangeLibrary: () -> Void
        init(onChangeLibrary: @escaping () -> Void) { self.onChangeLibrary = onChangeLibrary }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, preferences: WKWebpagePreferences) async -> (WKNavigationActionPolicy, WKWebpagePreferences) {
            let url = action.request.url
            let own = url?.scheme == "lastbook" && url?.host == "app"
            if own, url?.path == "/ui/setup.html" {   // desktop's "Library and local AI" page: on iPad, the native library picker
                await MainActor.run { onChangeLibrary() }
                return (.cancel, preferences)
            }
            if own || url?.scheme == "about" || url?.scheme == "blob" {
                if let t = action.targetFrame, !t.isMainFrame { preferences.allowsContentJavaScript = false }   // saved pages run no scripts
                if action.targetFrame == nil, own, let u = url {   // target=_blank on our own page: show it in the one window
                    let route = u.fragment.map { "#" + $0 } ?? "#/"
                    if u.path == "/" || u.path.isEmpty { _ = try? await webView.evaluateJavaScript("location.hash=" + jsString(route)) }
                    return (.cancel, preferences)
                }
                return (.allow, preferences)
            }
            if let u = url, ["http", "https"].contains(u.scheme ?? ""), action.navigationType == .linkActivated || action.targetFrame == nil {
                await MainActor.run { self.offerToLeave(u) }
            }
            return (.cancel, preferences)
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let u = action.request.url, ["http", "https"].contains(u.scheme ?? "") { offerToLeave(u) }
            else if let u = action.request.url, u.scheme == "lastbook" {
                webView.evaluateJavaScript("location.hash=" + jsString(u.fragment.map { "#" + $0 } ?? "#/"))
            }
            return nil
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

        #if DEBUG
        /// Simulator tests only (never in a release build): simctl launch ... -LBEval 'js' runs once, 3 s after the first page loads.
        private var evaluated = false
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            guard !evaluated, let js = UserDefaults.standard.string(forKey: "LBEval") else { return }
            evaluated = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                webView.evaluateJavaScript(js) { r, e in NSLog("LBEval result: %@ %@", String(describing: r), String(describing: e)) }
            }
        }
        #endif

        func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
            if (message.body as? String) == "change-library" { onChangeLibrary() }
        }

        /// The only way out to the internet: a web link the person tapped, and only after they agree.
        @MainActor func offerToLeave(_ u: URL) {
            guard let top = view?.window?.rootViewController else { return }
            let a = UIAlertController(title: "Open on the internet?", message: "\(u.host ?? u.absoluteString) is a web page, not part of your offline library. Safari will open it if this \(Device.name) is online.", preferredStyle: .alert)
            a.addAction(UIAlertAction(title: "Stay here", style: .cancel))
            a.addAction(UIAlertAction(title: "Open in Safari", style: .default) { _ in UIApplication.shared.open(u) })
            (top.presentedViewController ?? top).present(a, animated: true)
        }

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo) async {
            await withCheckedContinuation { (k: CheckedContinuation<Void, Never>) in
                Task { @MainActor in
                    guard let top = webView.window?.rootViewController else { k.resume(); return }
                    let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
                    a.addAction(UIAlertAction(title: "OK", style: .default) { _ in k.resume() })
                    top.present(a, animated: true)
                }
            }
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo) async -> Bool {
            await withCheckedContinuation { (k: CheckedContinuation<Bool, Never>) in
                Task { @MainActor in
                    guard let top = webView.window?.rootViewController else { k.resume(returning: false); return }
                    let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
                    a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in k.resume(returning: false) })
                    a.addAction(UIAlertAction(title: "OK", style: .default) { _ in k.resume(returning: true) })
                    top.present(a, animated: true)
                }
            }
        }
    }
}

func jsString(_ s: String) -> String {
    let d = try? JSONSerialization.data(withJSONObject: [s])
    return d.flatMap { String(data: $0, encoding: .utf8) }.map { String($0.dropFirst().dropLast()) } ?? "''"
}
