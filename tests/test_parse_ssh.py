"""Tests for the ssh command-line parser. Run with: python3 -m unittest"""

import os
import shlex
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shotdrop import build_upload_argv, parse_ssh_args, tmux_socket_args  # noqa: E402


def parse(cmdline):
    return parse_ssh_args(shlex.split(cmdline))


class ParseSshArgsTest(unittest.TestCase):
    # Cases from the briefing.

    def test_plain_host(self):
        self.assertEqual(parse("ssh devmac"), ([], "devmac"))

    def test_port_and_user(self):
        self.assertEqual(parse("ssh -p 2222 me@10.0.0.5"), (["-p", "2222"], "me@10.0.0.5"))

    def test_identity_and_jump(self):
        self.assertEqual(parse("ssh -i ~/.ssh/k -J bastion host"),
                         (["-i", "~/.ssh/k", "-J", "bastion"], "host"))

    def test_remote_command_discarded(self):
        self.assertEqual(parse("ssh -t devmac tmux attach -t main"), ([], "devmac"))

    def test_o_option_kept(self):
        self.assertEqual(parse("ssh -o Port=2222 devmac"), (["-o", "Port=2222"], "devmac"))

    def test_verbose_and_forward_dropped(self):
        self.assertEqual(parse("ssh -vvv -L 8080:localhost:80 devmac"), ([], "devmac"))

    # ssh grammar details.

    def test_options_after_destination(self):
        self.assertEqual(parse("ssh devmac -p 2222 -t tmux attach"), (["-p", "2222"], "devmac"))

    def test_attached_value(self):
        self.assertEqual(parse("ssh -p2222 -oPort=22 devmac"),
                         (["-p", "2222", "-o", "Port=22"], "devmac"))

    def test_bundled_flags(self):
        self.assertEqual(parse("ssh -tA4p 2222 devmac"), (["-4", "-p", "2222"], "devmac"))

    def test_double_dash(self):
        self.assertEqual(parse("ssh -- devmac -p 2222"), ([], "devmac"))
        self.assertEqual(parse("ssh devmac -- -p 2222"), ([], "devmac"))

    def test_kept_noarg_flags(self):
        self.assertEqual(parse("ssh -4 -6 -C devmac"), (["-4", "-6", "-C"], "devmac"))

    def test_dropped_flags(self):
        self.assertEqual(parse("ssh -t -T -N -f -v -R 9000:localhost:9000 -D 1080 devmac"),
                         ([], "devmac"))

    def test_login_name_config_and_control_path(self):
        self.assertEqual(parse("ssh -l me -F /tmp/cfg -S /tmp/cm devmac"),
                         (["-l", "me", "-F", "/tmp/cfg", "-S", "/tmp/cm"], "devmac"))

    def test_conflicting_o_options_dropped(self):
        self.assertEqual(
            parse("ssh -o RemoteCommand='tmux a' -o RequestTTY=yes -o 'User me' devmac"),
            (["-o", "User me"], "devmac"))

    def test_argument_with_spaces(self):
        self.assertEqual(parse("ssh -o 'ProxyCommand nc -X 5 %h %p' devmac"),
                         (["-o", "ProxyCommand nc -X 5 %h %p"], "devmac"))

    def test_uri_destination(self):
        self.assertEqual(parse("ssh ssh://me@devmac:2222"), ([], "ssh://me@devmac:2222"))

    def test_full_path_argv0(self):
        self.assertEqual(parse("/usr/bin/ssh devmac"), ([], "devmac"))

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
    def test_overrides_come_first(self):
        argv = build_upload_argv(["-p", "2222"], "devmac", "/tmp/shotdrop/x.png")
        self.assertEqual(argv[:3], ["/usr/bin/ssh", "-o", "BatchMode=yes"])
        self.assertLess(argv.index("RemoteCommand=none"), argv.index("-p"))
        self.assertEqual(argv[-3:-1], ["--", "devmac"])
        self.assertIn("cat > /tmp/shotdrop/x.png", argv[-1])


if __name__ == "__main__":
    unittest.main()
