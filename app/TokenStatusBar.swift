import Cocoa
import SwiftUI

// ─── Models ───────────────────────────────────────────────────────────────
struct StatusPayload: Codable {
    var generated_at: String
    var account_count: Int
    var accounts: [Account]
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
    var monthly_period_end: String?
    var reset_credits: [ResetCredit]?
    var last_poll: String?
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
    var github_email: String?
    var github_name: String?
    var tier_override: String?
}

struct ResetCredit: Codable {
    var title: String?
    var status: String?
    var expires_at: String?
    var granted_at: String?
    var description: String?
}

// ─── Status Loader ────────────────────────────────────────────────────────
class StatusLoader: ObservableObject {
    @Published var payload: StatusPayload?
    @Published var lastError: String?
    private var timer: Timer?

    private let dataDir: String
    let statusURL: URL

    init() {
        let dataDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("solo/token-status-bar/secrets").path
        self.dataDir = dataDir
        self.statusURL = URL(fileURLWithPath: dataDir).appendingPathComponent("status.json")
    }

    func start() {
        reload()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in
            self.reload()
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    func reload() {
        guard FileManager.default.fileExists(atPath: statusURL.path) else {
            DispatchQueue.main.async {
                self.lastError = "No status.json yet. Run: pool.py poll"
            }
            return
        }
        do {
            let data = try Data(contentsOf: statusURL)
            let decoded = try JSONDecoder().decode(StatusPayload.self, from: data)
            DispatchQueue.main.async {
                self.payload = decoded
                self.lastError = nil
            }
        } catch {
            DispatchQueue.main.async {
                self.lastError = "Parse error: \(error.localizedDescription)"
            }
        }
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
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("solo/token-status-bar/backend/pool.py").path
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
        let envPrefix = "AGENT_POOL_DB=\"\(dataDir)/pool.db\" AGENT_POOL_STATUS_JSON=\"\(dataDir)/status.json\""
        if let py = bundledPython, let pool = bundledPoolPy {
            return "\(envPrefix) \"\(py)\" \"\(pool)\" \(args.joined(separator: " "))"
        } else {
            let dir = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("solo/token-status-bar/backend").path
            return "cd \(dir) && \(envPrefix) python3 pool.py \(args.joined(separator: " "))"
        }
    }

    func runPoll() {
        DispatchQueue.global(qos: .userInitiated).async {
            let task = self.poolProcess(["poll"])
            try? task.run()
            task.waitUntilExit()
            DispatchQueue.main.async {
                self.reload()
            }
        }
    }

    func addAgent(provider: String) {
        // Devin needs an interactive API key, so keep Terminal for it.
        if provider == "devin" {
            let cmd = poolShellCommand(["add-devin"])
            let script = """
            tell application "Terminal"
                activate
                do script "\(cmd)"
            end tell
            """
            let task = Process()
            task.launchPath = "/usr/bin/osascript"
            task.arguments = ["-e", script]
            try? task.run()
            return
        }
        // Other providers use browser OAuth — run in background, then
        // reload after the backend exports status.json.
        DispatchQueue.global(qos: .userInitiated).async {
            let task = self.poolProcess(["add", provider])
            try? task.run()
            task.waitUntilExit()
            DispatchQueue.main.async {
                self.reload()
            }
        }
    }

    func deleteAgent(accountId: Int) {
        DispatchQueue.global(qos: .userInitiated).async {
            let task = self.poolProcess(["remove", "\(accountId)"])
            try? task.run()
            task.waitUntilExit()
            DispatchQueue.main.async {
                self.reload()
            }
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
    private let accentNumbers: Bool
    private let accentPercent: Bool
    private let accentResetTime: Bool
    private let checkmark: Bool
    private let destructive: Bool
    private let rawTitle: String
    private let badgeText: String?
    private let label = NSTextField(labelWithString: "")
    private let chevron = NSTextField(labelWithString: "›")
    private let check = NSTextField(labelWithString: "✓")
    private let badgeLabel = NSTextField(labelWithString: "")
    private let dotLayer = CALayer()
    private let highlightLayer = CALayer()
    private var hovered = false

    init(title: String, style: Style, action: (() -> Void)? = nil, submenu: NSMenu? = nil,
         dotColor: NSColor? = nil, accentNumbers: Bool = false, accentPercent: Bool = false,
         accentResetTime: Bool = false,
         checkmark: Bool = false, badge: String? = nil, destructive: Bool = false,
         width: CGFloat = MenuRowLayout.width) {
        self.style = style
        self.action = action
        self.submenu = submenu
        self.dotColor = dotColor
        self.accentNumbers = accentNumbers
        self.accentPercent = accentPercent
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
            dotLayer.backgroundColor = dotColor.cgColor
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
                attr.addAttribute(.foregroundColor, value: NSColor.systemGreen,
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
            "refresh_display": "Refresh Display",
            "add_new_agent": "Add New Agent",
            "language": "Language",
            "quit": "Quit",
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
            "remaining": "Remaining",
            "reset": "Reset",
            "limit": "Limit",
            "monthly": "Monthly",
            "sku": "SKU",
            "quota_limit": "Quota limit",
            "quota_reset": "Quota reset",
            // ── Messages ──
            "no_details": "No details",
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
            "monthly_limit": "monthly limit",
            "daily_tokens": "daily tokens",
            "tier_usage": "tier usage",
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
        ],
        .ko: [
            // ── Footer ──
            "poll_now": "지금 업데이트",
            "refresh_display": "표시 새로고침",
            "add_new_agent": "새 에이전트 추가",
            "language": "언어",
            "quit": "종료",
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
            "remaining": "남음",
            "reset": "리셋",
            "limit": "한도",
            "monthly": "월간",
            "sku": "SKU",
            "quota_limit": "할당량 한도",
            "quota_reset": "할당량 리셋",
            // ── Messages ──
            "no_details": "상세 정보 없음",
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
            "monthly_limit": "월간 제한",
            "daily_tokens": "일일 토큰",
            "tier_usage": "티어 사용량",
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
        ],
        .zh: [
            // ── Footer ──
            "poll_now": "立即轮询",
            "refresh_display": "刷新显示",
            "add_new_agent": "添加新代理",
            "language": "语言",
            "quit": "退出",
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
            "remaining": "剩余",
            "reset": "重置",
            "limit": "限额",
            "monthly": "每月",
            "sku": "SKU",
            "quota_limit": "配额限制",
            "quota_reset": "配额重置",
            // ── Messages ──
            "no_details": "无详情",
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
            "monthly_limit": "每月限制",
            "daily_tokens": "每日令牌",
            "tier_usage": "层级用量",
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
        ],
        .ja: [
            // ── Footer ──
            "poll_now": "今すぐポーリング",
            "refresh_display": "表示を更新",
            "add_new_agent": "新しいエージェントを追加",
            "language": "言語",
            "quit": "終了",
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
            "remaining": "残り",
            "reset": "リセット",
            "limit": "上限",
            "monthly": "月間",
            "sku": "SKU",
            "quota_limit": "クォータ上限",
            "quota_reset": "クォータリセット",
            // ── Messages ──
            "no_details": "詳細なし",
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
            "monthly_limit": "月間制限",
            "daily_tokens": "日次トークン",
            "tier_usage": "ティア使用量",
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

    func applicationDidFinishLaunching(_ notification: Notification) {
        language = Language.current
        L10n.lang = language
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if let image = NSImage(systemSymbolName: "chart.bar.fill", accessibilityDescription: "Agent Pool") {
                image.isTemplate = true
                button.image = image
            } else {
                button.title = "AP"
            }
        }

        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        loader.start()

        NSApp.setActivationPolicy(.accessory)
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

    func buildMenu(_ menu: NSMenu) {
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

        // Header
        menu.addItem(titleItem("Agent Pool: \(payload.account_count) accounts"))
        menu.addItem(infoItem("\(t("updated")): \(formatUpdated(payload.generated_at))"))
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

        addFooter(menu)
    }

    private func statusColor(_ status: String) -> NSColor {
        switch status {
        case "active": return .systemGreen
        case "error": return .systemRed
        case "expired": return .systemYellow
        default: return .tertiaryLabelColor
        }
    }

    func accountItem(_ acct: Account) -> NSMenuItem {
        var title = acct.email ?? acct.label ?? "unknown"
        if acct.provider == "copilot", let mail = acct.github_email, !mail.isEmpty {
            title = "\(acct.email ?? acct.label ?? "unknown") (\(mail))"
        }
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
        submenu.addItem(separatorRow(width: detailWidth))
        submenu.addItem(actionItem(t("delete_agent"), width: detailWidth, destructive: true) { [weak self] in
            self?.loader.confirmDeleteAgent(acct: acct)
        })
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.submenu = submenu
        item.view = FixedMenuRowView(title: title, style: .submenu, submenu: submenu,
                                     dotColor: statusColor(acct.status), badge: weeklyBadge(acct))
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

    /// Hours until the weekly (secondary) limit resets, when that reset is
    /// within the next 24h. Returns nil for providers whose secondary window
    /// is not a weekly limit (xai/copilot/antigravity), or when data is
    /// missing/stale.
    func weeklyResetHoursLeft(_ acct: Account) -> Double? {
        guard acct.provider == "codex" || acct.provider == "claude" || acct.provider == "devin" else { return nil }
        guard acct.secondary_used_pct != nil,
              let resetStr = acct.secondary_reset, !resetStr.isEmpty else { return nil }
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = TimeZone(identifier: "Asia/Seoul")
        fmt.dateFormat = "yyyy-MM-dd HH:mm"
        guard let reset = fmt.date(from: resetStr) else { return nil }
        let hours = reset.timeIntervalSinceNow / 3600.0
        guard hours > 0, hours <= 24 else { return nil }
        return hours
    }

    /// Compact top-level badge shown when the weekly limit finishes soon.
    func weeklyBadge(_ acct: Account) -> String? {
        guard weeklyResetHoursLeft(acct) != nil else { return nil }
        return t("finishes_soon")
    }

    func buildCodexSubmenu(_ submenu: NSMenu, acct: Account, width: CGFloat) {
        // ─── Status group ───
        submenu.addItem(groupHeaderItem("Status", width: width))
        if let line = planText(acct) {
            submenu.addItem(infoItem(line, width: width))
        }
        if let start = acct.plan_start, !start.isEmpty {
            submenu.addItem(infoItem(L10n.label("plan_started", start), width: width))
        }
        if let pr = acct.plan_reset, !pr.isEmpty {
            submenu.addItem(infoItem(L10n.label("plan_resets", pr), width: width))
        }
        if let exp = acct.token_expires {
            submenu.addItem(infoItem(L10n.label("token_expires", exp), width: width))
        }
        if let lp = acct.last_poll {
            submenu.addItem(infoItem(L10n.label("last_poll", lp), width: width))
        }

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
        submenu.addItem(groupHeaderItem("Status", width: width))
        if let line = planText(acct) {
            submenu.addItem(infoItem(line, width: width))
        }
        if let start = acct.plan_start, !start.isEmpty {
            submenu.addItem(infoItem(L10n.label("plan_started", start), width: width))
        }
        if let pr = acct.plan_reset, !pr.isEmpty {
            submenu.addItem(infoItem(L10n.label("plan_resets", pr), width: width))
        }
        if let exp = acct.token_expires {
            submenu.addItem(infoItem(L10n.label("token_expires", exp), width: width))
        }
        if let lp = acct.last_poll {
            submenu.addItem(infoItem(L10n.label("last_poll", lp), width: width))
        }

        // ─── Limit session group ───
        limitSessionGroup(submenu, acct: acct, width: width, fiveHour: true, weekly: true)
    }

    private func statusGroup(_ submenu: NSMenu, acct: Account, width: CGFloat, extra: [(String, String)] = []) {
        submenu.addItem(groupHeaderItem("Status", width: width))
        if let line = planText(acct) {
            submenu.addItem(infoItem(line, width: width))
        }
        if let start = acct.plan_start, !start.isEmpty {
            submenu.addItem(infoItem(L10n.label("plan_started", start), width: width))
        }
        if let pr = acct.plan_reset, !pr.isEmpty {
            submenu.addItem(infoItem(L10n.label("plan_resets", pr), width: width))
        }
        if let exp = acct.token_expires {
            submenu.addItem(infoItem(L10n.label("token_expires", exp), width: width))
        }
        if let lp = acct.last_poll {
            submenu.addItem(infoItem(L10n.label("last_poll", lp), width: width))
        }
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
                                  accentResetTime: weeklyResetHoursLeft(acct) != nil))
        }
        if monthly, let p = acct.primary_used_pct {
            items.append(infoItem(L10n.usedLine("monthly_limit", String(format: "%.1f", p), reset: acct.primary_reset),
                                  width: width, accentPercent: true))
        }
        if let label = primaryLabel, let p = acct.primary_used_pct {
            items.append(infoItem(L10n.usedLine(label, String(format: "%.1f", p), reset: acct.primary_reset),
                                  width: width, accentPercent: true))
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
        limitSessionGroup(submenu, acct: acct, width: width, primaryLabel: "tier_usage")
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
        // Devin-specific: plan reset date
        if acct.provider == "devin" {
            if let pr = acct.plan_reset {
                lines.append(L10n.label("plan_resets", pr))
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
        menu.addItem(actionItem(t("refresh_display")) { [weak self] in self?.loader.reload() })
        menu.addItem(submenuRow(t("add_new_agent"), submenu: addAgentSubmenu()))
        menu.addItem(submenuRow(t("language"), submenu: languageSubmenu()))
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
        let offset = TimeZone.current.secondsFromGMT() / 3600
        let offsetStr = offset >= 0 ? "UTC+\(offset)" : "UTC\(offset)"
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

    func addAgentSubmenu() -> NSMenu {
        let submenu = NSMenu()
        let w: CGFloat = 200
        // OAuth (browser/device-flow) providers
        let oauthProviders = [("Codex", "codex"), ("Claude", "claude"), ("Grok", "xai"),
                              ("Antigravity", "antigravity"), ("GitHub Copilot", "copilot")]
        for (title, provider) in oauthProviders {
            submenu.addItem(actionItem(title, width: w) { [weak self] in self?.loader.addAgent(provider: provider) })
        }
        // Devin uses an API key, not OAuth
        submenu.addItem(separatorRow(width: w))
        submenu.addItem(actionItem("Devin (API key)…", width: w) { [weak self] in self?.loader.addAgent(provider: "devin") })
        return submenu
    }

    // ─── Row builders ─────────────────────────────────────────────────────
    func titleItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .title, accentNumbers: true)
        return item
    }

    func headerItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .header)
        return item
    }

    func groupHeaderItem(_ title: String, width: CGFloat = MenuRowLayout.width) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .groupHeader, width: width)
        return item
    }

    func bulletItem(_ title: String, width: CGFloat = MenuRowLayout.width) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .bullet, width: width)
        return item
    }

    func infoItem(_ title: String, width: CGFloat = MenuRowLayout.width,
                  accentPercent: Bool = false, accentResetTime: Bool = false) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        item.view = FixedMenuRowView(title: title, style: .info, accentPercent: accentPercent,
                                     accentResetTime: accentResetTime, width: width)
        return item
    }

    func warningItem(_ title: String, width: CGFloat = MenuRowLayout.width) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
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
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.view = FixedMenuRowView(title: title, style: .action, action: action,
                                     checkmark: checkmark, destructive: destructive, width: width)
        return item
    }

    func submenuRow(_ title: String, submenu: NSMenu, width: CGFloat = MenuRowLayout.width,
                    dotColor: NSColor? = nil, badge: String? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
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
