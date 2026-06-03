# Desktop Remote SSH Hosts Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a VS Code Remote-SSH-style flow to Hermes Desktop so users can select an SSH host alias, start or reuse `hermes dashboard --tui --no-open --host 127.0.0.1 --port <port>` on the remote machine, and connect the desktop app without manually SSHing, tmuxing, copying the session token, and pasting it into Settings.

**Architecture:** Keep the existing remote URL/token path intact. Add an optional `ssh` remote source that uses the local `ssh` binary and the user's native SSH config first (`ssh <host-alias> ...`). The desktop main process owns SSH bootstrap, remote command execution, local port forwarding, token capture, and reconnect; the renderer only exposes a small Remote Hosts UI.

**Tech Stack:** Electron main process (`apps/desktop/electron/main.cjs`), preload IPC (`electron/preload.cjs`), React settings UI (`src/app/settings/gateway-settings.tsx`), existing dashboard backend (`hermes dashboard --tui`), Node child_process, tests under `apps/desktop/electron/*.test.cjs`.

---

## Key decisions

1. **Use native SSH config first.** Host input is usually a `~/.ssh/config` alias, e.g. `oracle-hermes`. We run `ssh oracle-hermes ...`; no need to store username/key/port unless a future fallback UI is added.
2. **Bind dashboard to remote loopback only.** Remote command should use `--host 127.0.0.1`, never `0.0.0.0`, and expose it to desktop via local port forwarding.
3. **Use desktop-generated token.** Instead of scraping the dashboard UI token, set `HERMES_DASHBOARD_SESSION_TOKEN` in the remote command. This matches local desktop boot and avoids token scraping.
4. **Use per-instance ports by default.** SSH mode should default remote port to `0` / auto. The desktop asks the remote host for a free loopback port, starts that dashboard, and forwards a separately picked local port. This lets multiple macOS desktop windows connect without fighting over remote `9119`.
5. **Use SSH local forwarding.** Start a persistent SSH process like: `ssh -N -L 127.0.0.1:<localPort>:127.0.0.1:<remotePort> <host>`. Desktop connects to `http://127.0.0.1:<localPort>` with the generated token.
6. **No tmux required.** The remote dashboard runs under the SSH session. If SSH drops, desktop reconnects and restarts it.
7. **Do not disturb manual remote mode.** Existing URL/token settings continue working.

---

## Task 1: Add SSH remote config shape

**Objective:** Extend connection config with `source: 'manual' | 'ssh'` and an `ssh` object without breaking existing manual remotes.

**Files:**
- Modify: `apps/desktop/electron/main.cjs`
- Modify: `apps/desktop/src/app/settings/gateway-settings.tsx`
- Modify: `apps/desktop/src/global.d.ts` or the desktop type file if IPC types live elsewhere

**Implementation notes:**
- Existing config remains valid: `{ mode: 'remote', remote: { url, token } }`.
- New saved shape:
  ```json
  {
    "mode": "remote",
    "remote": { "url": "http://127.0.0.1:49152", "token": { "encoding": "safeStorage", "value": "..." } },
    "remoteSource": "ssh",
    "ssh": {
      "host": "oracle-hermes",
      "remotePort": 0,
      "command": "hermes dashboard --no-open --tui --host 127.0.0.1 --port {port} --skip-build"
    }
  }
  ```
- `sanitizeDesktopConnectionConfig()` should return `remoteSource`, `sshHost`, `sshRemotePort`, and `sshCommand`.

**Verification:** Existing remote URL/token settings load exactly as before.

---

## Task 2: Add safe SSH host validation helpers

**Objective:** Prevent command injection in the SSH host alias and remote port fields.

**Files:**
- Modify: `apps/desktop/electron/main.cjs`
- Test: `apps/desktop/electron/remote-ssh.test.cjs` or existing relevant test file

**Implementation notes:**
- Host alias regex: `/^[A-Za-z0-9._@:-]+$/` is acceptable for SSH aliases, `user@host`, IPv4, IPv6-ish host forms, and ports in aliases.
- Reject whitespace and shell metacharacters.
- Remote port: integer `1..65535`, default `9119`.
- Do not shell-join SSH arguments. Use `spawn('ssh', args)`.

**Verification:** Tests cover accepted `oracle-hermes`, `ubuntu@1.2.3.4`, rejected `host; rm -rf /`, and invalid ports.

---

## Task 3: Implement SSH bootstrap in Electron main process

**Objective:** Add `connectSshRemoteBackend(config)` that starts a forwarded remote dashboard and returns the same connection object as manual remote mode.

**Files:**
- Modify: `apps/desktop/electron/main.cjs`

**Implementation outline:**
- Pick `localPort = await pickPort()`.
- Generate `token = crypto.randomBytes(32).toString('base64url')` unless saved token is still usable.
- Start forward process:
  ```js
  ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:${localPort}:127.0.0.1:${remotePort} ${host}
  ```
- Start remote dashboard command over a short SSH exec:
  ```bash
  HERMES_DASHBOARD_SESSION_TOKEN='<token>' HERMES_DASHBOARD_TUI=1 nohup hermes dashboard --no-open --tui --host 127.0.0.1 --port <remotePort> --skip-build > ~/.hermes/logs/desktop-remote-dashboard.log 2>&1 &
  ```
- Use `spawn('ssh', [host, remoteCommand])` for remote command. Quote only inside the remote command with a small `shQuote()` helper.
- Wait for local forwarded `http://127.0.0.1:<localPort>/api/status` using existing `waitForHermes(baseUrl, token)`.
- Return `{ baseUrl, mode: 'remote', source: 'ssh', token, wsUrl, logs, ...getWindowState() }`.

**Verification:** A fake `ssh` binary test can assert arguments include `-N`, `ExitOnForwardFailure=yes`, `-L`, and the host alias as a separate arg.

---

## Task 4: Wire SSH mode into `startHermes()`

**Objective:** Choose SSH bootstrap when saved config says `remoteSource === 'ssh'`.

**Files:**
- Modify: `apps/desktop/electron/main.cjs`

**Implementation notes:**
- `resolveRemoteBackend()` should keep manual remote behavior.
- Add `resolveSshRemoteBackendConfig()` or branch in `startHermes()` before manual remote.
- Env vars `HERMES_DESKTOP_REMOTE_URL/TOKEN` should continue to override everything.
- `resetHermesConnection()` must kill the SSH tunnel process if present.

**Verification:** Manual remote tests still pass; new SSH config test returns local forwarded base URL.

---

## Task 5: Expose SSH IPC methods

**Objective:** Let the renderer test and apply SSH host connections.

**Files:**
- Modify: `apps/desktop/electron/preload.cjs`
- Modify: `apps/desktop/electron/main.cjs`

**IPC:**
- `hermes:connection-config:test` accepts `{ mode: 'remote', remoteSource: 'ssh', sshHost, sshRemotePort, sshCommand? }`.
- Existing `saveConnectionConfig` and `applyConnectionConfig` accept the same payload.

**Verification:** Calling test with SSH payload does not require a pasted session token.

---

## Task 6: Add Remote SSH UI in Gateway Settings

**Objective:** Add a mode selector inside Remote Gateway settings: Manual URL/token vs SSH Host.

**Files:**
- Modify: `apps/desktop/src/app/settings/gateway-settings.tsx`

**UI:**
- Existing cards: Local gateway / Remote gateway remain.
- When Remote is selected, show subchoice:
  - Manual URL + session token
  - SSH host
- SSH host fields:
  - Host alias: placeholder `oracle-hermes` or `ubuntu@my-box`
  - Remote port: default `9119`
  - Advanced command: default `hermes dashboard --no-open --tui --host 127.0.0.1 --port 9119 --skip-build`
- Buttons become:
  - Test SSH host
  - Save for next restart
  - Save and reconnect

**Copy:**
- Explain that Hermes uses your local SSH config first: `~/.ssh/config`, keys, agents, ProxyJump, etc.

**Verification:** Manual URL/token UI still works and SSH UI allows apply without token input.

---

## Task 7: Add tests

**Objective:** Cover config coercion, validation, and SSH command argument construction.

**Files:**
- Create or modify: `apps/desktop/electron/remote-ssh.test.cjs`
- Possibly modify: `apps/desktop/package.json` test script if needed

**Cases:**
1. Manual remote payload still requires token.
2. SSH remote payload requires host but not token.
3. SSH host validation rejects shell metacharacters.
4. SSH port validation rejects invalid ports.
5. SSH tunnel args are split safely, not shell-concatenated.
6. Sanitized config includes SSH fields but never exposes token value.

**Run:**
```bash
cd apps/desktop
npm run test:desktop:platforms
npm run lint
```

---

## Task 8: Final verification and commit

**Objective:** Verify the feature and commit only intended files.

**Commands:**
```bash
git status --short
cd apps/desktop && npm run test:desktop:platforms
cd apps/desktop && npm run lint
git add apps/desktop/electron/main.cjs apps/desktop/electron/preload.cjs apps/desktop/src/app/settings/gateway-settings.tsx apps/desktop/src/global.d.ts apps/desktop/electron/remote-ssh.test.cjs .hermes/plans/desktop-remote-ssh-hosts.md
git commit -m "feat: add desktop remote ssh gateway bootstrap"
```

**Acceptance criteria:**
- User can select Remote → SSH host, enter a native SSH alias, and reconnect without a session token.
- Desktop starts/forwards `hermes dashboard --no-open --tui --host 127.0.0.1 --port 9119` remotely.
- Desktop connects through local forwarded URL with `X-Hermes-Session-Token`.
- Existing manual remote URL/token flow still works.
- Existing local desktop backend flow still works.
