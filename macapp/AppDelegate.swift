import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private(set) var window: NSWindow!
    private var container: NSView!
    private var loading: LoadingView!
    private var webView: WKWebView?
    private var webController: WebController?
    private var backend: BackendProcess?
    private var dataDir: URL?
    private var quitting = false
    private var backendReady = false

    var appVersion: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "1.6.6"
    }

    private var supportDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let directory = base.appendingPathComponent("Tsukikage", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private var logURL: URL {
        let base = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first!
        let directory = base.appendingPathComponent("Logs/Tsukikage", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("app.log")
    }

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        makeWindow()
        guard let directory = resolveDataDirectory() else {
            NSApp.terminate(nil)
            return
        }
        dataDir = directory
        startBackend()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        stopBackend()
    }

    func windowWillClose(_ notification: Notification) {
        stopBackend()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            window.makeKeyAndOrderFront(nil)
        }
        return true
    }

    // MARK: - Window

    private func makeWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1440, height: 920)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered,
                          defer: false)
        window.title = "Tsukikage 月面模拟"
        window.minSize = NSSize(width: 1000, height: 680)
        window.delegate = self
        window.center()

        container = NSView(frame: window.contentLayoutRect)
        window.contentView = container

        loading = LoadingView(frame: container.bounds)
        loading.autoresizingMask = [.width, .height]
        container.addSubview(loading)

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Data folder

    private func resolveDataDirectory() -> URL? {
        let arguments = CommandLine.arguments
        if let index = arguments.firstIndex(of: "--data-dir"), index + 1 < arguments.count {
            let url = URL(fileURLWithPath: arguments[index + 1])
            if isValidDataFolder(url) {
                UserDefaults.standard.set(url.path, forKey: "dataDir")
                return url
            }
        }
        if let saved = UserDefaults.standard.string(forKey: "dataDir") {
            let url = URL(fileURLWithPath: saved)
            if isValidDataFolder(url) {
                return url
            }
        }
        return chooseDataDirectory(required: true)
    }

    private func isValidDataFolder(_ url: URL) -> Bool {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory),
              isDirectory.boolValue else { return false }
        let contents = (try? FileManager.default.contentsOfDirectory(
            at: url, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
        )) ?? []
        return contents.contains { item in
            let ext = item.pathExtension.lowercased()
            return ext == "tif" || ext == "tiff"
        }
    }

    private func chooseDataDirectory(required: Bool) -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "选择月面数据目录（至少包含一个全球 2:1 高程 .tif）"
        panel.prompt = "选择"
        if let dataDir = dataDir {
            panel.directoryURL = dataDir
        }
        while true {
            if panel.runModal() != .OK {
                if required {
                    showAlert(title: "需要月面数据目录",
                              message: "程序需要至少一个全球 2:1 高程 .tif 才能渲染月面。")
                }
                return nil
            }
            guard let url = panel.url else { continue }
            if isValidDataFolder(url) {
                UserDefaults.standard.set(url.path, forKey: "dataDir")
                return url
            }
            showAlert(title: "目录里没有 .tif",
                      message: "请选择包含 ldem_*.tif、SLDEM2015_512.tif 或 Lunar_LRO_LOLA_*.tif 的目录。")
        }
    }

    // MARK: - Backend

    private func startBackend() {
        stopBackend()
        guard let directory = dataDir else { return }
        backendReady = false
        loading.isHidden = false
        loading.setStatus("正在启动月面服务…")
        if let webView = webView {
            webView.removeFromSuperview()
            self.webView = nil
        }
        webController = nil

        let portFile = supportDirectory.appendingPathComponent("session-port.txt")
        try? FileManager.default.removeItem(at: portFile)
        let process = BackendProcess(dataDir: directory, portFile: portFile, logURL: logURL)
        process.onStatus = { [weak self] text in
            self?.loading?.setStatus(text)
        }
        process.onReady = { [weak self] url in
            self?.backendReady = true
            self?.loadWeb(url)
        }
        process.onExit = { [weak self] code, tail in
            guard let self = self, !self.quitting, self.backend != nil else { return }
            self.backend = nil
            self.backendExited(code: code, tail: tail)
        }
        backend = process
        do {
            try process.start()
        } catch {
            self.backend = nil
            loading.setStatus("启动失败")
            showAlert(title: "启动失败", message: error.localizedDescription)
            return
        }
    }

    private func stopBackend() {
        backend?.stop()
        backend = nil
        backendReady = false
    }

    private func loadWeb(_ url: URL) {
        loading.isHidden = false
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")

        let controller = WebController()
        controller.app = self
        let web = WKWebView(frame: container.bounds, configuration: configuration)
        web.autoresizingMask = [.width, .height]
        web.allowsMagnification = true
        web.underPageBackgroundColor = NSColor(calibratedWhite: 0.028, alpha: 1)
        web.navigationDelegate = controller
        web.uiDelegate = controller
        controller.webView = web
        container.addSubview(web)
        container.addSubview(loading)
        let zoom = UserDefaults.standard.double(forKey: "pageZoom")
        if zoom > 0 {
            web.pageZoom = zoom
        }
        web.load(URLRequest(url: url))
        webView = web
        webController = controller
        loading.setStatus("正在打开界面…")
    }

    func webDidFinish() {
        loading.isHidden = true
        window.title = "Tsukikage 月面模拟"
        window.subtitle = ""
    }

    func webDidFail(_ error: Error) {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled {
            return
        }
        loading.isHidden = false
        loading.setStatus("界面加载失败：\(error.localizedDescription)")
        showAlert(title: "界面加载失败", message: error.localizedDescription)
    }

    func webProcessTerminated() {
        loading.isHidden = false
        loading.setStatus("界面进程已停止")
        showAlert(title: "界面进程已停止", message: "可以重新加载页面。")
        webView?.reload()
    }

    private func backendExited(code: Int32, tail: String) {
        loading.isHidden = false
        loading.setStatus("月面服务已停止（代码 \(code)）")
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "月面服务已停止"
        alert.informativeText = "后端退出代码：\(code)\n\n" + String(tail.suffix(1200))
        alert.addButton(withTitle: "选择数据目录")
        alert.addButton(withTitle: "重新启动")
        alert.addButton(withTitle: "退出")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            if let directory = chooseDataDirectory(required: false) {
                dataDir = directory
                startBackend()
            }
        case .alertSecondButtonReturn:
            startBackend()
        default:
            NSApp.terminate(nil)
        }
    }

    // MARK: - Downloads / status

    func downloadFinished(_ url: URL) {
        window.subtitle = "已保存 \(url.lastPathComponent)"
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func downloadFailed(_ error: Error) {
        showAlert(title: "下载失败", message: error.localizedDescription)
    }

    // MARK: - Actions

    @objc private func reloadPage() {
        webView?.reload()
    }

    @objc private func chooseDataFolder() {
        guard let directory = chooseDataDirectory(required: true) else { return }
        dataDir = directory
        startBackend()
    }

    @objc private func openDownloadsFolder() {
        let directory = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(directory)
    }

    @objc private func openLog() {
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        NSWorkspace.shared.open(logURL)
    }

    @objc private func openDataFolder() {
        if let dataDir = dataDir {
            NSWorkspace.shared.open(dataDir)
        }
    }

    @objc private func zoomIn() {
        guard let webView = webView else { return }
        let value = min(2.0, webView.pageZoom + 0.1)
        webView.pageZoom = value
        UserDefaults.standard.set(value, forKey: "pageZoom")
    }

    @objc private func zoomOut() {
        guard let webView = webView else { return }
        let value = max(0.5, webView.pageZoom - 0.1)
        webView.pageZoom = value
        UserDefaults.standard.set(value, forKey: "pageZoom")
    }

    @objc private func actualSize() {
        webView?.pageZoom = 1.0
        UserDefaults.standard.set(1.0, forKey: "pageZoom")
    }

    @objc private func toggleFullScreen() {
        window.toggleFullScreen(nil)
    }

    @objc private func openGitHub() {
        if let url = URL(string: "https://github.com/10deg/tsukikage") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func showAbout() {
        let credits = NSAttributedString(
            string: "月面模拟 · 本地计算\nmoonlib \(appVersion)\n数据与设置：~/Library/Application Support/Tsukikage",
            attributes: [.font: NSFont.systemFont(ofSize: 11)]
        )
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Tsukikage",
            .applicationVersion: appVersion,
            .version: appVersion,
            .credits: credits,
        ])
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.runModal()
    }

    // MARK: - Menu

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem(title: "Tsukikage", action: nil, keyEquivalent: "")
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        let about = NSMenuItem(title: "关于 Tsukikage", action: #selector(showAbout), keyEquivalent: "")
        about.target = self
        appMenu.addItem(about)
        appMenu.addItem(.separator())
        let settings = NSMenuItem(title: "设置…", action: #selector(chooseDataFolder), keyEquivalent: ",")
        settings.target = self
        appMenu.addItem(settings)
        appMenu.addItem(.separator())
        let quit = NSMenuItem(title: "退出 Tsukikage", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApp
        appMenu.addItem(quit)
        appItem.submenu = appMenu

        let fileItem = NSMenuItem()
        mainMenu.addItem(fileItem)
        let fileMenu = NSMenu(title: "文件")
        let reload = NSMenuItem(title: "重新加载", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
        fileMenu.addItem(reload)
        let downloads = NSMenuItem(title: "打开下载文件夹", action: #selector(openDownloadsFolder), keyEquivalent: "")
        downloads.target = self
        fileMenu.addItem(downloads)
        fileMenu.addItem(.separator())
        let close = NSMenuItem(title: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        close.target = nil
        fileMenu.addItem(close)
        fileItem.submenu = fileMenu

        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "视图")
        let zoomIn = NSMenuItem(title: "放大", action: #selector(zoomIn), keyEquivalent: "+")
        zoomIn.target = self
        viewMenu.addItem(zoomIn)
        let zoomOut = NSMenuItem(title: "缩小", action: #selector(zoomOut), keyEquivalent: "-")
        zoomOut.target = self
        viewMenu.addItem(zoomOut)
        let actual = NSMenuItem(title: "实际大小", action: #selector(actualSize), keyEquivalent: "0")
        actual.target = self
        viewMenu.addItem(actual)
        viewMenu.addItem(.separator())
        let full = NSMenuItem(title: "全屏", action: #selector(toggleFullScreen), keyEquivalent: "f")
        full.keyEquivalentModifierMask = [.command, .control]
        full.target = self
        viewMenu.addItem(full)
        viewItem.submenu = viewMenu

        let helpItem = NSMenuItem()
        mainMenu.addItem(helpItem)
        let helpMenu = NSMenu(title: "帮助")
        let log = NSMenuItem(title: "打开日志", action: #selector(openLog), keyEquivalent: "")
        log.target = self
        helpMenu.addItem(log)
        let data = NSMenuItem(title: "打开月面数据目录", action: #selector(openDataFolder), keyEquivalent: "")
        data.target = self
        helpMenu.addItem(data)
        let github = NSMenuItem(title: "GitHub 仓库", action: #selector(openGitHub), keyEquivalent: "")
        github.target = self
        helpMenu.addItem(github)
        helpItem.submenu = helpMenu

        NSApp.mainMenu = mainMenu
    }
}
