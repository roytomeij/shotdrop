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
# Used when the tab's ssh binary can't be determined. Apple's ssh understands
# UseKeychain in ~/.ssh/config.
DEFAULT_SSH = "/usr/bin/ssh"

# Scripts launched by iTerm2 don't get your shell's PATH. Add the usual
# locations so tmux, and any ProxyCommand helpers from ~/.ssh/config, resolve.
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]

# Environment variables copied from the tab's ssh (or tmux) process, so the
# upload uses the same agent and the tmux query finds the same server even if
# your shell rc sets them differently from iTerm2's own environment. macOS
# hides the environment of Apple's own binaries (including /usr/bin/ssh), so
# this only helps with e.g. Homebrew's ssh and tmux; otherwise iTerm2's
# environment is used as is.
INHERITED_ENV = ["SSH_AUTH_SOCK", "TMUX_TMPDIR"]

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
    """Parse an ssh command line into (options, destination, trailing_options).

    `argv` is the full argument vector including argv[0]. Follows OpenSSH's
    own parsing: options are read until the first non-option (the
    destination); after the destination ssh resumes reading options until the
    next non-option, which starts the remote command. `--` ends option
    parsing. Flags may be bundled (`-tA`) and values attached (`-p2222`).

    Options after the destination are returned separately: ssh keeps the
    first value it sees, so `ssh me@host -l other` connects as `me`, and the
    upload has to repeat them in the same position.

    Returns None if there is no destination (e.g. `ssh -V`).
    """
    options = []
    trailing = []
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
            kept = options if destination is None else trailing
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
                        kept += ["-" + flag, value]
                    break  # The rest of this word was the value.
                if flag in KEEP_NOARG_FLAGS:
                    kept.append("-" + flag)
                j += 1
            i += 1
            continue
        if destination is not None:
            break  # Start of the remote command.
        destination = arg
        i += 1
    if not destination:
        return None
    return options, destination, trailing


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


def build_remote_command(filename, size):
    """The command run on the remote to store stdin as REMOTE_DIR/filename.

    Run through /bin/sh so it works whatever the remote login shell is. The
    directory may live in a shared location like /tmp, so it must be a real
    directory owned by us; otherwise another user could pre-create it and
    read, swap or redirect the screenshot. The file is written under a
    temporary name with noclobber and only moved into place once all `size`
    bytes have arrived, so an interrupted upload never leaves a truncated PNG.
    """
    script = (
        'umask 077; d=$1; f=$2; n=$3; days=$4\n'
        'mkdir -p "$d" 2>/dev/null\n'
        'if [ -L "$d" ] || [ ! -d "$d" ] || [ ! -O "$d" ]; then\n'
        '  echo "$d is not a directory owned by you" >&2; exit 1\n'
        'fi\n'
        'chmod 700 "$d" || exit 1\n'
        'set -C\n'
        'if cat > "$d/$f.part" && [ "$(wc -c < "$d/$f.part" | tr -d " ")" = "$n" ]; then\n'
        '  mv "$d/$f.part" "$d/$f" || exit 1\n'
        'else\n'
        '  rm -f "$d/$f.part"; echo "upload incomplete" >&2; exit 1\n'
        'fi\n'
        'find "$d" \\( -name "*.png" -o -name "*.part" \\) -mtime +"$days" -delete 2>/dev/null\n'
        'exit 0\n'
    )
    return shlex.join(["/bin/sh", "-c", script, "shotdrop",
                       REMOTE_DIR, filename, str(int(size)), str(int(RETENTION_DAYS))])


def build_upload_argv(ssh_binary, options, destination, trailing, filename, size):
    argv = [ssh_binary, *UPLOAD_SSH_OPTIONS, *options]
    if destination.startswith("-"):
        argv += ["--", destination]  # Only reachable via `ssh -- -host`.
    else:
        argv += [destination, *trailing]
    return argv + [build_remote_command(filename, size)]


# --- Process inspection ------------------------------------------------------

class Process:
    def __init__(self, pid, ppid, pgid, tpgid, args, argv=None):
        self.pid = pid
        self.ppid = ppid
        self.pgid = pgid
        self.tpgid = tpgid  # Foreground process group of its terminal (0 if none).
        self.args = args    # As printed by ps: argv joined with spaces.
        self._argv = argv
        self._exe = None
        self._env = {}
        self.exact = argv is not None  # True if argv is the real argument vector.

    def _load(self):
        if self._argv is None:
            exe, argv, env = read_proc_args(self.pid)
            self._exe = exe
            self._env = env or {}
            self.exact = argv is not None
            # Fall back to ps' rendering, which loses quoting of arguments
            # containing spaces but is fine for recognising the program.
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
    def env(self):
        self._load()
        return self._env

    @property
    def name(self):
        return _basename(self.args.split(None, 1)[0]) if self.args else ""

    @property
    def foreground(self):
        return self.tpgid > 0 and self.pgid == self.tpgid


_libc = None
_libproc = None


def _load_libs():
    global _libc, _libproc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        _libc.sysctl.restype = ctypes.c_int
    if _libproc is None:
        _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        _libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                          ctypes.c_void_p, ctypes.c_int]
        _libproc.proc_pidinfo.restype = ctypes.c_int


def read_proc_args(pid):
    """Return (executable_path, argv, env) for `pid` via sysctl KERN_PROCARGS2.

    This gives the exact argument vector, unlike ps, which joins arguments
    with spaces. Returns (None, None, None) on failure.
    """
    try:
        _load_libs()
        CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49

        argmax = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        mib = (ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX)
        if _libc.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
            return None, None, None

        buf = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(argmax.value)
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None, None, None
        data = buf.raw[:size.value]

        # Layout: int argc, executable path, NUL padding, argv[0..argc-1],
        # then environment strings until an empty string.
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
        env = {}
        while pos < len(data):
            end = data.find(b"\0", pos)
            if end <= pos:
                break
            key, sep, value = data[pos:end].decode("utf-8", "replace").partition("=")
            if sep:
                env.setdefault(key, value)
            pos = end + 1
        return exe, argv, env
    except Exception:
        log.debug("read_proc_args(%s) failed", pid, exc_info=True)
        return None, None, None


def process_cwd(pid):
    """Return the working directory of `pid`, or None."""
    PROC_PIDVNODEPATHINFO = 9
    # struct proc_vnodepathinfo: two vnode_info_path (cwd, root), each a
    # 152-byte vnode_info followed by a MAXPATHLEN (1024) path.
    VNODE_INFO_SIZE, MAXPATHLEN = 152, 1024
    size = 2 * (VNODE_INFO_SIZE + MAXPATHLEN)
    try:
        _load_libs()
        buf = ctypes.create_string_buffer(size)
        if _libproc.proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, buf, size) != size:
            return None
        path = buf.raw[VNODE_INFO_SIZE:VNODE_INFO_SIZE + MAXPATHLEN].split(b"\0", 1)[0]
        path = path.decode("utf-8", "replace")
        return path if os.path.isabs(path) and os.path.isdir(path) else None
    except Exception:
        log.debug("process_cwd(%s) failed", pid, exc_info=True)
        return None


async def run(argv, timeout=None, env=None):
    """Run a command; return (returncode, stdout, stderr) as text."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def env_with(overrides):
    env = dict(os.environ)
    env.update({k: v for k, v in overrides.items() if k in INHERITED_ENV and v})
    return env


async def process_table():
    code, out, err = await run(["/bin/ps", "-A", "-ww", "-o", "pid=,ppid=,pgid=,tpgid=,args="])
    if code != 0:
        raise ShotDropError("ShotDrop", f"ps failed: {err.strip()}")
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        pid, ppid, pgid, tpgid = (int(p) for p in parts[:4])
        table[pid] = Process(pid, ppid, pgid, tpgid, parts[4] if len(parts) > 4 else "")
    return table


def find_ssh(table, root_pid):
    """Find the foreground ssh process in the tree rooted at `root_pid`.

    Only ssh processes in their terminal's foreground process group count, so
    a backgrounded or suspended `ssh -N tunnel &` in the same shell is never
    picked. Searches breadth-first, so the topmost ssh wins, and skips ssh
    processes whose parent is ssh (ProxyJump spawns `ssh -W ...` children).
    """
    children = {}
    for proc in table.values():
        children.setdefault(proc.ppid, []).append(proc)
    queue = [table[root_pid]] if root_pid in table else []
    while queue:
        proc = queue.pop(0)
        parent = table.get(proc.ppid)
        if (proc.name == "ssh" and proc.foreground
                and not (parent and parent.name == "ssh") and is_ssh(proc.argv)):
            return proc
        queue.extend(children.get(proc.pid, []))
    return None


class Target:
    """Where the screenshot goes and how to reach it."""

    def __init__(self, ssh, parsed, tmux=None, pane_id=None):
        self.ssh = ssh
        self.options, self.destination, self.trailing = parsed
        self.tmux = tmux          # (tmux argv prefix, env) when the tab runs local tmux.
        self.pane_id = pane_id    # The tmux pane the hotkey was pressed in.

    @property
    def ssh_binary(self):
        exe = self.ssh.exe
        return exe if exe and os.path.isabs(exe) and os.access(exe, os.X_OK) else DEFAULT_SSH


async def find_target(session):
    """Find the ssh connection in `session`."""
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
    tmux = pane_id = None
    if is_tmux(job.argv):
        # Case B: the tab runs a local tmux client; look inside its active pane.
        tmux = (local_tmux_argv(job), env_with(job.env))
        root_pid, pane_id = await tmux_active_pane(tmux, tty)
        log.info("local tmux client %s, active pane %s (pid %s)", job_pid, pane_id, root_pid)

    ssh = find_ssh(table, root_pid)
    if ssh is None:
        raise ShotDropError("No SSH connection found in this tab",
                            "Run ShotDrop in a tab (or tmux pane) running ssh.")
    if not ssh.exact:
        # Without the exact argv, an argument containing spaces could be
        # mistaken for the destination and the screenshot sent elsewhere.
        raise ShotDropError("Couldn't read the ssh command line",
                            f"Process {ssh.pid}: {ssh.args}")
    parsed = parse_ssh_args(ssh.argv)
    if parsed is None:
        raise ShotDropError("No SSH connection found in this tab",
                            f"Couldn't find a destination in: {ssh.args}")
    log.info("ssh pid %s argv=%r -> options=%r destination=%r trailing=%r",
             ssh.pid, ssh.argv, *parsed)
    return Target(ssh, parsed, tmux, pane_id)


def local_tmux_argv(client):
    """The tmux binary and socket options to talk to `client`'s server."""
    tmux = client.exe if client.exe and os.path.isabs(client.exe) else "tmux"
    return [tmux, *tmux_socket_args(client.argv)]


async def tmux_active_pane(tmux, tty):
    """Return (pane_pid, pane_id) of the active pane of the client on `tty`."""
    tmux_argv, env = tmux
    argv = [*tmux_argv, "display-message", "-c", tty, "-p", "#{pane_pid} #{pane_id}"]
    try:
        code, out, err = await run(argv, timeout=5, env=env)
    except (OSError, asyncio.TimeoutError) as e:
        raise ShotDropError("Couldn't query local tmux", str(e))
    fields = out.split()
    if code != 0 or len(fields) != 2 or not fields[0].isdigit():
        raise ShotDropError("Couldn't query local tmux", err.strip() or out.strip())
    return int(fields[0]), fields[1]


# --- Capture, upload, insert -------------------------------------------------

async def capture(path):
    """Interactive area capture. Returns False if the user cancelled."""
    code, _, err = await run([SCREENCAPTURE, "-i", "-x", path])
    if code != 0:
        log.warning("screencapture exited %s: %s", code, err.strip())
    return os.path.exists(path) and os.path.getsize(path) > 0


async def upload(target, local_path, filename):
    size = os.path.getsize(local_path)
    argv = build_upload_argv(target.ssh_binary, target.options, target.destination,
                             target.trailing, filename, size)
    # Relative paths in the tab's options (-i ./key, -F cfg) are relative to
    # where that ssh was started.
    cwd = process_cwd(target.ssh.pid)
    log.info("upload (cwd %s): %s", cwd, shlex.join(argv))
    # stderr goes to a file rather than a pipe: a ControlPersist master or a
    # ProxyCommand left running in the background can hold a pipe open long
    # after ssh itself has exited.
    with open(local_path, "rb") as stdin, tempfile.TemporaryFile() as stderr:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=stdin, stdout=asyncio.subprocess.DEVNULL, stderr=stderr,
            cwd=cwd, env=env_with(target.ssh.env))
        try:
            code = await asyncio.wait_for(proc.wait(), UPLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ShotDropError("Upload failed", f"Timed out after {UPLOAD_TIMEOUT} s.")
        stderr.seek(0)
        err = stderr.read().decode("utf-8", "replace")
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


async def paste_into_tmux_pane(target, remote_path):
    """Paste into the pane the hotkey was pressed in, even if you've moved on.

    `paste-buffer -p` adds bracketed-paste markers only if the program in the
    pane asked for them, so they can't turn into stray keystrokes.
    """
    tmux_argv, env = target.tmux
    proc = await asyncio.create_subprocess_exec(
        *tmux_argv, "load-buffer", "-b", "shotdrop", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE, env=env)
    _, err = await asyncio.wait_for(proc.communicate(remote_path.encode()), 5)
    if proc.returncode == 0:
        flags = ["-p"] if INSERT_MODE == "bracketed" else []
        code, _, err = await run([*tmux_argv, "paste-buffer", "-d", *flags, "-b", "shotdrop",
                                  "-t", target.pane_id], timeout=5, env=env)
        if code == 0:
            return
    else:
        err = err.decode("utf-8", "replace")
    raise ShotDropError("Couldn't paste into tmux",
                        f"{_short(err)} The path is on the clipboard.")


def remote_filename():
    now = datetime.datetime.now()
    # Milliseconds keep two shots taken in the same second apart.
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}.png"


async def capture_and_upload(session):
    """The whole flow. Raises ShotDropError for errors to show the user."""
    target = await find_target(session)

    fd, local_path = tempfile.mkstemp(prefix="shotdrop-", suffix=".png")
    os.close(fd)
    try:
        if not await capture(local_path):
            log.info("capture cancelled")
            return
        filename = remote_filename()
        await upload(target, local_path, filename)
        remote_path = f"{REMOTE_DIR.rstrip('/')}/{filename}"
        log.info("uploaded to %s:%s", target.destination, remote_path)
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass

    await copy_to_clipboard(remote_path)
    # No newline in either case: you add your own prompt text before submitting.
    if target.pane_id and INSERT_MODE in ("bracketed", "typed"):
        await paste_into_tmux_pane(target, remote_path)
        return
    text = text_to_insert(remote_path)
    if text:
        await session.async_send_text(text, suppress_broadcast=True)


# --- iTerm2 glue -------------------------------------------------------------

def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    # The log records ssh command lines, which may contain secrets (e.g. in a
    # ProxyCommand), so keep it private.
    old_umask = os.umask(0o077)
    try:
        handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1)
    finally:
        os.umask(old_umask)
    os.chmod(LOG_PATH, 0o600)
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
