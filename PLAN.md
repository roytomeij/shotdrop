# Briefing: ShotDrop — screenshot-to-remote for iTerm2

## Goal

Build an iTerm2 Python API script that lets me press a hotkey in an iTerm2 tab
that's SSH'd into a remote Mac, select a screen area, and have the screenshot
uploaded to that same remote host. The remote file path is then inserted into
the terminal so Claude Code (running on the remote) attaches the image.

This exists because Claude Code's Ctrl+V image paste reads the clipboard of the
machine it runs on, so it can't see my local Mac's clipboard over SSH.

## My environment

- Local: macOS, iTerm2 (recent version), Python API to be enabled.
- Remote: macOS, reached via `ssh` using hosts defined in `~/.ssh/config`,
  key-based auth.
- Claude Code runs on the remote inside tmux. tmux may be running **either**
  on the remote (iTerm tab runs `ssh` directly) **or** locally (iTerm tab runs a
  tmux client, and `ssh` runs inside a tmux pane). Support both.
- Not needed: tmux `-CC` integration mode. Skip it.

## User flow

1. I press the hotkey (bound via iTerm2 Settings → Keys → Key Bindings →
   "Invoke Script Function").
2. The script finds the SSH destination for the current session. If there is
   none, it shows an alert and stops.
3. macOS crosshair appears (`screencapture -i`). Esc cancels silently.
4. The PNG uploads to the remote at `/tmp/shotdrop/<YYYYMMDD-HHMMSS>.png`.
5. The remote path is inserted into the current session and also copied to
   the local clipboard.

## Implementation requirements

### Script form
- An iTerm2 **AutoLaunch** script so it starts with iTerm2:
  `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/shotdrop.py`
- Register an RPC with `@iterm2.RPC`, taking the session via
  `session_id=iterm2.Reference("id")`, so it can be bound to a key as
  `shotdrop(session_id: id)`.
- Prefer a "basic" script (standard library only) unless a dependency is
  clearly needed. If you need one, explain why and how to install it into the
  script's environment.
- **Verify all iTerm2 API names against the official iTerm2 Python API docs**
  (https://iterm2.com/python-api/). Don't rely on memory.

### Finding the SSH destination
1. Read the session's job PID and TTY from iTerm2 session variables
   (for example `jobPid`, `tty`, `jobName`; confirm the exact names).
2. Get a process table with `ps -A -ww -o pid=,ppid=,args=`.
3. **Case A, tab runs `ssh`:** use that process.
4. **Case B, tab runs a local tmux client:** get the active pane's PID with
   `tmux display-message -c <session tty> -p '#{pane_pid}'`, then search that
   PID's descendants for `ssh`.
5. If several `ssh` processes are found (e.g. ProxyJump spawns `ssh -W …`
   children), choose the topmost one whose parent is not `ssh`.
6. Parse the ssh command line using ssh's real option grammar:
   - Flags with arguments: `B b c D E e F I i J L l m O o P p Q R S W w`
   - The first non-option argument is the destination. Everything after it is
     a remote command (e.g. `tmux attach`) and must be discarded.
   - Keep the options that affect connection (`-p -i -F -J -l -o -4 -6 -C` etc.).
     Drop `-t`, `-T`, `-L`, `-R`, `-D`, `-N`, `-f`, `-v`.

### Upload
- Use **`ssh` itself**, not `scp`, so the parsed options can be reused
  verbatim with no translation (scp uses `-P` instead of `-p`, for example):
  ```
  ssh -o BatchMode=yes <options> <destination> \
    'umask 077; mkdir -p /tmp/shotdrop && cat > /tmp/shotdrop/<name>.png'
    < local.png
  ```
- Always add `-o BatchMode=yes` so it fails fast instead of hanging on a
  password prompt.
- On the same connection, delete old files: `find /tmp/shotdrop -name '*.png' -mtime +7 -delete`.
- Use a timeout (~30 s) and surface stderr in the error alert.
- Delete the local temp PNG afterwards.

### Screenshot
- `/usr/sbin/screencapture -i -x <tmpfile>`
- If the file doesn't exist or is empty afterwards, treat it as a cancel and
  exit quietly with no alert.

### Inserting the path
- Claude Code only turns a path into an `[Image #N]` attachment when it
  arrives as a **paste event**. So send the path wrapped in bracketed-paste
  markers: `\x1b[200~` + path + `\x1b[201~`, via the session's send-text API.
- Make this configurable at the top of the script:
  `INSERT_MODE = "bracketed" | "typed" | "none"` (default `"bracketed"`).
  `"typed"` sends the plain path (Claude still reads the file, just without
  the attachment pill). `"none"` only copies to the clipboard.
- Always also copy the path to the local clipboard with `pbcopy`.
- Do not send a newline. I want to add my own prompt text before submitting.

### Errors
- Show errors with an iTerm2 alert (title + short message), e.g.
  "No SSH connection found in this tab" or "Upload failed: <stderr>".
- Log details to `~/Library/Logs/shotdrop.log`.

### Config (top of script)
- `REMOTE_DIR = "/tmp/shotdrop"`
- `RETENTION_DAYS = 7`
- `INSERT_MODE = "bracketed"`
- `UPLOAD_TIMEOUT = 30`

## Deliverables

1. `shotdrop.py`, the AutoLaunch script, well commented.
2. `install.sh`, which copies the script into the AutoLaunch folder (creating
   it if needed).
3. `README.md` with setup steps, key binding instructions, troubleshooting,
   and how to uninstall.
4. A small test for the ssh argument parser covering at least:
   `ssh devmac`, `ssh -p 2222 me@10.0.0.5`, `ssh -i ~/.ssh/k -J bastion host`,
   `ssh -t devmac tmux attach -t main`, `ssh -o Port=2222 devmac`,
   `ssh -vvv -L 8080:localhost:80 devmac`.

## Things to test with me

- Case A (remote tmux) and Case B (local tmux) both detect the right host.
- Whether bracketed paste produces `[Image #1]` in Claude Code through tmux.
  If it doesn't in Case B, check whether the tmux pane has bracketed paste
  enabled and suggest a fix, or fall back to `"typed"`.
- Behaviour when the tab isn't SSH'd anywhere, and when Esc cancels the capture.

---

## Setup checklist (for me, the human)

1. **Enable the Python API:** iTerm2 → Settings → General → Magic →
   "Enable Python API". Install the Python runtime if prompted
   (Scripts → Manage → Install Python Runtime).
2. **SSH must work without prompts.** In `~/.ssh/config`:
   ```
   Host *
       AddKeysToAgent yes
       UseKeychain yes
       ControlMaster auto
       ControlPath ~/.ssh/cm-%r@%h:%p
       ControlPersist 10m
   ```
   `ControlMaster` lets uploads reuse the tab's existing connection, so they're
   near-instant. Test it with: `ssh -o BatchMode=yes <host> true`, which should
   succeed silently.
3. **Install:** run `./install.sh`, then restart iTerm2 (or start the script
   from Scripts → AutoLaunch).
4. **Bind the key:** Settings → Keys → Key Bindings → + → pick a shortcut →
   action "Invoke Script Function" → `shotdrop(session_id: id)`.
5. **Screen Recording permission:** the first capture triggers a macOS
   prompt. Grant it to iTerm2 under System Settings → Privacy & Security →
   Screen & System Audio Recording, then restart iTerm2.
6. **Use it:** in a tab running Claude Code on the remote, press the hotkey,
   select an area, then type your question after the inserted path and send.
