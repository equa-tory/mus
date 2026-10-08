import os
import sqlite3
import tempfile
import unicodedata
import unittest
from pathlib import Path

import macpull


class SourceTests(unittest.TestCase):
    def test_normalize(self):
        want = "equa@192.168.1.73:/Users/equa/Music/"
        for raw in ("equa@192.168.1.73:/Users/equa/Music", "equa@192.168.1.73:/Users/equa/Music/",
                    " equa@192.168.1.73:/Users/equa/Music/* "):
            self.assertEqual(macpull.normalize_source(raw), want)
        self.assertTrue(macpull.normalize_source("me@host.local:~/Music").endswith("~/Music/"))

    def test_rejects(self):
        for bad in ("", "nope", "host:/x", "-oProxyCommand=x@h:/a", "a@b:relative", "a@b:/x\ny", "a@-b:/x"):
            with self.assertRaises(ValueError, msg=bad):
                macpull.normalize_source(bad)


class ListingTests(unittest.TestCase):
    def test_parse(self):
        text = ("drwxr-xr-x             96 2026/04/21 01:18:40 .\n"
                "drwxr-xr-x             96 2026/04/21 01:18:40 Fragments\n"
                "-rwx------      5,323,405 2025/09/03 23:53:41 Fragments/a b (feat. X).m4a\n")
        self.assertEqual(macpull.parse_listing(text), [("Fragments/a b (feat. X).m4a", 5323405)])

    def test_pick_new_ignores_unicode_normalisation(self):
        nfd = unicodedata.normalize("NFD", "Café/Álbum/Tëst.m4a")
        have = {unicodedata.normalize("NFC", nfd)}
        new, long_ = macpull.pick_new([(nfd, 1), ("A/B/c.m4a", 2), ("A/B/" + "x" * 300 + ".m4a", 3)], have)
        self.assertEqual(new, [("A/B/c.m4a", 2)])
        self.assertEqual(len(long_), 1)

    def test_local_keys_and_place(self):
        with tempfile.TemporaryDirectory() as d:
            nfd_dir = unicodedata.normalize("NFD", "Café")
            os.makedirs(Path(d, nfd_dir, "Alb"))
            Path(d, nfd_dir, "Alb", "x.m4a").write_bytes(b"1")
            Path(d, nfd_dir, "Alb", "cover.jpg").write_bytes(b"1")
            self.assertEqual(macpull.local_keys(d), {unicodedata.normalize("NFC", "Café/Alb/x.m4a")})
            # a new song for the same artist lands in the existing (decomposed) folder
            self.assertEqual(macpull._place(d, "Café/Alb/y.m4a"), Path(d, nfd_dir, "Alb", "y.m4a"))
            self.assertEqual(macpull._place(d, "New/Alb/y.m4a"), Path(d, "New", "Alb", "y.m4a"))


class SettingTests(unittest.TestCase):
    def test_source_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            macpull.init(d, d, lambda: sqlite3.connect(os.path.join(d, "m.db")))
            self.assertEqual(macpull.get_source(), macpull.DEFAULT_SOURCE)
            self.assertTrue(macpull.set_source("a@b.c:/m")["custom"])
            self.assertEqual(macpull.get_source(), "a@b.c:/m/")
            self.assertFalse(macpull.set_source("")["custom"])


if __name__ == "__main__":
    unittest.main()
