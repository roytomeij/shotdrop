#!/usr/bin/env python3
"""ShotDrop: screenshot-to-remote for iTerm2.

Press a hotkey in an iTerm2 tab that is SSH'd into a remote host, select a
screen area, and the screenshot is uploaded to that same host. The remote path
is then inserted into the terminal (as a bracketed paste, so Claude Code turns
it into an [Image #N] attachment) and copied to the local clipboard.

Install as an iTerm2 AutoLaunch script and bind a key to the iTerm2 action
"Invoke Script Function" with:  shotdrop(session_id: id)

This is a "basic" script: it only needs the standard library plus the `iterm2`
module that ships with iTerm2's Python runtime. Everything except main() is
importable without iTerm2, so the ssh argument parser can be unit tested with
any Python 3.
"""

import asyncio
import ctypes
import ctypes.util
import datetime
import logging
import logging.handlers
import os
import shlex
import struct
import tempfile

# --- Config -----------------------------------------------------------------

REMOTE_DIR = "/tmp/shotdrop"   # Absolute path on the remote; created if missing.
RETENTION_DAYS = 7             # Older screenshots in REMOTE_DIR are deleted on upload.
INSERT_MODE = "bracketed"      # "bracketed" | "typed" | "none"
UPLOAD_TIMEOUT = 30            # Seconds before the upload is killed.

# --- Constants --------------------------------------------------------------

LOG_PATH = os.path.expanduser("~/Library/Logs/shotdrop.log")
SCREENCAPTURE = "/usr/sbin/screencapture"
SSH = "/usr/bin/ssh"  # Apple's ssh, which understands UseKeychain in ~/.ssh/config.

# Scripts launched by iTerm2 don't get your shell's PATH. Add the usual
# locations so tmux, and any ProxyCommand helpers from ~/.ssh/config, resolve.
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]

# ssh(1) option grammar: these flags take an argument.
SSH_ARG_FLAGS = set("BbcDEeFIiJLlmOoPpQRSWw")

# Options worth carrying over to the upload connection, because they affect
# where and how ssh connects. Everything else (-t -T -L -R -D -N -f -v -W ...)
# is dropped.
KEEP_ARG_FLAGS = set("BbcFIiJlmoPpS")
KEEP_NOARG_FLAGS = set("46CKk")

# `-o` options that would break a one-shot upload command. Dropped from the
# tab's command line; our own overrides (UPLOAD_SSH_OPTIONS) also take care of
# the same settings coming from ~/.ssh/config.
DROP_O_KEYS = {
    "remotecommand", "requesttty", "localforward", "remoteforward",
    "dynamicforward", "localcommand", "permitlocalcommand", "sessiontype",
    "forkafterauthentication", "stdinnull", "controlmaster", "batchmode",
}

# Passed before the kept options. ssh uses the first value it sees for an
# option, so these win over both the tab's options and ~/.ssh/config.
UPLOAD_SSH_OPTIONS = [
    "-o", "BatchMode=yes",            # Fail fast instead of prompting for a password.
    "-o", "RemoteCommand=none",       # A RemoteCommand in ssh config would clash with ours.
    "-o", "RequestTTY=no",
    "-o", "ClearAllForwardings=yes",  # Don't try to re-bind the tab's port forwards.
    "-o", "PermitLocalCommand=no",
]

BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"

log = logging.getLogger("shotdrop")


class ShotDropError(Exception):
    """An error to show the user in an alert."""

    def __init__(self, title, message):
        super().__init__(f"{title}: {message}")
        self.title = title
        self.message = message


# --- ssh argument parsing ----------------------------------------------------

def parse_ssh_args(argv):
    """Parse an ssh command line into (connection_options, destination).

    `argv` is the full argument vector including argv[0]. Follows OpenSSH's
    own parsing: options are read until the first non-option (the
    destination); after the destination ssh resumes reading options until the
    next non-option, which starts the remote command. `--` ends option
    parsing. Flags may be bundled (`-tA`) and values attached (`-p2222`).

    Returns None if there is no destination (e.g. `ssh -V`).
    """
    options = []
    destination = None
    args = list(argv[1:])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            # Ends option parsing: next is the destination (if we don't have
            # one yet), and anything after that is the remote command.
            if destination is None and i + 1 < len(args):
                destination = args[i + 1]
            break
        if arg.startswith("-") and len(arg) > 1:
            j = 1
            while j < len(arg):
                flag = arg[j]
                if flag in SSH_ARG_FLAGS:
                    value = arg[j + 1:]
                    if not value:
                        i += 1
                        if i >= len(args):
                            return None  # Flag missing its argument.
                        value = args[i]
                    if _keep_option(flag, value):
                        options += ["-" + flag, value]
                    break  # The rest of this word was the value.
                if flag in KEEP_NOARG_FLAGS:
                    options.append("-" + flag)
                j += 1
            i += 1
            continue
        if destination is not None:
            break  # Start of the remote command.
        destination = arg
        i += 1
    if not destination:
        return None
    return options, destination


def _keep_option(flag, value):
    if flag not in KEEP_ARG_FLAGS:
        return False
    if flag == "o":
        key = value.replace("=", " ").split(None, 1)[0] if value.strip() else ""
        return key.lower() not in DROP_O_KEYS
    return True


def _basename(path):
    return os.path.basename(path) if path else ""


def is_ssh(argv):
    return bool(argv) and _basename(argv[0]) == "ssh"


def is_tmux(argv):
    return bool(argv) and _basename(argv[0]).startswith("tmux")


def tmux_socket_args(argv):
    """Return the -L/-S socket options from a tmux client command line."""
    flags_with_args = set("cfLST")
    result = []
    args = argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--" or not arg.startswith("-") or len(arg) == 1:
            break
        j = 1
        while j < len(arg):
            flag = arg[j]
            if flag in flags_with_args:
                value = arg[j + 1:]
                if not value:
                    i += 1
                    if i >= len(args):
                        return result
                    value = args[i]
                if flag in "LS":
                    result += ["-" + flag, value]
                break
            j += 1
        i += 1
    return result


def build_remote_command(remote_path):
    """The shell command run on the remote to store stdin at `remote_path`."""
    remote_dir = shlex.quote(REMOTE_DIR)
    return (
        f"umask 077; mkdir -p {remote_dir} && cat > {shlex.quote(remote_path)} || exit 1; "
        f"find {remote_dir} -name '*.png' -mtime +{int(RETENTION_DAYS)} -delete 2>/dev/null; "
        "exit 0"
    )


def build_upload_argv(options, destination, remote_path):
    return [SSH, *UPLOAD_SSH_OPTIONS, *options, "--", destination,
            build_remote_command(remote_path)]


# --- Process inspection ------------------------------------------------------

class Process:
    def __init__(self, pid, ppid, args):
        self.pid = pid
        self.ppid = ppid
        self.args = args  # As printed by ps: argv joined with spaces.
        self._argv = None
        self._exe = None

    def _load(self):
        if self._argv is None:
            exe, argv = read_proc_args(self.pid)
            # Fall back to ps' rendering, which loses quoting of arguments
            # containing spaces but is fine for the common case.
            self._exe = exe
            self._argv = argv or self.args.split()

    @property
    def argv(self):
        self._load()
        return self._argv

    @property
    def exe(self):
        self._load()
        return self._exe

    @property
    def name(self):
        return _basename(self.args.split(None, 1)[0]) if self.args else ""


_libc = None


def read_proc_args(pid):
    """Return (executable_path, argv) for `pid` via sysctl KERN_PROCARGS2.

    This gives the exact argument vector, unlike ps, which joins arguments
    with spaces. Returns (None, None) on failure.
    """
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49

        argmax = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        mib = (ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX)
        if _libc.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
            return None, None

        buf = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(argmax.value)
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None, None
        data = buf.raw[:size.value]

        # Layout: int argc, executable path, NUL padding, argv[0..argc-1], env...
        (argc,) = struct.unpack_from("i", data, 0)
        pos = 4
        end = data.index(b"\0", pos)
        exe = data[pos:end].decode("utf-8", "replace")
        pos = end
        while pos < len(data) and data[pos] == 0:
            pos += 1
        argv = []
        for _ in range(argc):
            end = data.index(b"\0", pos)
            argv.append(data[pos:end].decode("utf-8", "replace"))
            pos = end + 1
        return exe, argv
    except Exception:
        log.debug("read_proc_args(%s) failed", pid, exc_info=True)
        return None, None


async def run(argv, stdin=None, timeout=None):
    """Run a command; return (returncode, stdout, stderr) as text."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=stdin if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def process_table():
    code, out, err = await run(["/bin/ps", "-A", "-ww", "-o", "pid=,ppid=,args="])
    if code != 0:
        raise ShotDropError("ShotDrop", f"ps failed: {err.strip()}")
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        table[pid] = Process(pid, ppid, parts[2] if len(parts) > 2 else "")
    return table


def find_ssh(table, root_pid):
    """Find the ssh process in the tree rooted at `root_pid`.

    Searches breadth-first, so the topmost ssh wins, and skips ssh processes
    whose parent is ssh (ProxyJump spawns `ssh -W ...` children).
    """
    children = {}
    for proc in table.values():
        children.setdefault(proc.ppid, []).append(proc)
    queue = [table[root_pid]] if root_pid in table else []
    while queue:
        proc = queue.pop(0)
        parent = table.get(proc.ppid)
        if proc.name == "ssh" and not (parent and parent.name == "ssh") and is_ssh(proc.argv):
            return proc
        queue.extend(sorted(children.get(proc.pid, []), key=lambda p: p.pid))
    return None


async def find_ssh_destination(session):
    """Return (options, destination) for the ssh connection in `session`."""
    job_pid = await session.async_get_variable("jobPid")
    tty = await session.async_get_variable("tty")
    log.info("session %s: jobPid=%r tty=%r", session.session_id, job_pid, tty)
    try:
        job_pid = int(job_pid)
    except (TypeError, ValueError):
        raise ShotDropError("No SSH connection found in this tab",
                            "This tab has no running job.")

    table = await process_table()
    job = table.get(job_pid)
    if job is None:
        raise ShotDropError("No SSH connection found in this tab",
                            f"Job process {job_pid} is gone.")

    root_pid = job_pid
    if is_tmux(job.argv):
        # Case B: the tab runs a local tmux client; look inside its active pane.
        root_pid = await tmux_active_pane_pid(job, tty)
        log.info("local tmux client %s, active pane pid %s", job_pid, root_pid)

    ssh = find_ssh(table, root_pid)
    if ssh is None:
        raise ShotDropError("No SSH connection found in this tab",
                            "Run ShotDrop in a tab (or tmux pane) running ssh.")
    parsed = parse_ssh_args(ssh.argv)
    if parsed is None:
        raise ShotDropError("No SSH connection found in this tab",
                            f"Couldn't find a destination in: {ssh.args}")
    log.info("ssh pid %s argv=%r -> options=%r destination=%r",
             ssh.pid, ssh.argv, parsed[0], parsed[1])
    return parsed


async def tmux_active_pane_pid(client, tty):
    """Ask the local tmux server for the pane_pid of the client on `tty`."""
    tmux = client.exe if client.exe and os.path.isabs(client.exe) else "tmux"
    argv = [tmux, *tmux_socket_args(client.argv),
            "display-message", "-c", tty, "-p", "#{pane_pid}"]
    try:
        code, out, err = await run(argv, timeout=5)
    except (OSError, asyncio.TimeoutError) as e:
        raise ShotDropError("Couldn't query local tmux", str(e))
    if code != 0 or not out.strip().isdigit():
        raise ShotDropError("Couldn't query local tmux", err.strip() or out.strip())
    return int(out.strip())


# --- Capture, upload, insert -------------------------------------------------

async def capture(path):
    """Interactive area capture. Returns False if the user cancelled."""
    code, _, err = await run([SCREENCAPTURE, "-i", "-x", path])
    if code != 0:
        log.warning("screencapture exited %s: %s", code, err.strip())
    return os.path.exists(path) and os.path.getsize(path) > 0


async def upload(local_path, options, destination, remote_path):
    argv = build_upload_argv(options, destination, remote_path)
    log.info("upload: %s", shlex.join(argv))
    with open(local_path, "rb") as f:
        try:
            code, _, err = await run(argv, stdin=f, timeout=UPLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            raise ShotDropError("Upload failed", f"Timed out after {UPLOAD_TIMEOUT} s.")
    if code != 0:
        log.error("upload exited %s: %s", code, err.strip())
        raise ShotDropError("Upload failed", _short(err) or f"ssh exited with status {code}.")


def _short(text, limit=300):
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


async def copy_to_clipboard(text):
    proc = await asyncio.create_subprocess_exec("/usr/bin/pbcopy", stdin=asyncio.subprocess.PIPE)
    await proc.communicate(text.encode())


def text_to_insert(remote_path):
    if INSERT_MODE == "bracketed":
        # Claude Code only turns a path into an [Image #N] attachment when it
        # arrives as a paste, so wrap it in bracketed-paste markers.
        return BRACKETED_PASTE_START + remote_path + BRACKETED_PASTE_END
    if INSERT_MODE == "typed":
        return remote_path
    if INSERT_MODE != "none":
        log.warning("unknown INSERT_MODE %r; not inserting", INSERT_MODE)
    return None


def remote_filename():
    now = datetime.datetime.now()
    # Milliseconds keep two shots taken in the same second apart.
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}.png"


async def capture_and_upload(session):
    """The whole flow. Raises ShotDropError for errors to show the user."""
    options, destination = await find_ssh_destination(session)

    fd, local_path = tempfile.mkstemp(prefix="shotdrop-", suffix=".png")
    os.close(fd)
    try:
        if not await capture(local_path):
            log.info("capture cancelled")
            return
        remote_path = f"{REMOTE_DIR.rstrip('/')}/{remote_filename()}"
        await upload(local_path, options, destination, remote_path)
        log.info("uploaded to %s:%s", destination, remote_path)
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass

    await copy_to_clipboard(remote_path)
    text = text_to_insert(remote_path)
    if text:
        # No newline: you add your own prompt text before submitting.
        await session.async_send_text(text, suppress_broadcast=True)


# --- iTerm2 glue -------------------------------------------------------------

def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main():
    import iterm2  # Only available inside iTerm2's Python runtime.

    setup_logging()
    path = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(path + [p for p in EXTRA_PATH if p not in path])

    tasks = set()  # Strong references so running tasks aren't garbage collected.
    busy = False

    async def alert(connection, title, message):
        try:
            await iterm2.Alert(title, message).async_run(connection)
        except Exception:
            log.exception("couldn't show alert")

    async def run_flow(connection, session):
        nonlocal busy
        try:
            await capture_and_upload(session)
        except ShotDropError as e:
            log.error("%s", e)
            await alert(connection, e.title, e.message)
        except Exception as e:
            log.exception("unexpected error")
            await alert(connection, "ShotDrop failed", f"{e} (see {LOG_PATH})")
        finally:
            busy = False

    async def start(connection):
        app = await iterm2.async_get_app(connection)

        @iterm2.RPC
        async def shotdrop(session_id=iterm2.Reference("id")):
            nonlocal busy
            if busy:
                log.info("already running; ignoring hotkey")
                return
            session = app.get_session_by_id(session_id)
            if session is None:
                log.error("no session with id %r", session_id)
                return
            busy = True
            # Return right away: the capture waits on the user for as long as
            # they like, which would exceed iTerm2's RPC timeout.
            task = asyncio.create_task(run_flow(connection, session))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        await shotdrop.async_register(connection)
        log.info("registered shotdrop(session_id: id)")

    iterm2.run_forever(start)


if __name__ == "__main__":
    main()
