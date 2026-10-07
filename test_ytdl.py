"""Tests for the YouTube music downloader (ytdl.py) and its routes.

    venv/bin/python -m unittest -v test_ytdl

Nothing here talks to YouTube or touches the real library/data: every test gets scratch
folders, and the job runner is exercised against a tiny stand-in for yt-dlp.
"""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ytdl


def make_m4a(path, with_cover=True, **tags):
    """A real (1 s sine) m4a, optionally with an embedded JPEG cover, via ffmpeg."""
    d = os.path.dirname(path)
    cover = os.path.join(d, "cover.jpg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                    "-frames:v", "1", cover], check=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1"]
    if with_cover:
        cmd += ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    cmd += ["-c:a", "aac"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [path], check=True)
    os.remove(cover)


class Scratch(unittest.TestCase):
    """Fresh DATA_DIR + download dir + sqlite db per test."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = self.root / "data"
        self.dl = self.root / "Loop"
        self.data.mkdir()
        self.dl.mkdir()
        self.dbp = self.data / "library.db"

        def db():
            c = sqlite3.connect(self.dbp)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            return c

        c = db()
        c.executescript("CREATE TABLE tracks (id INTEGER PRIMARY KEY, path TEXT, title TEXT, artist TEXT);")
        c.commit()
        c.close()
        ytdl.init(self.data, self.dl, db)
        self.db = db
        ytdl.JOB.clear()
        ytdl.JOB["status"] = "idle"

    def entries(self, *titles):
        return [{"id": f"vid{i:08d}", "title": t, "duration": 200, "index": i + 1, "channel": "Chan"}
                for i, t in enumerate(titles)]

    def source(self, *titles):
        sid = ytdl.add_source("https://www.youtube.com/playlist?list=PL1", "Mine")
        with self.db() as c:
            c.execute("UPDATE dl_sources SET entries_json=?", (json.dumps(self.entries(*titles)),))
        return sid


class HelperTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(ytdl.normalize_url("https://www.youtube.com/watch?v=abc&list=PL123"),
                         "https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(ytdl.normalize_url("https://youtu.be/dQw4w9WgXcQ"), "https://youtu.be/dQw4w9WgXcQ")
        for bad in ("", "ftp://youtube.com/x", "https://evil.example/playlist?list=PL1", "youtube.com"):
            with self.assertRaises(ValueError):
                ytdl.normalize_url(bad)

    def test_name_key_ignores_punctuation_case_and_width(self):
        self.assertEqual(ytdl.name_key("Hello: World? | Ｆｕｌｌ"), ytdl.name_key("hello world full"))
        self.assertEqual(ytdl.name_key("次元通信！"), ytdl.name_key("次元通信"))

    def test_cookie_parsing(self):
        body, n, login = ytdl.normalize_cookies("SID=abc; HSID=def; LOGIN_INFO=x")
        self.assertEqual((n, login), (3, True))
        self.assertTrue(body.startswith("# Netscape HTTP Cookie File"))
        net = ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tv\n#HttpOnly_.youtube.com TRUE / TRUE 1 LOGIN_INFO z\n"
        self.assertEqual(ytdl.normalize_cookies(net)[1:], (2, True))
        with self.assertRaises(ValueError):
            ytdl.normalize_cookies("   ")

    def test_clean_title_and_channel(self):
        self.assertEqual(ytdl.clean_title("Song (Official Video) [HD]"), "Song")
        self.assertEqual(ytdl.clean_title("Song 【Official Audio】"), "Song")
        self.assertEqual(ytdl.clean_title("Song (feat. X) (Remix)"), "Song (feat. X) (Remix)")
        self.assertEqual(ytdl.clean_title("次元通信【初音ミク】"), "次元通信【初音ミク】")   # not noise: kept
        self.assertEqual(ytdl.clean_channel("ABM - Topic"), "ABM")
        self.assertEqual(ytdl.clean_channel("2PacVEVO"), "2Pac")

    def test_derive_tags(self):
        d = ytdl.derive_tags
        # YouTube Music / "Topic" upload: real track, artists, album
        self.assertEqual(d({"title": "x", "track": "次元通信", "artists": ["ABM", "初音ミク", "重音テト"],
                            "album": "Signaling", "channel": "ABM - Topic"}),
                         {"title": "次元通信", "artist": "ABM / 初音ミク / 重音テト", "album": "Signaling"})
        # repeated artists (composer listed again as performer) collapse
        self.assertEqual(d({"track": "T", "artists": ["A", "B", "a", "C", "B"]})["artist"], "A / B / C")
        # "Artist - Title" is split only when the channel confirms a side ...
        self.assertEqual(d({"title": "Mitski - Your Best American Girl (Official Audio)", "uploader": "Mitski"}),
                         {"title": "Your Best American Girl", "artist": "Mitski", "album": "Your Best American Girl"})
        self.assertEqual(d({"title": "Your Best American Girl - Mitski", "uploader": "Mitski"})["artist"], "Mitski")
        self.assertEqual(d({"title": "Your Best American Girl - Mitski", "uploader": "Mitski"})["title"],
                         "Your Best American Girl")
        # ... otherwise the channel is the artist (Lasso's rule), title untouched
        r = d({"title": "Fake Love - MINUS4 _ 偽愛 - マイナスヨンド (Official Video)",
               "channel": "-4℃【マイナスヨンド】official"})
        self.assertEqual((r["artist"], r["title"]),
                         ("-4℃【マイナスヨンド】", "Fake Love - MINUS4 _ 偽愛 - マイナスヨンド"))
        # no channel at all: usual order
        self.assertEqual(d({"title": "Artist - Song"}), {"title": "Song", "artist": "Artist", "album": "Song"})
        # single with a Topic/VEVO channel; album falls back to the title
        self.assertEqual(d({"title": "California Love (Official Music Video)", "channel": "2PacVEVO"}),
                         {"title": "California Love", "artist": "2Pac", "album": "California Love"})
        self.assertEqual(d({}), {"title": "", "artist": "", "album": ""})


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed")
class RetagTests(Scratch):
    def test_retag_sets_tags_and_keeps_the_cover(self):
        from mutagen.mp4 import MP4
        p = str(self.dl / "Some Video Title.m4a")
        make_m4a(p, title="Some Video Title", artist="Channel")
        self.assertTrue(ytdl.retag(p, {"title": "Song", "artist": "A / B", "album": "Alb"}))
        t = MP4(p).tags
        self.assertEqual((t["\xa9nam"], t["\xa9ART"], t["\xa9alb"]), (["Song"], ["A / B"], ["Alb"]))
        self.assertTrue(t.get("covr"), "cover art must survive retagging")

    def test_retag_ignores_empty_values_and_other_files(self):
        from mutagen.mp4 import MP4
        p = str(self.dl / "x.m4a")
        make_m4a(p, with_cover=False, title="Keep", artist="Keep Artist")
        ytdl.retag(p, {"title": "", "artist": "", "album": "New"})
        t = MP4(p).tags
        self.assertEqual((t["\xa9nam"], t["\xa9ART"], t["\xa9alb"]), (["Keep"], ["Keep Artist"], ["New"]))
        self.assertFalse(ytdl.retag(str(self.dl / "missing.m4a"), {"title": "x"}))
        (self.dl / "a.opus").write_bytes(b"x")
        self.assertFalse(ytdl.retag(str(self.dl / "a.opus"), {"title": "x"}))


class CookieTests(Scratch):
    def test_saved_privately_and_status_has_no_secrets(self):
        ytdl.save_cookies("SID=supersecret; HSID=other")
        mode = stat.S_IMODE(os.stat(ytdl.cookie_path()).st_mode)
        self.assertEqual(mode, 0o600)
        st = ytdl.cookie_status()
        self.assertNotIn("supersecret", json.dumps(st))
        self.assertEqual((st["set"], st["count"], st["has_login"]), (True, 2, True))
        self.assertFalse(st["expired"])
        self.assertIsNone(st["login_expires"])          # a pasted header has no real expiry
        ytdl.clear_cookies()
        self.assertEqual(ytdl.cookie_status(), {"set": False})

    def test_expiry_from_login_cookies(self):
        def jar(exp):
            rows = ["# Netscape HTTP Cookie File"] + [
                "\t".join([".youtube.com", "TRUE", "/", "TRUE", str(e), n, "v"])
                for n, e in (("SID", exp), ("VISITOR_INFO1_LIVE", 1))]
            Path(ytdl.cookie_path()).write_text("\n".join(rows) + "\n")
        soon = int(time.time()) + 86400
        jar(soon)
        self.assertEqual(ytdl.cookie_status()["login_expires"], soon)
        jar(int(time.time()) - 10)
        self.assertTrue(ytdl.cookie_status()["expired"])

    def probe(self, stderr="", stdout="", rc=0):
        ytdl.save_cookies("SID=x; HSID=y")
        fake = mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)
        with mock.patch.object(ytdl, "ytdlp_available", return_value=True), \
             mock.patch.object(ytdl.subprocess, "run", return_value=fake):
            return ytdl.check_cookies()

    def test_health_probe_outcomes(self):
        self.assertIs(self.probe(stdout='{"title": "Watch later"}')["ok"], True)
        self.assertIs(self.probe(stderr="ERROR: WL: YouTube said: The playlist does not exist.", rc=1)["ok"], False)
        self.assertIs(self.probe(stderr="WARNING: account cookies are no longer valid", rc=1)["ok"], False)
        self.assertIsNone(self.probe(stderr="ERROR: unable to download: connection reset", rc=1)["ok"])
        self.assertIsNone(ytdl.cookie_status()["check"]["ok"])
        time.sleep(0.05)
        ytdl.save_cookies("SID=other; HSID=y")                  # new cookies: the old verdict is void
        self.assertIsNone(ytdl.cookie_status()["check"])

    def test_cookies_ok_for_job(self):
        self.assertIn("required", ytdl.cookies_ok_for_job())
        ytdl.save_cookies("SID=x; HSID=y")
        with mock.patch.object(ytdl, "check_cookies", return_value={"ok": False, "checked": time.time()}):
            self.assertIn("don't work anymore", ytdl.cookies_ok_for_job())
        with mock.patch.object(ytdl, "check_cookies", return_value={"ok": None, "checked": time.time()}):
            self.assertIsNone(ytdl.cookies_ok_for_job())        # can't tell: don't block
        ytdl._write_check(True, "Signed in")
        with mock.patch.object(ytdl, "check_cookies") as probe:
            self.assertIsNone(ytdl.cookies_ok_for_job())        # fresh good verdict is reused
        probe.assert_not_called()


class SourceTests(Scratch):
    def test_add_delete_and_duplicates(self):
        sid = ytdl.add_source("https://www.youtube.com/watch?v=abc&list=PL9")
        self.assertEqual([s["url"] for s in ytdl.list_sources()], ["https://www.youtube.com/playlist?list=PL9"])
        with self.assertRaises(ValueError):
            ytdl.add_source("https://www.youtube.com/playlist?list=PL9")
        ytdl.delete_source(sid)
        self.assertEqual(ytdl.list_sources(), [])
        with self.assertRaises(LookupError):
            ytdl.delete_source(sid)

    def test_statuses(self):
        sid = self.source("Alpha - Artist", "Beta", "Gamma", "Delta", "[Private video]", "Epsilon")
        es = self.entries("Alpha - Artist", "Beta", "Gamma", "Delta", "[Private video]", "Epsilon")
        (self.dl / "Alpha - Artist.m4a").write_bytes(b"x")                      # name match in the folder
        with self.db() as c:
            c.execute("INSERT INTO tracks(path,title,artist) VALUES(?,?,?)", ("/m/a/Beta.m4a", "Beta", "Someone"))
        ytdl.mark(sid, [es[2]["id"]])                                           # Gamma skipped
        with open(ytdl.archive_path("https://www.youtube.com/playlist?list=PL1"), "w") as f:
            f.write(f"youtube {es[3]['id']}\n")                                 # Delta only in the archive
        st = ytdl.source_state(sid)
        got = {e["title"]: e["status"] for e in st["entries"]}
        self.assertEqual(got, {"Alpha - Artist": "downloaded", "Beta": "library", "Gamma": "skipped",
                               "Delta": "archived", "[Private video]": "unavailable", "Epsilon": "new"})
        self.assertEqual(st["counts"]["new"], 1)
        self.assertEqual(st["disk"]["dir"], str(self.dl))

    def test_library_matches_artist_and_title(self):
        sid = self.source("Mitski - Your Best American Girl (Official Audio)")
        with self.db() as c:
            c.execute("INSERT INTO tracks(path,title,artist) VALUES(?,?,?)",
                      ("/m/y.m4a", "Your Best American Girl", "Mitski"))
        # video's channel is "Chan" so the split is unconfirmed -> no false match on the bare title
        self.assertEqual(ytdl.source_state(sid)["entries"][0]["status"], "new")
        with self.db() as c:
            c.execute("UPDATE dl_sources SET entries_json=?", (json.dumps(
                [{"id": "vid00000000", "title": "Mitski - Your Best American Girl", "channel": "Mitski"}]),))
        self.assertEqual(ytdl.source_state(sid)["entries"][0]["status"], "library")

    def test_bulk_skip_unskip_accepts_only_known_ids(self):
        sid = self.source("A", "B", "C", "D")
        es = self.entries("A", "B", "C", "D")
        n = ytdl.mark(sid, [e["id"] for e in es[:3]] + ["zzzzzzzzzzz", es[0]["id"]])
        self.assertEqual(n, 3)
        self.assertEqual(ytdl.source_state(sid)["counts"], {"skipped": 3, "new": 1})
        ytdl.mark(sid, [es[1]["id"]], skip=False)
        self.assertEqual(ytdl.source_state(sid)["counts"], {"skipped": 2, "new": 2})
        ytdl.delete_source(sid)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM dl_skipped").fetchone()[0], 0)

    def test_fetch_caches_and_failure_keeps_the_cache(self):
        sid = self.source("Keep")
        with mock.patch.object(ytdl, "fetch_playlist", return_value=("Name", self.entries("New1", "New2"), "")):
            ytdl.fetch_source(sid)
        self.assertEqual([e["title"] for e in ytdl.source_state(sid)["entries"]], ["New1", "New2"])
        with mock.patch.object(ytdl, "fetch_playlist", return_value=("", [], "boom")):
            with self.assertRaises(ValueError):
                ytdl.fetch_source(sid)
        self.assertEqual(len(ytdl.source_state(sid)["entries"]), 2)

    def test_tables_are_recreated_after_a_restore_of_an_old_backup(self):
        with self.db() as c:
            c.executescript("DROP TABLE dl_skipped; DROP TABLE dl_sources;")
        self.assertEqual(ytdl.list_sources(), [])               # no crash: tables re-created on demand


class BuildCmdTests(Scratch):
    def test_audio_command(self):
        ytdl.save_cookies("SID=x; HSID=y")
        cmd, before, after, batch = ytdl.build_cmd(["aaaaaaaaaaa", "bbbbbbbbbbb"], self.dl, "arch.txt",
                                                   ytdl.cookie_path())
        j = " ".join(cmd)
        for needle in ("-x --audio-format m4a", "--embed-thumbnail", "--convert-thumbnails jpg", "--embed-metadata",
                       "player_client=web_embedded,default", "--download-archive arch.txt", "--cookies"):
            self.assertIn(needle, j)
        self.assertIn("%(title)s.%(ext)s", j)
        self.assertEqual(Path(batch).read_text().split(),
                         ["https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=bbbbbbbbbbb"])
        # all marker files live under data/ytdl, never in the download folder
        for p in (before, after, batch):
            self.assertTrue(p.startswith(str(self.data)))

    def test_read_after_parses_and_deletes(self):
        p = self.data / "a.txt"
        p.write_text("aaaaaaaaaaa\t/x/A.m4a\t" + json.dumps({"title": "T", "artists": ["a", "b"]}, ensure_ascii=False)
                     + "\nbbbbbbbbbbb\n\nccccccccccc\t/x/C.m4a\tnot-json\n")
        got = ytdl.read_after(str(p))
        self.assertEqual(got[0], ("aaaaaaaaaaa", "/x/A.m4a", {"title": "T", "artists": ["a", "b"]}))
        self.assertEqual(got[1], ("bbbbbbbbbbb", "", {}))
        self.assertEqual(got[2], ("ccccccccccc", "/x/C.m4a", {}))
        self.assertFalse(p.exists())


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed")
class JobTests(Scratch):
    """The job runner against a stand-in for yt-dlp: it 'downloads' by copying a prepared m4a to
    <dir>/<title>.m4a and writes the same markers the real command is wired to write."""

    SCRIPT = (
        "import sys, json, shutil, time\n"
        "mode, src, outdir, before, after, *ids = sys.argv[1:]\n"
        "open(before, 'w').write('\\n'.join(ids) + '\\n')\n"
        "for n, i in enumerate(ids):\n"
        "    print('HFP|%s|  50.0%%|Title %s' % (i, i), flush=True)\n"
        "    if mode == 'dead':\n"
        "        print('WARNING: [youtube] The provided YouTube account cookies are no longer valid.', flush=True)\n"
        "        time.sleep(30)\n"
        "    if mode == 'slow': time.sleep(30)\n"
        "    if mode == 'partial' and n == 1:\n"
        "        print(\"ERROR: [youtube] x: Sign in to confirm you're not a bot\", flush=True); continue\n"
        "    dst = '%s/Video %s.m4a' % (outdir, i)\n"
        "    shutil.copy(src, dst)\n"
        "    info = {'title': 'Video %s - Official Video' % i, 'channel': 'Chan - Topic', 'track': 'Track %s' % i,\n"
        "            'artists': ['Art A', 'Art B'], 'album': 'Album %s' % i}\n"
        "    open(after, 'a').write('%s\\t%s\\t%s\\n' % (i, dst, json.dumps(info)))\n"
    )

    def setUp(self):
        super().setUp()
        self.src = str(self.root / "src.m4a")
        make_m4a(self.src, title="orig", artist="orig")
        ytdl.save_cookies("SID=x; HSID=y")
        self.sid = self.source("One", "Two")
        patches = [mock.patch.object(ytdl, "ytdlp_available", return_value=True),
                   mock.patch.object(ytdl, "js_runtime", return_value={
                       "name": "deno", "path": "/x/deno", "version": "2.9.7", "supported": True,
                       "args": ["--js-runtimes", "deno:/x/deno"]}),
                   mock.patch.object(ytdl, "check_cookies", return_value={"ok": True, "checked": time.time()})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_job(self, ids, mode="ok", wait=30):
        def fake_build(ids_, out_dir, archive, cookie):
            before, after = str(self.data / "b.txt"), str(self.data / "a.txt")
            return ([sys.executable, "-c", self.SCRIPT, mode, self.src, str(out_dir), before, after, *ids_],
                    before, after, str(self.data / "u.txt"))
        with mock.patch.object(ytdl, "build_cmd", side_effect=fake_build):
            ok, err = ytdl.start_job(self.sid, ids)
            self.assertTrue(ok, err)
            end = time.time() + wait
            while ytdl.job_state()["status"] in ("starting", "running") and time.time() < end:
                time.sleep(0.05)
        return ytdl.job_state()

    def test_success_downloads_and_retags(self):
        from mutagen.mp4 import MP4
        job = self.run_job(["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertEqual((job["status"], job["done"], job["fail"], job["summary"]), ("done", 2, 0, "2 downloaded"))
        t = MP4(str(self.dl / "Video aaaaaaaaaaa.m4a")).tags
        self.assertEqual((t["\xa9nam"], t["\xa9ART"], t["\xa9alb"]),
                         (["Track aaaaaaaaaaa"], ["Art A / Art B"], ["Album aaaaaaaaaaa"]))
        self.assertTrue(t.get("covr"))
        self.assertEqual(sorted(os.listdir(self.data / "ytdl")), ["cookie_check.json", "cookies.txt"]
                         if (self.data / "ytdl" / "cookie_check.json").exists() else ["cookies.txt"])

    def test_partial_success_reports_both(self):
        job = self.run_job(["aaaaaaaaaaa", "bbbbbbbbbbb"], mode="partial")
        self.assertEqual((job["status"], job["done"], job["fail"]), ("done", 1, 1))
        self.assertEqual(job["summary"], "1 downloaded, 1 failed or unavailable")
        self.assertIn("not a bot", job["log"])

    def test_a_selected_id_is_forgotten_in_the_archive(self):
        arch = ytdl.archive_path("https://www.youtube.com/playlist?list=PL1")
        arch.write_text("youtube aaaaaaaaaaa\nyoutube ccccccccccc\n")
        self.run_job(["aaaaaaaaaaa"])
        self.assertEqual(ytdl.read_archive_ids(arch), {"ccccccccccc"})

    def test_dead_cookies_stop_the_job_instead_of_downloading_logged_out(self):
        t0 = time.time()
        job = self.run_job(["aaaaaaaaaaa", "bbbbbbbbbbb"], mode="dead")
        self.assertLess(time.time() - t0, 20)                           # killed, not waited out
        self.assertEqual(job["status"], "failed")
        self.assertIn("don't work anymore", job["summary"])
        self.assertIs(ytdl.read_cookie_check()["ok"], False)

    def test_refuses_to_start_on_dead_cookies(self):
        with mock.patch.object(ytdl, "check_cookies", return_value={"ok": False, "checked": time.time()}), \
             mock.patch.object(ytdl, "build_cmd") as build:
            ok, _ = ytdl.start_job(self.sid, ["aaaaaaaaaaa"])
            self.assertTrue(ok)
            end = time.time() + 10
            while ytdl.job_state()["status"] in ("starting", "running") and time.time() < end:
                time.sleep(0.05)
        job = ytdl.job_state()
        self.assertEqual(job["status"], "failed")
        self.assertIn("Nothing was downloaded", job["summary"])
        build.assert_not_called()

    def test_cancel(self):
        def fake_build(ids_, out_dir, archive, cookie):
            before, after = str(self.data / "b.txt"), str(self.data / "a.txt")
            return ([sys.executable, "-c", self.SCRIPT, "slow", self.src, str(out_dir), before, after, *ids_],
                    before, after, str(self.data / "u.txt"))
        with mock.patch.object(ytdl, "build_cmd", side_effect=fake_build):
            ok, err = ytdl.start_job(self.sid, ["aaaaaaaaaaa"])
            self.assertTrue(ok, err)
            for _ in range(100):
                if ytdl.job_state()["status"] == "running":
                    break
                time.sleep(0.05)
            ok, err2 = ytdl.start_job(self.sid, ["bbbbbbbbbbb"])
            self.assertFalse(ok)
            self.assertIn("already running", err2)
            t0 = time.time()
            self.assertTrue(ytdl.cancel_job())
            while ytdl._PROC["p"] is not None and time.time() - t0 < 10:
                time.sleep(0.05)
        self.assertEqual(ytdl.job_state()["status"], "cancelled")
        self.assertLess(time.time() - t0, 10)
        self.assertFalse(ytdl.cancel_job())                              # nothing left to cancel

    def test_start_validation(self):
        self.assertEqual(ytdl.start_job(self.sid, [])[0], False)
        self.assertEqual(ytdl.start_job(self.sid, ["not an id; rm -rf /"])[0], False)
        with self.assertRaises(LookupError):
            ytdl.start_job(9999, ["aaaaaaaaaaa"])
        ytdl.clear_cookies()
        self.assertIn("cookies are required", ytdl.start_job(self.sid, ["aaaaaaaaaaa"])[1])
        ytdl.save_cookies("SID=x; HSID=y")
        with mock.patch.object(ytdl, "free_space", return_value=(10 * 1024 ** 2, 10 ** 12)):
            self.assertIn("MB free", ytdl.start_job(self.sid, ["aaaaaaaaaaa"])[1])
        with mock.patch.object(ytdl, "runtime_problem", return_value="Deno too old"):
            self.assertEqual(ytdl.start_job(self.sid, ["aaaaaaaaaaa"]), (False, "Deno too old"))


class RuntimeTests(unittest.TestCase):
    def test_old_node_is_rejected_new_deno_accepted(self):
        def fake_which(n):
            return {"node": "/usr/bin/node"}.get(n)
        with mock.patch.object(ytdl.shutil, "which", side_effect=fake_which), \
             mock.patch.object(ytdl, "_exe_version", return_value=(18, 19, 1)), \
             mock.patch.object(ytdl.os.path, "isfile", return_value=False):
            rt = ytdl.js_runtime()
            self.assertFalse(rt["supported"])
            self.assertIn("too old", ytdl.runtime_problem(rt))
        with mock.patch.object(ytdl.shutil, "which", side_effect=lambda n: {"deno": "/d/deno"}.get(n)), \
             mock.patch.object(ytdl, "_exe_version", return_value=(2, 9, 7)):
            rt = ytdl.js_runtime()
            self.assertTrue(rt["supported"])
            self.assertEqual(rt["args"], ["--js-runtimes", "deno:/d/deno"])


class RouteTests(unittest.TestCase):
    """Importing app runs its startup (scan thread config, backup loop), so point it all at scratch."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        os.environ.update(DATA_DIR=cls.root + "/data", MUSIC_DIR=cls.root + "/music",
                          DOWNLOAD_DIR=cls.root + "/Loop", BACKUP_DIR="", PASSWORD="")
        os.makedirs(cls.root + "/music")
        os.environ.pop("REQUIRE_LOGIN", None)
        import importlib
        import app as appmod
        cls.app = importlib.reload(appmod)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def test_every_download_route_is_owner_only(self):
        routes = [r for r in self.app.app.routes if getattr(r, "path", "").startswith("/api/dl/")]
        self.assertGreaterEqual(len(routes), 12)
        for r in routes:
            deps = [d.call for d in r.dependant.dependencies]
            self.assertIn(self.app.owner_only, deps, r.path)

    def test_route_helpers_map_errors(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self.app.dl_add_source({"url": "https://evil.example/x"})
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            self.app.dl_source(424242)
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(HTTPException) as cm:
            self.app.dl_skip(1, {"ids": "nope"})
        self.assertEqual(cm.exception.status_code, 400)
        r = self.app.dl_add_source({"url": "https://www.youtube.com/playlist?list=PLx", "name": "N"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.app.dl_tools()["sources"][0]["name"], "N")
        self.assertEqual(self.app.dl_tools()["disk"]["dir"], self.root + "/Loop")
        self.assertEqual(self.app.dl_delete_source(r["id"])["sources"], [])


if __name__ == "__main__":
    unittest.main()
