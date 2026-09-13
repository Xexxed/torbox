"""Tests for on-the-fly MKV remuxing.

Three layers:

1. Pure logic -- codec rules, ffprobe JSON parsing, command construction. No
   FFmpeg and no network, so these always run.
2. The mode decision -- direct / remux / external -- with the probe stubbed, so
   the decision tree is covered even on a machine without FFmpeg.
3. End to end -- a genuine H.264/AAC MKV served by a range-capable loopback
   server, remuxed through /api/stream/content and validated with ffprobe.
   These skip when FFmpeg is absent, since they cannot mean anything without it.

Isolation: AppState() writes settings.json into the current directory, so the
suite chdirs into a temp dir. The generated MKV lives in a temp dir too and is
removed afterwards.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

import requests

from torbox_downloader import remux
from torbox_downloader.mount.vfs import TorboxVfs
from tests.test_mount import FakeSession, RangeServerFixture
from tests.test_stream_api import _FakeState, _ServerFixture

HAVE_FFMPEG = remux.available()
SKIP_FFMPEG = "ffmpeg and ffprobe are required for this test"


# ---------------------------------------------------------------------------
# 1. Pure logic
# ---------------------------------------------------------------------------


class TestCodecRules(unittest.TestCase):
    def test_h264_aac_is_remuxable(self):
        self.assertTrue(remux.can_remux({"video": "h264", "audio": "aac"}))

    def test_h264_mp3_is_remuxable(self):
        self.assertTrue(remux.can_remux({"video": "h264", "audio": "mp3"}))

    def test_silent_h264_is_remuxable(self):
        # Audio is optional; a video-only file still plays.
        self.assertTrue(remux.can_remux({"video": "h264", "audio": None}))

    def test_hevc_is_not_remuxable(self):
        # The container swap would work, but the browser still cannot decode it.
        self.assertFalse(remux.can_remux({"video": "hevc", "audio": "aac"}))

    def test_unsupported_audio_blocks_the_remux(self):
        for codec in ("dts", "ac3", "truehd", "flac"):
            with self.subTest(codec=codec):
                self.assertFalse(
                    remux.can_remux({"video": "h264", "audio": codec}))

    def test_no_video_is_not_remuxable(self):
        self.assertFalse(remux.can_remux({"video": None, "audio": "aac"}))

    def test_missing_info_is_not_remuxable(self):
        self.assertFalse(remux.can_remux(None))
        self.assertFalse(remux.can_remux({}))

    def test_codec_matching_is_case_insensitive(self):
        self.assertTrue(remux.can_remux({"video": "H264", "audio": "AAC"}))


class TestProbeParsing(unittest.TestCase):
    def test_picks_the_first_video_and_audio_stream(self):
        info = remux._summarise({
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
                {"codec_type": "audio", "codec_name": "ac3"},
                {"codec_type": "subtitle", "codec_name": "subrip"},
            ],
            "format": {"duration": "1234.56"},
        })
        self.assertEqual(info["video"], "h264")
        self.assertEqual(info["audio"], "aac")     # first one wins
        self.assertAlmostEqual(info["duration"], 1234.56)

    def test_falls_back_to_a_stream_duration(self):
        info = remux._summarise({
            "streams": [{"codec_type": "video", "codec_name": "h264",
                         "duration": "42.5"}],
            "format": {},
        })
        self.assertAlmostEqual(info["duration"], 42.5)

    def test_tolerates_missing_and_na_values(self):
        info = remux._summarise({
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "format": {"duration": "N/A"},
        })
        self.assertIsNone(info["duration"])

    def test_empty_payload(self):
        info = remux._summarise({})
        self.assertIsNone(info["video"])
        self.assertIsNone(info["audio"])
        self.assertIsNone(info["duration"])

    def test_to_seconds_rejects_junk_and_non_positive(self):
        self.assertIsNone(remux._to_seconds("N/A"))
        self.assertIsNone(remux._to_seconds(None))
        self.assertIsNone(remux._to_seconds(0))
        self.assertIsNone(remux._to_seconds(-5))
        self.assertEqual(remux._to_seconds("1.5"), 1.5)


class TestCommandConstruction(unittest.TestCase):
    def test_uses_fragmented_mp4_never_faststart(self):
        command = remux.remux_command("http://cdn.test/a.mkv")
        joined = " ".join(command)
        # +faststart needs a seekable output and fails on a pipe; the
        # fragmented form is what makes streaming possible.
        self.assertNotIn("faststart", joined)
        self.assertIn("frag_keyframe+empty_moov+default_base_moof", joined)
        self.assertIn("-c copy", joined)
        self.assertEqual(command[-1], "pipe:1")

    def test_maps_only_the_first_video_and_audio_track(self):
        command = remux.remux_command("http://cdn.test/a.mkv")
        self.assertIn("0:v:0?", command)          # ? = optional
        self.assertIn("0:a:0?", command)
        self.assertIn("-sn", command)             # subtitles dropped
        self.assertIn("-dn", command)             # data streams dropped

    def test_no_seek_flag_by_default(self):
        self.assertNotIn("-ss", remux.remux_command("http://cdn.test/a.mkv"))

    def test_seek_goes_before_the_input_for_input_seeking(self):
        command = remux.remux_command("http://cdn.test/a.mkv", start=61.5)
        self.assertIn("-ss", command)
        self.assertLess(command.index("-ss"), command.index("-i"),
                        "-ss after -i would decode and discard instead of seeking")
        self.assertEqual(command[command.index("-ss") + 1], "61.500")

    def test_zero_start_adds_no_seek_flag(self):
        self.assertNotIn("-ss", remux.remux_command("http://cdn.test/a.mkv", start=0))

    def test_never_writes_to_disk(self):
        # The whole feature's premise: the output is a pipe, never a file. The
        # input path may well end in .mkv -- that is the source, not a sink.
        for start in (None, 30.0):
            with self.subTest(start=start):
                command = remux.remux_command("http://cdn.test/a.mkv", start=start)
                self.assertEqual(command[-1], "pipe:1")
                # Nothing follows the output spec, and no output file exists.
                # +2 skips the -i flag and its URL argument.
                after_input = command[command.index("-i") + 2:]
                self.assertEqual(
                    [t for t in after_input if not t.startswith("-")],
                    ["0:v:0?", "0:a:0?", "copy",
                     "frag_keyframe+empty_moov+default_base_moof", "mp4", "pipe:1"],
                    "the only non-flag argument after the input must be the pipe",
                )


class TestRedaction(unittest.TestCase):
    def test_strips_the_query_string(self):
        # CDN links carry an auth token; it must never reach the log.
        self.assertEqual(
            remux._redact("https://cdn.test/f.mkv?token=SECRET&x=1"),
            "https://cdn.test/f.mkv")

    def test_leaves_a_plain_url_alone(self):
        self.assertEqual(remux._redact("https://cdn.test/f.mkv"),
                         "https://cdn.test/f.mkv")


# ---------------------------------------------------------------------------
# 2. The mode decision
# ---------------------------------------------------------------------------


class TestMediaMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_cwd = os.getcwd()
        cls._tmp = tempfile.mkdtemp(prefix="torbox_mode_test_")
        os.chdir(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._orig_cwd)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _vfs(self, name):
        files = [{"name": name, "path": ["T"], "size": "1GB", "selector": "s"}]
        session = FakeSession(torrents=[{"name": "T", "size": "1GB"}],
                              structures={"T": files})
        vfs = TorboxVfs(session)
        vfs.scan()
        return vfs, session

    def test_playable_containers_are_direct(self):
        for name in ("a.mp4", "a.webm", "a.mov", "a.ogg", "a.m4v"):
            with self.subTest(name=name):
                vfs, session = self._vfs(name)
                self.assertEqual(vfs.media_mode("/T/" + name), "direct")
                # A direct file must never be probed.
                self.assertEqual(session.generated, [])

    def test_images_and_other_files_are_direct(self):
        for name in ("cover.jpg", "notes.txt"):
            with self.subTest(name=name):
                vfs, _session = self._vfs(name)
                self.assertEqual(vfs.media_mode("/T/" + name), "direct")

    def test_remuxable_container_reports_remux(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: {
            "video": "h264", "audio": "aac", "duration": 10.0}
        self.assertEqual(vfs.media_mode("/T/movie.mkv"), "remux")

    def test_non_remuxable_container_reports_external(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: {
            "video": "hevc", "audio": "dts", "duration": 10.0}
        self.assertEqual(vfs.media_mode("/T/movie.mkv"), "external")

    def test_unprobeable_file_reports_external(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: None
        self.assertEqual(vfs.media_mode("/T/movie.mkv"), "external")

    def test_missing_ffmpeg_reports_external_without_probing(self):
        vfs, _session = self._vfs("movie.mkv")
        called = []
        vfs.probe_media = lambda path, force=False: called.append(path) or None
        original = remux.available
        remux.available = lambda: False
        try:
            self.assertEqual(vfs.media_mode("/T/movie.mkv"), "external")
        finally:
            remux.available = original
        self.assertEqual(called, [], "must not probe when ffmpeg is absent")

    def test_playback_info_shape(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: {
            "video": "h264", "audio": "aac", "duration": 42.0}
        info = vfs.playback_info("/T/movie.mkv")
        self.assertEqual(info["kind"], "video")
        self.assertEqual(info["mode"], "remux")
        self.assertEqual(info["duration"], 42.0)
        self.assertEqual(info["video_codec"], "h264")

    def test_probe_successes_are_cached_and_failures_are_not(self):
        vfs, _session = self._vfs("movie.mkv")
        calls = []

        def fake_probe(url, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                raise remux.RemuxError("transient")
            return {"video": "h264", "audio": "aac", "duration": 5.0}

        original = remux.probe
        remux.probe = fake_probe
        try:
            self.assertIsNone(vfs.probe_media("/T/movie.mkv"))   # fails
            self.assertIsNotNone(vfs.probe_media("/T/movie.mkv"))  # retried
            self.assertIsNotNone(vfs.probe_media("/T/movie.mkv"))  # cached
        finally:
            remux.probe = original
        self.assertEqual(len(calls), 2, "a failure must not be cached")

    def test_refresh_clears_cached_probes(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs._probes["/T/movie.mkv"] = {"video": "h264"}
        vfs.refresh()
        self.assertEqual(vfs._probes, {})


# ---------------------------------------------------------------------------
# 3. End to end
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_FFMPEG, SKIP_FFMPEG)
class TestRemuxEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_cwd = os.getcwd()
        cls._tmp = tempfile.mkdtemp(prefix="torbox_remux_test_")
        os.chdir(cls._tmp)
        cls.mkv = os.path.join(cls._tmp, "movie.mkv")
        subprocess.run([
            remux.ffmpeg_path(), "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "3",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            cls.mkv,
        ], check=True, stdin=subprocess.DEVNULL, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._orig_cwd)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _vfs(self, payload_len):
        size_str = "%dB" % payload_len
        session = FakeSession(
            torrents=[{"name": "T", "size": size_str}],
            structures={"T": [
                {"name": "movie.mkv", "path": ["T"], "size": size_str, "selector": "s"},
            ]},
        )
        vfs = TorboxVfs(session)
        vfs.scan()
        return vfs

    def test_probe_reads_a_real_file_over_http(self):
        with open(self.mkv, "rb") as f:
            blob = f.read()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            info = remux.probe(base + "/movie.mkv")
        self.assertEqual(info["video"], "h264")
        self.assertEqual(info["audio"], "aac")
        self.assertGreater(info["duration"], 2.0)

    def test_stream_remux_produces_a_valid_fragmented_mp4(self):
        with open(self.mkv, "rb") as f:
            blob = f.read()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            stream = remux.stream_remux(base + "/movie.mkv")
            output = bytearray()
            try:
                for chunk in stream:
                    output += chunk
            finally:
                stream.close()

        self.assertGreater(len(output), 1000)
        # A fragmented MP4 is a sequence of boxes; `ftyp` is always first.
        self.assertEqual(bytes(output[4:8]), b"ftyp")

        out_path = os.path.join(self._tmp, "out.mp4")
        with open(out_path, "wb") as f:
            f.write(bytes(output))
        probe = subprocess.run(
            [remux.ffprobe_path(), "-v", "error",
             "-show_entries", "stream=codec_type,codec_name",
             "-of", "csv=p=0", out_path],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("h264,video", probe.stdout)
        self.assertIn("aac,audio", probe.stdout)

    def test_closing_early_reaps_ffmpeg(self):
        with open(self.mkv, "rb") as f:
            blob = f.read()
        before = _ffmpeg_count()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            stream = remux.stream_remux(base + "/movie.mkv")
            next(stream)
            stream.close()
            self.assertIsNotNone(_ffmpeg_count())
        if before is not None:
            # Give the OS a moment to reap the killed child.
            import time
            deadline = time.time() + 5
            while time.time() < deadline and _ffmpeg_count() > before:
                time.sleep(0.2)
            self.assertLessEqual(
                _ffmpeg_count(), before,
                "closing the stream must not leave an ffmpeg process behind")

    def test_content_endpoint_remuxes_a_matroska_file(self):
        with open(self.mkv, "rb") as f:
            blob = f.read()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs = self._vfs(len(blob))
            # Point the fake CDN at the loopback fixture.
            vfs._session.cdn_base = base
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/T/movie.mkv"},
                                 headers=srv.headers(), timeout=60)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Content-Type"), "video/mp4")
        self.assertIsNone(r.headers.get("Content-Length"),
                          "a remuxed stream has no known length")
        self.assertEqual(r.headers.get("Accept-Ranges"), "none")
        self.assertEqual(r.content[4:8], b"ftyp")

    def test_download_never_remuxes(self):
        # Asking for a download must yield the original Matroska bytes, not a
        # converted copy -- otherwise subtitles and extra audio vanish silently.
        with open(self.mkv, "rb") as f:
            blob = f.read()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs = self._vfs(len(blob))
            vfs._session.cdn_base = base
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/T/movie.mkv", "download": "1"},
                                 headers=srv.headers(), timeout=30)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, blob)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))

    def test_head_still_never_resolves_a_link(self):
        # Regression guard: deciding the playback mode probes the file, and
        # probing resolves a CDN link. HEAD must stay free of that.
        with open(self.mkv, "rb") as f:
            blob = f.read()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs = self._vfs(len(blob))
            vfs._session.cdn_base = base
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.head(srv.base + "/api/stream/content",
                                  params={"path": "/T/movie.mkv"},
                                  headers=srv.headers(), timeout=30)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(vfs._session.generated, [])

    def test_resolve_reports_mode_and_duration(self):
        with open(self.mkv, "rb") as f:
            blob = f.read()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs = self._vfs(len(blob))
            vfs._session.cdn_base = base
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.post(srv.base + "/api/stream/resolve",
                                  json={"path": "/T/movie.mkv"},
                                  headers=srv.headers(), timeout=60)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["mode"], "remux")
        self.assertEqual(body["kind"], "video")
        self.assertFalse(body["playable"])
        self.assertGreater(body["duration"], 2.0)
        self.assertEqual(body["video_codec"], "h264")


def _ffmpeg_count():
    """Number of running ffmpeg processes, or None if it cannot be determined."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe"],
                                 capture_output=True, text=True).stdout
            return out.lower().count("ffmpeg.exe")
        out = subprocess.run(["pgrep", "-c", "ffmpeg"],
                             capture_output=True, text=True).stdout.strip()
        return int(out) if out else 0
    except Exception:
        return None


if __name__ == "__main__":
    unittest.main()
