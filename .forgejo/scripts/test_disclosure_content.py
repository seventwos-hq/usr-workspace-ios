"""Tests for disclosure_check whole-file (UTF-16/32) scanning. Run: python3 -m unittest discover -s .forgejo/scripts"""
import codecs
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disclosure_check as dc  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.old)
        self.git("init", "-q", "-b", "main")
        self.deny = dc.compile_terms(["café", "secretterm"])

    def git(self, *args):
        return subprocess.run(("git",) + args, check=True, capture_output=True, env=GIT_ENV).stdout

    def commit(self, files):
        for name, data in files.items():
            with open(name, "wb") as f:
                f.write(data)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")
        return self.git("rev-parse", "HEAD").decode().strip()

    def scan(self, base):
        return dc.scan_content(base, self.deny)


class WideContent(RepoCase):
    def test_added_utf16_file_is_flagged(self):
        base = self.commit({"a.txt": b"x\n"})
        self.commit({"w.txt": "secretterm here\n".encode("utf-16-le")})
        self.assertTrue(any("denylist" in h for h in self.scan(base)))

    def test_deletion_only_edit_is_flagged(self):
        # U+0A15 is bytes 15 0A; removing that line leaves the term standalone.
        wide = "secretterm".encode("utf-16-le")
        before = b"\x15\n" + wide
        self.commit({"w.txt": b"x\n"})
        base = self.commit({"w.txt": before})
        self.commit({"w.txt": wide})
        # the edit is a pure line deletion: no added lines in the diff
        self.assertEqual([], list(dc.added_lines(base)))
        self.assertTrue(any("denylist" in h for h in self.scan(base)))

    def test_unchanged_wide_file_not_flagged(self):
        self.commit({"w.txt": "secretterm\n".encode("utf-16-le")})
        base = self.commit({"a.txt": b"x\n"})
        self.commit({"b.txt": b"y\n"})
        self.assertEqual([], self.scan(base))

    def test_forgejo_dir_skipped(self):
        base = self.commit({"a.txt": b"x\n"})
        os.makedirs(".forgejo")
        self.commit({".forgejo/w.txt": "secretterm\n".encode("utf-16-le")})
        self.assertEqual([], self.scan(base))


class BomContent(RepoCase):
    def test_bom_prefixed_files_are_flagged(self):
        text = "secretterm here\n"
        cases = {
            "utf-8-bom": codecs.BOM_UTF8 + text.encode("utf-8"),
            "utf-16-le-bom": codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
            "utf-16-be-bom": codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
            "utf-32-le-bom": codecs.BOM_UTF32_LE + text.encode("utf-32-le"),
            "utf-32-be-bom": codecs.BOM_UTF32_BE + text.encode("utf-32-be"),
        }
        base = self.commit({"a.txt": b"x\n"})
        for name, data in cases.items():
            with self.subTest(name):
                self.commit({"w.txt": data})
                self.assertTrue(any("denylist" in h for h in self.scan(base)))
                self.git("reset", "-q", "--hard", base)

    def test_clean_bom_files_are_not_flagged(self):
        base = self.commit({"a.txt": b"x\n"})
        self.commit({"u16.txt": codecs.BOM_UTF16_LE + "hello\n".encode("utf-16-le"),
                     "u32.txt": codecs.BOM_UTF32_BE + "hello\n".encode("utf-32-be"),
                     "u8.txt": codecs.BOM_UTF8 + b"hello\n"})
        self.assertEqual([], self.scan(base))

    def test_oversize_blob_fails_closed(self):
        self.commit({"a.txt": b"x\n"})
        self.commit({"secretname.bin": b"y" * 64})
        old = dc.MAX_BLOB_BYTES
        dc.MAX_BLOB_BYTES = 32
        self.addCleanup(setattr, dc, "MAX_BLOB_BYTES", old)
        with self.assertRaises(dc.InputError) as cm:
            dc.blob_at_head(b"secretname.bin", 7)
        msg = str(cm.exception)
        self.assertIn("#7", msg)
        self.assertNotIn("secretname", msg)


if __name__ == "__main__":
    unittest.main()
