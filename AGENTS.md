# AGENTS.md

Project-specific guidance for agents working in this repo.

## Build & Release

- After updating any code, build the app before opening a PR.
- Before opening a PR, always build the `.dmg` first and confirm it
  succeeds. Do not open a PR if the `.dmg` build fails.
- Build commands live in `build.sh`. Run it to produce the `.app`,
  then produce the `.dmg` from the bundled `.app`.
- After building a new `.app`, reopen it so the running menu-bar app
  reflects the latest code. Quit the old instance first
  (`osascript -e 'tell application "TokenStatusBar" to quit'`), then
  `open /Applications/TokenStatusBar.app`.
