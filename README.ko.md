<p align="center">
  <img src="./public/assets/readme/token-status-bar-icon.png" alt="투명 배경의 토큰 상태 표시줄 앱 아이콘" width="140">
</p>

<h1 align="center">Token Status Bar</h1>

<p align="center">
  <em>여러 AI 코딩 에이전트 계정의 실시간 토큰 / 쿼터 상태를 macOS 메뉴 막대 한 곳에서.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.0.1-111111?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/macOS-14%2B-111111?style=flat-square" alt="macOS 14+">
  <img src="https://img.shields.io/badge/Swift-menu%20bar-111111?style=flat-square" alt="Swift menu bar">
  <img src="https://img.shields.io/badge/Python-3.9%2B-111111?style=flat-square" alt="Python 3.9+">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="License: MIT"></a>
</p>

<p align="center">
  <sub><a href="./README.md">English</a> &middot; <a href="./README.ko.md">한국어</a></sub>
</p>

<p align="center">
  <a href="https://github.com/bytonylee/token-status-bar/releases/latest/download/TokenStatusBar.dmg"><img src="./public/assets/readme/download-macos.png" alt="Mac OS용 TokenStatusBar.dmg 다운로드" width="270"></a>
</p>

<p align="center">
  <img src="./public/assets/readme/token-status-bar-hero.png" alt="macOS 메뉴 막대에서 여러 AI 제공자의 토큰 쿼터 상태를 보여주는 토큰 상태 표시줄 히어로 배너" width="720">
</p>

---

> *토큰 상태 표시줄은 보유한 모든 AI 코딩 에이전트 계정 — OpenAI Codex, Anthropic
> Claude, xAI / Grok, Google Antigravity, GitHub Copilot, Devin — 의 실시간
> 토큰과 쿼터 상태를 하나의 macOS 메뉴 막대에서 보여줍니다. Python 백엔드가
> 각 제공자의 사용량 API를 폴링하고, 가벼운 Swift 메뉴 막대 앱이 이를
> 표시합니다.*

메뉴를 열면 제공자별로 묶인 초록 / 노랑 / 빨강 가용성 점을 볼 수 있고,
계정별 하위 메뉴로 들어가거나 **Poll Now**로 즉시 갱신할 수 있습니다.

> 여러 에이전트 계정을 함께 쓰면서, 어떤 계정에 아직 쿼터가 남았는지, 언제
> 리셋되는지, 어떤 토큰이 곧 만료하는지 한눈에 보고 싶은 사람을 위해
> 만들었습니다 — 대시보드를 따로 열 필요 없이.

**백엔드는 launchd 데몬으로 5분마다 폴링해 `status.json`을 기록하고, Swift
앱은 30초마다 이를 읽습니다. 온보딩은 `Add New Agent` 메뉴 한 번으로
터미널에서 OAuth를 실행합니다. 진짜 API가 있으면 스크래핑하지 않습니다.**

## 기능

- 제공자별로 묶인 드롭다운 메뉴와 계정마다 초록 / 노랑 / 빨강 가용성 점 표시.
- 계정별 하위 메뉴에 요금제, 상태, 토큰 만료, 쿼터 윈도우 표시.
- 지원하는 모든 제공자의 실시간 쿼터 조회(API가 있는 경우 스크래핑하지 않음).
- 백그라운드 폴러(launchd 데몬, 5분 주기)와 즉시 실행용 **Poll Now**.
- **Add New Agent** 한 번으로 OAuth / API 키 온보딩을 터미널에서 실행.

## 지원 제공자

| 제공자             | 키            | 인증 방식        |
|--------------------|---------------|------------------|
| OpenAI Codex       | `codex`       | OAuth (브라우저) |
| Anthropic Claude   | `claude`      | OAuth (브라우저) |
| xAI / Grok         | `xai`         | OAuth (브라우저) |
| Google Antigravity | `antigravity` | OAuth (브라우저) |
| GitHub Copilot     | `copilot`     | OAuth (디바이스 플로우) |
| Devin              | `devin`       | API 키           |

## 동작 방식

두 개의 파이프라인이 있습니다. **온보딩**(OAuth로 계정 연결)은 토큰을
`pool.db`에 저장하고, **폴링**은 그 토큰으로 각 제공자의 쿼터 API를 호출해
앱이 표시할 `status.json`을 생성합니다.

### 흐름도

```mermaid
flowchart TD
    A["Add New Agent<br/>(메뉴 또는 CLI)"] --> B[pool.py cmd_add]
    B --> C{인증 방식?}
    C -->|OAuth 브라우저| D[PKCE 플로우]
    C -->|디바이스 플로우| E[디바이스 코드]
    C -->|API 키| F[Devin API 키]
    D --> G[토큰 + 이메일 + 요금제]
    E --> G
    F --> G
    G --> H[(pool.db)]

    I[launchd 데몬<br/>5분 주기] --> J[poller.run_loop]
    K["Poll Now<br/>(즉시 실행)"] --> L[poller.run_once]
    J --> M[각 계정마다]
    L --> M
    M --> N{토큰 만료 임박?}
    N -->|예| O[토큰 갱신]
    O --> H
    N -->|아니오| P[제공자 쿼터 API]
    P --> Q[잔여 % + 리셋 시간 파싱]
    Q --> R[(pool.db 스냅샷)]
    R --> S[status.json]
    S --> T["MenuBarAgent.app<br/>30초마다 읽기"]
    T --> U[메뉴 막대 드롭다운 + 점]
```

### 온보딩 — OAuth 연결

```mermaid
flowchart TD
    A["Add New Agent (메뉴) /<br/>pool.py add &lt;provider&gt; (CLI)"] --> B[pool.py cmd_add]
    B --> C{인증 방식}
    C -->|OAuth 브라우저<br/>codex/claude/xai/antigravity| D1["authorize URL + PKCE verifier 생성<br/>로컬 콜백 서버 시작<br/>open_browser → 사용자 승인"]
    C -->|디바이스 플로우<br/>copilot| D2["POST device/code → user_code + verification_uri<br/>코드 표시, 브라우저 열기<br/>인증될 때까지 토큰 엔드포인트 폴링"]
    C -->|API 키<br/>devin| D3["사용자가 API 키 입력<br/>Devin API로 검증"]
    D1 --> E["access + refresh token"]
    D2 --> E
    D3 --> E
    E --> F["oauth.LOGIN_FUNCS 반환<br/>토큰 + 이메일 + 요금제"]
    F --> G["store.upsert_account +<br/>store.save_token"]
    G --> H[(pool.db)]
```

### 폴링 — 쿼터 정보 가져오기

```mermaid
flowchart TD
    A["launchd 데몬 (5분 주기)"] --> B[pool.py poll-loop → poller.run_loop]
    C["Poll Now (즉시 실행)"] --> D[pool.py poll → poller.run_once]
    B --> E[store.list_accounts 각 계정마다]
    D --> E
    E --> F[token = store.get_token]
    F --> G{"토큰 만료 임박 (< 1h)?"}
    G -->|예| H["oauth.REFRESH_FUNCS → 갱신된 토큰 저장"]
    H --> I[(pool.db)]
    G -->|아니오| J["POLLERS[provider](token) ─HTTPS─► 제공자 쿼터 API"]
    J --> K["잔여 % + 리셋 시간 파싱"]
    K --> L[store.save_snapshot]
    L --> I
    I --> M[status.cmd_export → status.json]
    M --> N["MenuBarAgent.app 30초마다 읽기 → 드롭다운 UI + 점"]
```

## 요구 사항

- macOS 14 (Sonoma) 이상 — 앱의 `LSMinimumSystemVersion`은 14.0입니다.
- 앱 빌드를 위한 Xcode 커맨드라인 도구(`swiftc`).
- 폴링 백엔드용 Python 3.9 이상.

## 구성

| 경로                 | 용도 |
|----------------------|------|
| `app/MenuBarAgent.swift` | 단일 파일 Swift 메뉴 막대 UI. |
| `build-app.sh`       | `MenuBarAgent.app` 컴파일 및 번들링. |
| `backend/pool.py`    | CLI: 온보딩, 폴링, 상태 내보내기. |
| `backend/poller.py`  | 제공자별 실시간 쿼터 폴링. |
| `backend/status.py`  | 앱이 읽을 `status.json` 생성. |
| `backend/store.py`   | SQLite 저장소 (`pool.db`). |
| `backend/oauth.py`   | 제공자별 OAuth / 디바이스 플로우 로그인. |
| `status.json`        | 메뉴 막대 앱이 사용하는 스냅샷 (git 무시). |
| `pool.db`            | SQLite 계정/쿼터 저장소 (git 무시). |

데이터는 기본적으로 `~/solo/token-status-bar/` 아래에 저장됩니다(`pool.db`,
`status.json`). `AGENT_POOL_DB`, `AGENT_POOL_STATUS_JSON` 환경 변수로 경로를
바꿀 수 있습니다.

## 앱 빌드 및 실행

```bash
./build-app.sh
open MenuBarAgent.app
```

앱은 30초마다 `status.json`을 읽고 메뉴 막대에 차트 아이콘을 표시합니다.

## CLI 사용법

```bash
python3 backend/pool.py add <provider> [label]   # OAuth 온보딩 (codex|claude|xai|antigravity|copilot)
python3 backend/pool.py add-devin <api_key> [label]
python3 backend/pool.py list                     # 모든 계정 목록
python3 backend/pool.py remove <account_id>
python3 backend/pool.py status                   # 계정 + 최신 한도 상태
python3 backend/pool.py poll                     # 1회 폴링 (모든 API 호출)
python3 backend/pool.py poll-loop                # 폴러 데몬 실행 (5분 주기)
python3 backend/pool.py refresh <account_id>     # 토큰 1개 갱신
python3 backend/pool.py refresh-all              # 만료 예정 토큰 전체 갱신
python3 backend/pool.py export-status            # status.json 작성
```

## 백그라운드 폴러 (launchd)

`~/Library/LaunchAgents/com.tonye.agentpool-poller.plist`의 launchd 에이전트가
`backend/pool.py poll-loop`를 실행하며 `status.json`을 최신 상태로 유지합니다.

`poller.py`를 수정한 뒤에는 새 코드를 반영하도록 데몬을 다시 시작하세요:

```bash
launchctl kickstart -k "gui/$(id -u)/com.tonye.agentpool-poller"
```

## Poll Now vs Refresh Display

- **Poll Now** — 모든 제공자의 API를 직접 호출해 `pool.db`와 `status.json`을
  갱신한 뒤 다시 불러옵니다. 느리지만 최신 수치를 가져옵니다.
- **Refresh Display** — 디스크에 저장된 `status.json`만 다시 읽습니다.
  즉시 반영되며 네트워크를 사용하지 않습니다.

## 라이선스

[MIT](./LICENSE)
