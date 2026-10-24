import os
import sqlite3
import tempfile
import unittest

import roots


class RootsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.default = os.path.join(self.d, "music"); os.makedirs(self.default)
        self.other = os.path.join(self.d, "other"); os.makedirs(self.other)
        roots.init(self.default, lambda: sqlite3.connect(os.path.join(self.d, "m.db")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_default(self):
        self.assertEqual([f["path"] for f in roots.config()], [self.default])
        self.assertTrue(roots.config()[0]["main"])
        self.assertFalse(roots.is_custom())

    def test_save_main_and_reset(self):
        roots.set_config([{"path": self.default}, {"path": self.other, "main": True}])
        self.assertEqual(str(roots.main_root()), self.other)
        self.assertEqual(len(roots.roots()), 2)
        roots.set_config([{"path": self.default}, {"path": self.other}, {"path": self.other}])   # dup dropped, first = main
        self.assertEqual(str(roots.main_root()), self.default)
        self.assertEqual(len(roots.roots()), 2)
        roots.set_config([])
        self.assertFalse(roots.is_custom())

    def test_rejects(self):
        inner = os.path.join(self.default, "sub"); os.makedirs(inner)
        for bad in ([{"path": "relative"}], [{"path": os.path.join(self.d, "nope")}],
                    [{"path": self.default}, {"path": inner}], [{"path": ""}], "x"):
            with self.assertRaises(ValueError, msg=str(bad)):
                roots.set_config(bad)
        self.assertFalse(roots.is_custom())

    def test_sibling_prefix_is_not_nested(self):
        sib = self.default + "2"; os.makedirs(sib)       # /a/music vs /a/music2
        roots.set_config([{"path": self.default}, {"path": sib}])
        self.assertEqual(len(roots.roots()), 2)


if __name__ == "__main__":
    unittest.main()
