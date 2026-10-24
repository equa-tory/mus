"""Moving songs must not lose likes, playlists, play counts or history (see _scan_worker)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="mus-scan-test-")
os.environ.update(MUSIC_DIR=os.path.join(TMP, "music"), DATA_DIR=os.path.join(TMP, "data"), BACKUP_DIR="", PASSWORD="",
                  LISTEN_ONLY="", REQUIRE_LOGIN="")
os.makedirs(os.environ["MUSIC_DIR"])
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_song(path, n):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={300 + n * 40}:duration=1",
                    "-metadata", f"title=Song {n}", "-metadata", "artist=Art", "-metadata", f"album=Album {n}", path], check=True)


def scan():
    app.SCAN.update(running=True, scanned=0, total=0, added=0, updated=0, removed=0, moved=0, missing=0, merged=0, error=None)
    app._scan_worker()
    assert app.SCAN["error"] is None, app.SCAN["error"]
    return dict(app.SCAN)


def q(sql, *a):
    with app.db() as c:
        return [tuple(r) for r in c.execute(sql, a)]


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class MovingSongs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = os.environ["MUSIC_DIR"]
        for n in range(1, 5):
            make_song(f"{cls.m}/A/s{n}.m4a", n)
        scan()
        cls.ids = {r[1]: r[0] for r in q("SELECT id,title FROM tracks")}
        with app.db() as c:
            c.execute("INSERT INTO playlists(name,is_smart,created_at) VALUES('P',0,1)")
            for pos, t in enumerate(("Song 1", "Song 2", "Song 3")):
                c.execute("INSERT INTO playlist_tracks VALUES(1,?,?)", (cls.ids[t], pos))
            c.execute("UPDATE tracks SET liked=1 WHERE id=?", (cls.ids["Song 1"],))
            c.execute("UPDATE tracks SET play_count=3, last_played_at=5 WHERE id=?", (cls.ids["Song 2"],))
            c.execute("INSERT INTO history(track_id,played_at) VALUES(?,5)", (cls.ids["Song 2"],))

    def snapshot(self):
        return (q("SELECT track_id,position FROM playlist_tracks WHERE playlist_id=1 ORDER BY position"),
                q("SELECT liked FROM tracks WHERE title='Song 1' AND missing=0"),
                q("SELECT play_count FROM tracks WHERE title='Song 2'"), q("SELECT track_id FROM history"))

    def test_1_move_keeps_everything(self):
        before = self.snapshot()
        os.makedirs(f"{self.m}/B/deeper")
        shutil.move(f"{self.m}/A/s1.m4a", f"{self.m}/B/deeper/renamed.m4a")
        shutil.move(f"{self.m}/A/s2.m4a", f"{self.m}/B/s2.m4a")
        r = scan()
        self.assertEqual((r["moved"], r["removed"], r["missing"]), (2, 0, 0))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(q("SELECT id FROM tracks")), 4)
        self.assertEqual(q("SELECT path FROM tracks WHERE id=?", self.ids["Song 1"])[0][0], f"{self.m}/B/deeper/renamed.m4a")

    def test_2_missing_then_back(self):
        before = self.snapshot()
        shutil.move(f"{self.m}/A/s3.m4a", os.path.join(TMP, "outside.m4a"))      # leaves every folder
        shutil.move(f"{self.m}/B/s2.m4a", os.path.join(TMP, "outside2.m4a"))
        r = scan()
        self.assertEqual((r["missing"], r["removed"]), (2, 0))
        self.assertEqual(q("SELECT COUNT(*) FROM tracks WHERE missing=0")[0][0], 2)
        self.assertEqual(self.snapshot(), before)                                  # playlist rows untouched while missing
        self.assertEqual(q("SELECT COUNT(*) FROM tracks t JOIN playlist_tracks p ON p.track_id=t.id WHERE t.missing=1")[0][0], 2)
        os.makedirs(f"{self.m}/C")
        shutil.move(os.path.join(TMP, "outside.m4a"), f"{self.m}/C/back3.m4a")
        shutil.move(os.path.join(TMP, "outside2.m4a"), f"{self.m}/C/back2.m4a")
        r = scan()
        self.assertEqual((r["moved"], r["missing"]), (2, 0))
        self.assertEqual(q("SELECT COUNT(*) FROM tracks WHERE missing=0")[0][0], 4)
        self.assertEqual(self.snapshot(), before)

    def test_3_copy_first_then_delete_original(self):
        orig = q("SELECT path FROM tracks WHERE id=?", self.ids["Song 1"])[0][0]
        os.makedirs(f"{self.m}/D")
        shutil.copy(orig, f"{self.m}/D/copy.m4a")                                 # new mtime, same bytes + tags
        scan()
        self.assertEqual(q("SELECT COUNT(*) FROM tracks WHERE title='Song 1'")[0][0], 2)
        os.remove(orig)
        r = scan()
        self.assertEqual(r["merged"], 1)
        self.assertEqual(q("SELECT COUNT(*) FROM tracks WHERE title='Song 1'")[0][0], 1)
        new_id = q("SELECT id FROM tracks WHERE title='Song 1'")[0][0]
        self.assertEqual(q("SELECT liked FROM tracks WHERE id=?", new_id)[0][0], 1)
        self.assertIn((new_id,), q("SELECT track_id FROM playlist_tracks WHERE playlist_id=1"))
        self.assertEqual(len(q("SELECT track_id FROM playlist_tracks WHERE playlist_id=1")), 3)

    def test_4_unused_deleted_song_is_removed(self):
        os.makedirs(f"{self.m}/E", exist_ok=True)
        make_song(f"{self.m}/E/tmp.m4a", 9)
        scan()
        os.remove(f"{self.m}/E/tmp.m4a")
        r = scan()
        self.assertEqual((r["removed"], r["missing"]), (1, 0))
        self.assertEqual(q("SELECT COUNT(*) FROM tracks WHERE title='Song 9'")[0][0], 0)

    def test_5_migration_adds_column(self):
        with app.db() as c:
            c.execute("ALTER TABLE tracks DROP COLUMN missing")
        app.migrate_db()
        self.assertEqual(q("SELECT COUNT(*) FROM tracks WHERE missing=0")[0][0], len(q("SELECT id FROM tracks")))

    def test_6_missing_folder_aborts_and_keeps_data(self):
        count = len(q("SELECT id FROM tracks"))
        app.roots.set_config([{"path": self.m}])
        os.makedirs(os.path.join(TMP, "other"))
        app.roots.set_config([{"path": self.m}, {"path": os.path.join(TMP, "other")}])
        os.rmdir(os.path.join(TMP, "other"))
        app.SCAN.update(error=None)
        app._scan_worker()
        self.assertIn("not found", app.SCAN["error"])
        self.assertEqual(len(q("SELECT id FROM tracks")), count)
        app.roots.set_config([])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
