import AppKit

// A native control window, not a second implementation of service orchestration.
final class TraceFangApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private let status = NSTextField(labelWithString: "正在启动项目服务…")
    private let detail = NSTextField(wrappingLabelWithString:
        "关闭这个窗口，将停止行情采集与项目服务。\n已保存的数据会保留；浏览器只是展示界面。")
    private var openButton: NSButton!
    private var quitButton: NSButton!
    private var worker: Process?
    private var input: Pipe?
    private var closing = false
    private var canQuit = false
    private var root: URL!
    private var pendingOutput = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let item = NSMenuItem()
        menu.addItem(item)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "停止服务并退出 TraceFang",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let closeItem = appMenu.addItem(withTitle: "关闭窗口并停止服务",
                        action: #selector(requestClose), keyEquivalent: "w")
        closeItem.target = self
        item.submenu = appMenu
        NSApp.mainMenu = menu

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 490, height: 270),
            styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "TraceFang"
        window.delegate = self
        window.isReleasedWhenClosed = false
        let title = NSTextField(labelWithString: "TraceFang")
        title.font = .systemFont(ofSize: 29, weight: .semibold)
        title.textColor = NSColor(srgbRed: 0.09, green: 0.17, blue: 0.30, alpha: 1)
        status.font = .monospacedSystemFont(ofSize: 14, weight: .medium)
        status.textColor = .secondaryLabelColor
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor
        openButton = NSButton(title: "打开行情界面", target: self, action: #selector(openInterface))
        openButton.bezelStyle = .rounded
        openButton.isEnabled = false
        quitButton = NSButton(title: "停止并退出", target: self, action: #selector(requestClose))
        quitButton.bezelStyle = .rounded
        let buttons = NSStackView(views: [openButton, quitButton])
        buttons.spacing = 12
        let content = NSStackView(views: [title, status, detail, buttons])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 20
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 32),
            content.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -32),
            content.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 28)
        ])
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        do {
            let config = Bundle.main.url(forResource: "project-root", withExtension: "txt")!
            let path = try String(contentsOf: config, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            root = URL(fileURLWithPath: path, isDirectory: true)
            try run(command: "session")
            DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
                guard let self = self, !self.closing, !self.openButton.isEnabled,
                      self.worker?.isRunning == true else { return }
                self.detail.stringValue = "启动尚未完成，请检查系统权限提示和 Docker。\n首次运行可能需要允许读取项目所在的文稿文件夹。"
            }
        } catch {
            canQuit = true
            status.stringValue = "无法打开项目运行环境"
            detail.stringValue = "请在代码目录重新构建应用入口，或检查项目是否被移动。"
            quitButton.title = "关闭"
        }
    }

    private func run(command: String) throws {
        let process = Process()
        process.executableURL = root.appendingPathComponent(".venv/bin/python")
        process.arguments = ["-u", "-m", "tracefang.service", command]
        process.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root.appendingPathComponent("src").path
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
            + (env["PATH"] ?? "")
        process.environment = env
        let output = Pipe()
        let stdinPipe = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.standardInput = stdinPipe
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.pendingOutput += text
                if self.pendingOutput.contains("TRACEFANG_APP_READY") {
                    self.ready()
                    self.pendingOutput = ""
                }
                if self.pendingOutput.count > 8192 {
                    self.pendingOutput = String(self.pendingOutput.suffix(1024))
                }
            }
        }
        process.terminationHandler = { [weak self] task in
            DispatchQueue.main.async { self?.finished(code: task.terminationStatus) }
        }
        try process.run()
        worker = process
        input = stdinPipe
        if command != "session" { stdinPipe.fileHandleForWriting.closeFile() }
    }

    private func ready() {
        guard !closing, worker?.isRunning == true else { return }
        status.stringValue = "●  项目服务运行中"
        detail.stringValue = "关闭这个窗口，将停止行情采集与项目服务。\n已保存的数据会保留；浏览器只是展示界面。"
        status.textColor = NSColor(srgbRed: 0.15, green: 0.45, blue: 0.32, alpha: 1)
        openButton.isEnabled = true
        if ProcessInfo.processInfo.environment["TRACEFANG_APP_NO_BROWSER"] != "1" {
            openInterface()
        }
    }

    @objc private func openInterface() {
        NSWorkspace.shared.open(URL(string: "http://127.0.0.1:8000")!)
    }

    @objc private func requestClose() {
        if canQuit { NSApp.terminate(nil); return }
        guard !closing else { return }
        closing = true
        status.stringValue = "正在停止服务，请稍候…"
        detail.stringValue = "正在停止行情采集与项目服务，已保存的数据会保留。\n确认停止后，这个窗口会自动关闭。"
        status.textColor = .secondaryLabelColor
        openButton.isEnabled = false
        quitButton.isEnabled = false
        if worker?.isRunning == true {
            input?.fileHandleForWriting.closeFile()
            input = nil
        } else {
            do { try run(command: "stop-app") }
            catch { finished(code: 1) }
        }
    }

    private func finished(code: Int32) {
        worker = nil
        input = nil
        if code == 3 {
            canQuit = true
            closing = false
            status.stringValue = "TraceFang 已在另一个窗口打开"
            detail.stringValue = "请使用已有的应用窗口。本窗口不会停止另一个窗口的服务。"
            openButton.isEnabled = false
            quitButton.isEnabled = true
            quitButton.title = "关闭此窗口"
            return
        }
        if closing && code == 0 {
            canQuit = true
            NSApp.terminate(nil)
        } else {
            closing = false
            status.stringValue = "需要处理：服务未能完成启动或停止"
            status.textColor = .systemRed
            detail.stringValue = "请检查 Docker 和项目配置。停止失败时窗口会保留，\n不会把未确认停止的服务显示为已退出。"
            openButton.isEnabled = false
            quitButton.isEnabled = true
            quitButton.title = "重试停止并退出"
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        requestClose()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if canQuit { return .terminateNow }
        requestClose()
        return .terminateCancel
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }
}

let application = NSApplication.shared
let delegate = TraceFangApp()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
