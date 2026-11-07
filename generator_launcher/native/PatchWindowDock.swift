import AppKit
import Darwin
import Foundation

struct PatchInteraction: Equatable {
    let stageID: String
    let pid: pid_t
    let targetMacOS: String
    let targetWine: String?
    let wineBinary: String
    let executable: String

    init?(_ value: [String: Any]) {
        guard let stage = value["stage_id"] as? String, !stage.isEmpty, stage.count <= 128,
              let pid = ExistingServer.integer(value["process_pid"]), pid > 1, pid <= Int(Int32.max),
              let mac = value["patch_target_macos"] as? String, mac.hasPrefix("/"),
              value["patch_target_wine"] is NSNull || value["patch_target_wine"] is String,
              let binary = value["wine_binary"] as? String, binary.hasPrefix("/"),
              let executable = value["patch_executable"] as? String, executable.hasPrefix("/"),
              [mac, binary, executable].allSatisfy({ !$0.contains("\0") && !$0.contains("\n") }) else { return nil }
        let wine = value["patch_target_wine"] as? String
        if let wine, wine.isEmpty || wine.count >= 32_768 || wine.contains("\0") || wine.contains("\n") { return nil }
        stageID = stage; self.pid = pid_t(pid); targetMacOS = mac; targetWine = wine
        wineBinary = binary; self.executable = executable
    }

    static func confirmed(_ status: [String: Any], requested: PatchInteraction) -> Bool {
        guard status["status"] as? String == "running", status["phase"] as? String == "german_patch",
              let active = status["patch_interaction"] as? [String: Any],
              let confirmed = PatchInteraction(active) else { return false }
        return confirmed == requested
    }
}

/// Wine re-execs its wrapper and may change argv; process birth remains stable.
struct PatchProcessIdentity: Equatable {
    let pid: pid_t
    let uid: uid_t
    let startSeconds: Int
    let startMicroseconds: Int

    static func read(_ pid: pid_t) -> PatchProcessIdentity? {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.size
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        guard sysctl(&mib, UInt32(mib.count), &info, &size, nil, 0) == 0,
              size == MemoryLayout<kinfo_proc>.size,
              info.kp_proc.p_stat != SZOMB, info.kp_eproc.e_ucred.cr_uid == geteuid() else { return nil }
        return PatchProcessIdentity(pid: pid, uid: info.kp_eproc.e_ucred.cr_uid,
            startSeconds: Int(info.kp_proc.p_starttime.tv_sec),
            startMicroseconds: Int(info.kp_proc.p_starttime.tv_usec))
    }
}

enum PatchProcessGuard {
    static func canonical(_ path: String) -> String {
        ExistingServer.canonicalRoot(URL(fileURLWithPath: path)).path
    }

    /// The kernel executable path remains Wine even when argv[0] names a PE program.
    static func executablePath(_ pid: pid_t) -> String? {
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var size = 0
        guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > 4, size < 16_777_216 else { return nil }
        var bytes = [UInt8](repeating: 0, count: size)
        guard bytes.withUnsafeMutableBytes({ sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }) == 0 else { return nil }
        let start = MemoryLayout<Int32>.size
        guard let end = bytes[start..<size].firstIndex(of: 0),
              let path = String(bytes: bytes[start..<end], encoding: .utf8), path.hasPrefix("/") else { return nil }
        return PatchProcessGuard.canonical(path)
    }

    static func runtimePath(_ actual: String, matches binary: String) -> Bool {
        let expected = canonical(binary), runtime = canonical(actual)
        if runtime == expected { return true }
        // Wine 11.18's bin/wine wrapper re-execs this exact bundled loader.
        // Source: dlls/ntdll/unix/loader.c, init_paths()/reexec_loader().
        let binaryURL = URL(fileURLWithPath: expected)
        if binaryURL.lastPathComponent == "wine", binaryURL.deletingLastPathComponent().lastPathComponent == "bin" {
            let loader = binaryURL.deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("lib/wine/x86_64-unix/wine").path
            if runtime == canonical(loader) { return true }
        }
        // Wine may exec its own sibling preloader. A displayed .exe process name
        // does not broaden this kernel executable-path check to arbitrary programs.
        return runtime == expected + "-preloader"
    }

}

/// Presents the authenticated original patch without Accessibility or screen permissions.
/// The historical class name remains the native bridge implementation detail; it does not dock windows.
final class PatchWindowDock: NSObject, URLSessionTaskDelegate {
    private let report: ([String: Any]) -> Void
    private let copyTarget: (String) -> Bool
    private var interaction: PatchInteraction?
    private var rootIdentity: PatchProcessIdentity?
    private var timer: Timer?
    private var task: URLSessionDataTask?
    private lazy var session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
    private var generation = 0
    private var endpoint: URL?
    private var token = ""
    private var confirmedAt = Date.distantPast
    private var checkedAt = Date.distantPast
    private var appearanceDeadline = Date.distantPast
    private var copiedStages = Set<String>()
    private var clipboardCopied = false
    private var copyPending = false
    private var copyOnlyRequest = false
    private var selectedIdentity: PatchProcessIdentity?
    private var activationRequested: Date?
    private var presented = false
    private var lastState = ""
    private var lastMessage = ""

    init(copyTarget: @escaping (String) -> Bool = { path in
        NSPasteboard.general.clearContents()
        return NSPasteboard.general.setString(path, forType: .string)
    }, report: @escaping ([String: Any]) -> Void) {
        self.copyTarget = copyTarget; self.report = report; super.init()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil) // Never forward the launcher session to a redirect target.
    }

    func receive(_ body: [String: Any], origin: URL) {
        guard ExistingServer.boolean(body["active"]) == true else { stop(); return }
        guard let requested = PatchInteraction(body),
              var parts = URLComponents(url: origin, resolvingAgainstBaseURL: false),
              parts.scheme == "http", ["127.0.0.1", "localhost"].contains(parts.host ?? ""),
              let sessionToken = parts.queryItems?.first(where: { $0.name == "session" })?.value,
              !sessionToken.isEmpty else {
            stop(state: "unavailable", message: "Patchfenster: ungültige aktive Stufe."); return
        }
        let copyOnly = ExistingServer.boolean(body["copy_only"]) == true
        let retry = ExistingServer.boolean(body["retry"]) == true
        if interaction == requested, !retry {
            if copyOnly {
                copyPending = true; copyOnlyRequest = true
                timer?.invalidate(); timer = nil
                validateHost()
            }
            return
        }
        stop(emit: false)
        interaction = requested; token = sessionToken
        clipboardCopied = requested.targetWine != nil && copiedStages.contains(requested.stageID)
        lastState = ""
        copyPending = copyOnly; copyOnlyRequest = copyOnly
        parts.path = "/api/preparation/game-status"; parts.query = nil; parts.fragment = nil
        endpoint = parts.url
        appearanceDeadline = Date().addingTimeInterval(120)
        emit("waiting", "Aktiven Originalpatch prüfen …")
        validateHost(initial: true)
    }

    func stop(state: String = "stopped", message: String = "Patchfenster-Begleitung beendet.", emit: Bool = true) {
        generation += 1; timer?.invalidate(); timer = nil; task?.cancel(); task = nil
        selectedIdentity = nil; activationRequested = nil; presented = false
        rootIdentity = nil; confirmedAt = .distantPast
        if emit { self.emit(state, message) }
        interaction = nil; token = ""; endpoint = nil; copyPending = false; copyOnlyRequest = false
    }

    private func emit(_ state: String, _ message: String) {
        guard state != lastState || message != lastMessage else { return }
        lastState = state; lastMessage = message
        let foreground = selectedIdentity.map {
            isLive($0) && NSWorkspace.shared.frontmostApplication?.processIdentifier == $0.pid
        } ?? false
        report(["state": state, "message": message, "stage_id": interaction?.stageID ?? "",
                "clipboard_copied": clipboardCopied, "foreground": foreground])
    }

    private func validateHost(initial: Bool = false) {
        guard task == nil, let endpoint, let current = interaction else { return }
        checkedAt = Date()
        let requestGeneration = generation
        var request = URLRequest(url: endpoint); request.timeoutInterval = 3
        request.setValue(token, forHTTPHeaderField: "X-Launcher-Session")
        task = session.dataTask(with: request) { [weak self] data, response, error in
            let status = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                guard let self, self.generation == requestGeneration else { return }
                self.task = nil
                guard error == nil, (response as? HTTPURLResponse)?.statusCode == 200, let status else {
                    self.stop(state: "unavailable", message: "Aktive Patchstufe nicht mehr sicher erreichbar. Klicke im macOS-Dock auf das gelbe Wine-Symbol."); return
                }
                guard PatchInteraction.confirmed(status, requested: current) else { self.stop(); return }
                self.confirmedAt = Date()
                // The authenticated host owns the live stage and exact target path.
                // Clipboard access does not depend on Wine's mutable argv or GUI registration.
                let requestedCopy = self.copyPending
                self.copyPending = false
                if initial || requestedCopy {
                    self.clipboardCopied = current.targetWine != nil && self.copiedStages.contains(current.stageID)
                    if let path = current.targetWine, !self.clipboardCopied || requestedCopy {
                        self.clipboardCopied = self.copyTarget(path)
                        if self.clipboardCopied { self.copiedStages.insert(current.stageID) }
                    }
                    if requestedCopy {
                        self.lastState = ""
                        self.emit(self.clipboardCopied ? "copied" : "unavailable",
                                  self.clipboardCopied ? "Windows-Zielpfad erneut kopiert." :
                                    current.targetWine == nil ? "Wine-Zielpfad nicht verfügbar; den angezeigten macOS-Zielordner verwenden." : "Windows-Zielpfad konnte nicht kopiert werden.")
                    }
                }
                if self.copyOnlyRequest { return } // A copy request must never activate Wine.
                guard let identity = PatchProcessIdentity.read(current.pid),
                      self.matchesRuntime(identity, current),
                      self.rootIdentity == nil || self.rootIdentity == identity else {
                    self.stop(state: "unavailable", message: "Der aktive Wine-Patchprozess ist nicht mehr eindeutig nachweisbar. Klicke im macOS-Dock auf das gelbe Wine-Symbol."); return
                }
                self.rootIdentity = identity
                if initial {
                    let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in self?.tick() }
                    self.timer = timer; RunLoop.main.add(timer, forMode: .common)
                }
                self.tick()
            }
        }
        task?.resume()
    }

    private func matchesRuntime(_ identity: PatchProcessIdentity, _ stage: PatchInteraction) -> Bool {
        guard let path = PatchProcessGuard.executablePath(identity.pid) else { return false }
        return PatchProcessGuard.runtimePath(path, matches: stage.wineBinary)
    }

    private func isLive(_ expected: PatchProcessIdentity) -> Bool {
        guard let stage = interaction, let actual = PatchProcessIdentity.read(expected.pid) else { return false }
        return actual == expected && matchesRuntime(actual, stage)
    }

    private func liveRoot() -> Bool {
        guard let expected = rootIdentity else { return false }
        return isLive(expected)
    }

    private func tick() {
        guard let stage = interaction, !copyOnlyRequest else { return }
        guard liveRoot() else { stop(); return }
        if Date().timeIntervalSince(checkedAt) >= 2 { validateHost() }
        guard Date().timeIntervalSince(confirmedAt) < 4, !presented else { return }
        if let identity = selectedIdentity, !isLive(identity) {
            selectedIdentity = nil; activationRequested = nil
        }
        if let identity = selectedIdentity {
            if NSWorkspace.shared.frontmostApplication?.processIdentifier == identity.pid {
                presented = true
                emit("foreground", clipboardCopied ? "Patchfenster im Vordergrund; Windows-Zielpfad kopiert." :
                    stage.targetWine == nil ? "Patchfenster im Vordergrund; Wine-Zielpfad nicht verfügbar. Den angezeigten macOS-Zielordner verwenden." : "Patchfenster im Vordergrund; Zielpfad bitte manuell kopieren.")
                return
            }
            if let attempted = activationRequested {
                if Date().timeIntervalSince(attempted) > 2 {
                    presented = true // Do not repeatedly steal focus after an accepted activation.
                    emit("unavailable", "macOS hat den Vordergrundwechsel nicht bestätigt. Klicke im macOS-Dock auf das gelbe Wine-Symbol.")
                }
                return
            }
        }
        guard let (application, identity) = findApplication(), liveRoot() else {
            if Date() > appearanceDeadline {
                stop(state: "unavailable", message: "Keine eindeutig zugeordnete Wine-Patchanwendung gefunden. Klicke im macOS-Dock auf das gelbe Wine-Symbol.")
            } else {
                emit("waiting", clipboardCopied ? "Windows-Zielpfad kopiert; auf die Wine-Patchanwendung warten …" :
                    stage.targetWine == nil ? "Wine-Zielpfad nicht verfügbar; auf die Wine-Patchanwendung warten …" : "Auf die Wine-Patchanwendung warten; Zielpfad bitte manuell kopieren.")
            }
            return
        }
        selectedIdentity = identity
        if NSWorkspace.shared.frontmostApplication?.processIdentifier == identity.pid {
            tick(); return
        }
        // Ordinary AppKit activation requires no Accessibility permission.
        if application.isHidden { _ = application.unhide() }
        NSApp.yieldActivation(to: application)
        if application.activate(from: .current, options: [.activateAllWindows]) { activationRequested = Date() }
        else if Date() > appearanceDeadline {
            presented = true
            emit("unavailable", "Wine konnte nicht automatisch in den Vordergrund wechseln. Klicke im macOS-Dock auf das gelbe Wine-Symbol.")
        }
    }

    /// Only this root's descendants qualify. A shared Wine server or another Wine job cannot qualify.
    private func descendants(of root: pid_t) -> [pid_t] {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL]
        var size = 0
        guard sysctl(&mib, 3, nil, &size, nil, 0) == 0 else { return [] }
        var entries = [kinfo_proc](repeating: kinfo_proc(), count: size / MemoryLayout<kinfo_proc>.stride + 32)
        size = entries.count * MemoryLayout<kinfo_proc>.stride
        guard entries.withUnsafeMutableBytes({ sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }) == 0 else { return [] }
        let records = entries.prefix(size / MemoryLayout<kinfo_proc>.stride)
        var owned: Set<pid_t> = [root]
        for _ in 0..<16 {
            let before = owned.count
            for record in records where record.kp_proc.p_stat != SZOMB &&
                record.kp_eproc.e_ucred.cr_uid == geteuid() && owned.contains(record.kp_eproc.e_ppid) {
                owned.insert(record.kp_proc.p_pid)
            }
            if owned.count == before || owned.count > 256 { break }
        }
        return owned.count <= 256 ? owned.sorted() : []
    }

    private func findApplication() -> (NSRunningApplication, PatchProcessIdentity)? {
        guard let current = interaction else { return nil }
        var candidates = [(NSRunningApplication, PatchProcessIdentity)]()
        for pid in descendants(of: current.pid) {
            guard let path = PatchProcessGuard.executablePath(pid),
                  PatchProcessGuard.runtimePath(path, matches: current.wineBinary),
                  let identity = PatchProcessIdentity.read(pid),
                  let application = NSRunningApplication(processIdentifier: pid),
                  !application.isTerminated, application.activationPolicy != .prohibited else { continue }
            candidates.append((application, identity))
        }
        // Process identity, not application name or incidental ordering, selects the patch.
        if let root = candidates.first(where: { $0.1.pid == current.pid }) { return root }
        return candidates.count == 1 ? candidates[0] : nil
    }
}
