import Cocoa
import Combine
import SwiftUI

// ─── Models ───────────────────────────────────────────────────────────────
struct StatusPayload: Codable {
    var generated_at: String
    var account_count: Int
    var heartbeat: HeartbeatSummary?
    var headline: Headline?
    var accounts: [Account]
    // Last auto-swap event (M4 swap engine; absent until then).
    var last_swap: LastSwap?
}

/// Placeholder for the M4 swap engine's "account_swapped" export.
/// All fields optional so the row simply renders nothing until the
/// backend starts emitting `last_swap`.
struct LastSwap: Codable {
    var provider: String?
    var from: String?
    var to: String?
    var at: String?
    var at_epoch: Double?
}

struct HeartbeatSummary: Codable {
    var status: String
    var next: String?
    var accounts: Int?
    var failed: Int?
}

struct Account: Codable, Identifiable {
    var id: Int
    var provider: String
    var email: String?
    var label: String?
    var plan: String?
    var status: String
    var status_message: String?
    var token_expires: String?
    var token_expired: Bool?
    var primary_used_pct: Double?
    var primary_reset: String?
    var secondary_used_pct: Double?
    var secondary_reset: String?
    var credits_balance: Double?
    var banked_resets: Int?
    var rate_limit_remaining: String?
    var rate_limit_reset: String?
    var rate_limit_limit: String?
    var sku: String?
    var limited_user_quotas: String?
    var limited_user_reset_date: String?
    var plan_reset: String?
    var monthly_used: Double?
    var monthly_limit: Double?
    var monthly_used_pct: Double?
    var monthly_period_start: String?
    var monthly_period_end: String?
    var reset_credits: [ResetCredit]?
    var last_poll: String?
    var heartbeat_status: String?
    var heartbeat_last: String?
    var heartbeat_next: String?
    var heartbeat_message: String?
    // Claude subscription / window-status details
    var subscription_status: String?
    var billing_type: String?
    var rate_limit_tier: String?
    var extra_usage_enabled: Bool?
    var subscription_created: String?
    var member_since: String?
    var display_name: String?
    var org_name: String?
    var primary_status: String?
    var secondary_status: String?
    var binding_window: String?
    var overage_status: String?
    // Claude Fable model-scoped weekly window (separate weekly limit)
    var fable_used_pct: Double?
    var fable_reset: String?
    var fable_label: String?
    var fable_status: String?
    // Grok / Antigravity / Copilot / Devin subscription details
    var on_demand_cap: Int?
    var billing_period_start: String?
    var tier_id: String?
    var tier_description: String?
    var access_sku: String?
    var premium_entitlement: Int?
    var premium_overage: Int?
    var chat_unlimited: Bool?
    var completions_unlimited: Bool?
    var can_upgrade: Bool?
    var organizations: String?
    var credit_balance: Double?
    var plan_start: String?
    var plan_price: String?
    var active_tier: String?
    var paid_since: String?
    var renews_at: String?
    var expires_at: String?
    var account_created: String?
    var subscription_plan: String?
    var has_active_subscription: Bool?
    var is_active_subscription_gratis: Bool?
    var has_previously_paid_subscription: Bool?
    var payment_history: String?
    var billing_note: String?
    var github_email: String?
    var github_name: String?
    var tier_override: String?
    var heartbeat_last_success: String?
    var usage_windows: [UsageWindow]?
    var windows: [WindowInfo]?
    var live: LiveActivity?
    // Per-poll lifecycle classification (backend account_state, spec §1.2).
    // Optional so older status.json files still decode.
    var state: AccountState?
}

/// Exact per-account lifecycle state exported by the backend on every poll.
/// Every field is optional-tolerant: decoding must not break on payloads
/// written before this field existed.
struct AccountState: Codable {
    var auth: String?           // "ok" | "token_expired" | "error"
    var subscription: String?   // "paid" | "free" | "expired" | "renews_soon" | "unknown"
    var sub_renews_at: String?
    var sub_expires_at: String?
    var quota: String?          // "ok" | "warning" | "exhausted" | "unknown"
    var usable: Bool?
    var binding_window: WindowInfo?
}

struct ResetCredit: Codable {
    var title: String?
    var status: String?
    var expires_at: String?
    var granted_at: String?
    var description: String?
}

struct UsageWindow: Codable {
    var group: String?
    var window: String?
    var used_pct: Double?
    var reset: String?
}

struct WindowInfo: Codable {
    var kind: String
    var label: String?
    var used_pct: Double?
    var reset_at_epoch: Double?
    var severity: String?
    var is_active: Bool?
    var source: String?
    var as_of_epoch: Double?
    var projected_exhaust_epoch: Double?
    // Window phase fields (backend refresh_windows, spec §1.3). Optional so
    // older status.json files still decode.
    var phase: String?              // "live" | "reset"
    var used_pct_effective: Double?
    var stale: Bool?
}

struct Headline: Codable {
    var account_id: Int
    var provider: String
    var email: String?
    var kind: String
    var label: String?
    var used_pct: Double
    var reset_at_epoch: Double?
    var severity: String
}

struct LiveActivity: Codable {
    var provider: String?
    var event_epoch: Double?
    var last_total_tokens: Int?
    var last_cached_tokens: Int?
    var last_output_tokens: Int?
    var context_used_pct: Double?
    var tokens_60m: Int?
    var as_of_epoch: Double?
}

// ─── Status Loader ────────────────────────────────────────────────────────
class StatusLoader: ObservableObject {
    @Published var payload: StatusPayload?
    @Published var lastError: String?
    private var timer: Timer?
    // Serial queue for file I/O + JSON decode, off the main run loop.
    private let ioQueue = DispatchQueue(label: "com.tonye.tokenstatusbar.status-io")

    private let poolDir: String
    private let dataDir: String
    let statusURL: URL
    // Persistent local control server (opencodex-style): the app launches
    // `pool.py server` once and drives all add/reconnect/manage flows through
    // the browser panel it serves on this loopback port.
    private var serverProcess: Process?
    let serverPort: Int

    init() {
        let env = ProcessInfo.processInfo.environment
        let poolDir: String
        if let override = env["AGENT_POOL_DIR"], !override.isEmpty {
            poolDir = override
        } else {
            poolDir = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("solo/token-status-bar").path
        }
        self.poolDir = poolDir
        let dataDir = "\(poolDir)/secrets"
        self.dataDir = dataDir
        if let override = env["AGENT_POOL_SERVER_PORT"], let p = Int(override) {
            self.serverPort = p
        } else {
            self.serverPort = 7817
        }
        if let override = env["AGENT_POOL_STATUS_JSON"], !override.isEmpty {
            self.statusURL = URL(fileURLWithPath: override)
        } else {
            self.statusURL = URL(fileURLWithPath: dataDir).appendingPathComponent("status.json")
        }
    }

    func start() {
        reload()
        ensureServer()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.reload()
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
        }
        serverProcess = nil
    }

    /// Start the local control server if it is not already running. The server
    /// binds 127.0.0.1:serverPort and serves the accounts panel + /api/oauth/*.
    /// Launch failure is non-fatal: the menu bar still reads status.json.
    func ensureServer() {
        if let proc = serverProcess, proc.isRunning { return }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let proc = self.poolProcess(["server"])
            proc.terminationHandler = { [weak self] _ in
                self?.serverProcess = nil
            }
            do {
                try proc.run()
                self.serverProcess = proc
            } catch {
                NSLog("token-bar server failed to launch: \(error.localizedDescription)")
            }
        }
    }

    /// URL of the browser accounts panel.
    var panelURL: URL { URL(string: "http://127.0.0.1:\(serverPort)/")! }

    /// Ensure the server is up, then open the browser accounts panel. All
    /// add / reconnect / swap / remove happens there (opencodex parity).
    func openPanel() {
        ensureServer()
        let url = panelURL
        // Small delay on cold start so the first open lands after the bind.
        let delay: DispatchTimeInterval = serverProcess?.isRunning == true ? .milliseconds(0) : .milliseconds(600)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            NSWorkspace.shared.open(url)
        }
    }

    func reload() {
        ioQueue.async { [weak self] in
            guard let self else { return }
            guard FileManager.default.fileExists(atPath: self.statusURL.path) else {
                DispatchQueue.main.async {
                    self.lastError = "No status.json yet. Run: pool.py poll"
                }
                return
            }
            do {
                let data = try Data(contentsOf: self.statusURL)
                let decoded = try JSONDecoder().decode(StatusPayload.self, from: data)
                DispatchQueue.main.async {
                    self.payload = decoded
                    self.lastError = nil
                }
            } catch {
                // Keep the cached payload; surface the failure via statusWarning.
                DispatchQueue.main.async {
                    self.lastError = "Parse error: \(error.localizedDescription)"
                }
            }
        }
    }

    /// Parse a status.json timestamp (ISO8601, with or without timezone suffix).
    static func parseStatusDate(_ iso: String) -> Date? {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = parser.date(from: iso) { return d }
        parser.formatOptions = [.withInternetDateTime]
        if let d = parser.date(from: iso) { return d }
        // generated_at has no timezone suffix; parse as local time.
        let local = DateFormatter()
        local.locale = Locale(identifier: "en_US_POSIX")
        local.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        if let d = local.date(from: iso) { return d }
        local.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return local.date(from: iso)
    }

    /// Warning surfaced in the menu (and status-item title) when the last
    /// reload failed or the cached payload is older than 15 minutes.
    var statusWarning: String? {
        if let err = lastError { return err }
        guard let payload,
              let date = StatusLoader.parseStatusDate(payload.generated_at) else { return nil }
        let age = Date().timeIntervalSince(date)
        if age > 15 * 60 {
            return "status.json is stale (updated \(Int(age / 60))m ago)"
        }
        return nil
    }

    // ─── Bundled Python / backend paths ────────────────────────────────
    // The .app bundles a standalone Python and the backend scripts under
    // Contents/Resources so the app works without a system python3. In dev
    // mode (running the binary directly from build/), fall back to system
    // python3 and the repo's backend/ dir.

    private var bundledPython: String? {
        let p = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/python/bin/python3").path
        return FileManager.default.fileExists(atPath: p) ? p : nil
    }

    private var bundledPoolPy: String? {
        let p = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/backend/pool.py").path
        return FileManager.default.fileExists(atPath: p) ? p : nil
    }

    private var devPoolPy: String {
        "\(poolDir)/backend/pool.py"
    }

    /// Quote a string for safe use as a single word in a POSIX shell command.
    static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// Process configured to run `pool.py <args>` with data-path env vars set.
    func poolProcess(_ args: [String]) -> Process {
        var env = ProcessInfo.processInfo.environment
        if env["AGENT_POOL_DB"] == nil { env["AGENT_POOL_DB"] = "\(dataDir)/pool.db" }
        if env["AGENT_POOL_STATUS_JSON"] == nil { env["AGENT_POOL_STATUS_JSON"] = "\(dataDir)/status.json" }
        let p = Process()
        p.environment = env
        if let py = bundledPython, let pool = bundledPoolPy {
            p.launchPath = py
            p.arguments = [pool] + args
        } else {
            p.launchPath = "/usr/bin/env"
            p.arguments = ["python3", devPoolPy] + args
        }
        return p
    }

    /// Shell command string for running `pool.py <args>` (for Terminal-based flows).
    func poolShellCommand(_ args: [String]) -> String {
        let q = StatusLoader.shellQuote
        let envPrefix = "AGENT_POOL_DB=\(q("\(dataDir)/pool.db")) AGENT_POOL_STATUS_JSON=\(q("\(dataDir)/status.json"))"
        let quotedArgs = args.map(q).joined(separator: " ")
        if let py = bundledPython, let pool = bundledPoolPy {
            return "\(envPrefix) \(q(py)) \(q(pool)) \(quotedArgs)"
        } else {
            let dir = "\(poolDir)/backend"
            return "cd \(q(dir)) && \(envPrefix) python3 pool.py \(quotedArgs)"
        }
    }

    /// Run `pool.py <args>` synchronously, optionally writing `stdinText`
    /// (plus a newline) to the process's stdin. Returns true on exit code 0.
    /// Launch failures and non-zero exits are logged and surfaced via
    /// lastError. Must not be called on the main thread.
    @discardableResult
    private func runPool(_ args: [String], stdinText: String? = nil) -> Bool {
        let task = poolProcess(args)
        var stdinPipe: Pipe?
        if stdinText != nil {
            let pipe = Pipe()
            task.standardInput = pipe
            stdinPipe = pipe
        }
        do {
            try task.run()
        } catch {
            NSLog("pool.py \(args.first ?? "?") failed to launch: \(error.localizedDescription)")
            DispatchQueue.main.async {
                self.lastError = "Failed to launch backend: \(error.localizedDescription)"
            }
            return false
        }
        if let pipe = stdinPipe, let stdinText {
            pipe.fileHandleForWriting.write(Data((stdinText + "\n").utf8))
            pipe.fileHandleForWriting.closeFile()
        }
        task.waitUntilExit()
        if task.terminationStatus != 0 {
            NSLog("pool.py \(args.first ?? "?") exited with status \(task.terminationStatus)")
            return false
        }
        return true
    }

    func runPoll() {
        DispatchQueue.global(qos: .userInitiated).async {
            self.runPool(["poll"])
            self.reload()
        }
    }

    func runHeartbeat(accountId: Int? = nil) {
        DispatchQueue.global(qos: .userInitiated).async {
            var args = ["heartbeat"]
            if let id = accountId {
                args += ["--account", "\(id)"]
            }
            self.runPool(args)
            // heartbeat writes refresh_log only; export so the menu sees it.
            self.runPool(["export-status"])
            self.reload()
        }
    }

    func runDashboard() {
        DispatchQueue.global(qos: .userInitiated).async {
            // Regenerates history/dashboard.html fresh; the backend's --open
            // flag then opens it in the default browser.
            self.runPool(["dashboard", "--open"])
        }
    }

    func addAgent(provider: String) {
        // Devin needs an API key: prompt in-app, then pass it via stdin so
        // it never appears in the process argument list.
        if provider == "devin" {
            guard let apiKey = promptDevinApiKey(confirmTitle: L10n.tr("add_new_agent")) else { return }
            DispatchQueue.global(qos: .userInitiated).async {
                self.runPool(["add-devin"], stdinText: apiKey)
                self.reload()
            }
            return
        }
        // Copilot uses the GitHub device flow (paste a code): keep the Terminal
        // path — the browser panel drives loopback-callback OAuth only.
        if provider == "copilot" {
            runPoolInTerminal(["add", provider])
            return
        }
        // Every other provider is browser OAuth: hand off to the accounts panel.
        openPanel()
    }

    /// Manual "Swap to this account" (spec §3.2): rewrites ~/.codex/auth.json
    /// with this pool account's tokens via `pool.py swap`. --force because a
    /// user click is explicit intent; the backend still backs up the old
    /// auth.json, writes atomically, records the event, and notifies.
    func swapToAgent(_ acct: Account) {
        DispatchQueue.global(qos: .userInitiated).async {
            self.runPool(["swap", "--provider", acct.provider,
                          "--account-id", "\(acct.id)", "--force"])
            self.reload()
        }
    }

    func reconnectAgent(_ acct: Account) {
        if acct.provider == "devin" {
            reconnectDevinAgent(acct)
            return
        }
        if acct.provider == "copilot" {
            runPoolInTerminal(["reconnect", "\(acct.id)"])
            return
        }
        // Browser OAuth providers reconnect through the accounts panel, which
        // deep-reauths the exact account via /api/oauth/login?account_id=…
        openPanel()
    }

    /// Modal secure-field prompt for a Devin API key. Returns nil on cancel/empty.
    private func promptDevinApiKey(confirmTitle: String) -> String? {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = L10n.tr("devin_api_key_title")
        alert.informativeText = L10n.tr("devin_api_key_message")
        alert.addButton(withTitle: confirmTitle)
        alert.addButton(withTitle: L10n.tr("cancel"))
        let input = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.placeholderString = "API key"
        alert.accessoryView = input
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        let apiKey = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        return apiKey.isEmpty ? nil : apiKey
    }

    private func reconnectDevinAgent(_ acct: Account) {
        guard let apiKey = promptDevinApiKey(confirmTitle: L10n.tr("reconnect_agent")) else { return }
        DispatchQueue.global(qos: .userInitiated).async {
            // Key goes over stdin, not argv, so it never shows up in `ps`.
            self.runPool(["reconnect", "\(acct.id)"], stdinText: apiKey)
            self.reload()
        }
    }

    private func runPoolInTerminal(_ args: [String]) {
        let cmd = poolShellCommand(args)
        // Escape for embedding in an AppleScript string literal.
        let escapedCmd = cmd
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = """
        tell application "Terminal"
            activate
            do script "\(escapedCmd)"
        end tell
        """
        let task = Process()
        task.launchPath = "/usr/bin/osascript"
        task.arguments = ["-e", script]
        do {
            try task.run()
        } catch {
            NSLog("osascript launch failed: \(error.localizedDescription)")
            DispatchQueue.main.async {
                self.lastError = "Failed to open Terminal: \(error.localizedDescription)"
            }
        }
    }

    func deleteAgent(accountId: Int) {
        DispatchQueue.global(qos: .userInitiated).async {
            self.runPool(["remove", "\(accountId)"])
            self.reload()
        }
    }

    func confirmDeleteAgent(acct: Account) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = L10n.tr("delete_agent")
        let name = acct.email ?? acct.label ?? "account #\(acct.id)"
        alert.informativeText = String(format: L10n.tr("delete_agent_confirm"), name)
        alert.addButton(withTitle: L10n.tr("delete_agent"))
        alert.addButton(withTitle: L10n.tr("cancel"))
        if alert.runModal() == .alertFirstButtonReturn {
            deleteAgent(accountId: acct.id)
        }
    }
}

// ─── Menu Row Design (ported from codex-status-bar) ───────────────────────
enum MenuRowLayout {
    static let width: CGFloat = 320
    static let standardHeight: CGFloat = 24
}

final class FixedMenuSeparatorView: NSView {
    init(width: CGFloat = MenuRowLayout.width) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 9))
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        NSColor.separatorColor.setStroke()
        let path = NSBezierPath()
        path.move(to: NSPoint(x: 12, y: bounds.midY))
        path.line(to: NSPoint(x: bounds.width - 12, y: bounds.midY))
        path.lineWidth = 1
        path.stroke()
    }
}

final class FixedMenuRowView: NSView {
    enum Style {
        case header
        case title
        case info
        case action
        case submenu
        case groupHeader
        case bullet
        case warning
    }

    private let style: Style
    private let action: (() -> Void)?
    private let submenu: NSMenu?
    private let dotColor: NSColor?
    private let hollowDot: Bool
    private let accentNumbers: Bool
    private let accentPercent: Bool
    private let warnPercent: Bool
    private let accentResetTime: Bool
    private let checkmark: Bool
    private let destructive: Bool
    private var rawTitle: String
    private let badgeText: String?
    private let label = NSTextField(labelWithString: "")
    private let chevron = NSTextField(labelWithString: "›")
    private let check = NSTextField(labelWithString: "✓")
    private let badgeLabel = NSTextField(labelWithString: "")
    private let dotLayer = CALayer()
    private let highlightLayer = CALayer()
    private var hovered = false

    init(title: String, style: Style, action: (() -> Void)? = nil, submenu: NSMenu? = nil,
         dotColor: NSColor? = nil, hollowDot: Bool = false,
         accentNumbers: Bool = false, accentPercent: Bool = false,
         warnPercent: Bool = false, accentResetTime: Bool = false,
         checkmark: Bool = false, badge: String? = nil, destructive: Bool = false,
         width: CGFloat = MenuRowLayout.width) {
        self.style = style
        self.action = action
        self.submenu = submenu
        self.dotColor = dotColor
        self.hollowDot = hollowDot
        self.accentNumbers = accentNumbers
        self.accentPercent = accentPercent
        self.warnPercent = warnPercent
        self.accentResetTime = accentResetTime
        self.checkmark = checkmark
        self.destructive = destructive
        self.rawTitle = title
        self.badgeText = badge
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: MenuRowLayout.standardHeight))

        wantsLayer = true
        highlightLayer.cornerRadius = 5
        highlightLayer.isHidden = true
        layer?.addSublayer(highlightLayer)

        if let dotColor {
            dotLayer.cornerRadius = 4
            if hollowDot {
                // Hollow dot: outline only (blocked accounts).
                dotLayer.backgroundColor = NSColor.clear.cgColor
                dotLayer.borderColor = dotColor.cgColor
                dotLayer.borderWidth = 1.5
            } else {
                dotLayer.backgroundColor = dotColor.cgColor
            }
            layer?.addSublayer(dotLayer)
        }

        switch style {
        case .header:
            label.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        case .title:
            label.font = NSFont.systemFont(ofSize: NSFont.menuFont(ofSize: 0).pointSize, weight: .semibold)
        case .groupHeader:
            label.font = NSFont.systemFont(ofSize: 11, weight: .bold)
        case .warning:
            label.font = NSFont.systemFont(ofSize: NSFont.menuFont(ofSize: 0).pointSize, weight: .medium)
        default:
            label.font = NSFont.menuFont(ofSize: 0)
        }
        label.textColor = baseLabelColor
        label.lineBreakMode = .byTruncatingTail
        applyTitle(highlighted: false)
        addSubview(label)

        chevron.font = NSFont.menuFont(ofSize: 0)
        chevron.textColor = .secondaryLabelColor
        chevron.alignment = .center
        chevron.isHidden = style != .submenu
        addSubview(chevron)

        check.font = NSFont.menuFont(ofSize: 0)
        check.textColor = .white
        check.alignment = .center
        check.isHidden = !(style == .action && checkmark)
        addSubview(check)

        if let badgeText {
            badgeLabel.stringValue = badgeText
            badgeLabel.font = NSFont.systemFont(ofSize: 10, weight: .medium)
            badgeLabel.textColor = .systemOrange
            badgeLabel.alignment = .right
            addSubview(badgeLabel)
        }
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    /// Replace the row text in place (used by the 1s menu ticker so reset
    /// countdowns keep ticking while the menu stays open).
    func updateTitle(_ title: String) {
        guard title != rawTitle else { return }
        rawTitle = title
        applyTitle(highlighted: hovered && (style == .action || style == .submenu))
    }

    private func applyTitle(highlighted: Bool) {
        let base = highlighted ? NSColor.white : baseLabelColor
        if style == .bullet {
            let attr = NSMutableAttributedString(
                string: rawTitle, attributes: [.font: label.font as Any, .foregroundColor: base])
            // Shrink only the leading bullet glyph; keep text at normal size.
            if !rawTitle.isEmpty {
                attr.addAttribute(.font, value: NSFont.systemFont(ofSize: 7),
                                  range: NSRange(location: 0, length: 1))
            }
            label.attributedStringValue = attr
            return
        }
        if accentPercent, !highlighted {
            let attr = NSMutableAttributedString(
                string: rawTitle, attributes: [.font: label.font as Any, .foregroundColor: base])
            if let r = rawTitle.range(of: "[0-9]+(\\.[0-9]+)?%", options: .regularExpression) {
                attr.addAttribute(.foregroundColor, value: warnPercent ? NSColor.systemOrange : NSColor.systemGreen,
                                  range: NSRange(r, in: rawTitle))
            }
            if accentResetTime,
               let r = rawTitle.range(of: "\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}", options: .regularExpression) {
                attr.addAttribute(.foregroundColor, value: NSColor.systemOrange,
                                  range: NSRange(r, in: rawTitle))
            }
            label.attributedStringValue = attr
            return
        }
        guard accentNumbers, !highlighted else {
            label.attributedStringValue = NSAttributedString(
                string: rawTitle, attributes: [.font: label.font as Any, .foregroundColor: base])
            return
        }
        let attr = NSMutableAttributedString(
            string: rawTitle, attributes: [.font: label.font as Any, .foregroundColor: base])
        let ns = rawTitle as NSString
        ns.enumerateSubstrings(in: NSRange(location: 0, length: ns.length),
                               options: .byComposedCharacterSequences) { sub, range, _, _ in
            if let sub, sub.rangeOfCharacter(from: .decimalDigits) != nil {
                attr.addAttribute(.foregroundColor, value: NSColor.systemGreen, range: range)
            }
        }
        label.attributedStringValue = attr
    }

    override func layout() {
        super.layout()
        highlightLayer.frame = bounds.insetBy(dx: 6, dy: 2)
        let showCheck = style == .action && checkmark
        let labelX: CGFloat = dotColor == nil ? (showCheck ? 30 : 14) : 28
        dotLayer.frame = NSRect(x: 14, y: bounds.midY - 4, width: 8, height: 8)
        check.frame = NSRect(x: 12, y: 4, width: 16, height: 16)
        // Reserve space on the right for badge (when present) + chevron.
        let badgeW: CGFloat = badgeText == nil ? 0 : 80
        let rightInset: CGFloat = (style == .submenu ? 28 : labelX) + badgeW
        label.frame = NSRect(x: labelX, y: 4, width: bounds.width - labelX - rightInset, height: 16)
        if badgeText != nil {
            let badgeHeight = ceil(badgeLabel.intrinsicContentSize.height)
            badgeLabel.frame = NSRect(
                x: bounds.width - 25 - badgeW,
                y: floor((bounds.height - badgeHeight) / 2),
                width: badgeW - 4,
                height: badgeHeight
            )
        }
        chevron.frame = NSRect(x: bounds.width - 25, y: 4, width: 13, height: 16)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self
        ))
    }

    override func mouseEntered(with event: NSEvent) {
        hovered = true
        applyHighlight()
    }

    override func mouseExited(with event: NSEvent) {
        hovered = false
        applyHighlight()
    }

    private func applyHighlight() {
        let highlighted = hovered && (style == .action || style == .submenu)
        highlightLayer.isHidden = !highlighted
        highlightLayer.backgroundColor = NSColor.controlAccentColor.withAlphaComponent(0.92).cgColor
        applyTitle(highlighted: highlighted)
        chevron.textColor = highlighted ? .white : .secondaryLabelColor
        check.textColor = .white
    }

    private var baseLabelColor: NSColor {
        if destructive { return .systemRed }
        switch style {
        case .header, .info, .bullet: return .secondaryLabelColor
        case .warning: return .systemOrange
        case .title, .action, .submenu, .groupHeader: return .labelColor
        }
    }

    override func mouseDown(with event: NSEvent) {
        switch style {
        case .action:
            enclosingMenuItem?.menu?.cancelTracking()
            action?()
        case .submenu:
            guard let submenu else { return }
            submenu.popUp(positioning: nil, at: NSPoint(x: bounds.maxX - 4, y: bounds.maxY - 2), in: self)
        case .header, .title, .info, .groupHeader, .bullet, .warning:
            break
        }
    }
}

/// One thin usage gauge: [label | bar | pct · time-left].
final class GaugeRowView: NSView {
    private let info: WindowInfo
    private let color: NSColor

    init(window: WindowInfo, color: NSColor, width: CGFloat) {
        self.info = window
        self.color = color
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 16))
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    private func kindLabel() -> String {
        if info.kind == "model_weekly" { return info.label ?? "model" }
        if let label = info.label, info.kind == "monthly" { return label }
        return info.kind
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let pct = max(0, min(100, info.used_pct ?? 0))
        let labelX: CGFloat = 28
        let labelW: CGFloat = 62
        let rightW: CGFloat = 96
        let barX = labelX + labelW + 6
        let barW = bounds.width - barX - rightW - 16
        let attrsLabel: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 10.5),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        (kindLabel() as NSString).draw(
            at: NSPoint(x: labelX, y: 2), withAttributes: attrsLabel)

        let track = NSRect(x: barX, y: 5.5, width: barW, height: 5)
        NSColor.quaternaryLabelColor.setFill()
        NSBezierPath(roundedRect: track, xRadius: 2.5, yRadius: 2.5).fill()
        if pct > 0 {
            let fill = NSRect(x: barX, y: 5.5, width: barW * pct / 100.0, height: 5)
            color.setFill()
            NSBezierPath(roundedRect: fill, xRadius: 2.5, yRadius: 2.5).fill()
        }

        var right = "\(Int(pct.rounded()))%"
        if let left = AppDelegate.timeLeft(info.reset_at_epoch) { right += " · \(left)" }
        let attrsRight: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        let size = (right as NSString).size(withAttributes: attrsRight)
        (right as NSString).draw(
            at: NSPoint(x: bounds.width - 16 - size.width, y: 2),
            withAttributes: attrsRight)
    }
}

/// Account row + its gauge bars stacked into one menu-item view.
final class AccountRowWithGauges: NSView {
    /// The title row, exposed so the 1s menu ticker can update countdowns.
    let rowView: FixedMenuRowView

    init(row: FixedMenuRowView, gauges: [GaugeRowView], width: CGFloat) {
        self.rowView = row
        let gaugeH: CGFloat = 16
        let pad: CGFloat = gauges.isEmpty ? 0 : 4
        let height = MenuRowLayout.standardHeight + CGFloat(gauges.count) * gaugeH + pad
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        row.setFrameOrigin(NSPoint(x: 0, y: height - MenuRowLayout.standardHeight))
        addSubview(row)
        for (i, g) in gauges.enumerated() {
            g.setFrameOrigin(NSPoint(x: 0, y: CGFloat(gauges.count - 1 - i) * gaugeH + 2))
            g.setFrameSize(NSSize(width: width, height: gaugeH))
            addSubview(g)
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
}

// ─── Language Mode ─────────────────────────────────────────────────────────
enum Language: String, CaseIterable {
    case en, ko, zh, ja

    var nativeName: String {
        switch self {
        case .en: return "English"
        case .ko: return "한국어"
        case .zh: return "中文"
        case .ja: return "日本語"
        }
    }

    static let storageKey = "TSBLanguage"
    static var current: Language {
        get {
            let raw = UserDefaults.standard.string(forKey: storageKey) ?? "en"
            return Language(rawValue: raw) ?? .en
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: storageKey) }
    }
}

enum L10n {
    static var lang: Language = .en

    // Single source of truth for every translated string.
    // Keys are stable identifiers; values are per-language.
    static let table: [Language: [String: String]] = [
        .en: [
            // ── Footer ──
            "poll_now": "Poll Now",
            "open_dashboard": "Open Dashboard",
            "refresh_display": "Refresh Display",
            "add_new_agent": "Add New Agent",
            "manage_accounts": "Manage Accounts…",
            "language": "Language",
            "quit": "Quit",
            "heartbeat": "Heartbeat",
            "heartbeat_next": "Next",
            "heartbeat_last": "Last",
            "heartbeat_last_success": "Last success",
            "run_heartbeat_now": "Run Heartbeat Now",
            "heartbeat_success": "Success",
            "heartbeat_fail": "Fail",
            "heartbeat_unknown": "Unknown",
            "reconnect_agent": "Reconnect This Account",
            "swap_to_agent": "Swap to This Account",
            "devin_api_key_title": "Reconnect Devin",
            "devin_api_key_message": "Enter the API key for this Devin account.",
            "delete_agent": "Delete This Agent",
            "delete_agent_confirm": "Remove %@ from the agent pool? This cannot be undone.",
            "cancel": "Cancel",
            // ── Group headers ──
            "status": "Status",
            "limit_session": "Limit session",
            "resets": "Resets",
            "subscription": "Subscription",
            // ── Labels ──
            "plan": "Plan",
            "token_expires": "Token expires",
            "last_poll": "Last poll",
            "additional_credits": "Additional credits",
            "banked_resets": "Banked resets",
            "binding_window": "Binding window",
            "rate_limit_tier": "Rate limit tier",
            "billing": "Billing",
            "extra_usage": "Extra usage",
            "subscribed": "Subscribed",
            "member_since": "Member since",
            "org": "Org",
            "account": "Account",
            "monthly_credits": "Monthly credits",
            "on_demand_cap": "On-demand cap",
            "billing_period": "Billing period",
            "tier": "Tier",
            "description": "Description",
            "active_session_tier": "Active session tier",
            "premium_entitlement": "Premium entitlement",
            "premium_overage": "Premium overage",
            "chat": "Chat",
            "completions": "Completions",
            "upgradeable": "Upgradeable",
            "credit_balance": "Credit balance",
            "plan_started": "Plan started",
            "plan_resets": "Plan resets",
            "plan_expires": "Plan expires",
            "remaining": "Remaining",
            "reset": "Reset",
            "limit": "Limit",
            "monthly": "Monthly",
            "sku": "SKU",
            "quota_limit": "Quota limit",
            "quota_reset": "Quota reset",
            "account_created": "Account created",
            "payment_history": "Previous payments",
            // ── Messages ──
            "no_details": "No details",
            "na": "n/a",
            "updated": "UPDATED",
            "loading": "Loading…",
            // ── Booleans ──
            "on": "on",
            "off": "off",
            "unlimited": "unlimited",
            "limited": "limited",
            "yes": "yes",
            "no": "no",
            // ── Limit names ──
            "5h_limit": "5h limit",
            "weekly_limit": "weekly limit",
            "fable_limit": "Fable limit",
            "fable_rate_limited": "rate-limited",
            "monthly_limit": "monthly limit",
            "daily_tokens": "daily tokens",
            "tier_usage": "tier usage",
            "ag_group_gemini": "Gemini models",
            "ag_group_other": "Claude & GPT models",
            "premium_requests": "premium requests",
            "chat_limit": "chat",
            "daily_limit": "daily limit",
            // ── Window labels (detailLines) ──
            "window_label": "window",
            "window_monthly": "monthly",
            "window_quota": "quota",
            "window_tier": "tier",
            "window_win": "win",
            "window_24h_tokens": "24h tokens",
            // ── Misc ──
            "used": "used",
            "credits": "credits",
            "expire_label": "expire",
            "issued_label": "issued",
            "coupon_reason_label": "Reason",
            "coupon_issued_label": "Issued",
            "coupon_expire_label": "Expire",
            "reset_coupons": "Reset coupons",
            "coupon_usage": "usage",
            "coupon_referral": "referral",
            "warn_weekly_closing": "resets in %dh — weekly limit closing",
            "weekly_short": "weekly",
            "finishes_soon": "End Soon",
            // ── Layout experiment (usable-first, spec §2) ──
            "layout": "Layout",
            "layout_classic": "Classic",
            "layout_usable_first": "Usable-first",
            "use_now_header": "USE NOW",
            "limited_header": "LIMITED / COOLING DOWN",
            "blocked_header": "BLOCKED",
            "other_header": "OTHER",
            "usable_count": "usable",
            "pct_left": "left",
            "resets_in": "resets in %@",
            "usable_now": "reset · usable now",
            "quota_exhausted": "quota exhausted",
            "token_expired_reconnect": "token expired · reconnect",
            "error_prefix": "error",
            "sub_expired_on": "sub expired %@",
            // ── Subscription line (account submenus) ──
            "sub_paid": "paid",
            "sub_free": "free",
            "sub_renews": "renews %@",
            "sub_renews_soon": "renews soon %@",
            "sub_expired_since": "expired since %@",
            "sub_expired": "expired",
        ],
        .ko: [
            // ── Footer ──
            "poll_now": "지금 업데이트",
            "open_dashboard": "대시보드 열기",
            "refresh_display": "표시 새로고침",
            "add_new_agent": "새 에이전트 추가",
            "manage_accounts": "계정 관리…",
            "language": "언어",
            "quit": "종료",
            "heartbeat": "Heartbeat",
            "heartbeat_next": "다음",
            "heartbeat_last": "마지막",
            "heartbeat_last_success": "마지막 성공",
            "run_heartbeat_now": "지금 하트비트 실행",
            "heartbeat_success": "성공",
            "heartbeat_fail": "실패",
            "heartbeat_unknown": "알 수 없음",
            "reconnect_agent": "계정 다시 연결하기",
            "swap_to_agent": "이 계정으로 전환하기",
            "devin_api_key_title": "Devin 다시 연결",
            "devin_api_key_message": "이 Devin 계정의 API 키를 입력하세요.",
            "delete_agent": "이 에이전트 삭제",
            "delete_agent_confirm": "%@ 를 에이전트 풀에서 삭제하시겠습니까? 되돌릴 수 없습니다.",
            "cancel": "취소",
            // ── Group headers ──
            "status": "상태",
            "limit_session": "제한 세션",
            "resets": "리셋",
            "subscription": "구독",
            // ── Labels ──
            "plan": "플랜",
            "token_expires": "토큰 만료",
            "last_poll": "마지막 업데이트",
            "additional_credits": "추가 크레딧",
            "banked_resets": "적립 리셋",
            "binding_window": "바인딩 윈도우",
            "rate_limit_tier": "레이트 리밋 티어",
            "billing": "청구",
            "extra_usage": "추가 사용",
            "subscribed": "구독 시작",
            "member_since": "가입일",
            "org": "조직",
            "account": "계정",
            "monthly_credits": "월간 크레딧",
            "on_demand_cap": "온디맨드 상한",
            "billing_period": "청구 기간",
            "tier": "티어",
            "description": "설명",
            "active_session_tier": "활성 세션 티어",
            "premium_entitlement": "프리미엄 권한",
            "premium_overage": "프리미엄 초과",
            "chat": "채팅",
            "completions": "컴플리션",
            "upgradeable": "업그레이드 가능",
            "credit_balance": "크레딧 잔액",
            "plan_started": "플랜 시작",
            "plan_resets": "플랜 리셋",
            "plan_expires": "플랜 만료",
            "remaining": "남음",
            "reset": "리셋",
            "limit": "한도",
            "monthly": "월간",
            "sku": "SKU",
            "quota_limit": "할당량 한도",
            "quota_reset": "할당량 리셋",
            "account_created": "계정 생성",
            "payment_history": "이전 결제",
            // ── Messages ──
            "no_details": "상세 정보 없음",
            "na": "정보 없음",
            "updated": "업데이트",
            "loading": "불러오는 중…",
            // ── Booleans ──
            "on": "켜짐",
            "off": "꺼짐",
            "unlimited": "무제한",
            "limited": "제한",
            "yes": "예",
            "no": "아니오",
            // ── Limit names ──
            "5h_limit": "5시간 제한",
            "weekly_limit": "주간 제한",
            "fable_limit": "Fable 제한",
            "fable_rate_limited": "제한 도달",
            "monthly_limit": "월간 제한",
            "daily_tokens": "일일 토큰",
            "tier_usage": "티어 사용량",
            "ag_group_gemini": "Gemini 모델",
            "ag_group_other": "Claude & GPT 모델",
            "premium_requests": "프리미엄 요청",
            "chat_limit": "채팅",
            "daily_limit": "일일 제한",
            // ── Window labels (detailLines) ──
            "window_label": "윈도우",
            "window_monthly": "월간",
            "window_quota": "할당량",
            "window_tier": "티어",
            "window_win": "윈도우",
            "window_24h_tokens": "24시간 토큰",
            // ── Misc ──
            "used": "사용",
            "credits": "크레딧",
            "expire_label": "만료",
            "issued_label": "발급",
            "coupon_reason_label": "사유",
            "coupon_issued_label": "발급",
            "coupon_expire_label": "만료",
            "reset_coupons": "리셋 쿠폰",
            "coupon_usage": "사용 보상",
            "coupon_referral": "추천",
            "warn_weekly_closing": "%d시간 후 리셋 — 주간 제한 마감",
            "weekly_short": "주간",
            "finishes_soon": "곧 종료",
            // ── Layout experiment (usable-first, spec §2) ──
            "layout": "레이아웃",
            "layout_classic": "클래식",
            "layout_usable_first": "사용 가능 우선",
            "use_now_header": "지금 사용",
            "limited_header": "제한 / 쿨다운",
            "blocked_header": "차단됨",
            "other_header": "기타",
            "usable_count": "사용 가능",
            "pct_left": "남음",
            "resets_in": "%@ 후 리셋",
            "usable_now": "리셋됨 · 사용 가능",
            "quota_exhausted": "할당량 소진",
            "token_expired_reconnect": "토큰 만료 · 다시 연결",
            "error_prefix": "오류",
            "sub_expired_on": "구독 만료 %@",
            // ── Subscription line (account submenus) ──
            "sub_paid": "결제 중",
            "sub_free": "무료",
            "sub_renews": "%@ 갱신",
            "sub_renews_soon": "곧 갱신 (%@)",
            "sub_expired_since": "%@부터 만료",
            "sub_expired": "만료됨",
        ],
        .zh: [
            // ── Footer ──
            "poll_now": "立即轮询",
            "open_dashboard": "打开仪表盘",
            "refresh_display": "刷新显示",
            "add_new_agent": "添加新代理",
            "manage_accounts": "管理账户…",
            "language": "语言",
            "quit": "退出",
            "heartbeat": "Heartbeat",
            "heartbeat_next": "下次",
            "heartbeat_last": "上次",
            "heartbeat_last_success": "上次成功",
            "run_heartbeat_now": "立即运行 Heartbeat",
            "heartbeat_success": "成功",
            "heartbeat_fail": "失败",
            "heartbeat_unknown": "未知",
            "reconnect_agent": "重新连接此账户",
            "swap_to_agent": "切换到此账户",
            "devin_api_key_title": "重新连接 Devin",
            "devin_api_key_message": "输入此 Devin 账户的 API 密钥。",
            "delete_agent": "删除此代理",
            "delete_agent_confirm": "从代理池中移除 %@？此操作无法撤销。",
            "cancel": "取消",
            // ── Group headers ──
            "status": "状态",
            "limit_session": "限制会话",
            "resets": "重置",
            "subscription": "订阅",
            // ── Labels ──
            "plan": "计划",
            "token_expires": "令牌过期",
            "last_poll": "最后轮询",
            "additional_credits": "额外积分",
            "banked_resets": "累积重置",
            "binding_window": "绑定窗口",
            "rate_limit_tier": "速率限制层级",
            "billing": "计费",
            "extra_usage": "额外使用",
            "subscribed": "订阅于",
            "member_since": "注册于",
            "org": "组织",
            "account": "账户",
            "monthly_credits": "每月积分",
            "on_demand_cap": "按需上限",
            "billing_period": "计费周期",
            "tier": "层级",
            "description": "描述",
            "active_session_tier": "当前会话层级",
            "premium_entitlement": "高级配额",
            "premium_overage": "高级超额",
            "chat": "聊天",
            "completions": "补全",
            "upgradeable": "可升级",
            "credit_balance": "积分余额",
            "plan_started": "计划开始",
            "plan_resets": "计划重置",
            "plan_expires": "计划到期",
            "remaining": "剩余",
            "reset": "重置",
            "limit": "限额",
            "monthly": "每月",
            "sku": "SKU",
            "quota_limit": "配额限制",
            "quota_reset": "配额重置",
            "account_created": "账户创建",
            "payment_history": "历史付款",
            // ── Messages ──
            "no_details": "无详情",
            "na": "暂无",
            "updated": "已更新",
            "loading": "加载中…",
            // ── Booleans ──
            "on": "开",
            "off": "关",
            "unlimited": "无限",
            "limited": "有限",
            "yes": "是",
            "no": "否",
            // ── Limit names ──
            "5h_limit": "5小时限制",
            "weekly_limit": "每周限制",
            "fable_limit": "Fable 限制",
            "fable_rate_limited": "已达限制",
            "monthly_limit": "每月限制",
            "daily_tokens": "每日令牌",
            "tier_usage": "层级用量",
            "ag_group_gemini": "Gemini 模型",
            "ag_group_other": "Claude & GPT 模型",
            "premium_requests": "高级请求",
            "chat_limit": "聊天",
            "daily_limit": "每日限制",
            // ── Window labels (detailLines) ──
            "window_label": "窗口",
            "window_monthly": "每月",
            "window_quota": "配额",
            "window_tier": "层级",
            "window_win": "窗口",
            "window_24h_tokens": "24小时令牌",
            // ── Misc ──
            "used": "已用",
            "credits": "积分",
            "expire_label": "过期",
            "issued_label": "发放",
            "coupon_reason_label": "原因",
            "coupon_issued_label": "发放",
            "coupon_expire_label": "过期",
            "reset_coupons": "重置优惠券",
            "coupon_usage": "使用奖励",
            "coupon_referral": "推荐",
            "warn_weekly_closing": "%d小时后重置 — 每周限制即将关闭",
            "weekly_short": "每周",
            "finishes_soon": "即将结束",
            // ── Layout experiment (usable-first, spec §2) ──
            "layout": "布局",
            "layout_classic": "经典",
            "layout_usable_first": "可用优先",
            "use_now_header": "立即可用",
            "limited_header": "受限 / 冷却中",
            "blocked_header": "已阻断",
            "other_header": "其他",
            "usable_count": "可用",
            "pct_left": "剩余",
            "resets_in": "%@ 后重置",
            "usable_now": "已重置 · 可用",
            "quota_exhausted": "配额耗尽",
            "token_expired_reconnect": "令牌过期 · 重新连接",
            "error_prefix": "错误",
            "sub_expired_on": "订阅过期 %@",
            // ── Subscription line (account submenus) ──
            "sub_paid": "已付费",
            "sub_free": "免费",
            "sub_renews": "%@ 续订",
            "sub_renews_soon": "即将续订 %@",
            "sub_expired_since": "自 %@ 过期",
            "sub_expired": "已过期",
        ],
        .ja: [
            // ── Footer ──
            "poll_now": "今すぐポーリング",
            "open_dashboard": "ダッシュボードを開く",
            "refresh_display": "表示を更新",
            "add_new_agent": "新しいエージェントを追加",
            "manage_accounts": "アカウントを管理…",
            "language": "言語",
            "quit": "終了",
            "heartbeat": "Heartbeat",
            "heartbeat_next": "次回",
            "heartbeat_last": "前回",
            "heartbeat_last_success": "最終成功",
            "run_heartbeat_now": "今すぐハートビート実行",
            "heartbeat_success": "成功",
            "heartbeat_fail": "失敗",
            "heartbeat_unknown": "不明",
            "reconnect_agent": "このアカウントを再接続",
            "swap_to_agent": "このアカウントに切り替え",
            "devin_api_key_title": "Devin を再接続",
            "devin_api_key_message": "この Devin アカウントの API キーを入力してください。",
            "delete_agent": "このエージェントを削除",
            "delete_agent_confirm": "%@ をエージェントプールから削除しますか？この操作は元に戻せません。",
            "cancel": "キャンセル",
            // ── Group headers ──
            "status": "ステータス",
            "limit_session": "リミットセッション",
            "resets": "リセット",
            "subscription": "サブスクリプション",
            // ── Labels ──
            "plan": "プラン",
            "token_expires": "トークン期限",
            "last_poll": "最終ポーリング",
            "additional_credits": "追加クレジット",
            "banked_resets": "蓄積リセット",
            "binding_window": "バインディングウィンドウ",
            "rate_limit_tier": "レート制限ティア",
            "billing": "課金",
            "extra_usage": "追加使用",
            "subscribed": "登録日",
            "member_since": "メンバー開始",
            "org": "組織",
            "account": "アカウント",
            "monthly_credits": "月間クレジット",
            "on_demand_cap": "オンデマンド上限",
            "billing_period": "課金期間",
            "tier": "ティア",
            "description": "説明",
            "active_session_tier": "アクティブセッションティア",
            "premium_entitlement": "プレミアム権限",
            "premium_overage": "プレミアム超過",
            "chat": "チャット",
            "completions": "補完",
            "upgradeable": "アップグレード可",
            "credit_balance": "クレジット残高",
            "plan_started": "プラン開始",
            "plan_resets": "プランリセット",
            "plan_expires": "プラン期限",
            "remaining": "残り",
            "reset": "リセット",
            "limit": "上限",
            "monthly": "月間",
            "sku": "SKU",
            "quota_limit": "クォータ上限",
            "quota_reset": "クォータリセット",
            "account_created": "アカウント作成",
            "payment_history": "過去の支払い",
            // ── Messages ──
            "no_details": "詳細なし",
            "na": "情報なし",
            "updated": "更新",
            "loading": "読み込み中…",
            // ── Booleans ──
            "on": "オン",
            "off": "オフ",
            "unlimited": "無制限",
            "limited": "制限あり",
            "yes": "はい",
            "no": "いいえ",
            // ── Limit names ──
            "5h_limit": "5時間制限",
            "weekly_limit": "週間制限",
            "fable_limit": "Fable 制限",
            "fable_rate_limited": "制限に達しました",
            "monthly_limit": "月間制限",
            "daily_tokens": "日次トークン",
            "tier_usage": "ティア使用量",
            "ag_group_gemini": "Gemini モデル",
            "ag_group_other": "Claude & GPT モデル",
            "premium_requests": "プレミアムリクエスト",
            "chat_limit": "チャット",
            "daily_limit": "日次制限",
            // ── Window labels (detailLines) ──
            "window_label": "ウィンドウ",
            "window_monthly": "月間",
            "window_quota": "クォータ",
            "window_tier": "ティア",
            "window_win": "ウィンドウ",
            "window_24h_tokens": "24時間トークン",
            // ── Misc ──
            "used": "使用済み",
            "credits": "クレジット",
            "expire_label": "期限",
            "issued_label": "発行",
            "coupon_reason_label": "理由",
            "coupon_issued_label": "発行",
            "coupon_expire_label": "期限",
            "reset_coupons": "リセットクーポン",
            "coupon_usage": "使用報酬",
            "coupon_referral": "紹介",
            "warn_weekly_closing": "%d時間後にリセット — 週間制限終了間近",
            "weekly_short": "週間",
            "finishes_soon": "まもなく終了",
            // ── Layout experiment (usable-first, spec §2) ──
            "layout": "レイアウト",
            "layout_classic": "クラシック",
            "layout_usable_first": "使用可能優先",
            "use_now_header": "今すぐ使用",
            "limited_header": "制限 / クールダウン中",
            "blocked_header": "ブロック",
            "other_header": "その他",
            "usable_count": "使用可能",
            "pct_left": "残り",
            "resets_in": "%@ 後にリセット",
            "usable_now": "リセット済み · 使用可能",
            "quota_exhausted": "クォータ枯渇",
            "token_expired_reconnect": "トークン期限切れ · 再接続",
            "error_prefix": "エラー",
            "sub_expired_on": "サブスク期限切れ %@",
            // ── Subscription line (account submenus) ──
            "sub_paid": "有料",
            "sub_free": "無料",
            "sub_renews": "%@ 更新",
            "sub_renews_soon": "まもなく更新 %@",
            "sub_expired_since": "%@ から期限切れ",
            "sub_expired": "期限切れ",
        ],
    ]

    /// Look up a translated string. Falls back to English, then to the key itself.
    static func tr(_ key: String) -> String {
        table[lang]?[key] ?? table[.en]?[key] ?? key
    }

    /// "Label: value" with a translated label.
    static func label(_ key: String, _ value: String) -> String {
        "\(tr(key)): \(value)"
    }

    /// "limitName: pct% used" with optional "(reset: r)" suffix — all parts translated.
    static func usedLine(_ limitKey: String, _ pct: String, reset: String? = nil) -> String {
        var line = "\(tr(limitKey)): \(pct)% \(tr("used"))"
        if let reset { line += " (\(tr("reset")): \(reset))" }
        return line
    }

    /// Translate a boolean-like value via its key ("on"/"off", "yes"/"no", etc.).
    static func bool(_ key: String) -> String { tr(key) }
}

// ─── Dropdown layout experiment (spec §2.1) ────────────────────────────────
enum MenuLayout: String, CaseIterable {
    case classic
    case usableFirst = "usable-first"

    var titleKey: String {
        switch self {
        case .classic: return "layout_classic"
        case .usableFirst: return "layout_usable_first"
        }
    }

    static let storageKey = "menuLayout"
    static var current: MenuLayout {
        get {
            let raw = UserDefaults.standard.string(forKey: storageKey) ?? MenuLayout.classic.rawValue
            return MenuLayout(rawValue: raw) ?? .classic
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: storageKey) }
    }
}

// ─── Menu Bar App ─────────────────────────────────────────────────────────
@main
struct TokenStatusBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var loader = StatusLoader()
    var popover: NSPopover!
    var language: Language = .en
    var menuLayout: MenuLayout = .classic
    // 1s ticker: keeps LIMITED-section reset countdowns ticking while the
    // menu is open (usable-first layout only). Section moves happen on the
    // next menu open (menuNeedsUpdate re-derives phase client-side).
    private var menuTicker: Timer?
    private var limitedRows: [(row: FixedMenuRowView, account: Account)] = []
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        language = Language.current
        L10n.lang = language
        menuLayout = MenuLayout.current
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        updateStatusIcon()

        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        loader.$payload
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateStatusIcon() }
            .store(in: &cancellables)

        loader.$lastError
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateStatusIcon() }
            .store(in: &cancellables)

        loader.start()

        NSApp.setActivationPolicy(.accessory)
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopMenuTicker()
        loader.stop()
    }

    private func windowRiskColor(pct: Double, severity: String?, projected: Bool) -> NSColor {
        if projected || pct > 80 || (severity ?? "normal") != "normal" { return .systemRed }
        if pct >= 50 { return .systemYellow }
        return .systemGreen
    }

    static func timeLeft(_ epoch: Double?) -> String? {
        guard let epoch else { return nil }
        let s = Int(epoch - Date().timeIntervalSince1970)
        if s <= 0 { return nil }
        if s < 3600 { return "\(s / 60)m" }
        if s < 86400 { return "\(s / 3600)h\((s % 3600) / 60)m" }
        return String(format: "%.1fd", Double(s) / 86400.0)
    }

    private func headlineTitle() -> NSAttributedString? {
        let warn = loader.statusWarning != nil
        guard let h = loader.payload?.headline else {
            guard warn else { return nil }
            return NSAttributedString(string: " ⚠︎", attributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
                .foregroundColor: NSColor.systemOrange,
                .baselineOffset: 0.5,
            ])
        }
        var text = " \(Int(h.used_pct.rounded()))%"
        if let left = AppDelegate.timeLeft(h.reset_at_epoch) { text += " · \(left)" }
        if warn { text += " ⚠︎" }
        let color = warn ? NSColor.systemOrange
            : windowRiskColor(pct: h.used_pct, severity: h.severity, projected: false)
        return NSAttributedString(string: text, attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
            .foregroundColor: color,
            .baselineOffset: 0.5,
        ])
    }

    /// Pool-wide health shown as the icon's corner dot.
    private func poolDotColor() -> NSColor {
        guard let payload = loader.payload else { return .systemOrange }
        if (payload.heartbeat?.failed ?? 0) > 0
            || payload.accounts.contains(where: { $0.status == "error" || $0.heartbeat_status == "fail" }) {
            return .systemRed
        }
        if payload.accounts.contains(where: { $0.status == "expired" }) {
            return .systemOrange
        }
        return .systemGreen
    }

    private func updateStatusIcon() {
        guard let button = statusItem.button else { return }
        guard let symbol = NSImage(systemSymbolName: "chart.bar.fill", accessibilityDescription: "Agent Pool")?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 13, weight: .regular)) else {
            button.title = "AP"
            return
        }
        let dotColor = poolDotColor()
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { rect in
            let symbolRect = NSRect(x: (rect.width - symbol.size.width) / 2,
                                    y: (rect.height - symbol.size.height) / 2,
                                    width: symbol.size.width,
                                    height: symbol.size.height)
            symbol.draw(in: symbolRect)
            NSColor.labelColor.set()
            symbolRect.fill(using: .sourceAtop)

            let dotRect = NSRect(x: rect.maxX - 7, y: 0, width: 7, height: 7)
            if let ctx = NSGraphicsContext.current {
                // Punch a gap around the dot so it reads against the bars.
                ctx.compositingOperation = .destinationOut
                NSBezierPath(ovalIn: dotRect.insetBy(dx: -1.5, dy: -1.5)).fill()
                ctx.compositingOperation = .sourceOver
            }
            dotColor.setFill()
            NSBezierPath(ovalIn: dotRect).fill()
            return true
        }
        image.isTemplate = false
        image.accessibilityDescription = "Agent Pool"
        button.image = image
        if let title = headlineTitle() {
            button.attributedTitle = title
            button.imagePosition = .imageLeft
        } else {
            button.attributedTitle = NSAttributedString(string: "")
            button.imagePosition = .imageOnly
        }
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }
}

extension AppDelegate: NSMenuDelegate {
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        buildMenu(menu)
    }

    func menuWillOpen(_ menu: NSMenu) {
        guard menu === statusItem.menu, menuLayout == .usableFirst else { return }
        startMenuTicker()
    }

    func menuDidClose(_ menu: NSMenu) {
        guard menu === statusItem.menu else { return }
        stopMenuTicker()
    }

    func buildMenu(_ menu: NSMenu) {
        limitedRows = []
        guard let payload = loader.payload else {
            menu.addItem(headerItem(t("loading")))
            if let err = loader.lastError {
                menu.addItem(separatorRow())
                menu.addItem(infoItem(err))
            }
            menu.addItem(separatorRow())
            addFooter(menu)
            return
        }

        // Layout experiment (spec §2.1): usable-first restructures the list
        // around "what can I use right now"; classic stays untouched below.
        if menuLayout == .usableFirst {
            buildUsableFirstMenu(menu, payload: payload)
            return
        }

        // Header
        if let warning = loader.statusWarning {
            menu.addItem(warningItem("⚠︎ \(warning)"))
            menu.addItem(separatorRow())
        }
        menu.addItem(titleItem("Agent Pool: \(payload.account_count) accounts"))
        menu.addItem(infoItem("\(t("updated")): \(formatUpdated(payload.generated_at))"))
        if let heartbeat = payload.heartbeat {
            menu.addItem(heartbeatItem(heartbeat, accounts: payload.accounts))
        }
        menu.addItem(separatorRow())

        // Group by provider
        let providerOrder = ["codex", "claude", "xai", "antigravity", "copilot", "cursor", "devin", "droid", "opencode"]
        let grouped = Dictionary(grouping: payload.accounts, by: { $0.provider })
        for provider in providerOrder {
            guard let accts = grouped[provider] else { continue }
            menu.addItem(headerItem(providerDisplayName(provider)))
            for acct in accts {
                menu.addItem(accountItem(acct))
            }
            menu.addItem(separatorRow())
        }

        addLiveActivityRow(menu, payload: payload)

        addFooter(menu)
    }

    /// Live ticker row: freshest local session activity across accounts.
    /// Shared by both layouts (unchanged from classic).
    private func addLiveActivityRow(_ menu: NSMenu, payload: StatusPayload) {
        let fresh = payload.accounts.compactMap { a -> (Account, LiveActivity, Double)? in
            guard let live = a.live, let ts = live.as_of_epoch ?? live.event_epoch,
                  Date().timeIntervalSince1970 - ts < 600 else { return nil }
            return (a, live, ts)
        }.max(by: { $0.2 < $1.2 })
        if let (acct, live, _) = fresh {
            var parts: [String] = [providerDisplayName(acct.provider)]
            if let tokens = live.last_total_tokens {
                parts.append("+\(tokens.formatted()) tok")
            }
            if let ctx = live.context_used_pct {
                parts.append("context \(Int(ctx.rounded()))%")
            }
            if let t60 = live.tokens_60m, live.last_total_tokens == nil {
                parts.append("\(t60.formatted()) tok/60m")
            }
            menu.addItem(infoItem("⚡︎ " + parts.joined(separator: " · ")))
            menu.addItem(separatorRow())
        }
    }

    // ─── usable-first layout (spec §2.2) ─────────────────────────────────

    enum AccountSection {
        case useNow, limited, blocked, other
    }

    /// Effective per-window used% with the client-side phase rule applied:
    /// a window whose reset_at_epoch has already passed counts as reset
    /// (0% used) without waiting for the next poll (spec §1.3 / §2.2).
    func windowEffectivePct(_ w: WindowInfo, now: Double) -> Double? {
        if w.phase == "reset" { return 0 }
        if let reset = w.reset_at_epoch, reset < now { return 0 }
        return w.used_pct_effective ?? w.used_pct
    }

    /// Client-side section assignment from the exported `state`, re-deriving
    /// window phase at `now` so a countdown hitting 0 moves the account to
    /// USE NOW on the next rebuild without waiting for the next poll.
    func effectiveSection(_ acct: Account, now: Double) -> AccountSection {
        guard let state = acct.state else { return .other }
        if (state.auth ?? "ok") != "ok" || state.subscription == "expired" {
            return .blocked
        }
        var quota = state.quota ?? "unknown"
        if quota == "exhausted" {
            // Phase flip: exhaustion requires every live window at 100%, so
            // once any live window's reset time passes the account recovers.
            let live = (acct.windows ?? []).filter { $0.stale != true && $0.phase != "reset" }
            if live.contains(where: { ($0.reset_at_epoch ?? .infinity) < now }) {
                quota = "ok"
            }
        }
        switch quota {
        case "exhausted": return .limited
        case "unknown": return .other
        default: return .useNow // ok and warning are both usable
        }
    }

    private func windowKindLabel(_ w: WindowInfo) -> String {
        if w.kind == "model_weekly" { return w.label ?? "model" }
        if w.kind == "monthly", let label = w.label { return label }
        return w.kind
    }

    /// "weekly 78% left" from the account's binding window.
    private func bindingSummary(_ acct: Account, now: Double) -> String? {
        guard let w = acct.state?.binding_window,
              let pct = windowEffectivePct(w, now: now) else { return nil }
        let left = max(0, 100 - Int(pct.rounded()))
        return "\(windowKindLabel(w)) \(left)% \(t("pct_left"))"
    }

    /// "5h 0% left · resets in 42m" — why the account is cooling down.
    private func limitedReason(_ acct: Account, now: Double) -> String {
        let windows = (acct.windows ?? []).filter { $0.stale != true }
        let exhausted = windows.filter { w in
            let sev = (w.severity ?? "normal").lowercased()
            return (windowEffectivePct(w, now: now) ?? 0) >= 100
                || sev == "rate_limited" || sev == "exceeded"
        }
        let nextReset = exhausted.compactMap { $0.reset_at_epoch }.filter { $0 > now }.min()
        guard let nextReset else { return t("quota_exhausted") }
        let kind = exhausted.first(where: { $0.reset_at_epoch == nextReset }).map(windowKindLabel)
        var text = kind.map { "\($0) 0% \(t("pct_left"))" } ?? t("quota_exhausted")
        if let left = AppDelegate.timeLeft(nextReset) {
            text += " · " + String(format: t("resets_in"), left)
        }
        return text
    }

    /// Full row title for a LIMITED account; recomputed by the 1s ticker so
    /// the countdown ticks while the menu is open. When the countdown hits 0
    /// (phase flips client-side) it says so in place; the row moves to
    /// USE NOW on the next rebuild.
    private func limitedRowTitle(_ acct: Account, now: Double) -> String {
        let base = "\(providerDisplayName(acct.provider)) · \(accountTitle(acct))"
        if effectiveSection(acct, now: now) != .limited {
            return "\(base) · \(t("usable_now"))"
        }
        return "\(base) · \(limitedReason(acct, now: now))"
    }

    /// Exact reason a BLOCKED account can't be used.
    private func blockedReason(_ acct: Account) -> String {
        guard let state = acct.state else { return t("no_details") }
        switch state.auth ?? "ok" {
        case "token_expired":
            return t("token_expired_reconnect")
        case "error":
            let msg = acct.status_message ?? ""
            return msg.isEmpty ? t("error_prefix") : "\(t("error_prefix")): \(msg)"
        default:
            break
        }
        if state.subscription == "expired" {
            return String(format: t("sub_expired_on"), shortMonthDay(state.sub_expires_at) ?? "?")
        }
        return t("no_details")
    }

    func buildUsableFirstMenu(_ menu: NSMenu, payload: StatusPayload) {
        let now = Date().timeIntervalSince1970

        if let warning = loader.statusWarning {
            menu.addItem(warningItem("⚠︎ \(warning)"))
            menu.addItem(separatorRow())
        }

        var useNow: [Account] = []
        var limited: [Account] = []
        var blocked: [Account] = []
        var other: [Account] = []
        for acct in payload.accounts {
            switch effectiveSection(acct, now: now) {
            case .useNow: useNow.append(acct)
            case .limited: limited.append(acct)
            case .blocked: blocked.append(acct)
            case .other: other.append(acct)
            }
        }

        // Header
        menu.addItem(titleItem(
            "Agent Pool: \(payload.account_count) accounts · \(useNow.count) \(t("usable_count"))"))
        menu.addItem(infoItem("\(t("updated")): \(formatUpdated(payload.generated_at))"))
        if let heartbeat = payload.heartbeat {
            menu.addItem(heartbeatItem(heartbeat, accounts: payload.accounts))
        }
        menu.addItem(separatorRow())

        // USE NOW: best-headroom account first within each provider.
        let providerOrder = ["codex", "claude", "xai", "antigravity", "copilot", "cursor", "devin", "droid", "opencode"]
        func providerRank(_ p: String) -> Int { providerOrder.firstIndex(of: p) ?? providerOrder.count }
        func headroom(_ a: Account) -> Double {
            guard let w = a.state?.binding_window,
                  let pct = windowEffectivePct(w, now: now) else { return 100 }
            return 100 - pct
        }
        useNow.sort { a, b in
            if providerRank(a.provider) != providerRank(b.provider) {
                return providerRank(a.provider) < providerRank(b.provider)
            }
            return headroom(a) > headroom(b)
        }

        if !useNow.isEmpty {
            menu.addItem(headerItem(t("use_now_header")))
            for acct in useNow {
                var title = "\(providerDisplayName(acct.provider)) · \(accountTitle(acct))"
                if let summary = bindingSummary(acct, now: now) { title += " · \(summary)" }
                menu.addItem(accountItem(acct, titleOverride: title, dotColorOverride: .systemGreen))
            }
            menu.addItem(separatorRow())
        }

        // LIMITED / COOLING DOWN: strictly quota-exhausted (auth ok, sub fine).
        if !limited.isEmpty {
            menu.addItem(headerItem(t("limited_header")))
            for acct in limited {
                let item = accountItem(acct, titleOverride: limitedRowTitle(acct, now: now),
                                       dotColorOverride: .systemYellow)
                if let container = item.view as? AccountRowWithGauges {
                    limitedRows.append((row: container.rowView, account: acct))
                }
                menu.addItem(item)
            }
            menu.addItem(separatorRow())
        }

        // BLOCKED: auth or subscription problem.
        if !blocked.isEmpty {
            menu.addItem(headerItem(t("blocked_header")))
            for acct in blocked {
                let title = "\(providerDisplayName(acct.provider)) · \(accountTitle(acct)) · \(blockedReason(acct))"
                menu.addItem(accountItem(acct, titleOverride: title,
                                         dotColorOverride: .systemGray, hollowDot: true))
            }
            menu.addItem(separatorRow())
        }

        // OTHER: no exported state (older status.json) or unknown quota —
        // rendered as plain classic-style rows in a trailing group.
        if !other.isEmpty {
            menu.addItem(headerItem(t("other_header")))
            for acct in other {
                menu.addItem(accountItem(acct))
            }
            menu.addItem(separatorRow())
        }

        // Last auto-swap event (hidden placeholder until M4 exports it).
        if let swapRow = lastSwapItem(payload) {
            menu.addItem(swapRow)
            menu.addItem(separatorRow())
        }

        addLiveActivityRow(menu, payload: payload)
        addFooter(menu)
    }

    /// "⇄ auto-swap: Codex → codex-2 at 16:41". Returns nil until the M4
    /// swap engine starts exporting `last_swap` in status.json.
    func lastSwapItem(_ payload: StatusPayload) -> NSMenuItem? {
        guard let swap = payload.last_swap else { return nil }
        var text = "⇄ auto-swap: " + (swap.provider.map(providerDisplayName) ?? "?")
        if let to = swap.to { text += " → \(to)" }
        var when: Date?
        if let epoch = swap.at_epoch {
            when = Date(timeIntervalSince1970: epoch)
        } else if let at = swap.at {
            when = StatusLoader.parseStatusDate(at)
        }
        if let when {
            let out = DateFormatter()
            out.locale = Locale(identifier: "en_US_POSIX")
            out.dateFormat = "HH:mm"
            text += " at \(out.string(from: when))"
        }
        return infoItem(text)
    }

    // ─── 1s menu ticker (usable-first countdowns) ────────────────────────

    private func startMenuTicker() {
        stopMenuTicker()
        let ticker = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            self?.tickCountdowns()
        }
        // Menu tracking runs in the event-tracking run-loop mode; register
        // for both so countdowns keep ticking while the menu is open.
        RunLoop.main.add(ticker, forMode: .common)
        RunLoop.main.add(ticker, forMode: .eventTracking)
        menuTicker = ticker
    }

    func stopMenuTicker() {
        menuTicker?.invalidate()
        menuTicker = nil
    }

    private func tickCountdowns() {
        let now = Date().timeIntervalSince1970
        for (row, acct) in limitedRows {
            row.updateTitle(limitedRowTitle(acct, now: now))
        }
    }

    private func statusColor(_ status: String) -> NSColor {
        switch status {
        case "active": return .systemGreen
        case "error": return .systemRed
        case "expired": return .systemYellow
        default: return .tertiaryLabelColor
        }
    }

    private func heartbeatColor(_ status: String) -> NSColor {
        switch status {
        case "success": return .systemGreen
        case "fail": return .systemRed
        default: return .systemYellow
        }
    }

    private func heartbeatStatusText(_ status: String?) -> String {
        switch status {
        case "success": return t("heartbeat_success")
        case "fail": return t("heartbeat_fail")
        default: return t("heartbeat_unknown")
        }
    }

    private func heartbeatLine(status: String?, next: String?) -> String {
        "\(t("heartbeat")): \(heartbeatStatusText(status)) · \(t("heartbeat_next")) \(next ?? "?")"
    }

    func heartbeatItem(_ heartbeat: HeartbeatSummary, accounts: [Account]) -> NSMenuItem {
        let submenu = NSMenu()
        let width: CGFloat = 360
        let heartbeatAccounts = accounts.filter { ["codex", "claude", "antigravity"].contains($0.provider) }
        for acct in heartbeatAccounts {
            let name = acct.email ?? acct.label ?? "account #\(acct.id)"
            submenu.addItem(infoItem("\(providerDisplayName(acct.provider)): \(name)", width: width))
            submenu.addItem(infoItem(heartbeatLine(status: acct.heartbeat_status, next: acct.heartbeat_next),
                                     width: width))
            if let last = acct.heartbeat_last {
                submenu.addItem(infoItem("\(t("heartbeat_last")): \(last)", width: width))
            }
            if let lastOk = acct.heartbeat_last_success {
                submenu.addItem(infoItem("\(t("heartbeat_last_success")): \(lastOk)", width: width))
            }
            if let msg = acct.heartbeat_message, !msg.isEmpty {
                submenu.addItem(infoItem(msg, width: width))
            }
            submenu.addItem(separatorRow(width: width))
        }
        submenu.addItem(actionItem(t("run_heartbeat_now"), width: width) { [weak self] in
            self?.loader.runHeartbeat()
        })
        let failed = heartbeat.failed ?? 0
        let count = heartbeat.accounts ?? heartbeatAccounts.count
        let suffix = failed > 0 ? " · \(failed)/\(count) failed" : " · \(count) accounts"
        return submenuRow(heartbeatLine(status: heartbeat.status, next: heartbeat.next) + suffix,
                          submenu: submenu,
                          dotColor: heartbeatColor(heartbeat.status))
    }

    private func addHeartbeatStatus(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        guard acct.heartbeat_status != nil || acct.heartbeat_next != nil || acct.heartbeat_last != nil else { return }
        submenu.addItem(groupHeaderItem(t("heartbeat"), width: width))
        submenu.addItem(infoItem(heartbeatLine(status: acct.heartbeat_status, next: acct.heartbeat_next), width: width))
        if let last = acct.heartbeat_last {
            submenu.addItem(infoItem("\(t("heartbeat_last")): \(last)", width: width))
        }
        if let lastOk = acct.heartbeat_last_success {
            submenu.addItem(infoItem("\(t("heartbeat_last_success")): \(lastOk)", width: width))
        }
        if let msg = acct.heartbeat_message, !msg.isEmpty {
            submenu.addItem(infoItem(msg, width: width))
        }
        submenu.addItem(actionItem(t("run_heartbeat_now"), width: width) { [weak self] in
            self?.loader.runHeartbeat(accountId: acct.id)
        })
    }

    /// Display name used for account rows in both layouts.
    func accountTitle(_ acct: Account) -> String {
        var title = acct.email ?? acct.label ?? "unknown"
        if acct.provider == "copilot", let mail = acct.github_email, !mail.isEmpty {
            title = "\(acct.email ?? acct.label ?? "unknown") (\(mail))"
        }
        return title
    }

    func accountItem(_ acct: Account, titleOverride: String? = nil,
                     dotColorOverride: NSColor? = nil, hollowDot: Bool = false) -> NSMenuItem {
        let title = titleOverride ?? accountTitle(acct)
        let submenu = NSMenu()
        let detailWidth: CGFloat = 420
        if acct.provider == "codex" {
            buildCodexSubmenu(submenu, acct: acct, width: detailWidth)
        } else if acct.provider == "claude" {
            buildClaudeSubmenu(submenu, acct: acct, width: detailWidth)
        } else if acct.provider == "xai" {
            buildGrokSubmenu(submenu, acct: acct, width: detailWidth)
        } else if acct.provider == "antigravity" {
            buildAntigravitySubmenu(submenu, acct: acct, width: detailWidth)
        } else if acct.provider == "copilot" {
            buildCopilotSubmenu(submenu, acct: acct, width: detailWidth)
        } else if acct.provider == "devin" {
            buildDevinSubmenu(submenu, acct: acct, width: detailWidth)
        } else {
            for line in detailLines(acct) {
                submenu.addItem(infoItem(line, width: detailWidth))
            }
        }
        for w in acct.windows ?? [] {
            guard let exhaust = w.projected_exhaust_epoch,
                  let left = AppDelegate.timeLeft(exhaust) else { continue }
            let name = w.kind == "model_weekly" ? (w.label ?? "model") : w.kind
            submenu.addItem(warningItem("⚠︎ \(name): exhausts in ~\(left) at current pace",
                                        width: detailWidth))
        }
        submenu.addItem(separatorRow(width: detailWidth))
        addHeartbeatStatus(submenu, acct: acct, width: detailWidth)
        if acct.heartbeat_status != nil || acct.heartbeat_next != nil || acct.heartbeat_last != nil {
            submenu.addItem(separatorRow(width: detailWidth))
        }
        // Manual same-provider swap (codex only, spec §3.2) — offered only
        // when this account could actually take over (usable == true).
        if acct.provider == "codex", acct.state?.usable == true {
            submenu.addItem(actionItem(t("swap_to_agent"), width: detailWidth) { [weak self] in
                self?.loader.swapToAgent(acct)
            })
        }
        submenu.addItem(actionItem(t("reconnect_agent"), width: detailWidth) { [weak self] in
            self?.loader.reconnectAgent(acct)
        })
        submenu.addItem(actionItem(t("delete_agent"), width: detailWidth, destructive: true) { [weak self] in
            self?.loader.confirmDeleteAgent(acct: acct)
        })
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.submenu = submenu
        let row = FixedMenuRowView(title: title, style: .submenu, submenu: submenu,
                                   dotColor: dotColorOverride ?? statusColor(acct.status),
                                   hollowDot: hollowDot, badge: endSoonBadge(acct))
        let gauges = (acct.windows ?? []).prefix(3).map { w in
            GaugeRowView(window: w,
                         color: windowRiskColor(pct: w.used_pct ?? 0,
                                                severity: w.severity,
                                                projected: w.projected_exhaust_epoch != nil),
                         width: MenuRowLayout.width)
        }
        item.view = AccountRowWithGauges(row: row, gauges: Array(gauges),
                                         width: MenuRowLayout.width)
        return item
    }

    func normalizePlan(_ acct: Account) -> String? {
        let raw = (acct.plan ?? "").lowercased()
        let tier = (acct.rate_limit_tier ?? "").lowercased()
        let override = (acct.tier_override ?? "").lowercased()
        switch acct.provider {
        case "codex":
            if raw.contains("enterprise") { return "Enterprise" }
            if raw.contains("business") { return "Business" }
            switch raw {
            case "free": return "Free"
            case "go": return "Go"
            case "plus": return "Plus"
            default:
                if raw.contains("pro") {
                    if override.contains("20x") || raw.contains("20x") || tier.contains("20x") { return "Pro 20x" }
                    if override.contains("5x") || raw.contains("5x") || tier.contains("5x") { return "Pro 5x" }
                    return "Pro"
                }
                return acct.plan
            }
        case "claude":
            if raw.contains("enterprise") { return "Enterprise" }
            if raw.contains("team") { return "Team" }
            if raw.contains("free") { return "Free" }
            if raw.contains("pro") && !raw.contains("max") { return "Pro" }
            if raw.contains("max") {
                if tier.contains("20x") { return "Max 20x" }
                if tier.contains("5x") { return "Max 5x" }
                return "Max"
            }
            return acct.plan
        case "antigravity":
            if override.contains("ultra") || override.contains("20x") || override.contains("5x") {
                if override.contains("20x") || override.contains("20") { return "Ultra 20x" }
                if override.contains("5x") || override.contains("5") { return "Ultra 5x" }
                return "Ultra"
            }
            if override.contains("pro") { return "Pro" }
            if override.contains("plus") { return "Plus" }
            if raw.contains("free") { return "Free" }
            if raw.contains("plus") { return "Plus" }
            if raw.contains("ultra") {
                if raw.contains("20x") { return "Ultra 20x" }
                if raw.contains("5x") { return "Ultra 5x" }
                return "Ultra"
            }
            if raw.contains("pro") { return "Pro" }
            return acct.plan
        case "xai":
            if raw.contains("heavy") { return "SuperGrok Heavy" }
            if raw.contains("super") { return "SuperGrok" }
            if raw.contains("free") { return "Free" }
            if let limit = acct.monthly_limit {
                if limit >= 30000 { return "SuperGrok Heavy" }
                if limit >= 15000 { return "SuperGrok" }
                return "Free"
            }
            return acct.plan
        case "copilot":
            if raw.contains("enterprise") { return "Enterprise" }
            if raw.contains("business") { return "Business" }
            if raw.contains("max") { return "Max" }
            if raw.contains("pro_plus") || raw.contains("pro+") { return "Pro+" }
            if raw.contains("pro") { return "Pro" }
            if raw.contains("free") { return "Free" }
            return acct.plan
        case "devin":
            if raw.contains("enterprise") { return "Enterprise" }
            if raw.contains("team") { return "Teams" }
            if raw.contains("max") { return "Max" }
            if raw.contains("pro") { return "Pro" }
            if raw.contains("free") { return "Free" }
            return acct.plan
        case "cursor":
            if raw.contains("enterprise") { return "Enterprise" }
            if raw.contains("team") { return "Teams" }
            if raw.contains("ultra") { return "Ultra" }
            if raw.contains("pro_plus") || raw.contains("pro+") { return "Pro+" }
            if raw.contains("pro") { return "Pro" }
            if raw.contains("free") { return "Free" }
            return acct.plan
        default:
            return acct.plan
        }
    }

    func planPrice(_ acct: Account) -> String? {
        guard let plan = normalizePlan(acct) else { return nil }
        switch acct.provider {
        case "codex":
            switch plan {
            case "Free": return "$0"
            case "Go": return "$8"
            case "Plus": return "$20"
            case "Pro 5x": return "$100"
            case "Pro 20x": return "$200"
            case "Business": return "$25/user"
            case "Enterprise": return "Custom"
            default: return nil
            }
        case "claude":
            switch plan {
            case "Free": return "$0"
            case "Pro": return "$20"
            case "Max 5x": return "$100"
            case "Max 20x": return "$200"
            case "Team": return "$25/seat"
            case "Enterprise": return "Custom"
            default: return nil
            }
        case "antigravity":
            switch plan {
            case "Free": return "$0"
            case "Plus": return "$7.99"
            case "Pro": return "$19.99"
            case "Ultra 5x": return "$100"
            case "Ultra 20x": return "$200"
            default: return nil
            }
        case "xai":
            switch plan {
            case "Free": return "$0"
            case "SuperGrok": return "$30"
            case "SuperGrok Heavy": return "$300"
            default: return nil
            }
        case "copilot":
            switch plan {
            case "Free": return "$0"
            case "Pro": return "$10"
            case "Pro+": return "$39"
            case "Max": return "$100"
            case "Business": return "$19/user"
            case "Enterprise": return "$39/user"
            default: return nil
            }
        case "devin":
            switch plan {
            case "Free": return "$0"
            case "Pro": return "$20"
            case "Max": return "$200"
            case "Teams": return "$80+$40/seat"
            case "Enterprise": return "Custom"
            default: return nil
            }
        case "cursor":
            switch plan {
            case "Free": return "$0"
            case "Pro": return "$20"
            case "Pro+": return "$60"
            case "Ultra": return "$200"
            case "Teams": return "$40/user"
            case "Enterprise": return "Custom"
            default: return nil
            }
        default:
            return nil
        }
    }

    func planText(_ acct: Account) -> String? {
        guard let plan = normalizePlan(acct), !plan.isEmpty else { return nil }
        if let price = planPrice(acct) {
            return "\(t("plan")): \(plan) (\(price))"
        }
        return "\(t("plan")): \(plan)"
    }

    /// "2026-08-01T00:00:00+00:00" → "08-01" (local time).
    func shortMonthDay(_ iso: String?) -> String? {
        guard let iso, let date = StatusLoader.parseStatusDate(iso) else { return nil }
        let out = DateFormatter()
        out.locale = Locale(identifier: "en_US_POSIX")
        out.dateFormat = "MM-dd"
        return out.string(from: date)
    }

    /// Subscription lifecycle line from the exported per-account `state`:
    /// "Plan · paid · renews 08-01" / "Plan · expired since 07-19".
    func subscriptionLine(_ acct: Account) -> String? {
        guard let state = acct.state, let sub = state.subscription, sub != "unknown" else { return nil }
        let plan = normalizePlan(acct) ?? t("plan")
        switch sub {
        case "paid":
            if let d = shortMonthDay(state.sub_renews_at) {
                return "\(plan) · \(t("sub_paid")) · " + String(format: t("sub_renews"), d)
            }
            return "\(plan) · \(t("sub_paid"))"
        case "renews_soon":
            let d = shortMonthDay(state.sub_renews_at) ?? "?"
            return "\(plan) · \(t("sub_paid")) · " + String(format: t("sub_renews_soon"), d)
        case "free":
            return "\(plan) · \(t("sub_free"))"
        case "expired":
            if let d = shortMonthDay(state.sub_expires_at) {
                return "\(plan) · " + String(format: t("sub_expired_since"), d)
            }
            return "\(plan) · \(t("sub_expired"))"
        default:
            return nil
        }
    }

    func planStartText(_ acct: Account) -> String? {
        let start = acct.plan_start ?? acct.billing_period_start ?? acct.monthly_period_start
        if let start, !start.isEmpty {
            return L10n.label("plan_started", start)
        }
        return nil
    }

    func planResetText(_ acct: Account) -> String? {
        let end = acct.plan_reset ?? acct.monthly_period_end
        if let end, !end.isEmpty {
            if acct.provider == "codex", acct.is_active_subscription_gratis == true {
                return L10n.label("plan_expires", end)
            }
            return L10n.label("plan_resets", end)
        }
        return nil
    }

    func quotaResetHoursLeft(_ resetStr: String?) -> Double? {
        guard let resetStr, !resetStr.isEmpty else { return nil }
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = TimeZone(identifier: "Asia/Seoul")
        fmt.dateFormat = "yyyy-MM-dd HH:mm"
        guard let reset = fmt.date(from: resetStr) else { return nil }
        let hours = reset.timeIntervalSinceNow / 3600.0
        guard hours > 0, hours <= 24 else { return nil }
        return hours
    }

    func hasWeeklyQuota(_ acct: Account) -> Bool {
        acct.provider == "codex" || acct.provider == "claude" || acct.provider == "devin"
    }

    func hasMonthlyQuota(_ acct: Account) -> Bool {
        acct.provider == "xai" || acct.provider == "copilot"
    }

    func weeklyResetEndsSoon(_ acct: Account) -> Bool {
        guard hasWeeklyQuota(acct), acct.secondary_used_pct != nil else { return false }
        return quotaResetHoursLeft(acct.secondary_reset) != nil
    }

    func monthlyResetEndsSoon(_ acct: Account) -> Bool {
        guard hasMonthlyQuota(acct), acct.primary_used_pct != nil else { return false }
        return quotaResetHoursLeft(acct.primary_reset) != nil
    }

    func weeklyQuotaLow(_ acct: Account) -> Bool {
        guard hasWeeklyQuota(acct), let used = acct.secondary_used_pct else { return false }
        return used > 80
    }

    func monthlyQuotaLow(_ acct: Account) -> Bool {
        guard hasMonthlyQuota(acct), let used = acct.primary_used_pct else { return false }
        return used > 80
    }

    /// Compact top-level badge shown when a weekly/monthly quota resets within
    /// one day or has less than 20% remaining.
    func endSoonBadge(_ acct: Account) -> String? {
        guard weeklyResetEndsSoon(acct) || monthlyResetEndsSoon(acct) ||
              weeklyQuotaLow(acct) || monthlyQuotaLow(acct) else { return nil }
        return t("finishes_soon")
    }

    func buildCodexSubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        // ─── Status group ───
        var extra: [(String, String)] = []
        if let created = acct.account_created, !created.isEmpty {
            extra.append(("account_created", created))
        }
        if let history = acct.payment_history, !history.isEmpty {
            extra.append(("payment_history", history))
        }
        statusGroup(submenu, acct: acct, width: width, extra: extra)

        // ─── Limit session group ───
        limitSessionGroup(submenu, acct: acct, width: width, fiveHour: true, weekly: true, credits: true)

        // ─── Resets group ───
        submenu.addItem(separatorRow(width: width))
        let credits = acct.reset_credits ?? []
        submenu.addItem(groupHeaderItem("Resets (\(credits.count))", width: width))
        if !credits.isEmpty {
            let couponDetailWidth: CGFloat = 260
            for c in credits {
                let typeTag = couponType(c.description)
                let issued = formatCouponExpiry(c.granted_at)
                let expire = formatCouponExpiry(c.expires_at)
                let detailSub = NSMenu()
                detailSub.autoenablesItems = false
                detailSub.addItem(infoItem(L10n.label("coupon_reason_label", typeTag), width: couponDetailWidth))
                detailSub.addItem(infoItem(L10n.label("coupon_issued_label", issued), width: couponDetailWidth))
                detailSub.addItem(infoItem(L10n.label("coupon_expire_label", expire), width: couponDetailWidth))
                submenu.addItem(submenuRow(expire, submenu: detailSub, width: width,
                                           dotColor: couponDotColor(c), badge: couponBadge(c)))
            }
        }
    }

    func buildClaudeSubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        // ─── Status group ───
        statusGroup(submenu, acct: acct, width: width)

        // ─── Limit session group ───
        limitSessionGroup(submenu, acct: acct, width: width, fiveHour: true, weekly: true)
    }

    private func statusGroup(_ submenu: NSMenu, acct: Account, width: CGFloat, extra: [(String, String)] = []) {
        submenu.addItem(groupHeaderItem(t("status"), width: width))
        let na = t("na")
        if let line = planText(acct) {
            submenu.addItem(infoItem(line, width: width))
        } else {
            submenu.addItem(infoItem(L10n.label("plan", na), width: width))
        }
        if let line = subscriptionLine(acct) {
            submenu.addItem(infoItem(line, width: width))
        }
        submenu.addItem(infoItem(planStartText(acct) ?? L10n.label("plan_started", na), width: width))
        submenu.addItem(infoItem(planResetText(acct) ?? L10n.label("plan_resets", na), width: width))
        for (key, value) in extra {
            submenu.addItem(infoItem(L10n.label(key, value), width: width))
        }
        submenu.addItem(infoItem(L10n.label("token_expires", acct.token_expires ?? na), width: width))
        submenu.addItem(infoItem(L10n.label("last_poll", acct.last_poll ?? na), width: width))
    }

    /// Limit session group: only 5h / weekly / monthly / additional credits,
    /// whichever are available. The whole group is omitted when none apply.
    private func limitSessionGroup(_ submenu: NSMenu, acct: Account, width: CGFloat,
                                   fiveHour: Bool = false, weekly: Bool = false,
                                   monthly: Bool = false, credits: Bool = false,
                                   primaryLabel: String? = nil) {
        var items: [NSMenuItem] = []
        if fiveHour, let p = acct.primary_used_pct {
            items.append(infoItem(L10n.usedLine("5h_limit", String(format: "%.1f", p), reset: acct.primary_reset),
                                  width: width, accentPercent: true))
        }
        if weekly, let s = acct.secondary_used_pct {
            items.append(infoItem(L10n.usedLine("weekly_limit", String(format: "%.1f", s), reset: acct.secondary_reset),
                                  width: width, accentPercent: true,
                                  warnPercent: weeklyQuotaLow(acct),
                                  accentResetTime: weeklyResetEndsSoon(acct)))
        }
        if acct.provider == "claude", let p = acct.fable_used_pct {
            items.append(infoItem(L10n.usedLine("fable_limit", String(format: "%.1f", p), reset: acct.fable_reset),
                                  width: width, accentPercent: true))
        } else if acct.provider == "claude", let st = acct.fable_status, !st.isEmpty {
            items.append(infoItem("\(L10n.tr("fable_limit")): \(L10n.tr("fable_\(st)"))",
                                  width: width))
        }
        if monthly, let p = acct.primary_used_pct {
            items.append(infoItem(L10n.usedLine("monthly_limit", String(format: "%.1f", p), reset: acct.primary_reset),
                                  width: width, accentPercent: true,
                                  warnPercent: monthlyQuotaLow(acct),
                                  accentResetTime: monthlyResetEndsSoon(acct)))
        }
        if let label = primaryLabel, let p = acct.primary_used_pct {
            items.append(infoItem(L10n.usedLine(label, String(format: "%.1f", p), reset: acct.primary_reset),
                                  width: width, accentPercent: true,
                                  warnPercent: monthlyQuotaLow(acct),
                                  accentResetTime: monthlyResetEndsSoon(acct)))
        }
        if credits, let b = acct.credits_balance {
            items.append(infoItem(L10n.label("additional_credits", String(format: "%.0f", b)), width: width))
        }
        if items.isEmpty { return }
        submenu.addItem(separatorRow(width: width))
        submenu.addItem(groupHeaderItem("Limit session", width: width))
        for item in items { submenu.addItem(item) }
    }

    func buildGrokSubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        statusGroup(submenu, acct: acct, width: width)

        // ─── Limit session group ───
        limitSessionGroup(submenu, acct: acct, width: width, monthly: true)
    }

    func buildAntigravitySubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        statusGroup(submenu, acct: acct, width: width)

        // ─── Limit session group ───
        if let windows = acct.usage_windows, !windows.isEmpty {
            submenu.addItem(separatorRow(width: width))
            submenu.addItem(groupHeaderItem(t("limit_session"), width: width))
            for w in windows {
                let groupLabel = w.group == "gemini" ? t("ag_group_gemini") : t("ag_group_other")
                let windowKey = w.window == "weekly" ? "weekly_limit" : "5h_limit"
                let pct = w.used_pct ?? 0
                let line = "\(groupLabel) · " + L10n.usedLine(windowKey, String(format: "%.1f", pct), reset: w.reset)
                submenu.addItem(infoItem(line, width: width, accentPercent: true, warnPercent: pct > 80))
            }
        } else {
            limitSessionGroup(submenu, acct: acct, width: width, primaryLabel: "tier_usage")
        }
    }

    func buildCopilotSubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        statusGroup(submenu, acct: acct, width: width)

        // ─── Limit session group ───
        limitSessionGroup(submenu, acct: acct, width: width, primaryLabel: "premium_requests")
    }

    func buildDevinSubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        statusGroup(submenu, acct: acct, width: width)

        // ─── Limit session group ───
        limitSessionGroup(submenu, acct: acct, width: width, weekly: true)
    }

    func detailLines(_ acct: Account) -> [String] {
        var lines: [String] = []
        if let plan = normalizePlan(acct), !plan.isEmpty {
            lines.append(L10n.label("plan", plan))
        }
        if let sub = subscriptionLine(acct) {
            lines.append(sub)
        }
        if let start = planStartText(acct) {
            lines.append(start)
        }
        if let reset = planResetText(acct) {
            lines.append(reset)
        }
        if let msg = acct.status_message, !msg.isEmpty {
            lines.append(L10n.label("status", msg))
        }
        if let exp = acct.token_expires {
            lines.append(L10n.label("token_expires", exp))
        }
        // Primary window (5h for codex/claude, 24h for xai, etc.)
        if let p = acct.primary_used_pct {
            let w1: String
            switch acct.provider {
            case "codex", "claude": w1 = "5h"
            case "xai": w1 = t("window_monthly")
            case "copilot": w1 = t("window_quota")
            case "antigravity": w1 = t("window_tier")
            default: w1 = t("window_win")
            }
            lines.append("\(w1) \(t("window_label")): \(String(format: "%.1f", p))% \(t("used"))")
            if let r = acct.primary_reset {
                lines.append("  \(t("reset")): \(r)")
            }
        }
        // Secondary window (7d for codex/claude, 24h tokens for xai)
        if let s = acct.secondary_used_pct {
            let w2: String
            switch acct.provider {
            case "codex", "claude": w2 = "7d"
            case "xai": w2 = t("window_24h_tokens")
            default: w2 = t("window_win")
            }
            lines.append("\(w2) \(t("window_label")): \(String(format: "%.1f", s))% \(t("used"))")
            if let r = acct.secondary_reset {
                lines.append("  \(t("reset")): \(r)")
            }
        }
        // Codex-specific details are rendered via buildCodexSubmenu (grouped).
        // Copilot-specific: SKU
        if acct.provider == "copilot" {
            if let sku = acct.sku {
                lines.append(L10n.label("sku", sku))
            }
            if let q = acct.limited_user_quotas {
                lines.append(L10n.label("quota_limit", q))
            }
            if let r = acct.limited_user_reset_date {
                lines.append(L10n.label("quota_reset", r))
            }
        }
        // Grok-specific: monthly credits
        if acct.provider == "xai" {
            if let used = acct.monthly_used, let limit = acct.monthly_limit {
                lines.append("\(t("monthly")): \(Int(used))/\(Int(limit)) \(t("credits"))")
            }
            if let r = acct.primary_reset ?? acct.monthly_period_end {
                lines.append("  \(t("reset")): \(r)")
            }
        }
        // Fallback for providers without window data
        if acct.primary_used_pct == nil {
            if let rem = acct.rate_limit_remaining {
                lines.append(L10n.label("remaining", rem))
            }
            if let reset = acct.rate_limit_reset {
                lines.append(L10n.label("reset", reset))
            }
            if let lim = acct.rate_limit_limit {
                lines.append(L10n.label("limit", lim))
            }
        }
        if let lp = acct.last_poll {
            lines.append(L10n.label("last_poll", lp))
        }
        if lines.isEmpty {
            lines.append(t("no_details"))
        }
        return lines
    }

    func providerDisplayName(_ provider: String) -> String {
        switch provider {
        case "xai": return "Grok"
        case "codex": return "Codex"
        case "claude": return "Claude"
        case "antigravity": return "Antigravity"
        case "copilot": return "Copilot"
        case "cursor": return "Cursor"
        case "devin": return "Devin"
        case "droid": return "Droid"
        case "opencode": return "Opencode"
        default: return provider.capitalized
        }
    }

    func addFooter(_ menu: NSMenu) {
        menu.addItem(actionItem(t("poll_now")) { [weak self] in self?.loader.runPoll() })
        menu.addItem(actionItem(t("manage_accounts")) { [weak self] in self?.loader.openPanel() })
        menu.addItem(actionItem(t("open_dashboard")) { [weak self] in self?.loader.runDashboard() })
        menu.addItem(actionItem(t("refresh_display")) { [weak self] in self?.loader.reload() })
        menu.addItem(submenuRow(t("add_new_agent"), submenu: addAgentSubmenu()))
        menu.addItem(submenuRow(t("language"), submenu: languageSubmenu()))
        menu.addItem(submenuRow(t("layout"), submenu: layoutSubmenu()))
        menu.addItem(infoItem(timezoneLabel()))
        menu.addItem(separatorRow())
        let quit = NSMenuItem(title: t("quit"), action: #selector(quitApp), keyEquivalent: "q")
        quit.keyEquivalentModifierMask = [.command]
        quit.target = self
        menu.addItem(quit)
    }

    func t(_ key: String) -> String { L10n.tr(key) }

    func languageSubmenu() -> NSMenu {
        let submenu = NSMenu()
        let w: CGFloat = 200
        for lang in Language.allCases {
            let active = (lang == language)
            submenu.addItem(actionItem(lang.nativeName, width: w, checkmark: active) { [weak self] in
                self?.setLanguage(lang)
            })
        }
        return submenu
    }

    func timezoneLabel() -> String {
        let seconds = TimeZone.current.secondsFromGMT()
        let sign = seconds < 0 ? "-" : "+"
        let hours = abs(seconds) / 3600
        let minutes = (abs(seconds) % 3600) / 60
        let offsetStr = minutes == 0
            ? "UTC\(sign)\(hours)"
            : String(format: "UTC%@%d:%02d", sign, hours, minutes)
        return "Time Zone: \(offsetStr)"
    }

    func setLanguage(_ lang: Language) {
        Language.current = lang
        language = lang
        L10n.lang = lang
        if let menu = statusItem.menu {
            menu.removeAllItems()
            buildMenu(menu)
        }
    }

    /// Layout experiment switcher (spec §2.1): both layouts with a checkmark
    /// on the active one; switching rebuilds the menu like setLanguage does.
    func layoutSubmenu() -> NSMenu {
        let submenu = NSMenu()
        let w: CGFloat = 200
        for layout in MenuLayout.allCases {
            let active = (layout == menuLayout)
            submenu.addItem(actionItem(t(layout.titleKey), width: w, checkmark: active) { [weak self] in
                self?.setMenuLayout(layout)
            })
        }
        return submenu
    }

    func setMenuLayout(_ layout: MenuLayout) {
        MenuLayout.current = layout
        menuLayout = layout
        if let menu = statusItem.menu {
            menu.removeAllItems()
            buildMenu(menu)
        }
    }

    func addAgentSubmenu() -> NSMenu {
        let submenu = NSMenu()
        let w: CGFloat = 200
        // Browser panel (opencodex-style): all OAuth add/reconnect/manage
        // happens in the accounts page the local server serves.
        submenu.addItem(actionItem(t("manage_accounts"), width: w) { [weak self] in self?.loader.openPanel() })
        // Devin uses an API key, not OAuth — keep the in-app secure prompt.
        submenu.addItem(separatorRow(width: w))
        submenu.addItem(actionItem("Devin (API key)…", width: w) { [weak self] in self?.loader.addAgent(provider: "devin") })
        return submenu
    }

    // ─── Row builders ─────────────────────────────────────────────────────
    // NSMenuItems get an empty title: the fixed-width view carries the text,
    // and a non-empty title would also feed NSMenu's width calculation,
    // widening the menu past the views when the string is long.
    func titleItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .title, accentNumbers: true)
        return item
    }

    func headerItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .header)
        return item
    }

    func groupHeaderItem(_ title: String, width: CGFloat = MenuRowLayout.width) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .groupHeader, width: width)
        return item
    }

    func bulletItem(_ title: String, width: CGFloat = MenuRowLayout.width) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .bullet, width: width)
        return item
    }

    func infoItem(_ title: String, width: CGFloat = MenuRowLayout.width,
                  accentPercent: Bool = false, warnPercent: Bool = false,
                  accentResetTime: Bool = false) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .info, accentPercent: accentPercent,
                                     warnPercent: warnPercent, accentResetTime: accentResetTime, width: width)
        return item
    }

    func warningItem(_ title: String, width: CGFloat = MenuRowLayout.width) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .warning, width: width)
        return item
    }

    func formatExpiryKST(_ raw: String?) -> String {
        guard let raw, !raw.isEmpty else { return "?" }
        if raw.hasSuffix(" KST") { return raw }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = parser.date(from: raw)
        if date == nil {
            parser.formatOptions = [.withInternetDateTime]
            date = parser.date(from: raw)
        }
        guard let date else { return raw }
        let out = DateFormatter()
        out.locale = Locale(identifier: "en_US_POSIX")
        out.timeZone = TimeZone(identifier: "Asia/Seoul")
        out.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return "\(out.string(from: date)) KST"
    }

    func formatCouponExpiry(_ raw: String?) -> String {
        let full = formatExpiryKST(raw)
        // "2026-07-12 11:11:10 KST" → "2026-07-12 11:11" (drop seconds and KST)
        if full.hasSuffix(" KST"), full.count >= 16 {
            return String(full.prefix(16))
        }
        return full
    }

    func couponExpiryDate(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let normalized = raw.replacingOccurrences(of: " KST", with: "")
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Seoul")
        for format in ["yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm"] {
            formatter.dateFormat = format
            if let date = formatter.date(from: normalized) {
                return date
            }
        }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = parser.date(from: raw) {
            return date
        }
        parser.formatOptions = [.withInternetDateTime]
        return parser.date(from: raw)
    }

    func couponEndsSoon(_ credit: ResetCredit) -> Bool {
        guard credit.status == "available",
              let expiry = couponExpiryDate(credit.expires_at) else { return false }
        let secondsLeft = expiry.timeIntervalSinceNow
        return secondsLeft > 0 && secondsLeft <= 3 * 24 * 60 * 60
    }

    func couponDotColor(_ credit: ResetCredit) -> NSColor {
        if couponEndsSoon(credit) {
            return .systemOrange
        }
        return credit.status == "available" ? .systemGreen : .systemGray
    }

    func couponBadge(_ credit: ResetCredit) -> String? {
        couponEndsSoon(credit) ? t("finishes_soon") : nil
    }

    func couponType(_ desc: String?) -> String {
        guard let desc, !desc.isEmpty else { return "?" }
        if desc.lowercased().contains("inviting") {
            return t("coupon_referral")
        }
        return t("coupon_usage")
    }

    func actionItem(_ title: String, width: CGFloat = MenuRowLayout.width,
                    checkmark: Bool = false, destructive: Bool = false,
                    action: @escaping () -> Void) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.view = FixedMenuRowView(title: title, style: .action, action: action,
                                     checkmark: checkmark, destructive: destructive, width: width)
        return item
    }

    func submenuRow(_ title: String, submenu: NSMenu, width: CGFloat = MenuRowLayout.width,
                    dotColor: NSColor? = nil, badge: String? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.submenu = submenu
        item.view = FixedMenuRowView(title: title, style: .submenu, submenu: submenu,
                                     dotColor: dotColor, badge: badge, width: width)
        return item
    }

    func separatorRow(width: CGFloat = MenuRowLayout.width) -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuSeparatorView(width: width)
        return item
    }

    func formatUpdated(_ iso: String) -> String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = parser.date(from: iso)
        if date == nil {
            parser.formatOptions = [.withInternetDateTime]
            date = parser.date(from: iso)
        }
        if date == nil {
            // generated_at has no timezone suffix; parse as local time.
            let local = DateFormatter()
            local.locale = Locale(identifier: "en_US_POSIX")
            local.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
            date = local.date(from: iso) ?? {
                local.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                return local.date(from: iso)
            }()
        }
        guard let date else { return iso }
        let out = DateFormatter()
        out.locale = Locale(identifier: "en_US")
        out.dateFormat = "MMMM d, yyyy HH:mm"
        return out.string(from: date)
    }
}
