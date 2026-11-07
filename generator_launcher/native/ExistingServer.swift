import CoreFoundation
import Darwin
import Foundation

enum ExistingServerError: LocalizedError {
    case unsafe, busy, unavailable, timeout
    var errorDescription: String? {
        switch self {
        case .unsafe: return "Der geöffnete Patch konnte diesem Arbeitsordner nicht sicher zugeordnet werden. Er wurde nicht beendet."
        case .busy: return "Der Patch verarbeitet noch einen Auftrag. Warte auf dessen Abschluss und starte erneut."
        case .unavailable: return "Der geöffnete Patch antwortet nicht. Er wurde nicht beendet."
        case .timeout: return "Der geöffnete Patch hat sich nicht rechtzeitig beendet. Es wurde kein weiterer Patch gestartet."
        }
    }
}

struct HostProcessIdentity: Equatable {
    let pid: pid_t
    let uid: uid_t
    let startSeconds: Int
    let startMicroseconds: Int
    let arguments: [String]

    static func read(_ pid: pid_t) throws -> HostProcessIdentity {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.size
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        guard sysctl(&mib, UInt32(mib.count), &info, &size, nil, 0) == 0,
              size == MemoryLayout<kinfo_proc>.size, info.kp_eproc.e_ucred.cr_uid == geteuid() else {
            throw ExistingServerError.unsafe
        }
        var argMax: Int32 = 0
        size = MemoryLayout<Int32>.size
        var maxMIB: [Int32] = [CTL_KERN, KERN_ARGMAX]
        guard sysctl(&maxMIB, 2, &argMax, &size, nil, 0) == 0, argMax > 0, argMax < 16_777_216 else {
            throw ExistingServerError.unsafe
        }
        var bytes = [UInt8](repeating: 0, count: Int(argMax))
        size = Int(argMax)
        mib = [CTL_KERN, KERN_PROCARGS2, pid]
        let result = bytes.withUnsafeMutableBytes { sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }
        guard result == 0, size > MemoryLayout<Int32>.size else { throw ExistingServerError.unsafe }
        let argc = bytes.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }
        guard argc > 0, argc < 4096 else { throw ExistingServerError.unsafe }
        var cursor = MemoryLayout<Int32>.size
        // KERN_PROCARGS2 contains argc, executable path, padding, then argv.
        while cursor < size && bytes[cursor] != 0 { cursor += 1 }
        while cursor < size && bytes[cursor] == 0 { cursor += 1 }
        var arguments = [String]()
        for _ in 0..<argc {
            let start = cursor
            while cursor < size && bytes[cursor] != 0 { cursor += 1 }
            guard cursor < size, let value = String(bytes: bytes[start..<cursor], encoding: .utf8) else {
                throw ExistingServerError.unsafe
            }
            arguments.append(value); cursor += 1
        }
        return HostProcessIdentity(pid: pid, uid: info.kp_eproc.e_ucred.cr_uid,
            startSeconds: Int(info.kp_proc.p_starttime.tv_sec),
            startMicroseconds: Int(info.kp_proc.p_starttime.tv_usec), arguments: arguments)
    }

    func matches(root: URL) -> Bool {
        let args = Array(arguments.dropFirst())
        // Match Python's module invocation, never a substring of an unrelated command.
        var index = 0
        while index < args.count && ["-B", "-S"].contains(args[index]) { index += 1 }
        guard index + 4 <= args.count, Array(args[index..<(index + 3)]) ==
            ["-m", "generator_launcher.host", "--root"],
            args[index + 3].hasPrefix("/"),
            ExistingServer.canonicalRoot(URL(fileURLWithPath: args[index + 3])).path ==
                ExistingServer.canonicalRoot(root).path else { return false }
        index += 4
        var seen = Set<String>()
        while index < args.count {
            if ["--native-owned", "--start-setup"].contains(args[index]) {
                guard seen.insert(args[index]).inserted else { return false }
                index += 1; continue
            }
            guard index + 1 < args.count,
                  ["--manifest", "--correction", "--game-directory", "--audio-mode", "--parent-pid", "--initial-consents", "--startup-log"].contains(args[index]),
                  seen.insert(args[index]).inserted, !args[index + 1].isEmpty else { return false }
            // Earlier native hosts included this argument.
            if args[index] == "--audio-mode" {
                guard args[index + 1] == "native_experimental" else { return false }
            }
            if args[index] == "--parent-pid" {
                guard let parent = Int32(args[index + 1]), parent > 1 else { return false }
            }
            index += 2
        }
        guard seen.contains("--native-owned") == seen.contains("--parent-pid") else { return false }
        return true
    }

    static func hasLiveChildren(_ pid: pid_t) throws -> Bool {
        // Older hosts have no atomic stop gate and may have active subprocesses.
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL]
        for _ in 0..<3 {
            var size = 0
            guard sysctl(&mib, 3, nil, &size, nil, 0) == 0 else { throw ExistingServerError.unsafe }
            var processes = [kinfo_proc](repeating: kinfo_proc(), count: size / MemoryLayout<kinfo_proc>.stride + 32)
            size = processes.count * MemoryLayout<kinfo_proc>.stride
            let result = processes.withUnsafeMutableBytes { sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }
            if result != 0 {
                if errno == ENOMEM { continue }
                throw ExistingServerError.unsafe
            }
            return processes.prefix(size / MemoryLayout<kinfo_proc>.stride).contains {
                $0.kp_eproc.e_ppid == pid && $0.kp_proc.p_stat != SZOMB
            }
        }
        throw ExistingServerError.unsafe
    }
}

struct ExistingServer {
    let root: URL
    let lockURL: URL
    let pid: pid_t
    let token: String
    let baseURL: URL
    let version: String
    let lockData: Data
    let device: dev_t
    let inode: ino_t
    let identity: HostProcessIdentity

    static func canonicalRoot(_ url: URL) -> URL {
        // Foundation preserves aliases such as /tmp on macOS; Python Path.resolve
        // and the host's authenticated root use the actual filesystem path.
        if let path = realpath(url.path, nil) {
            defer { free(path) }
            return URL(fileURLWithPath: String(cString: path), isDirectory: true)
        }
        return url.standardizedFileURL.resolvingSymlinksInPath()
    }

    static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite, number.doubleValue == Double(number.int64Value),
              let result = Int(exactly: number.int64Value) else { return nil }
        return result
    }

    static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    static func endpoint(_ lock: [String: Any]) throws -> (pid_t, String, URL, String) {
        guard lock["profile"] as? String == "mi1-patch-launcher/1",
              let pid = integer(lock["pid"]), pid > 1, pid <= Int(Int32.max),
              let port = integer(lock["port"]), (1...65535).contains(port),
              let token = lock["token"] as? String, !token.isEmpty, token.count <= 256,
              token.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95 }),
              let text = lock["url"] as? String, var parts = URLComponents(string: text),
              parts.scheme == "http", ["127.0.0.1", "localhost"].contains(parts.host ?? ""),
              parts.port == port, parts.user == nil, parts.password == nil, parts.fragment == nil,
              parts.path == "/", parts.queryItems?.count == 1,
              parts.queryItems?.first?.name == "session", parts.queryItems?.first?.value == token else {
            throw ExistingServerError.unsafe
        }
        var version = "unbekannt"
        if let value = lock["launcher_version"] {
            guard let text = value as? String, !text.isEmpty else { throw ExistingServerError.unsafe }
            version = text
        }
        parts.query = nil
        guard let base = parts.url else { throw ExistingServerError.unsafe }
        return (pid_t(pid), token, base, version)
    }

    // A free lock is stale metadata. Never delete it or signal the PID it contains.
    static func lockedData(_ url: URL) throws -> (Data, dev_t, ino_t)? {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW)
        if fd < 0 {
            if errno == ENOENT { return nil }
            throw ExistingServerError.unsafe
        }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == geteuid() else { throw ExistingServerError.unsafe }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 { flock(fd, LOCK_UN); return nil }
        guard errno == EWOULDBLOCK else { throw ExistingServerError.unsafe }
        var bytes = [UInt8](repeating: 0, count: 16_385)
        let count = read(fd, &bytes, bytes.count)
        guard count > 0, count <= 16_384 else { throw ExistingServerError.unsafe }
        return (Data(bytes.prefix(count)), info.st_dev, info.st_ino)
    }

    static func discover(root selected: URL) throws -> ExistingServer? {
        let root = canonicalRoot(selected)
        let lockURL = root.appendingPathComponent("MI1 Generator/.launcher/host.lock")
        guard let (data, device, inode) = try lockedData(lockURL) else { return nil }
        guard let lock = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ExistingServerError.unsafe
        }
        let (pid, token, base, version) = try endpoint(lock)
        let identity = try HostProcessIdentity.read(pid)
        guard identity.matches(root: root) else { throw ExistingServerError.unsafe }
        return ExistingServer(root: root, lockURL: lockURL, pid: pid, token: token,
            baseURL: base, version: version, lockData: data, device: device, inode: inode, identity: identity)
    }

    func revalidate() throws {
        guard let (data, device, inode) = try Self.lockedData(lockURL),
              data == lockData, device == self.device, inode == self.inode,
              try HostProcessIdentity.read(pid) == identity else { throw ExistingServerError.unsafe }
    }

    struct Response { let status: Int; let body: [String: Any] }
    typealias Request = (String, Bool) throws -> Response

    func request(_ path: String, stop: Bool = false) throws -> Response {
        var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        parts.path = path
        var request = URLRequest(url: parts.url!)
        request.timeoutInterval = 3
        request.setValue(token, forHTTPHeaderField: "X-Launcher-Session")
        if stop {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = Data("{\"confirmed\":true}".utf8)
        }
        let semaphore = DispatchSemaphore(value: 0)
        let result = ResponseBox()
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if error == nil, let response = response as? HTTPURLResponse {
                let body = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
                result.value = Response(status: response.statusCode, body: body)
            }
            semaphore.signal()
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + 4) == .success, let value = result.value else {
            task.cancel(); throw ExistingServerError.unavailable
        }
        return value
    }

    private final class ResponseBox: @unchecked Sendable { var value: Response? }

    static func validateServerStatus(_ value: Response, pid: pid_t, root: URL) throws {
        guard value.status == 200, integer(value.body["pid"]) == Int(pid),
              value.body["root"] as? String == root.path,
              value.body["profile"] as? String == "mi1-patch-launcher/1",
              let version = value.body["launcher_version"] as? String, !version.isEmpty,
              let busy = boolean(value.body["busy"]) else { throw ExistingServerError.unsafe }
        if busy { throw ExistingServerError.busy }
    }

    static func inactive(_ response: Response, key: String = "status") throws {
        guard response.status == 200 else { throw ExistingServerError.unavailable }
        if response.body[key] as? String == "running" { throw ExistingServerError.busy }
        if let busy = boolean(response.body["busy"]) {
            if busy { throw ExistingServerError.busy }
            return
        }
        guard let state = response.body[key] as? String,
              ["idle", "completed", "failed", "interrupted", "stopped", "cancelled"].contains(state) else {
            if response.body[key] as? String == "running" { throw ExistingServerError.busy }
            throw ExistingServerError.unsafe
        }
    }

    static func legacyReady(_ request: Request, root: URL) throws {
        let status = try request("/api/status", false)
        guard status.status == 200, let project = status.body["project"] as? [String: Any],
              project["root"] as? String == root.path,
              let reviewAvailable = boolean(status.body["review_available"]) else { throw ExistingServerError.unsafe }
        try inactive(request("/api/render/status", false))
        let setup = try request("/api/setup/status", false)
        if setup.status != 404 {
            guard boolean(setup.body["busy"]) != nil else { throw ExistingServerError.unsafe }
            try inactive(setup)
        }
        let repair = try request("/api/repair/status", false)
        if repair.status != 404 {
            guard boolean(repair.body["busy"]) != nil else { throw ExistingServerError.unsafe }
            try inactive(repair)
        }
        let game = try request("/api/preparation/game-status", false)
        if game.status != 404 { try inactive(game) }
        if reviewAvailable {
            for path in ["/api/patch-review/status", "/api/patch-review/build-status"] {
                let value = try request(path, false)
                guard let job = value.body["job"] as? [String: Any] else { throw ExistingServerError.unsafe }
                try inactive(Response(status: value.status, body: job))
            }
        }
    }

    // Reuse is read-only and deliberately allows an active render job.
    // Match the selected project and current bundle before opening its session.
    func resumeURL(project: [String: Any], launcherVersion: String) throws -> URL {
        try revalidate()
        let status = try request("/api/server/status")
        guard status.status == 200, Self.integer(status.body["pid"]) == Int(pid),
              status.body["root"] as? String == root.path,
              status.body["profile"] as? String == "mi1-patch-launcher/1",
              version == launcherVersion,
              status.body["launcher_version"] as? String == launcherVersion,
              status.body["audio_mode"] as? String == "native_experimental",
              Self.boolean(status.body["stopping"]) == false else { throw ExistingServerError.unsafe }
        let selection = try request("/api/status")
        guard selection.status == 200,
              let active = selection.body["project"] as? [String: Any],
              NSDictionary(dictionary: active).isEqual(to: project) else { throw ExistingServerError.unsafe }
        try revalidate()
        var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "session", value: token)]
        parts.fragment = "render"
        guard let url = parts.url else { throw ExistingServerError.unsafe }
        return url
    }

    // Called only after explicit confirmation, on a background queue.
    func stopAndWait() throws {
        try revalidate()
        let status = try request("/api/server/status")
        if status.status == 404 {
            try Self.legacyReady({ try self.request($0, stop: $1) }, root: root)
            if try HostProcessIdentity.hasLiveChildren(pid) { throw ExistingServerError.busy }
            try revalidate()
            guard kill(pid, SIGTERM) == 0 else { throw ExistingServerError.unavailable }
        } else {
            try Self.validateServerStatus(status, pid: pid, root: root)
            try revalidate()
            let result = try request("/api/server/stop", stop: true)
            if result.status == 409 { throw ExistingServerError.busy }
            guard result.status == 202 else { throw ExistingServerError.unavailable }
        }
        let deadline = Date().addingTimeInterval(8)
        while Date() < deadline {
            let pidGone = kill(pid, 0) != 0 && errno == ESRCH
            let lockHeld = try Self.lockedData(lockURL) != nil
            if pidGone && !lockHeld { return }
            Thread.sleep(forTimeInterval: 0.1)
        }
        throw ExistingServerError.timeout
    }
}
