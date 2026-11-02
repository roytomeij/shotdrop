#!/usr/bin/env python3
"""ShotDrop: screenshot-to-remote for iTerm2.

Press a hotkey in an iTerm2 tab that is SSH'd into a remote host, select a
screen area, and the screenshot is uploaded to that same host. The remote path
is then inserted into the terminal (as a bracketed paste, so Claude Code turns
it into an [Image #N] attachment) and copied to the local clipboard.

Install as an iTerm2 AutoLaunch script and bind a key to the iTerm2 action
"Invoke Script Function" with:  shotdrop(session_id: id)

It also adds "Open Remote Image" to the terminal's right-click menu: it
downloads an image path shown in the tab (such as one Claude Code printed) over
the tab's ssh connection and opens it locally.

This is a "basic" script: it only needs the standard library plus the `iterm2`
module that ships with iTerm2's Python runtime. Everything except main() is
importable without iTerm2, so the ssh argument parser can be unit tested with
any Python 3.
"""

import asyncio
import ctypes
import ctypes.util
import datetime
import hashlib
import logging
import logging.handlers
import os
import re
import shlex
import struct
import tempfile

# --- Config -----------------------------------------------------------------

REMOTE_DIR = "/tmp/shotdrop"   # Absolute path on the remote; "-<uid>" is appended.
RETENTION_DAYS = 7             # Older screenshots there are deleted on upload.
INSERT_MODE = "bracketed"      # "bracketed" | "typed" | "none"
UPLOAD_TIMEOUT = 30            # Seconds before the upload is killed.
# "Open Remote Image" (right-click menu) downloads images here and opens them.
DOWNLOAD_DIR = "~/Library/Caches/shotdrop"  # Files older than RETENTION_DAYS are deleted.
DOWNLOAD_TIMEOUT = 30          # Seconds before a download is killed.
MAX_DOWNLOAD_MB = 50           # Larger remote files are refused.

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
# tab's command line; our own overrides (SSH_OVERRIDES) also take care of the
# same settings coming from ~/.ssh/config.
DROP_O_KEYS = {
    "remotecommand", "requesttty", "localforward", "remoteforward",
    "dynamicforward", "localcommand", "permitlocalcommand", "sessiontype",
    "forkafterauthentication", "stdinnull", "controlmaster", "batchmode",
}

# Passed to uploads and downloads before the kept options. ssh uses the first
# value it sees for an option, so these win over both the tab's options and
# ~/.ssh/config.
SSH_OVERRIDES = [
    "-o", "BatchMode=yes",            # Fail fast instead of prompting for a password.
    "-o", "RemoteCommand=none",       # A RemoteCommand in ssh config would clash with ours.
    "-o", "RequestTTY=no",
    "-o", "ClearAllForwardings=yes",  # Don't try to re-bind the tab's port forwards.
    "-o", "PermitLocalCommand=no",
]

# The remote script prints the stored file's path after this marker, so it can
# be told apart from anything a login script prints to stdout.
PATH_MARKER = "shotdrop-path:"
# Likewise, the download command prints this and the file's size before it.
DATA_MARKER = "shotdrop-data:"

# Characters accepted in a path found on screen. No spaces, quotes or
# brackets, so the path ends where the surrounding text starts.
_PATH_CHARS = r"[\w.~+@%=/-]"
IMAGE_PATH_RE = re.compile(
    rf"(?<!{_PATH_CHARS})"   # Not the tail of a longer word or path.
    rf"(/{_PATH_CHARS}*\.(?:png|jpe?g|gif|webp|hei[cf]|tiff?|bmp))"
    r"(?![\w~+@%=/-]|\.\w)",  # Not followed by more path: "x.png.bak", "x.png/".
    re.IGNORECASE)
# A path, possibly incomplete, that runs into the end of a line.
PATH_FRAGMENT_RE = re.compile(rf"(?<!{_PATH_CHARS})/{_PATH_CHARS}*$")
MAX_WRAPPED_LINES = 5  # How many following lines a broken path may continue on.

# Names of files ShotDrop writes to DOWNLOAD_DIR (see local_image_name).
DOWNLOAD_NAME_RE = re.compile(r"(-[0-9a-f]{12}\.(png|jpg|gif|webp|heic|tiff|bmp)|^\.download-.*)$")

HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}

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
    """The command run on the remote to store stdin as REMOTE_DIR-<uid>/filename.

    Run through /bin/sh so it works whatever the remote login shell is. The
    directory may live in a shared location like /tmp, so it must be a real
    directory owned by us; otherwise another user could pre-create it and
    read, swap or redirect the screenshot. The remote uid is part of its name,
    so different users on one host (you and root, say) each get their own
    instead of locking each other out. The file is written under a temporary
    name with noclobber and only moved into place once all `size` bytes have
    arrived, so an interrupted upload never leaves a truncated PNG. The final
    path is printed after PATH_MARKER.
    """
    script = (
        'umask 077; f=$2; n=$3; days=$4\n'
        'u=$(id -u) && [ -n "$u" ] || { echo "couldn\'t determine remote uid" >&2; exit 1; }\n'
        'd=$1-$u\n'
        'mkdir -p "$d" 2>/dev/null\n'
        'if [ -L "$d" ] || [ ! -d "$d" ] || [ ! -O "$d" ]; then\n'
        '  echo "$d is not a directory owned by you" >&2; exit 1\n'
        'fi\n'
        'chmod 700 "$d" || exit 1\n'
        'set -C\n'
        'if cat > "$d/$f.part" && [ "$(wc -c < "$d/$f.part" | tr -d " ")" = "$n" ]; then\n'
        '  mv "$d/$f.part" "$d/$f" || exit 1\n'
        # Leading newline: a login script may have printed text without one.
        f'  printf \'\\n{PATH_MARKER}%s\\n\' "$d/$f"\n'
        'else\n'
        '  rm -f "$d/$f.part"; echo "upload incomplete" >&2; exit 1\n'
        'fi\n'
        'find "$d" \\( -name "*.png" -o -name "*.part" \\) -mtime +"$days" -delete 2>/dev/null\n'
        'exit 0\n'
    )
    return shlex.join(["/bin/sh", "-c", script, "shotdrop",
                       REMOTE_DIR.rstrip("/"), filename, str(int(size)), str(int(RETENTION_DAYS))])


def parse_remote_path(stdout, filename):
    """The path the remote script reported storing `filename` at, or None."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(PATH_MARKER):
            path = line[len(PATH_MARKER):]
            return path if path.endswith("/" + filename) else None
    return None


def build_ssh_argv(ssh_binary, options, destination, trailing, remote_command):
    argv = [ssh_binary, *SSH_OVERRIDES, *options]
    if destination.startswith("-"):
        argv += ["--", destination]  # Only reachable via `ssh -- -host`.
    else:
        argv += [destination, *trailing]
    return argv + [remote_command]


def build_upload_argv(ssh_binary, options, destination, trailing, filename, size):
    return build_ssh_argv(ssh_binary, options, destination, trailing,
                          build_remote_command(filename, size))


# --- Finding an image path on screen ----------------------------------------

def find_image_paths(lines):
    """Absolute image paths in `lines` (screen lines, top to bottom), in order.

    tmux and full-screen apps such as Claude Code break long lines themselves,
    so a path that runs into the end of a line is completed with the start of
    the following lines, ignoring their indentation. Whatever such a path
    takes from the next line isn't searched again, so the tail of a path that
    happens to start with "/" isn't reported as a path of its own.
    """
    found = []
    taken = {}  # Line index -> number of leading characters already used.
    for i, line in enumerate(lines):
        for m in IMAGE_PATH_RE.finditer(line):
            if m.start(1) >= taken.get(i, 0):
                found.append(m.group(1))
        fragment = PATH_FRAGMENT_RE.search(line.rstrip())
        if (not fragment or fragment.start() < taken.get(i, 0)
                or IMAGE_PATH_RE.fullmatch(fragment.group())):
            continue
        text = fragment.group()
        used = {}
        for j in range(i + 1, min(i + 1 + MAX_WRAPPED_LINES, len(lines))):
            stripped = lines[j].lstrip()
            indent = len(lines[j]) - len(stripped)
            stripped = stripped.rstrip()
            if not stripped:
                break
            start = len(text)
            text += stripped
            m = IMAGE_PATH_RE.match(text)
            if m and m.end() > start:
                used[j] = indent + m.end() - start
                found.append(m.group(1))
                taken.update(used)
                break
            if not PATH_FRAGMENT_RE.fullmatch(text):
                break  # The path ended on this line without becoming an image path.
            used[j] = len(lines[j])
    return found


def join_soft_wraps(lines):
    """Join (text, hard_eol) screen lines that iTerm2 wrapped itself."""
    joined, current = [], ""
    for text, hard_eol in lines:
        current += text
        if hard_eol:
            joined.append(current)
            current = ""
    if current:
        joined.append(current)
    return joined


def choose_image_path(selection, screen_lines):
    """The image path to open, from the selected text and the visible lines.

    A selected path wins. A selection that holds only part of a path (say,
    because a right-click selected it up to a line break) picks the visible
    path it's part of. Without a selection, the last path on screen is used.
    """
    screen_paths = find_image_paths(screen_lines)
    if selection and selection.strip():
        selection_lines = selection.split("\n")
        paths = find_image_paths(selection_lines)
        if paths:
            return paths[-1]
        hint = "".join(line.strip() for line in selection_lines)
        paths = [p for p in screen_paths if hint in p]
        if paths:
            return paths[-1]
        raise ShotDropError("No image path in the selection",
                            "Select an image path (or right-click one), or clear the selection "
                            "to open the last image path in the tab.")
    if screen_paths:
        return screen_paths[-1]
    raise ShotDropError("No image path found",
                        "Right-click an image path (such as /tmp/shot.png) shown in this tab.")


def is_safe_remote_image_path(path):
    """A path ShotDrop is willing to fetch: absolute, no "..", image extension."""
    return (len(path) <= 1024 and IMAGE_PATH_RE.fullmatch(path) is not None
            and ".." not in path.split("/"))


# --- Downloading -------------------------------------------------------------

def build_download_command(path, max_bytes):
    """The command run on the remote to print the file at `path`.

    Prints DATA_MARKER and the file's size first, so the file's bytes can be
    told apart from anything a login script prints to stdout.
    """
    script = (
        'f=$1; max=$2\n'
        'if [ ! -f "$f" ]; then echo "$f: no such file" >&2; exit 1; fi\n'
        'if [ ! -r "$f" ]; then echo "$f: permission denied" >&2; exit 1; fi\n'
        'n=$(wc -c < "$f" | tr -d " ") || exit 1\n'
        'if [ "$n" -gt "$max" ]; then echo "$f is larger than $max bytes" >&2; exit 1; fi\n'
        # Leading newline: a login script may have printed text without one.
        f'printf \'\\n{DATA_MARKER}%s\\n\' "$n"\n'
        'exec cat -- "$f"\n'
    )
    return shlex.join(["/bin/sh", "-c", script, "shotdrop", path, str(int(max_bytes))])


def parse_download_output(stdout):
    """The file's bytes from the download command's stdout, or None."""
    marker = b"\n" + DATA_MARKER.encode()
    start = stdout.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = stdout.find(b"\n", start)
    size = stdout[start:end]
    if end < 0 or not size.isdigit():
        return None
    data = stdout[end + 1:]
    return data if len(data) == int(size) else None


def image_type(data):
    """The file extension for image `data`, judged by its contents, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] in (b"II*\0", b"MM\0*"):
        return "tiff"
    if data[:2] == b"BM":
        return "bmp"
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "heic"
    return None


def local_image_name(destination, remote_path, extension):
    """Local file name for a downloaded image.

    Keeps the remote name readable and adds a hash of host and path, so
    equally named files from different places don't overwrite each other.
    """
    digest = hashlib.sha256(f"{destination}\0{remote_path}".encode()).hexdigest()[:12]
    stem = os.path.splitext(os.path.basename(remote_path))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:80] or "image"
    return f"{stem}-{digest}.{extension}"


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


async def run_ssh(target, remote_command, stdin, timeout, what):
    """Run `remote_command` over the tab's connection; return (stdout bytes, stderr).

    `what` names the operation ("Upload", "Download") in logs and errors.
    """
    argv = build_ssh_argv(target.ssh_binary, target.options, target.destination,
                          target.trailing, remote_command)
    # Relative paths in the tab's options (-i ./key, -F cfg) are relative to
    # where that ssh was started.
    cwd = process_cwd(target.ssh.pid)
    log.info("%s (cwd %s): %s", what.lower(), cwd, shlex.join(argv))
    # Output goes to files rather than pipes: a ControlPersist master or a
    # ProxyCommand left running in the background can hold a pipe open long
    # after ssh itself has exited.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=stdin, stdout=stdout, stderr=stderr,
            cwd=cwd, env=env_with(target.ssh.env))
        try:
            code = await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ShotDropError(f"{what} failed", f"Timed out after {timeout} s.")
        stdout.seek(0)
        out = stdout.read()
        stderr.seek(0)
        err = stderr.read().decode("utf-8", "replace")
    if code != 0:
        log.error("%s exited %s: %s", what.lower(), code, err.strip())
        raise ShotDropError(f"{what} failed", _short(err) or f"ssh exited with status {code}.")
    return out, err


async def upload(target, local_path, filename):
    """Store the file on the remote; return its path there."""
    size = os.path.getsize(local_path)
    with open(local_path, "rb") as stdin:
        out, _ = await run_ssh(target, build_remote_command(filename, size), stdin,
                               UPLOAD_TIMEOUT, "Upload")
    out = out.decode("utf-8", "replace")
    remote_path = parse_remote_path(out, filename)
    if remote_path is None:
        log.error("no path in upload output: %r", out)
        raise ShotDropError("Upload failed", "The remote didn't report where it stored the file.")
    return remote_path


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
        remote_path = await upload(target, local_path, filename)
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


# --- Open Remote Image ---------------------------------------------------------

async def image_path_in_session(session):
    """The image path to open: the selected one, or the last one on screen."""
    selection = await session.async_get_selection()
    text = ""
    if selection.sub_selections:
        text = await session.async_get_selection_text(selection)
    contents = await session.async_get_screen_contents()
    lines = join_soft_wraps((contents.line(i).string, contents.line(i).hard_eol)
                            for i in range(contents.number_of_lines))
    path = choose_image_path(text, lines)
    log.info("selection %r -> image path %r", text, path)
    return path


async def download(target, remote_path):
    """Fetch the image at `remote_path`; return the local copy's path."""
    max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024
    out, _ = await run_ssh(target, build_download_command(remote_path, max_bytes),
                           asyncio.subprocess.DEVNULL, DOWNLOAD_TIMEOUT, "Download")
    data = parse_download_output(out)
    if data is None:
        log.error("unexpected download output (%d bytes): %r", len(out), out[:200])
        raise ShotDropError("Download failed", "The remote's output wasn't the file it announced.")
    extension = image_type(data)
    if extension is None:
        # Only open real images: the path came from text on screen, which
        # anything printing to this terminal can control.
        raise ShotDropError("Not an image",
                            f"{remote_path} isn't a PNG, JPEG, GIF, WebP, HEIC, TIFF or BMP file.")

    directory = os.path.expanduser(DOWNLOAD_DIR)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    remove_old_downloads(directory)
    local_path = os.path.join(directory, local_image_name(target.destination, remote_path,
                                                          extension))
    fd, part = tempfile.mkstemp(dir=directory, prefix=".download-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(part, local_path)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    return local_path


def remove_old_downloads(directory):
    """Delete images ShotDrop downloaded more than RETENTION_DAYS ago.

    Only files named the way ShotDrop names them, in case DOWNLOAD_DIR is set
    to a folder with other files in it.
    """
    cutoff = datetime.datetime.now().timestamp() - RETENTION_DAYS * 86400
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not DOWNLOAD_NAME_RE.search(entry.name):
            continue
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass


async def open_remote_image(session):
    """The whole "Open Remote Image" flow. Raises ShotDropError for errors to show."""
    remote_path = await image_path_in_session(session)
    if not is_safe_remote_image_path(remote_path):
        raise ShotDropError("Won't open this path", remote_path)
    target = await find_target(session)
    local_path = await download(target, remote_path)
    log.info("downloaded %s:%s to %s", target.destination, remote_path, local_path)
    code, _, err = await run(["/usr/bin/open", local_path], timeout=10)
    if code != 0:
        raise ShotDropError("Couldn't open the image", _short(err) or local_path)


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

    async def report_errors(connection, flow):
        try:
            await flow
        except ShotDropError as e:
            log.error("%s", e)
            await alert(connection, e.title, e.message)
        except Exception as e:
            log.exception("unexpected error")
            await alert(connection, "ShotDrop failed", f"{e} (see {LOG_PATH})")

    async def run_capture(connection, session):
        nonlocal busy
        try:
            await report_errors(connection, capture_and_upload(session))
        finally:
            busy = False

    def spawn(flow):
        # RPCs return right away: the capture waits on the user for as long
        # as they like, and a download can be slow, either of which would
        # exceed iTerm2's RPC timeout.
        task = asyncio.create_task(flow)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

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
            spawn(run_capture(connection, session))

        await shotdrop.async_register(connection)
        log.info("registered shotdrop(session_id: id)")

        @iterm2.ContextMenuProviderRPC
        async def shotdrop_open(session_id=iterm2.Reference("id")):
            session = app.get_session_by_id(session_id)
            if session is None:
                log.error("no session with id %r", session_id)
                return
            spawn(report_errors(connection, open_remote_image(session)))

        try:
            await shotdrop_open.async_register(connection, "Open Remote Image",
                                               "shotdrop.open-remote-image")
            log.info("registered context menu item")
        except Exception:
            # E.g. iTerm2 too old for context menu items: keep the hotkey working.
            log.exception("couldn't register the context menu item")

    iterm2.run_forever(start)


if __name__ == "__main__":
    main()
