"""Tests for ShotDrop's pure logic. Run with: python3 -m unittest"""

import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import shotdrop  # noqa: E402
from shotdrop import (  # noqa: E402
    Process, build_remote_command, build_upload_argv, find_ssh, parse_ssh_args,
    process_cwd, read_proc_args, tmux_socket_args,
)


def parse(cmdline):
    return parse_ssh_args(shlex.split(cmdline))


class ParseSshArgsTest(unittest.TestCase):
    # Cases from the briefing.

    def test_plain_host(self):
        self.assertEqual(parse("ssh devmac"), ([], "devmac", []))

    def test_port_and_user(self):
        self.assertEqual(parse("ssh -p 2222 me@10.0.0.5"), (["-p", "2222"], "me@10.0.0.5", []))

    def test_identity_and_jump(self):
        self.assertEqual(parse("ssh -i ~/.ssh/k -J bastion host"),
                         (["-i", "~/.ssh/k", "-J", "bastion"], "host", []))

    def test_remote_command_discarded(self):
        self.assertEqual(parse("ssh -t devmac tmux attach -t main"), ([], "devmac", []))

    def test_o_option_kept(self):
        self.assertEqual(parse("ssh -o Port=2222 devmac"), (["-o", "Port=2222"], "devmac", []))

    def test_verbose_and_forward_dropped(self):
        self.assertEqual(parse("ssh -vvv -L 8080:localhost:80 devmac"), ([], "devmac", []))

    # ssh grammar details.

    def test_options_after_destination(self):
        self.assertEqual(parse("ssh devmac -p 2222 -t tmux attach"),
                         ([], "devmac", ["-p", "2222"]))

    def test_attached_value(self):
        self.assertEqual(parse("ssh -p2222 -oPort=22 devmac"),
                         (["-p", "2222", "-o", "Port=22"], "devmac", []))

    def test_bundled_flags(self):
        self.assertEqual(parse("ssh -tA4p 2222 devmac"), (["-4", "-p", "2222"], "devmac", []))

    def test_double_dash(self):
        self.assertEqual(parse("ssh -- devmac -p 2222"), ([], "devmac", []))
        self.assertEqual(parse("ssh devmac -- -p 2222"), ([], "devmac", []))

    def test_kept_noarg_flags(self):
        self.assertEqual(parse("ssh -4 -6 -C devmac"), (["-4", "-6", "-C"], "devmac", []))

    def test_dropped_flags(self):
        self.assertEqual(parse("ssh -t -T -N -f -v -R 9000:localhost:9000 -D 1080 devmac"),
                         ([], "devmac", []))

    def test_login_name_config_and_control_path(self):
        self.assertEqual(parse("ssh -l me -F /tmp/cfg -S /tmp/cm devmac"),
                         (["-l", "me", "-F", "/tmp/cfg", "-S", "/tmp/cm"], "devmac", []))

    def test_conflicting_o_options_dropped(self):
        self.assertEqual(
            parse("ssh -o RemoteCommand='tmux a' -o RequestTTY=yes -o 'User me' devmac"),
            (["-o", "User me"], "devmac", []))

    def test_argument_with_spaces(self):
        self.assertEqual(parse("ssh -o 'ProxyCommand nc -X 5 %h %p' devmac"),
                         (["-o", "ProxyCommand nc -X 5 %h %p"], "devmac", []))

    def test_uri_destination(self):
        self.assertEqual(parse("ssh ssh://me@devmac:2222"), ([], "ssh://me@devmac:2222", []))

    def test_full_path_argv0(self):
        self.assertEqual(parse("/usr/bin/ssh devmac"), ([], "devmac", []))

    def test_no_destination(self):
        self.assertIsNone(parse("ssh -V"))
        self.assertIsNone(parse("ssh -p"))


class TmuxSocketArgsTest(unittest.TestCase):
    def test_default_socket(self):
        self.assertEqual(tmux_socket_args(["tmux", "attach"]), [])

    def test_named_and_path_sockets(self):
        self.assertEqual(tmux_socket_args(["tmux", "-L", "work", "-u", "attach"]), ["-L", "work"])
        self.assertEqual(tmux_socket_args(["tmux", "-S/tmp/s", "new"]), ["-S", "/tmp/s"])


class UploadArgvTest(unittest.TestCase):
    def test_overrides_come_first_and_trailing_options_keep_their_place(self):
        argv = build_upload_argv("/usr/bin/ssh", ["-p", "2222"], "me@devmac", ["-l", "other"],
                                 "x.png", 10)
        self.assertEqual(argv[:3], ["/usr/bin/ssh", "-o", "BatchMode=yes"])
        self.assertLess(argv.index("RemoteCommand=none"), argv.index("-p"))
        self.assertEqual(argv[-4:-1], ["me@devmac", "-l", "other"])
        self.assertTrue(argv[-1].startswith("/bin/sh -c "))


class RemoteCommandTest(unittest.TestCase):
    """Runs the remote command locally with /bin/sh against a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "shotdrop")
        self._old = shotdrop.REMOTE_DIR
        shotdrop.REMOTE_DIR = self.dir

    def tearDown(self):
        shotdrop.REMOTE_DIR = self._old
        self.tmp.cleanup()

    def run_remote(self, data, size=None, filename="x.png"):
        cmd = build_remote_command(filename, len(data) if size is None else size)
        return subprocess.run(["/bin/sh", "-c", cmd], input=data, capture_output=True)

    def test_stores_file_in_private_directory(self):
        result = self.run_remote(b"png bytes")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.dir, "x.png"), "rb") as f:
            self.assertEqual(f.read(), b"png bytes")
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.dir, "x.png")).st_mode & 0o777, 0o600)

    def test_tightens_existing_directory(self):
        os.mkdir(self.dir, 0o777)
        os.chmod(self.dir, 0o777)
        self.assertEqual(self.run_remote(b"data").returncode, 0)
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)

    def test_refuses_symlinked_directory(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, self.dir)
        result = self.run_remote(b"data")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"not a directory owned by you", result.stderr)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_incomplete_upload_leaves_nothing(self):
        result = self.run_remote(b"short", size=100)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(os.listdir(self.dir), [])

    def test_quotes_awkward_names(self):
        shotdrop.REMOTE_DIR = os.path.join(self.tmp.name, "it's $HOME; dir")
        self.assertEqual(self.run_remote(b"data", filename="a b.png").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(shotdrop.REMOTE_DIR, "a b.png")))


def proc(pid, ppid, args, pgid=None, tpgid=100):
    return Process(pid, ppid, pid if pgid is None else pgid, tpgid, args, argv=args.split())


class FindSshTest(unittest.TestCase):
    def table(self, *procs):
        return {p.pid: p for p in procs}

    def test_tab_running_ssh(self):
        table = self.table(proc(100, 1, "ssh devmac"))
        self.assertEqual(find_ssh(table, 100).pid, 100)

    def test_skips_proxyjump_child(self):
        table = self.table(proc(100, 1, "ssh -J bastion devmac"),
                           proc(101, 100, "ssh -W devmac:22 bastion", pgid=100))
        self.assertEqual(find_ssh(table, 100).pid, 100)

    def test_ignores_background_ssh_in_same_shell(self):
        # Pane shell (pgid 50) ran `ssh -N -L ... tunnel &` and then `ssh devmac`.
        table = self.table(proc(50, 1, "-zsh", tpgid=300),
                           proc(200, 50, "ssh -N -L 8080:x:80 tunnel", tpgid=300),
                           proc(300, 50, "ssh devmac", tpgid=300))
        self.assertEqual(find_ssh(table, 50).pid, 300)

    def test_only_background_ssh_means_none(self):
        table = self.table(proc(50, 1, "-zsh", tpgid=50),
                           proc(200, 50, "ssh -N tunnel", tpgid=50))
        self.assertIsNone(find_ssh(table, 50))

    def test_ssh_started_by_wrapper(self):
        table = self.table(proc(50, 1, "-zsh", tpgid=60),
                           proc(60, 50, "/bin/sh ./connect", tpgid=60),
                           proc(61, 60, "ssh devmac", pgid=60, tpgid=60))
        self.assertEqual(find_ssh(table, 50).pid, 61)


class ProcessInfoTest(unittest.TestCase):
    def test_reads_argv_env_and_cwd_of_another_process(self):
        with tempfile.TemporaryDirectory() as cwd:
            # Not an Apple binary: macOS hides the environment of those.
            argv = [sys.executable, "-c", "import time; time.sleep(5)", "an arg"]
            child = subprocess.Popen(argv, cwd=cwd, env={"SSH_AUTH_SOCK": "/tmp/agent sock"})
            try:
                time.sleep(0.3)  # Let the interpreter finish starting up.
                _, child_argv, env = read_proc_args(child.pid)
                self.assertEqual(child_argv[1:], argv[1:])
                self.assertEqual(env.get("SSH_AUTH_SOCK"), "/tmp/agent sock")
                self.assertEqual(os.path.realpath(process_cwd(child.pid)), os.path.realpath(cwd))
            finally:
                child.kill()
                child.wait()


if __name__ == "__main__":
    unittest.main()
