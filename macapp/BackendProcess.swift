import Foundation

enum BackendError: LocalizedError {
    case missingExecutable(String)
    case couldNotStart(String)

    var errorDescription: String? {
        switch self {
        case .missingExecutable(let path):
            return "找不到内置月面后端：\(path)"
        case .couldNotStart(let message):
            return "无法启动内置月面后端：\(message)"
        }
    }
}

/// Runs the bundled Python HTTP backend and watches its port file.
final class BackendProcess {
    private let dataDir: URL
    private let portFile: URL
    private let logURL: URL
    private let executableURL: URL
    private let process = Process()
    private let pipe = Pipe()
    private var timer: Timer?
    private var pending = ""
    private var tail = ""
    private var stopping = false
    private var ready = false

    var onStatus: ((String) -> Void)?
    var onReady: ((URL) -> Void)?
    var onExit: ((Int32, String) -> Void)?

    init(dataDir: URL, portFile: URL, logURL: URL) {
        self.dataDir = dataDir
        self.portFile = portFile
        self.logURL = logURL
        if let resources = Bundle.main.resourceURL {
            self.executableURL = resources.appendingPathComponent("backend/TsukikageBackend")
        } else {
            self.executableURL = URL(fileURLWithPath: "backend/TsukikageBackend")
        }
    }

    func start() throws {
        let fileManager = FileManager.default
        guard fileManager.isExecutableFile(atPath: executableURL.path) else {
            throw BackendError.missingExecutable(executableURL.path)
        }
        try? fileManager.createDirectory(at: portFile.deletingLastPathComponent(),
                                         withIntermediateDirectories: true)
        try? fileManager.createDirectory(at: logURL.deletingLastPathComponent(),
                                         withIntermediateDirectories: true)
        try? fileManager.removeItem(at: portFile)
        appendLog("\n=== \(Date()) starting backend ===\n")

        process.executableURL = executableURL
        process.arguments = [
            "--data-dir", dataDir.path,
            "--no-browser",
            "--port", "0",
            "--port-file", portFile.path,
        ]
        process.currentDirectoryURL = executableURL.deletingLastPathComponent()
        var environment = ProcessInfo.processInfo.environment
        environment["TSUKIKAGE_FORCE_CONSOLE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardOutput = pipe
        process.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            if let text = String(data: data, encoding: .utf8) {
                DispatchQueue.main.async {
                    self?.consume(text)
                }
            }
        }
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                self?.exited(code: finished.terminationStatus)
            }
        }

        do {
            try process.run()
        } catch {
            throw BackendError.couldNotStart(error.localizedDescription)
        }
        onStatus?("正在加载月面数据…")

        let timer = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.pollPort()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func pollPort() {
        guard !ready else { return }
        guard let text = try? String(contentsOf: portFile, encoding: .utf8) else { return }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme == "http" else { return }
        ready = true
        timer?.invalidate()
        timer = nil
        onStatus?("正在打开界面…")
        onReady?(url)
    }

    private func consume(_ text: String) {
        pending += text
        while let index = pending.firstIndex(of: "\n") {
            let line = String(pending[..<index])
            pending.removeSubrange(...index)
            handle(line: line)
        }
        if pending.count > 8000 {
            pending = String(pending.suffix(4000))
        }
    }

    private func handle(line: String) {
        let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { return }
        tail += text + "\n"
        if tail.count > 8000 {
            tail = String(tail.suffix(6000))
        }
        appendLog(text + "\n")
        let lowered = text.lowercased()
        if lowered.contains("opening terrain") {
            onStatus?("正在加载月面数据…")
        } else if lowered.contains("terrain ready") {
            onStatus?("月面数据就绪")
        } else if lowered.contains("kernels ready") {
            onStatus?("内核就绪")
        } else if text.contains("地址：") {
            onStatus?("正在打开界面…")
        }
    }

    private func appendLog(_ text: String) {
        let fileManager = FileManager.default
        if !fileManager.fileExists(atPath: logURL.path) {
            fileManager.createFile(atPath: logURL.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: logURL) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        if let data = text.data(using: .utf8) {
            try? handle.write(contentsOf: data)
        }
    }

    private func exited(code: Int32) {
        if stopping { return }
        timer?.invalidate()
        timer = nil
        onExit?(code, tail)
    }

    func stop() {
        stopping = true
        timer?.invalidate()
        timer = nil
        if process.isRunning {
            process.terminate()
            let pid = process.processIdentifier
            DispatchQueue.global().asyncAfter(deadline: .now() + 2.0) { [weak self] in
                if let self = self, self.process.isRunning {
                    kill(pid, SIGKILL)
                }
            }
        }
        try? FileManager.default.removeItem(at: portFile)
    }
}
