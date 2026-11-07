import AppKit
import CryptoKit
import Darwin
import Foundation
import UniformTypeIdentifiers
import WebKit

// Keep local Wiki navigation in its own window without replacing the review.
private final class WikiHelpWindow: NSWindowController, NSWindowDelegate, WKNavigationDelegate {
    private let entry: URL
    private var page: WKWebView?
    private(set) var isClosed = false

    static func accepts(_ url: URL?, hostOrigin: URL?, resourceRoot: URL) -> Bool {
        guard let url, url.user == nil, url.password == nil else { return false }
        if url.isFileURL {
            let directory = resourceRoot.appendingPathComponent("docs/wiki/site", isDirectory: true).resolvingSymlinksInPath().standardizedFileURL
            return url.resolvingSymlinksInPath().standardizedFileURL.path == directory.appendingPathComponent("index.html").path
        }
        guard let origin = hostOrigin else { return false }
        return url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port && url.path == "/wiki/index.html"
    }

    init(configuration: WKWebViewConfiguration, entry: URL) {
        self.entry = entry
        let page = WKWebView(frame: NSRect(x: 0, y: 0, width: 1000, height: 760), configuration: configuration)
        let panel = NSWindow(contentRect: page.frame, styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        panel.title = "MI1 · Wiki und Bedienhilfe"
        panel.minSize = NSSize(width: 600, height: 500)
        panel.isReleasedWhenClosed = false
        panel.contentView = page
        self.page = page
        super.init(window: panel)
        panel.delegate = self
        page.navigationDelegate = self
    }

    required init?(coder: NSCoder) { nil }
    var webView: WKWebView { page! }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        let samePage = url.scheme == entry.scheme && url.host == entry.host && url.port == entry.port &&
            url.user == nil && url.password == nil && url.path == entry.path && url.query == entry.query
        if samePage { decisionHandler(.allow); return }
        if navigationAction.navigationType == .linkActivated, ["http", "https"].contains(url.scheme ?? ""), url.user == nil, url.password == nil {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func windowWillClose(_ notification: Notification) {
        isClosed = true
        page?.stopLoading()
        window?.contentView = nil
        page = nil
    }
}

final class Launcher: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var root: URL?
    private var gameDirectory: URL?
    private var manifest: URL?
    private var correction: URL?
    private var web: WKWebView!
    private var webNotice: NSView?
    private var pageLoadGeneration = 0
    private var bootstrapping = false
    private var startupURL: URL!
    private var hostOrigin: URL?
    private var startupLog = [String]()
    private var statusText = "Bereit · Noch keine Downloads gestartet"
    private var statusKind = "ready"
    private var bootstrapDescription = "Der erste Download wird vorbereitet."
    private var packageTerms = [[String: String]]()
    private var initialConsents = [String: Any]()
    private var resumeEntry = false
    private var resumeCheckKey: String?
    private let helperLock = NSLock()
    private var bootstrapProcesses = [Process]()
    private var process: Process?
    private var launchGeneration = 0
    private var runtimeRoot: URL!
    private var resourceRoot: URL!
    private var terminationPending = false
    private var diagnosticURL: URL?
    private let diagnosticLock = NSLock()
    private var diagnosticCount = 0
    private var patchWindowDock: PatchWindowDock?
    private var wikiWindows = [WikiHelpWindow]()

    private func diagnostic(_ event: String, _ fields: [String: Any] = [:]) {
        diagnosticLock.lock(); defer { diagnosticLock.unlock() }
        guard let url = diagnosticURL, diagnosticCount < 5000 else { return }
        var entry = fields.mapValues { value -> Any in
            guard let text = value as? String else { return value }
            return String(text.prefix(4000)).replacingOccurrences(of: #"(?i)(session|token)[\s\"':=]+[^\s\",&}]+"#, with: "$1=[redacted]", options: .regularExpression)
        }
        entry["event"] = event; entry["time"] = ISO8601DateFormatter().string(from: Date())
        entry["pid"] = Int(getpid())
        guard let data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]),
              let file = try? FileHandle(forWritingTo: url) else { return }
        defer { try? file.close() }
        do { try file.seekToEnd(); try file.write(contentsOf: data); try file.write(contentsOf: Data([10])); diagnosticCount += 1 }
        catch { fputs("Startprotokoll: \(error.localizedDescription)\n", stderr) }
    }

    private func diagnosticAddress(_ url: URL?) -> String {
        guard let url, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return "" }
        parts.query = nil; parts.fragment = nil; parts.user = nil; parts.password = nil
        return parts.string ?? ""
    }

    private func beginDiagnostics() {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/MI German Talkie Patch", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
            let url = directory.appendingPathComponent("patch-start-\(stamp)-\(getpid()).jsonl")
            guard FileManager.default.createFile(atPath: url.path, contents: Data(), attributes: [.posixPermissions: 0o600]) else { return }
            diagnosticURL = url
            diagnostic("application_start", ["version": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "",
                "macOS": ProcessInfo.processInfo.operatingSystemVersionString,
                "ram_bytes": ProcessInfo.processInfo.physicalMemory,
                "bundle": Bundle.main.bundleURL.path,
                "webkit_version": Bundle(for: WKWebView.self).object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"])
        } catch { fputs("Startprotokoll nicht verfügbar: \(error.localizedDescription)\n", stderr) }
    }

    @objc private func revealDiagnostics() {
        if let url = diagnosticURL { NSWorkspace.shared.activateFileViewerSelecting([url]) }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        beginDiagnostics()
        NSApp.setActivationPolicy(.regular)
        let menuBar = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        let quit = NSMenuItem(title: "MI German Talkie Patch beenden", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApp
        let logItem = NSMenuItem(title: "Startprotokoll im Finder zeigen", action: #selector(revealDiagnostics), keyEquivalent: "")
        logItem.target = self; appMenu.addItem(logItem)
        appMenu.addItem(quit); appItem.submenu = appMenu
        menuBar.addItem(appItem); NSApp.mainMenu = menuBar
        buildWindow()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationPending { return .terminateLater }
        helperLock.lock()
        terminationPending = true
        patchWindowDock?.stop()
        let children = bootstrapProcesses + (process.map { [$0] } ?? [])
        helperLock.unlock()
        update()
        let running = children.filter { $0.isRunning }
        guard !running.isEmpty else { return .terminateNow }
        setStatus("Eigene Startprozesse beenden …", kind: "running")
        running.forEach { $0.terminate() }
        DispatchQueue.global(qos: .userInitiated).async {
            let deadline = Date().addingTimeInterval(5)
            while running.contains(where: { $0.isRunning }) && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            for child in running where child.isRunning {
                // Signal only our still-running, tracked Process instances.
                kill(child.processIdentifier, SIGKILL)
            }
            DispatchQueue.main.async { sender.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        patchWindowDock?.stop()
        diagnostic("application_stop")
        if !terminationPending, let process, process.isRunning { process.terminate() }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === window { NSApp.terminate(self); return false }
        return true
    }

    private func buildWindow() {
        resourceRoot = Bundle.main.resourceURL!.resolvingSymlinksInPath().standardizedFileURL
        startupURL = resourceRoot.appendingPathComponent("generator_launcher/native/start.html").standardizedFileURL
        loadPackagedDefaults()
        diagnostic("startup_resources", ["resources": resourceRoot.path, "start_page": startupURL.path,
            "html_readable": FileManager.default.isReadableFile(atPath: startupURL.path),
            "js_readable": FileManager.default.isReadableFile(atPath: resourceRoot.appendingPathComponent("generator_launcher/native/start.js").path)])
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "launcher")
        let diagnosticScript = """
        (()=>{let count=0;const report=(event,message)=>{if(count++<100)window.webkit.messageHandlers.launcher.postMessage({action:'diagnostic',event,message:String(message||'').slice(0,2000)});};
        window.addEventListener('error',e=>report(e.message?'javascript_error':'resource_error',e.message||e.target?.src||e.target?.href||'resource failed'),true);
        window.addEventListener('unhandledrejection',e=>report('javascript_rejection',e.reason?.message||e.reason));
        window.addEventListener('DOMContentLoaded',()=>report('dom_ready',JSON.stringify({title:document.title,bodyText:document.body?.innerText?.length||0,scripts:document.scripts.length,nativeStart:!!window.NativeStart})));
        })();
        """
        configuration.userContentController.addUserScript(WKUserScript(source: diagnosticScript, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1100, height: 950), configuration: configuration)
        web.navigationDelegate = self
        web.uiDelegate = self
        let panel = NSWindow(contentRect: web.frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        panel.title = "MI German Talkie Patch"
        panel.minSize = NSSize(width: 720, height: 700)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        let surface = NSView(frame: web.frame)
        web.autoresizingMask = [.width, .height]; surface.addSubview(web)
        panel.contentView = surface; panel.center()
        window = panel
        patchWindowDock = PatchWindowDock() { [weak self] status in
            guard let self, self.isHostPage(self.web.url),
                  let data = try? JSONSerialization.data(withJSONObject: status, options: [.sortedKeys]),
                  let json = String(data: data, encoding: .utf8) else { return }
            self.web.evaluateJavaScript("window.LauncherPatchWindowStatus?.(\(json))", completionHandler: nil)
        }
        panel.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        retryWebPage()
    }

    // Local start mutations are never available to the later HTTP page.
    private func isLocalStart(_ url: URL?) -> Bool {
        guard let url, url.isFileURL, url.query == nil, url.fragment == nil else { return false }
        return url.resolvingSymlinksInPath().standardizedFileURL == startupURL
    }

    private func isHostPage(_ url: URL?) -> Bool {
        guard let url, let origin = hostOrigin else { return false }
        return url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port &&
            url.user == nil && url.password == nil
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "launcher", message.frameInfo.isMainFrame,
              let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        let source = message.frameInfo.request.url
        if action == "diagnostic" {
            guard isLocalStart(source) || isHostPage(source),
                  let event = body["event"] as? String,
                  ["javascript_error", "javascript_rejection", "resource_error", "dom_ready"].contains(event),
                  let text = body["message"] as? String else { return }
            // Do not record page URL parameters or arbitrary bridge payloads.
            let redacted = String(text.prefix(2000)).replacingOccurrences(of: #"(?i)(session|token)[\s\"':=]+[^\s\",&}]+"#, with: "$1=[redacted]", options: .regularExpression)
            diagnostic(event, ["message": redacted]); return
        }
        if action == "exportLog" {
            guard isLocalStart(source) || isHostPage(source), let text = body["text"] as? String,
                  text.utf8.count <= 2_097_152 else { return }
            exportLog(text); return
        }
        if action == "chooseDirectory" {
            guard isHostPage(source), isHostPage(web.url), !terminationPending,
                  let requestID = body["requestId"] as? String, requestID.count < 200,
                  let kind = body["kind"] as? String, ["export-target", "game-source"].contains(kind) else { return }
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = kind == "export-target"
            panel.allowsMultipleSelection = false
            panel.message = kind == "export-target" ? "Wähle den Ordner für deine spielbare Version." : "Wähle die vorbereitete deutsche Spielkopie."
            panel.beginSheetModal(for: window) { [weak self] response in
                guard let self, self.isHostPage(self.web.url) else { return }
                let path: Any = response == .OK ? (panel.url?.path as Any? ?? NSNull()) : NSNull()
                guard let data = try? JSONSerialization.data(withJSONObject: [requestID, path]),
                      let args = String(data: data, encoding: .utf8) else { return }
                self.web.evaluateJavaScript("window.MI1NativeDirectoryPicked?.(...\(args))", completionHandler: nil)
            }
            return
        }
        if action == "quitApp" {
            guard isHostPage(source), isHostPage(web.url), !terminationPending else { return }
            NSApplication.shared.terminate(nil)
            return
        }
        if action == "patchWindow" {
            guard isHostPage(source), isHostPage(web.url), !terminationPending,
                  let origin = hostOrigin else { return }
            patchWindowDock?.receive(body, origin: origin); return
        }
        guard isLocalStart(source), isLocalStart(web.url), !terminationPending else { return }
        if action == "ready" { diagnostic("native_bridge_ready"); update(); checkResume(); return }
        guard !bootstrapping && hostOrigin == nil else { return }
        switch action {
        case "pick":
            switch body["field"] as? String {
            case "root": chooseRoot()
            case "game": chooseGameDirectory()
            case "manifest": chooseManifest()
            case "correction": chooseCorrection()
            default: return
            }
        case "clearCorrection": correction = nil; update(); checkResume()
        case "start":
            let consentKeys = Set(["packages", "wine", "game"])
            guard let consents = body["consents"] as? [String: Any],
                  Set(consents.keys) == consentKeys,
                  consentKeys.allSatisfy({ ExistingServer.boolean(consents[$0]) == true }) else { return }
            initialConsents = consents
            initialConsents["schema_version"] = 1
            start()
        default: return
        }
    }

    private func exportLog(_ text: String) {
        let picker = NSSavePanel(); picker.title = "Patch-Protokoll speichern"
        picker.allowedContentTypes = [.plainText]; picker.nameFieldStringValue = "MI German Talkie Patch - Protokoll.txt"
        picker.beginSheetModal(for: window) { response in
            guard response == .OK, let destination = picker.url else { return }
            do { try text.write(to: destination, atomically: true, encoding: .utf8) }
            catch {
                let alert = NSAlert(); alert.messageText = "Protokoll konnte nicht gespeichert werden"
                alert.informativeText = error.localizedDescription; alert.beginSheetModal(for: self.window)
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        let wikiWindow = navigationAction.targetFrame == nil && WikiHelpWindow.accepts(url, hostOrigin: hostOrigin, resourceRoot: resourceRoot)
        let allowed = (hostOrigin == nil && isLocalStart(url)) || isHostPage(url) || wikiWindow
        diagnostic("navigation_policy", ["url": diagnosticAddress(url), "allowed": allowed, "main_frame": navigationAction.targetFrame?.isMainFrame ?? false])
        if allowed {
            decisionHandler(.allow); return
        }
        if navigationAction.navigationType == .linkActivated, let url,
           ["https", "http"].contains(url.scheme ?? ""), url.user == nil, url.password == nil {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        guard frame.isMainFrame, let window = webView.window else { completionHandler(nil); return }
        let picker = NSOpenPanel()
        picker.title = "Datei auswählen"
        picker.prompt = "Auswählen"
        picker.canChooseFiles = true
        picker.canChooseDirectories = parameters.allowsDirectories
        picker.allowsMultipleSelection = parameters.allowsMultipleSelection
        picker.canCreateDirectories = false
        picker.treatsFilePackagesAsDirectories = true
        picker.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? picker.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.navigationType == .linkActivated,
              let url = navigationAction.request.url,
              WikiHelpWindow.accepts(url, hostOrigin: hostOrigin, resourceRoot: resourceRoot) else { return nil }
        wikiWindows.removeAll { $0.isClosed }
        let controller = WikiHelpWindow(configuration: configuration, entry: url)
        wikiWindows.append(controller)
        controller.window?.center()
        controller.showWindow(self)
        return controller.webView
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        patchWindowDock?.stop()
        diagnostic("navigation_start", ["url": diagnosticAddress(webView.url)])
    }
    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) { diagnostic("navigation_commit", ["url": diagnosticAddress(webView.url)]) }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        diagnostic("navigation_finish", ["url": diagnosticAddress(webView.url)])
        pageLoadGeneration += 1; webNotice?.removeFromSuperview(); webNotice = nil
        if isLocalStart(webView.url) { update() }
    }

    private func showWebNotice(_ text: String, retry: Bool) {
        guard let surface = window?.contentView else { return }
        diagnostic("native_notice", ["retry": retry, "message": text])
        webNotice?.removeFromSuperview()
        let notice = NSView(frame: surface.bounds)
        notice.autoresizingMask = [.width, .height]; notice.wantsLayer = true
        notice.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: 17); label.alignment = .center
        let stack = NSStackView(views: [label]); stack.orientation = .vertical; stack.spacing = 20
        if retry { stack.addArrangedSubview(NSButton(title: "Oberfläche erneut laden", target: self, action: #selector(retryWebPage))) }
        stack.translatesAutoresizingMaskIntoConstraints = false; notice.addSubview(stack)
        NSLayoutConstraint.activate([stack.centerXAnchor.constraint(equalTo: notice.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: notice.centerYAnchor),
            stack.widthAnchor.constraint(equalTo: notice.widthAnchor, multiplier: 0.8)])
        surface.addSubview(notice); webNotice = notice
    }

    @objc private func retryWebPage() {
        diagnostic("page_load_requested", ["host": hostOrigin != nil])
        showWebNotice("MI German Talkie Patch\nDie Oberfläche wird geladen …", retry: false)
        pageLoadGeneration += 1; let generation = pageLoadGeneration
        if hostOrigin == nil {
            guard FileManager.default.isReadableFile(atPath: startupURL.path) else {
                showWebNotice("Die Startseite fehlt oder ist nicht lesbar. Entpacke den vollständigen Patch erneut.\n" + startupURL.path, retry: true); return
            }
            web.loadFileURL(startupURL, allowingReadAccessTo: resourceRoot)
        } else { web.reload() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { [weak self] in
            guard let self, self.pageLoadGeneration == generation, self.webNotice != nil else { return }
            self.diagnostic("navigation_timeout", ["url": self.diagnosticAddress(self.web.url), "loading": self.web.isLoading])
            self.showWebNotice("Die Oberfläche konnte nicht geladen werden. Versuche es erneut oder starte den Patch neu.", retry: true)
        }
    }

    private func webLoadFailed(_ error: Error) {
        let failure = error as NSError
        diagnostic("navigation_error", ["domain": failure.domain, "code": failure.code, "description": failure.localizedDescription, "url": diagnosticAddress(web.url)])
        if failure.domain == NSURLErrorDomain && failure.code == NSURLErrorCancelled { return }
        pageLoadGeneration += 1
        showWebNotice("Die Oberfläche konnte nicht geladen werden.\n\(failure.localizedDescription)\n\(failure.domain) · \(failure.code)", retry: true)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { webLoadFailed(error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { webLoadFailed(error) }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        diagnostic("webkit_process_terminated", ["url": diagnosticAddress(webView.url)])
        pageLoadGeneration += 1
        showWebNotice("Der WebKit-Anzeigeprozess wurde beendet. Lade die Oberfläche erneut. Bereits gestartete Aufträge werden dadurch nicht neu gestartet.", retry: true)
    }

    private func send(_ method: String, _ value: [String: Any]) {
        guard web != nil, isLocalStart(web.url),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.NativeStart?.\(method)(\(json))") { [weak self] _, error in
            if let error { self?.diagnostic("native_update_javascript_error", ["error": error.localizedDescription]) }
        }
    }

    private func setStatus(_ text: String, kind: String = "running") {
        let bounded = String(text.prefix(2000))
        statusText = bounded; statusKind = kind
        if startupLog.last != bounded { startupLog.append(bounded) }
        startupLog = Array(startupLog.suffix(400))
        while startupLog.joined(separator: "\n").utf8.count > 24_000 { startupLog.removeFirst() }
        update()
    }

    private func loadPackagedDefaults() {
        resourceRoot = Bundle.main.resourceURL!.resolvingSymlinksInPath().standardizedFileURL
        if let data = try? Data(contentsOf: resourceRoot.appendingPathComponent("runtime.json")),
           let catalog = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let artifacts = catalog["artifacts"] as? [[String: Any]] {
            let components = artifacts.filter { $0["id"] as? String == "python" }
            let total = components.reduce(0) { $0 + ($1["bytes"] as? Int ?? 0) }
            bootstrapDescription = "Startpaket · " + byteLabel(total)
            var seen = Set<String>()
            packageTerms = artifacts.compactMap { artifact in
                guard let terms = artifact["terms"] as? [String: Any], let url = terms["url"] as? String,
                      url.hasPrefix("https://"), seen.insert(url).inserted else { return nil }
                return ["name": artifact["id"] as? String ?? "Originalpaket", "url": url]
            }

        }
        let manifestDefault = resourceRoot.appendingPathComponent("defaults/manifest.json")
        let correctionDefault = resourceRoot.appendingPathComponent("defaults/correction.json")
        if FileManager.default.fileExists(atPath: manifestDefault.path) { manifest = manifestDefault }
        if FileManager.default.fileExists(atPath: correctionDefault.path) { correction = correctionDefault }
        let saved = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/MI1PatchLauncher/last-root")
        if let path = try? String(contentsOf: saved, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           !path.isEmpty, validDirectory(URL(fileURLWithPath: path)) {
            root = URL(fileURLWithPath: path); loadSavedGameSource()
        }
    }

    private func validDirectory(_ url: URL?) -> Bool {
        guard let url, let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isReadableKey]) else { return false }
        return values.isDirectory == true && values.isReadable == true
    }

    private func validGame(_ url: URL?) -> Bool {
        guard let url else { return false }
        if validDirectory(url) { return true }
        return url.pathExtension.lowercased() == "exe" && FileManager.default.isReadableFile(atPath: url.path) &&
            (try? url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true
    }

    private func loadSavedGameSource() {
        gameDirectory = nil
        guard let root, let data = try? Data(contentsOf: root.appendingPathComponent("MI1 Generator/.launcher/project.json")),
              let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        guard let path = saved["pc_special_edition_directory"] as? String, path.hasPrefix("/") else { return }
        let candidate = URL(fileURLWithPath: path)
        if validGame(candidate) { gameDirectory = candidate }
    }

    @objc private func chooseRoot() {
        let picker = NSOpenPanel(); picker.canChooseDirectories = true; picker.canChooseFiles = false
        picker.allowsMultipleSelection = false; picker.prompt = "Arbeitslaufwerk wählen"
        if picker.runModal() == .OK { root = picker.url; loadSavedGameSource(); update(); checkResume() }
    }

    @objc private func chooseGameDirectory() {
        let picker = NSOpenPanel(); picker.title = "PC-Special-Edition: Spielordner oder GOG-Offline-Installer"
        picker.canChooseDirectories = true; picker.canChooseFiles = true
        picker.allowedContentTypes = [.directory, UTType(filenameExtension: "exe") ?? .data]
        picker.allowsOtherFileTypes = false
        picker.treatsFilePackagesAsDirectories = true
        picker.allowsMultipleSelection = false; picker.prompt = "PC-SE / GOG auswählen"
        if picker.runModal() == .OK { gameDirectory = picker.url; update(); checkResume() }
    }

    @objc private func chooseManifest() { if let selected = chooseJSON("Dialogvorgaben auswählen") { manifest = selected }; update(); checkResume() }
    @objc private func chooseCorrection() { if let selected = chooseJSON("Rezept auswählen") { correction = selected }; update(); checkResume() }
    private func chooseJSON(_ title: String) -> URL? {
        let picker = NSOpenPanel(); picker.title = title; picker.canChooseFiles = true
        picker.canChooseDirectories = false; picker.allowsMultipleSelection = false
        picker.allowedContentTypes = [.json]; picker.prompt = "Auswählen"
        return picker.runModal() == .OK ? picker.url : nil
    }

    private func correctionMetadata() -> [String: String] {
        guard let correction else { return [:] }
        do {
            let data = try Data(contentsOf: correction)
            guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  value["format"] as? String == "mi1-render-recipe/1" else { return [:] }
            let labels = ["ane_gpu": "ANE+GPU", "ane": "ANE", "gpu": "GPU"]
            guard let pipeline = value["pipeline"] as? String, let label = labels[pipeline] else {
                return ["correctionPipelineError": "Das Rezept verwendet einen unbekannten Rendermodus. Bitte prüfe die Datei."]
            }
            let allowed: Set<String> = pipeline == "ane_gpu" ? ["gpu", "ane"] : [pipeline]
            let rows = value["dialogues"] as? [String: [String: Any]] ?? [:]
            let conflicts = rows.keys.sorted().compactMap { id -> String? in
                guard let backend = rows[id]?["backend"] as? String, ["gpu", "ane"].contains(backend),
                      !allowed.contains(backend) else { return nil }
                return "Warnung: Dialog \(id) verwendet \(backend.uppercased()), obwohl die Pipeline \(label) angibt. Die konkrete JSON-Einstellung wird gerendert."
            }
            return ["correctionPipeline": label, "correctionWarnings": conflicts.joined(separator: "\n")]
        } catch {
            return ["correctionPipelineError": "Rezept konnte nicht als JSON gelesen werden."]
        }
    }

    private func update() {
        let metadata = correctionMetadata()
        send("update", ["root": root?.path ?? "", "dataRoot": root?.appendingPathComponent("MI1 Generator").path ?? "",
            "game": gameDirectory?.path ?? "", "gameKind": gameDirectory?.pathExtension.lowercased() == "exe" ? "GOG-Offline-Installer" : "PC-Special-Edition-Ordner",
            "manifest": manifest?.path ?? "", "correction": correction?.path ?? "",
            "correctionPipeline": metadata["correctionPipeline"] ?? "", "correctionPipelineError": metadata["correctionPipelineError"] ?? "",
            "correctionWarnings": metadata["correctionWarnings"] ?? "",
            "ready": validDirectory(root) && validGame(gameDirectory) && manifest != nil,
            "busy": bootstrapping || terminationPending, "status": statusText, "statusKind": statusKind,
            "version": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "",
            "bootstrap": bootstrapDescription, "terms": packageTerms, "log": startupLog])
    }

    // Inspect only existing local packages and job receipts. No bootstrap,
    // download, installation or rendering is allowed in this entry check.
    private func checkResume() {
        guard let root, let manifest, validDirectory(root), validGame(gameDirectory),
              !bootstrapping, !terminationPending, hostOrigin == nil else { return }
        let chosenRoot = ExistingServer.canonicalRoot(root)
        let selectedCorrection = correction
        let selectedGame = gameDirectory
        let key = [chosenRoot.path, manifest.path, selectedCorrection?.path ?? "", selectedGame?.path ?? ""].joined(separator: "\n")
        guard resumeCheckKey != key else { return }
        resumeCheckKey = key
        let pythonRoot = chosenRoot.appendingPathComponent("MI1 Generator/foreign/bootstrap/python", isDirectory: true)
        guard let python = find(named: "python3.12", under: pythonRoot),
              FileManager.default.isExecutableFile(atPath: python.path) else { return }
        bootstrapping = true; setStatus("Vorhandenen Renderauftrag und lokale Pakete prüfen …")
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let check = Process(); check.executableURL = python; check.currentDirectoryURL = self.resourceRoot
                var arguments = ["-B", "-S", "-m", "generator_launcher.host", "--resume-check",
                                 "--root", chosenRoot.path, "--manifest", manifest.path]
                if let selectedCorrection { arguments += ["--correction", selectedCorrection.path] }
                check.arguments = arguments
                let output = Pipe(); check.standardOutput = output; check.standardError = FileHandle.nullDevice
                try self.runBootstrap(check)
                let data = output.fileHandleForReading.readDataToEndOfFile(); check.waitUntilExit()
                try self.checkLaunchAllowed()
                guard check.terminationStatus == 0,
                      let result = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      ExistingServer.boolean(result["resume_available"]) == true,
                      let project = result["project"] as? [String: Any] else {
                    DispatchQueue.main.async { self.bootstrapping = false; self.setStatus("Bereit · Einrichtung auswählen oder bestätigen", kind: "ready") }
                    return
                }
                let existing = try ExistingServer.discover(root: chosenRoot)
                let reusedURL = try existing?.resumeURL(project: project,
                    launcherVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "")
                DispatchQueue.main.async {
                    guard !self.terminationPending else { return }
                    self.resumeEntry = true
                    self.runtimeRoot = pythonRoot
                    self.diagnostic("render_resume_entry", ["server_reused": existing != nil])
                    if let reusedURL {
                        self.bootstrapping = false; self.hostOrigin = reusedURL
                        self.web.load(URLRequest(url: reusedURL)); NSApp.activate(ignoringOtherApps: true)
                    } else {
                        self.launchGeneration += 1
                        self.launchHost(root: chosenRoot, manifest: manifest, correction: selectedCorrection, gameDirectory: selectedGame)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    guard !self.terminationPending else { return }
                    self.bootstrapping = false
                    self.setStatus("Direkter Wiedereinstieg nicht verfügbar: \(error.localizedDescription)", kind: "warning")
                }
            }
        }
    }

    private func start() {
        guard let root, let manifest, validDirectory(root), validGame(gameDirectory),
              !initialConsents.isEmpty, !bootstrapping, !terminationPending else { return }
        let chosenRoot = ExistingServer.canonicalRoot(root)
        let chosenGame = gameDirectory
        bootstrapping = true; update()
        setStatus("Nach einem geöffneten Patch suchen …")
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let existing = try ExistingServer.discover(root: chosenRoot)
                self.diagnostic("server_discovery", ["found": existing != nil, "pid": existing?.pid ?? 0, "version": existing?.version ?? ""])
                DispatchQueue.main.async {
                    if let existing {
                        let message = NSAlert()
                        message.messageText = "Der Patch ist bereits geöffnet"
                        message.informativeText = "Geöffnete Version: \(existing.version)\nArbeitsordner: \(chosenRoot.path)\n\nMöchtest du den Patch mit dieser Version und deiner aktuellen Auswahl neu starten? Deine Arbeit bleibt erhalten. Solange ein Auftrag läuft, ist kein Neustart möglich."
                        message.addButton(withTitle: "Patch neu starten")
                        message.addButton(withTitle: "Abbrechen")
                        guard message.runModal() == .alertFirstButtonReturn else {
                            self.bootstrapping = false; self.update()
                            self.setStatus("Neustart abgebrochen; der geöffnete Patch läuft weiter.", kind: "warning")
                            return
                        }
                    }
                    self.prepareLaunch(root: chosenRoot, manifest: manifest, correction: self.correction,
                                       gameDirectory: chosenGame, replacing: existing)
                }
            } catch { self.startFailed(error) }
        }
    }

    private func startFailed(_ error: Error) {
        DispatchQueue.main.async {
            guard !self.terminationPending else { return }
            self.bootstrapping = false; self.update()
            self.setStatus("Start fehlgeschlagen: \(error.localizedDescription)", kind: "failed")
            let message = NSAlert()
            message.messageText = "Der Patch konnte nicht starten"
            message.informativeText = error.localizedDescription
            message.addButton(withTitle: "OK"); message.runModal()
        }
    }

    private func prepareLaunch(root: URL, manifest: URL, correction: URL?,
                               gameDirectory: URL?, replacing existing: ExistingServer?) {
        launchGeneration += 1 // Ignore delayed output from our previous child after confirmation.
        setStatus(existing == nil ? "Patch wird vorbereitet …" : "Geöffneten Patch beenden …")
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                self.diagnostic("server_stop_requested", ["existing": existing != nil])
                try existing?.stopAndWait()
                self.diagnostic("server_stop_finished")
                try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
                try self.ensureBootstrap(root: root)
                let support = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent("Library/Application Support/MI1PatchLauncher", isDirectory: true)
                try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
                try root.path.write(to: support.appendingPathComponent("last-root"), atomically: true, encoding: .utf8)
                DispatchQueue.main.async {
                    guard !self.terminationPending else { return }
                    self.launchHost(root: root, manifest: manifest, correction: correction, gameDirectory: gameDirectory)
                }
            } catch { self.startFailed(error) }
        }
    }

    private func byteLabel(_ bytes: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }

    private func bootstrapStatus(_ text: String) {
        DispatchQueue.main.async { self.setStatus(text) }
    }

    private func checkLaunchAllowed() throws {
        helperLock.lock(); defer { helperLock.unlock() }
        if terminationPending { throw StartError.cancelled }
    }

    private func runBootstrap(_ helper: Process) throws {
        helperLock.lock(); defer { helperLock.unlock() }
        guard !terminationPending else { throw StartError.cancelled }
        bootstrapProcesses.removeAll { !$0.isRunning }
        try helper.run()
        bootstrapProcesses.append(helper)
    }

    private func ensureBootstrap(root: URL) throws {
        try checkLaunchAllowed()
        let catalogURL = resourceRoot.appendingPathComponent("runtime.json")
        let catalogData = try Data(contentsOf: catalogURL)
        let catalogHash = SHA256.hash(data: catalogData).map { String(format: "%02x", $0) }.joined()
        guard catalogHash == "15c915cd4617837348a4957732012d1eac3a04182d88fe0ff46622866fbe0799" else { throw StartError.badRecipe }
        let catalog = try JSONSerialization.jsonObject(with: catalogData) as! [String: Any]
        let artifacts = catalog["artifacts"] as! [[String: Any]]
        let recipes = catalog["recipes"] as! [String: Any]
        let bootstrap = catalog["bootstrap"] as! [String: Any]
        guard bootstrap["requires_python"] as? Bool == false else { throw StartError.badRecipe }
        let bootstrapRoot = root.appendingPathComponent("MI1 Generator/foreign/bootstrap", isDirectory: true)
        try FileManager.default.createDirectory(at: bootstrapRoot, withIntermediateDirectories: true)
        for (index, id) in ["python"].enumerated() {
            guard let artifact = artifacts.first(where: { $0["id"] as? String == id }),
                  let recipe = recipes[id] as? [String: Any], recipe["kind"] as? String == "native-bootstrap",
                  let urlText = artifact["url"] as? String, let expected = artifact["hash"] as? String,
                  let expectedSize = artifact["bytes"] as? Int else { throw StartError.badRecipe }
            let target = bootstrapRoot.appendingPathComponent("python.tar.gz")
            bootstrapStatus("\(index + 1)/1 · \(id.capitalized) · vorhandene Datei prüfen …")
            if !valid(target, bytes: expectedSize, hash: expected) {
                let part = target.appendingPathExtension("part")
                let curl = Process(); curl.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
                curl.arguments = ["--http1.1", "--location", "--fail", "--show-error", "--silent",
                    "--connect-timeout", "30", "--speed-limit", "1024", "--speed-time", "120",
                    "--continue-at", "-", "--output", part.path, urlText]
                try runBootstrap(curl)
                while curl.isRunning {
                    let attributes = try? FileManager.default.attributesOfItem(atPath: part.path)
                    let received = (attributes?[.size] as? NSNumber)?.intValue ?? 0
                    bootstrapStatus("\(index + 1)/1 · \(id.capitalized): \(byteLabel(received)) von \(byteLabel(expectedSize))")
                    Thread.sleep(forTimeInterval: 0.4)
                }
                curl.waitUntilExit()
                try checkLaunchAllowed()
                bootstrapStatus("\(index + 1)/1 · \(id.capitalized) · Größe und SHA-256 prüfen …")
                guard curl.terminationStatus == 0, valid(part, bytes: expectedSize, hash: expected) else { throw StartError.download(id) }
                try FileManager.default.moveItem(at: part, to: target)
            }
            let destination = bootstrapRoot.appendingPathComponent(id, isDirectory: true)
            bootstrapStatus("\(index + 1)/1 · \(id.capitalized) geprüft · entpacken bzw. wiederverwenden …")
            if !FileManager.default.fileExists(atPath: destination.path) {
                let stage = bootstrapRoot.appendingPathComponent(id + ".part", isDirectory: true)
                try? FileManager.default.removeItem(at: stage)
                try FileManager.default.createDirectory(at: stage, withIntermediateDirectories: true)
                let unpack = Process(); unpack.executableURL = URL(fileURLWithPath: "/usr/bin/tar")
                unpack.arguments = ["-xzf", target.path, "-C", stage.path]
                try runBootstrap(unpack); unpack.waitUntilExit()
                try checkLaunchAllowed()
                guard unpack.terminationStatus == 0 else { throw StartError.unpack(id) }
                try FileManager.default.moveItem(at: stage, to: destination)
            }
            runtimeRoot = destination
            guard let executable = find(named: "python3.12", under: destination), FileManager.default.isExecutableFile(atPath: executable.path) else { throw StartError.pythonMissing }

        }
        _ = bootstrap // P17 source catalog controls that Python is not required for acquisition.
    }

    private func valid(_ file: URL, bytes: Int, hash: String) -> Bool {
        guard let data = try? Data(contentsOf: file), data.count == bytes else { return false }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() == hash
    }

    private func find(named name: String, under root: URL) -> URL? {
        let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles])
        while let item = enumerator?.nextObject() as? URL { if item.lastPathComponent == name { return item } }
        return nil
    }

    private func launchHost(root: URL, manifest: URL, correction: URL?, gameDirectory: URL?) {
        setStatus("Projektserver starten …")
        guard let python = find(named: "python3.12", under: runtimeRoot) else { setStatus("Das Hilfsprogramm Python fehlt. Starte die Einrichtung erneut.", kind: "failed"); return }
        let projectRoot = resourceRoot
        let child = Process(); child.executableURL = python
        child.currentDirectoryURL = projectRoot
        // Keep the signed bundle immutable: Python must not add __pycache__.
        var arguments = ["-B", "-S", "-m", "generator_launcher.host", "--root", root.path,
                         "--manifest", manifest.path]
        if let correction { arguments += ["--correction", correction.path] }
        if let gameDirectory { arguments += ["--game-directory", gameDirectory.path] }
        arguments += ["--parent-pid", String(getpid()), "--native-owned"]
        guard let consentData = try? JSONSerialization.data(withJSONObject: initialConsents),
              let consentJSON = String(data: consentData, encoding: .utf8),
              let logData = try? JSONSerialization.data(withJSONObject: startupLog),
              let logJSON = String(data: logData, encoding: .utf8) else { return }
        arguments += ["--startup-log", logJSON]
        if !resumeEntry { arguments += ["--initial-consents", consentJSON, "--start-setup"] }
        child.arguments = arguments
        let output = Pipe(); child.standardOutput = output; child.standardError = output
        process = child
        let generation = launchGeneration
        child.terminationHandler = { [weak self] stopped in
            DispatchQueue.main.async {
                guard let self, !self.terminationPending, self.process === stopped,
                      self.launchGeneration == generation else { return }
                self.diagnostic("server_exit", ["status": stopped.terminationStatus])
                self.bootstrapping = false
                let text = "Projektserver unerwartet beendet · Status \(stopped.terminationStatus)"
                if self.hostOrigin == nil { self.setStatus(text, kind: "failed") }
                else {
                    self.web.evaluateJavaScript("window.LauncherConsole?.update('setup','Die Verarbeitung wurde unerwartet beendet. Starte den Patch erneut.',[],'native-host',{status:'failed'})", completionHandler: nil)
                }
            }
        }
        do {
            try child.run()
            diagnostic("server_started", ["pid": Int(child.processIdentifier)])
            DispatchQueue.global().async { [weak self] in
                guard let self else { return }
                let handle = output.fileHandleForReading
                var pending = Data()
                while child.isRunning {
                    let part = handle.availableData
                    if part.isEmpty { break }
                    pending.append(part)
                    while let newline = pending.firstIndex(of: 10) {
                        let line = Data(pending[..<newline])
                        pending.removeSubrange(pending.startIndex...newline)
                        if let result = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                           let url = result["url"] as? String {
                            DispatchQueue.main.async {
                                guard self.launchGeneration == generation, self.process === child else { return }
                                self.openURL(url)
                            }
                            // Keep draining tool output after the startup message.
                            // Otherwise a compiler can fill the pipe and stall setup.
                            while !handle.availableData.isEmpty {}
                            return
                        }
                        let text = String(data: line, encoding: .utf8) ?? "Startausgabe"
                        DispatchQueue.main.async {
                            guard self.launchGeneration == generation, self.process === child else { return }
                            self.setStatus(text.trimmingCharacters(in: .whitespacesAndNewlines))
                        }
                        // Keep reading after diagnostics: closing this reader
                        // can SIGPIPE a compiler writing into the host pipe.
                    }
                }
            }
        } catch { startFailed(error) }
    }

    private func openURL(_ string: String) {
        guard let parts = URLComponents(string: string), parts.scheme == "http", parts.host == "127.0.0.1",
              let port = parts.port, (1...65535).contains(port), parts.path == "/", parts.fragment == nil,
              parts.user == nil, parts.password == nil, parts.queryItems?.count == 1,
              let item = parts.queryItems?.first, item.name == "session", let token = item.value,
              !token.isEmpty, token.count <= 256, token.utf8.allSatisfy({
                  (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95
              }), let url = parts.url else {
            startFailed(StartError.badRecipe); return
        }
        guard let child = process, child.isRunning, let root else { return }
        let generation = launchGeneration
        var endpoint = parts; endpoint.path = "/api/server/status"; endpoint.query = nil
        var request = URLRequest(url: endpoint.url!); request.timeoutInterval = 5
        request.setValue(token, forHTTPHeaderField: "X-Launcher-Session")
        URLSession.shared.dataTask(with: request) { data, response, error in
            let identity = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let verified = error == nil && (response as? HTTPURLResponse)?.statusCode == 200 &&
                identity?["profile"] as? String == "mi1-patch-launcher/1" &&
                ExistingServer.integer(identity?["pid"]) == Int(child.processIdentifier) &&
                identity?["root"] as? String == ExistingServer.canonicalRoot(root).path
            DispatchQueue.main.async {
                guard !self.terminationPending, self.process === child, self.launchGeneration == generation else { return }
                self.diagnostic("server_identity_checked", ["verified": verified, "pid": Int(child.processIdentifier),
                    "http_status": (response as? HTTPURLResponse)?.statusCode ?? 0,
                    "reported_pid": ExistingServer.integer(identity?["pid"]) ?? 0,
                    "profile": identity?["profile"] as? String ?? "", "error": error?.localizedDescription ?? ""])
                guard verified else { self.startFailed(ExistingServerError.unsafe); return }
                self.bootstrapping = false
                var destination = URLComponents(url: url, resolvingAgainstBaseURL: false)!
                if self.resumeEntry { destination.fragment = "render" }
                self.hostOrigin = destination.url!
                self.diagnostic("server_page_requested", ["url": self.diagnosticAddress(url)])
                self.web.load(URLRequest(url: destination.url!)); NSApp.activate(ignoringOtherApps: true)
            }
        }.resume()
    }


}

enum StartError: LocalizedError {
    case cancelled, badRecipe, download(String), unpack(String), pythonMissing
    var errorDescription: String? {
        switch self {
        case .cancelled: return "Start wurde beim Beenden des Patchs abgebrochen."
        case .badRecipe: return "P17-Bootstraprezept ist unvollständig."
        case .download(let id): return "Download/Hashprüfung für \(id) fehlgeschlagen; unvollständige Datei bleibt fortsetzbar."
        case .unpack(let id): return "Entpacken von \(id) fehlgeschlagen."
        case .pythonMissing: return "Ein benötigtes Hilfsprogramm fehlt. Starte die Einrichtung erneut."
        }
    }
}

@main
struct LauncherApp {
    static func main() {
        let app = NSApplication.shared
        let delegate = Launcher()
        app.delegate = delegate
        app.run()
    }
}
