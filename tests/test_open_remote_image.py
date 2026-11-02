"""Tests for "Open Remote Image". Run with: python3 -m unittest"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import shotdrop  # noqa: E402
from shotdrop import (  # noqa: E402
    ShotDropError, build_download_command, choose_image_path, find_image_paths, image_type,
    is_safe_remote_image_path, join_soft_wraps, local_image_name, parse_download_output,
    remove_old_downloads,
)

CLAUDE_PATH = ("/private/tmp/claude-501/-Users-roy-Projects-testapp/"
               "9fbb9c58-71a1-464d-9c56-5f64084912d2/scratchpad/counter-6.jpg")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20


class FindImagePathsTest(unittest.TestCase):
    def test_path_in_claude_output(self):
        self.assertEqual(find_image_paths([f"- Screenshot: {CLAUDE_PATH}"]), [CLAUDE_PATH])

    def test_several_paths_in_order(self):
        lines = ["saved /tmp/a.png and `/tmp/b.JPEG`.", "then /tmp/c.webp"]
        self.assertEqual(find_image_paths(lines), ["/tmp/a.png", "/tmp/b.JPEG", "/tmp/c.webp"])

    def test_not_images_or_not_absolute(self):
        lines = ["/tmp/a.png.bak /tmp/a.pngx /tmp/a.png/ x.png ./x.png ~/x.png and/x.png",
                 "/tmp/notes.txt"]
        self.assertEqual(find_image_paths(lines), [])

    def test_trailing_punctuation(self):
        self.assertEqual(find_image_paths(["See /tmp/a.png. Or (/tmp/b.gif), '/tmp/c.png'"]),
                         ["/tmp/a.png", "/tmp/b.gif", "/tmp/c.png"])

    def test_path_broken_across_lines_with_indent(self):
        lines = ["  - Screenshot: /private/tmp/claude-501/-Users-roy-Projects-testapp/9fbb9c58-71",
                 "    a1-464d-9c56-5f64084912d2/scratchpad/counter-6.jpg (shown above)"]
        self.assertEqual(find_image_paths(lines), [CLAUDE_PATH])

    def test_path_broken_over_three_lines(self):
        lines = ["x /private/tmp/claude-501/-Users-roy",
                 "-Projects-testapp/9fbb9c58-71a1-464d-9c56-5f64084912d2",
                 "/scratchpad/counter-6.jpg"]
        # The last line on its own looks like a path, but it's the tail of
        # the first one.
        self.assertEqual(find_image_paths(lines), [CLAUDE_PATH])

    def test_break_before_slash_is_not_a_second_path(self):
        lines = ["/private/tmp/scratchpad", "/counter-6.jpg"]
        self.assertEqual(find_image_paths(lines), ["/private/tmp/scratchpad/counter-6.jpg"])

    def test_unrelated_next_line_keeps_its_paths(self):
        lines = ["cd /Users/roy", "ls: /tmp/a.png"]
        self.assertEqual(find_image_paths(lines), ["/tmp/a.png"])

    def test_blank_line_stops_continuation(self):
        lines = ["/tmp/dir", "", "x.png"]
        self.assertEqual(find_image_paths(lines), [])

    def test_path_at_line_end_is_complete(self):
        self.assertEqual(find_image_paths(["saved /tmp/a.png", "/tmp/b.png"]),
                         ["/tmp/a.png", "/tmp/b.png"])


class JoinSoftWrapsTest(unittest.TestCase):
    def test_joins_until_hard_newline(self):
        lines = [("abc /tmp/x", False), ("y.png", True), ("next", True), ("tail", False)]
        self.assertEqual(join_soft_wraps(lines), ["abc /tmp/xy.png", "next", "tail"])


class ChooseImagePathTest(unittest.TestCase):
    screen = ["one /tmp/a.png", "two /tmp/b.png", "$ "]

    def test_without_selection_takes_last_on_screen(self):
        self.assertEqual(choose_image_path("", self.screen), "/tmp/b.png")

    def test_selected_path_wins(self):
        self.assertEqual(choose_image_path("/tmp/a.png", self.screen), "/tmp/a.png")
        self.assertEqual(choose_image_path("off screen /tmp/z.gif", self.screen), "/tmp/z.gif")

    def test_partial_selection_picks_path_it_belongs_to(self):
        screen = [f"- Screenshot: {CLAUDE_PATH[:50]}", f"   {CLAUDE_PATH[50:]}", "/tmp/b.png"]
        self.assertEqual(choose_image_path(CLAUDE_PATH[:50], screen), CLAUDE_PATH)
        self.assertEqual(choose_image_path("counter-6", screen), CLAUDE_PATH)

    def test_selection_without_path_is_an_error(self):
        with self.assertRaises(ShotDropError):
            choose_image_path("Screenshot:", self.screen)

    def test_nothing_found_is_an_error(self):
        with self.assertRaises(ShotDropError):
            choose_image_path("", ["$ ls", "notes.txt"])


class SafePathTest(unittest.TestCase):
    def test_safe(self):
        self.assertTrue(is_safe_remote_image_path(CLAUDE_PATH))
        self.assertTrue(is_safe_remote_image_path("/tmp/a..b.png"))

    def test_unsafe(self):
        for path in ["/tmp/../etc/x.png", "tmp/x.png", "/tmp/x.txt", "/tmp/x y.png",
                     "/tmp/x.png\n", "/tmp/$(id).png", "/" + "a" * 1100 + ".png"]:
            self.assertFalse(is_safe_remote_image_path(path), path)


class DownloadCommandTest(unittest.TestCase):
    """Runs the download command locally with /bin/sh."""

    def run_remote(self, path, max_bytes=1000, login_noise=b""):
        cmd = build_download_command(path, max_bytes)
        result = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True)
        return result.returncode, login_noise + result.stdout, result.stderr.decode()

    def write(self, directory, name, data):
        path = os.path.join(directory, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_round_trip_with_login_script_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, "it's a shot.png", PNG)
            code, out, err = self.run_remote(path, login_noise=b"Welcome!")
            self.assertEqual(code, 0, err)
            self.assertEqual(parse_download_output(out), PNG)

    def test_data_containing_the_marker(self):
        data = PNG + b"\nshotdrop-data:3\nabc"
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.run_remote(self.write(d, "x.png", data))
            self.assertEqual(code, 0, err)
            self.assertEqual(parse_download_output(out), data)

    def test_missing_file(self):
        code, _, err = self.run_remote("/nonexistent/x.png")
        self.assertNotEqual(code, 0)
        self.assertIn("no such file", err)

    def test_too_large(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = self.run_remote(self.write(d, "x.png", PNG), max_bytes=10)
            self.assertNotEqual(code, 0)
            self.assertIn("larger than", err)

    def test_directory_named_like_an_image(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "x.png"))
            self.assertNotEqual(self.run_remote(os.path.join(d, "x.png"))[0], 0)

    def test_truncated_or_missing_output(self):
        self.assertIsNone(parse_download_output(b"\nshotdrop-data:30\n" + PNG[:10]))
        self.assertIsNone(parse_download_output(b"no marker"))
        self.assertIsNone(parse_download_output(b"\nshotdrop-data:"))


class ImageTypeTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(image_type(PNG), "png")
        self.assertEqual(image_type(b"\xff\xd8\xff\xe0rest"), "jpg")
        self.assertEqual(image_type(b"GIF89a..."), "gif")
        self.assertEqual(image_type(b"RIFF\0\0\0\0WEBPVP8 "), "webp")
        self.assertEqual(image_type(b"\0\0\0\x18ftypheic"), "heic")
        self.assertEqual(image_type(b"MM\0*rest"), "tiff")

    def test_not_an_image(self):
        self.assertIsNone(image_type(b"<html><script>"))
        self.assertIsNone(image_type(b""))


class LocalFilesTest(unittest.TestCase):
    def test_name(self):
        name = local_image_name("devmac", CLAUDE_PATH, "jpg")
        self.assertRegex(name, r"^counter-6-[0-9a-f]{12}\.jpg$")
        self.assertNotEqual(name, local_image_name("other", CLAUDE_PATH, "jpg"))
        self.assertRegex(local_image_name("h", "/tmp/©®.png", "png"), r"^image-")
        self.assertRegex(local_image_name("h", "/tmp/a b;c.png", "png"), r"^a_b_c-")
        self.assertTrue(shotdrop.DOWNLOAD_NAME_RE.search(name))

    def test_removes_only_own_old_files(self):
        with tempfile.TemporaryDirectory() as d:
            old = time.time() - (shotdrop.RETENTION_DAYS + 1) * 86400
            names = {"old": local_image_name("h", "/tmp/a.png", "png"),
                     "new": local_image_name("h", "/tmp/b.png", "png"),
                     "foreign": "holiday.png"}
            for key, name in names.items():
                path = os.path.join(d, name)
                open(path, "wb").close()
                if key != "new":
                    os.utime(path, (old, old))
            remove_old_downloads(d)
            self.assertEqual(sorted(os.listdir(d)), sorted([names["new"], "holiday.png"]))


if __name__ == "__main__":
    unittest.main()
