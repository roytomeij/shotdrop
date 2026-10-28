<p align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="ShotDrop logo">
</p>

# ShotDrop

> [!WARNING]
> **This code was written entirely by Claude Code (an AI), without human
> intervention or human code review.** A human has tested it and found it to
> work, but only with macOS on both ends. Linux remotes should work but haven't
> been tested by a human. Nobody has reviewed the code line by line. **Use it at
> your own risk.** It runs
> with your permissions, reads your process list, uses your SSH credentials and
> types into your terminal. Read `shotdrop.py` before you install it.

Screenshot-to-remote for iTerm2. Press a hotkey in a tab that's SSH'd into a
remote host, select a screen area, and the screenshot is uploaded to that host.
The remote path is then pasted into the terminal and copied to your local
clipboard.

**Where it runs:**
- **Locally:** macOS with iTerm2 only.
- **Remote:** any Unix-like host reached with OpenSSH, such as macOS or Linux
  (Claude Code on Linux works the same way). On the remote, ShotDrop only uses
  `/bin/sh` and standard tools (`mkdir`, `cat`, `wc`, `mv`, `find`). Windows
  remotes aren't supported.

It exists for Claude Code: its Ctrl+V image paste reads the clipboard of the
machine it runs on, so it can't see your local Mac's clipboard over SSH.
ShotDrop puts the file on the remote instead and pastes its path. Claude Code
turns a pasted image path into an `[Image #1]` attachment.

## How it works

1. Finds the SSH destination of the current tab:
   - **Tab runs `ssh`** (tmux, if any, runs on the remote): uses that process.
   - **Tab runs a local tmux client**: asks tmux for the active pane
     (`tmux display-message -c <tty> -p '#{pane_pid}'`) and looks for `ssh`
     in that pane's process tree.

   Only an `ssh` in the foreground counts, so a backgrounded tunnel
   (`ssh -N -L … &`) in the same shell is never picked.

   It reads the exact `ssh` command line, keeps the options that affect the
   connection (`-p -i -F -J -l -o -S -4 -6 -C` …), and drops the rest
   (`-t -L -R -D -N -f -v` …) as well as any remote command.
2. Runs `screencapture -i`. Esc cancels silently.
3. Uploads with `ssh` itself, so the options are reused verbatim:
   `ssh -o BatchMode=yes … <dest> '/bin/sh -c …'`. The remote script checks
   that `/tmp/shotdrop` is a real directory owned by you, so another user on
   the remote can't intercept the file. It writes to a temporary name and only
   moves the file into place once every byte has arrived. On the same
   connection it deletes screenshots older than 7 days.
4. Pastes the remote path, with no newline so you can type your prompt after
   it, and copies it with `pbcopy`. In a plain tab it sends the path wrapped
   in bracketed-paste markers. With local tmux it pastes through tmux
   (`paste-buffer -p`) into the pane where you pressed the hotkey, even if
   you've switched panes since.

## Setup

1. **Enable the Python API:** iTerm2 → Settings → General → Magic → "Enable
   Python API". Install the Python runtime when prompted (or via Scripts →
   Manage → Install Python Runtime).

2. **SSH must work without prompts.** Uploads use `BatchMode=yes`, so they fail
   instead of asking for a password. Suggested `~/.ssh/config`:

   ```
   Host *
       AddKeysToAgent yes
       UseKeychain yes
       ControlMaster auto
       ControlPath ~/.ssh/cm-%r@%h:%p
       ControlPersist 10m
   ```

   With `ControlMaster`, uploads reuse the tab's existing connection and are
   near-instant. Check with `ssh -o BatchMode=yes <host> true`, which should
   succeed silently.

3. **Install:**

   ```sh
   ./install.sh
   ```

   This copies `shotdrop.py` to
   `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/`. Quit and
   reopen iTerm2 to load it. To update, `git pull` (or edit `shotdrop.py`),
   then run `./install.sh` and restart iTerm2 again.

4. **Bind a key:** Settings → Keys → Key Bindings → **+** → pick a shortcut →
   Action "Invoke Script Function" → enter:

   ```
   shotdrop(session_id: id)
   ```

   If iTerm2 asks which sessions to apply the action to, choose the **current
   session**. ShotDrop works on the tab you invoke it from: it finds that tab's
   ssh connection and pastes the path back into it. Applied to all sessions, it
   would start in every tab at once, and since only one capture runs at a
   time, the screenshot would land in an arbitrary tab.

   Bind it here (Settings → Keys) rather than under Profiles → Keys so the
   hotkey works in every profile.

5. **Screen Recording permission:** the first capture triggers a macOS prompt.
   Grant it to iTerm2 under System Settings → Privacy & Security → Screen &
   System Audio Recording, then restart iTerm2.

## Usage

In a tab running Claude Code on the remote, press the hotkey, select an area,
then type your question after the inserted path and press Enter.

## Configuration

At the top of `shotdrop.py`:

| Setting          | Default           | Meaning |
|------------------|-------------------|---------|
| `REMOTE_DIR`     | `"/tmp/shotdrop"` | Absolute directory on the remote. Must be owned by you; it's created if missing and set to mode 700. |
| `RETENTION_DAYS` | `7`               | Older `.png` files in `REMOTE_DIR` are deleted on each upload. |
| `INSERT_MODE`    | `"bracketed"`     | `"bracketed"`: paste the path (gives `[Image #N]` in Claude Code). `"typed"`: type the plain path (Claude still reads the file, without the attachment pill). `"none"`: clipboard only. |
| `UPLOAD_TIMEOUT` | `30`              | Seconds before the upload is aborted. |

## Troubleshooting

Details of every run are logged to `~/Library/Logs/shotdrop.log`, including the
detected destination and the exact upload command:

```sh
tail -f ~/Library/Logs/shotdrop.log
```

Script output and Python errors also show in Scripts → Manage → Console.

- **Hotkey does nothing.** Check the script is running (Scripts → Manage →
  Console should list `shotdrop.py`) and that the key binding reads exactly
  `shotdrop(session_id: id)`.
- **"No SSH connection found in this tab".** The tab's foreground job (or the
  active pane of a local tmux client) has no `ssh` process. `mosh`, `et` and
  tmux `-CC` integration mode aren't supported.
- **"Couldn't query local tmux" / "Couldn't paste into tmux".** The tmux
  client in the tab couldn't be reached. Check the log for tmux's error
  message. After a paste error, the path is still on your clipboard.
- **"Couldn't read the ssh command line".** macOS didn't return the exact
  arguments of the tab's ssh process. ShotDrop refuses to guess, so the
  screenshot can't end up on the wrong host.
- **"Upload failed: /tmp/shotdrop is not a directory owned by you".** Someone
  else (or a symlink) owns that path on the remote. Remove it, or set
  `REMOTE_DIR` to a directory in your home folder.
- **"Upload failed: Permission denied (publickey…)".** Run
  `ssh -o BatchMode=yes <host> true` in a terminal. Note that the script
  inherits iTerm2's environment, not your shell's: if your shell rc sets
  `SSH_AUTH_SOCK` (e.g. for 1Password or Secretive), set `IdentityAgent` in
  `~/.ssh/config` instead so every ssh finds the agent.
- **"Upload failed: Timed out".** The host was unreachable, or an
  interactive step (host key confirmation, hardware key touch) was waiting.
  Connect once from a terminal first.
- **The screenshot only shows the desktop wallpaper.** iTerm2 lacks Screen
  Recording permission (see Setup step 5), or it was granted without
  restarting iTerm2.
- **The path appears but no `[Image #1]`.** Claude Code only attaches images
  that arrive as a paste. If the paste markers get lost on the way (e.g.
  through tmux), set `INSERT_MODE = "typed"`. Claude Code still reads the
  file when you mention the path.

## Uninstall

```sh
./install.sh --uninstall
```

Then quit iTerm2 (or stop the script under Scripts → Manage), and remove the
key binding. Screenshots left on remotes are in `/tmp/shotdrop`. Most systems
also clear old files from `/tmp` on their own.

## Development

The ssh argument parser has unit tests that run with any Python 3:

```sh
python3 -m unittest -v
```

## License

Public domain, under [The Unlicense](LICENSE). The author claims no ownership:
anyone may use, copy, modify and distribute this code for any purpose, without
conditions. It comes with no warranty of any kind.
