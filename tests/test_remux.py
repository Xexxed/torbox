"""Tests for on-the-fly remuxing and transcoding.

Three layers:

1. Pure logic -- codec rules, ffprobe JSON parsing, command construction,
   encoder preference and the mode decision. No FFmpeg and no network, so these
   always run.
2. The playback plan -- direct / remux / transcode / external -- with the probe
   stubbed, so the decision tree is covered even on a machine without FFmpeg.
3. End to end -- a genuine H.264/AAC MKV remuxed through /api/stream/content,
   and a genuine HEVC/AC3 MKV transcoded through it, both served by a
   range-capable loopback server and validated with ffprobe. These skip when
   FFmpeg is absent, since they cannot mean anything without it.

Isolation: AppState() writes settings.json into the current directory, so the
suite chdirs into a temp dir. Generated media lives in a temp dir too and is
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


class TestEncoderDetection(unittest.TestCase):
    """The encoder must reflect what the machine can do, not what it lists.

    `ffmpeg -encoders` reports what the build was compiled with, which on
    Windows includes h264_nvenc whether or not there is an NVIDIA card. So the
    choice is driven by a real one-frame encode, and these tests pin the order
    and the caching rather than the probing itself.
    """

    def setUp(self):
        self._orig = remux._encoder_works
        remux.reset_encoder_cache()

    def tearDown(self):
        remux._encoder_works = self._orig
        remux.reset_encoder_cache()

    def _only(self, *usable):
        """Pretend only these encoders can actually run. Returns probe order."""
        seen = []

        def fake(name):
            seen.append(name)
            return name in usable

        remux._encoder_works = fake
        return seen

    def test_prefers_hardware_over_software(self):
        self._only("h264_nvenc", "libx264")
        self.assertEqual(remux.pick_encoder(), "h264_nvenc")

    def test_quick_sync_beats_amd_beats_software(self):
        self._only("h264_qsv", "h264_amf", "libx264")
        self.assertEqual(remux.pick_encoder(), "h264_qsv")
        remux.reset_encoder_cache()
        self._only("h264_amf", "libx264")
        self.assertEqual(remux.pick_encoder(), "h264_amf")

    def test_falls_back_to_software(self):
        self._only("libx264")
        self.assertEqual(remux.pick_encoder(), "libx264")

    def test_returns_none_when_nothing_works(self):
        self._only()
        with self.assertLogs("torbox_downloader.remux", level="WARNING") as logs:
            self.assertIsNone(remux.pick_encoder())
        self.assertIn("transcoding is unavailable", "\n".join(logs.output))
        self.assertFalse(remux.can_transcode())

    def test_stops_probing_once_something_works(self):
        seen = self._only("h264_nvenc", "libx264")
        remux.pick_encoder()
        self.assertEqual(seen, ["h264_nvenc"],
                         "a working encoder must end the search")

    def test_detection_is_cached(self):
        seen = self._only("libx264")
        remux.pick_encoder()
        remux.pick_encoder()
        remux.pick_encoder()
        self.assertEqual(seen, list(remux.ENCODER_CANDIDATES),
                         "each candidate must be probed once, not once per call")

    def test_finding_nothing_is_also_cached(self):
        seen = self._only()
        with self.assertLogs("torbox_downloader.remux", level="WARNING"):
            remux.pick_encoder()
        remux.pick_encoder()
        self.assertEqual(len(seen), len(remux.ENCODER_CANDIDATES))

    def test_every_candidate_has_tuning_args(self):
        for name in remux.ENCODER_CANDIDATES:
            with self.subTest(encoder=name):
                self.assertIn(name, remux.ENCODER_ARGS,
                              "an untuned encoder would silently use defaults")

    def test_software_encoder_is_the_last_resort(self):
        self.assertEqual(remux.ENCODER_CANDIDATES[-1], "libx264")


class TestTranscodeCommand(unittest.TestCase):
    def _command(self, url="http://cdn.test/a.mkv", start=None, encoder="libx264"):
        return remux.transcode_command(url, start, encoder)

    def test_reencodes_video_instead_of_copying(self):
        command = self._command()
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertNotIn("copy", command)

    def test_forces_8_bit_output(self):
        # 10-bit HEVC is the common case in x265 releases, and 10-bit H.264 is
        # not something browsers accept -- so the pixel format must be pinned.
        command = self._command()
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")

    def test_reencodes_audio_to_aac(self):
        # Reaching this path usually means the audio codec is unplayable too
        # (DTS, TrueHD, AC3), so copying it is not an option.
        command = self._command()
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_applies_encoder_specific_tuning(self):
        command = self._command(encoder="h264_nvenc")
        self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")
        self.assertIn("p4", command)

    def test_uses_the_same_fragmented_mp4_output(self):
        command = self._command()
        self.assertIn("frag_keyframe+empty_moov+default_base_moof", command)
        self.assertNotIn("faststart", " ".join(command))
        self.assertEqual(command[-1], "pipe:1")

    def test_never_writes_to_disk(self):
        command = self._command()
        after_input = command[command.index("-i") + 2:]
        self.assertEqual([t for t in after_input if not t.startswith("-")][-1],
                         "pipe:1",
                         "the only non-flag argument after the input is the pipe")

    def test_seek_goes_before_the_input(self):
        command = self._command(start=61.5)
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "61.500")

    def test_drops_subtitles_and_extra_tracks(self):
        command = self._command()
        self.assertIn("-sn", command)
        self.assertIn("-dn", command)
        self.assertIn("0:v:0?", command)
        self.assertIn("0:a:0?", command)

    def test_raises_when_no_encoder_is_available(self):
        original = remux._encoder_works
        remux.reset_encoder_cache()
        remux._encoder_works = lambda name: False
        try:
            with self.assertLogs("torbox_downloader.remux", level="WARNING"):
                with self.assertRaises(remux.RemuxError):
                    remux.transcode_command("http://cdn.test/a.mkv")
        finally:
            remux._encoder_works = original
            remux.reset_encoder_cache()


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

    # --- the reason, which the UI needs in order to explain itself ----------

    def test_reason_native_for_a_playable_container(self):
        vfs, session = self._vfs("movie.mp4")
        plan = vfs.playback_plan("/T/movie.mp4")
        self.assertEqual((plan["mode"], plan["reason"]), ("direct", "native"))
        self.assertIsNone(plan["info"])
        self.assertEqual(session.generated, [], "a direct file must never be probed")

    def test_reason_remuxable(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: {
            "video": "h264", "audio": "aac", "duration": 10.0}
        self.assertEqual(vfs.playback_plan("/T/movie.mkv")["reason"], "remuxable")

    def test_reason_codec_when_the_codec_is_the_problem(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: {
            "video": "hevc", "audio": "dts", "duration": 10.0}
        self.assertEqual(vfs.playback_plan("/T/movie.mkv")["reason"], "codec")

    def test_reason_no_ffmpeg_without_ffmpeg(self):
        vfs, _session = self._vfs("movie.mkv")
        original = remux.available
        remux.available = lambda: False
        try:
            self.assertEqual(vfs.playback_plan("/T/movie.mkv")["reason"], "no-ffmpeg")
        finally:
            remux.available = original

    def test_reason_unprobeable_when_the_probe_fails(self):
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: None
        self.assertEqual(vfs.playback_plan("/T/movie.mkv")["reason"], "unprobeable")

    def test_external_verdict_still_reports_the_codec(self):
        """Regression: the probe ran, so its result must reach the UI.

        The whole point of the codec-aware panel is that a user with an HEVC
        library learns *why* the file will not play. Dropping the probe result
        on the 'external' path left them with a generic "this container cannot
        play" message that named the wrong culprit entirely.
        """
        vfs, _session = self._vfs("movie.mkv")
        vfs.probe_media = lambda path, force=False: {
            "video": "hevc", "audio": "dts", "duration": 1234.0}
        info = vfs.playback_info("/T/movie.mkv")
        self.assertEqual(info["mode"], "external")
        self.assertEqual(info["reason"], "codec")
        self.assertEqual(info["video_codec"], "hevc")
        self.assertEqual(info["audio_codec"], "dts")
        self.assertEqual(info["duration"], 1234.0)

    def test_direct_file_is_not_probed_by_playback_info(self):
        """A playable mp4 must not pay for an ffprobe it does not need."""
        vfs, session = self._vfs("movie.mp4")
        info = vfs.playback_info("/T/movie.mp4")
        self.assertEqual(info["mode"], "direct")
        self.assertIsNone(info["video_codec"])
        self.assertEqual(session.generated, [], "resolving a link here is wasted work")


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


class TestTranscodeEndToEnd(unittest.TestCase):
    """A genuinely unplayable file, re-encoded into one a browser accepts.

    The source is 10-bit HEVC with AC3 audio, which is exactly the shape of a
    modern x265 release and is doubly unplayable: browsers do not decode HEVC
    reliably, 10-bit even less so, and AC3 is not something a remux can carry
    into MP4 either. Both tracks have to be re-encoded, which is the point.
    """

    @classmethod
    def setUpClass(cls):
        if not HAVE_FFMPEG:
            raise unittest.SkipTest("ffmpeg is not installed")
        cls._orig_cwd = os.getcwd()
        cls._tmp = tempfile.mkdtemp(prefix="torbox_transcode_test_")
        os.chdir(cls._tmp)
        cls.source = os.path.join(cls._tmp, "hevc.mkv")
        subprocess.run([
            remux.ffmpeg_path(), "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "2",
            "-c:v", "libx265", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "log-level=none",
            "-c:a", "ac3",
            cls.source,
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
                {"name": "hevc.mkv", "path": ["T"], "size": size_str, "selector": "s"},
            ]},
        )
        vfs = TorboxVfs(session)
        vfs.scan()
        return vfs

    def _blob(self):
        with open(self.source, "rb") as f:
            return f.read()

    def _ffprobe(self, path_or_bytes):
        """Streams as ffprobe sees them: {codec_type: (codec_name, pix_fmt)}.

        Keyed by type rather than returned as a list because ffprobe emits a
        different number of columns for audio (no pixel format) than for video,
        which makes positional tuples awkward to compare.
        """
        if isinstance(path_or_bytes, (bytes, bytearray)):
            tmp = os.path.join(self._tmp, "probe_me.mp4")
            with open(tmp, "wb") as f:
                f.write(bytes(path_or_bytes))
            path = tmp
        else:
            path = path_or_bytes
        out = subprocess.run(
            [remux.ffprobe_path(), "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,pix_fmt",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        streams = {}
        for line in out.stdout.strip().splitlines():
            parts = line.split(",")
            if len(parts) >= 2:
                streams[parts[1]] = (parts[0], parts[2] if len(parts) > 2 else None)
        return streams

    # --- the fixture is what it claims to be --------------------------------

    def test_the_source_is_really_unplayable(self):
        streams = self._ffprobe(self.source)
        self.assertEqual(streams["video"], ("hevc", "yuv420p10le"))
        self.assertEqual(streams["audio"][0], "ac3")

    def test_the_source_is_not_remuxable(self):
        # If this ever starts passing, the fixture has stopped testing the
        # transcode path and the end-to-end test below proves nothing.
        with RangeServerFixture(self._blob()) as remote:
            base = remote.url.split("/file.bin")[0]
            info = remux.probe(base + "/hevc.mkv")
        self.assertEqual(info["video"], "hevc")
        self.assertFalse(remux.can_remux(info))

    # --- the re-encode itself -----------------------------------------------

    def test_transcode_produces_playable_h264_and_aac(self):
        with RangeServerFixture(self._blob()) as remote:
            base = remote.url.split("/file.bin")[0]
            stream = remux.stream_transcode(base + "/hevc.mkv")
            output = bytearray()
            try:
                for chunk in stream:
                    output += chunk
            finally:
                stream.close()

        self.assertGreater(len(output), 1000)
        self.assertEqual(bytes(output[4:8]), b"ftyp")

        streams = self._ffprobe(output)
        # The video must come out as H.264, and 8-bit: 10-bit H.264 would be a
        # technically correct re-encode that no browser would play.
        self.assertEqual(streams["video"], ("h264", "yuv420p"))
        self.assertEqual(streams["audio"][0], "aac")

    def test_transcoding_costs_less_than_the_source_is_long(self):
        """Sanity: this is a live conversion, not a stalled one."""
        import time
        with RangeServerFixture(self._blob()) as remote:
            base = remote.url.split("/file.bin")[0]
            started = time.time()
            stream = remux.stream_transcode(base + "/hevc.mkv")
            try:
                for _chunk in stream:
                    pass
            finally:
                stream.close()
            elapsed = time.time() - started
        self.assertLess(elapsed, 60.0, "a 2s clip must not take a minute")

    def test_closing_early_reaps_ffmpeg(self):
        before = _ffmpeg_count()
        with RangeServerFixture(self._blob()) as remote:
            base = remote.url.split("/file.bin")[0]
            stream = remux.stream_transcode(base + "/hevc.mkv")
            next(stream)
            stream.close()
            self.assertIsNotNone(_ffmpeg_count())
        if before is not None:
            import time
            deadline = time.time() + 5
            while time.time() < deadline and _ffmpeg_count() > before:
                time.sleep(0.2)
            self.assertLessEqual(
                _ffmpeg_count(), before,
                "abandoning a transcode must not leave ffmpeg running")

    # --- through the endpoint ------------------------------------------------

    def test_content_endpoint_transcodes_on_request(self):
        blob = self._blob()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs = self._vfs(len(blob))
            vfs._session.cdn_base = base
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/T/hevc.mkv", "transcode": "1"},
                                 headers=srv.headers(), timeout=120)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Content-Type"), "video/mp4")
        self.assertIsNone(r.headers.get("Content-Length"))
        self.assertEqual(r.headers.get("Accept-Ranges"), "none")
        self.assertEqual(r.content[4:8], b"ftyp")
        self.assertEqual(self._ffprobe(r.content)["video"], ("h264", "yuv420p"))

    def test_the_endpoint_does_not_transcode_unasked(self):
        """The whole cost model rests on this: no click, no encode."""
        blob = self._blob()
        before = _ffmpeg_count()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs = self._vfs(len(blob))
            vfs._session.cdn_base = base
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/T/hevc.mkv"},
                                 headers=srv.headers(), timeout=60)
        # Without the flag this is a plain byte stream of the original file,
        # because an HEVC file's mode is 'external' and never 'remux'.
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, blob)
        if before is not None:
            self.assertLessEqual(_ffmpeg_count(), before,
                                 "no encoder should have been started")

    def test_resolve_reports_that_transcoding_is_possible(self):
        blob = self._blob()
        with RangeServerFixture(blob) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs = self._vfs(len(blob))
            vfs._session.cdn_base = base
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.post(srv.base + "/api/stream/resolve",
                                  json={"path": "/T/hevc.mkv"},
                                  headers=srv.headers(), timeout=60)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["mode"], "external")
        self.assertEqual(body["reason"], "codec")
        self.assertEqual(body["video_codec"], "hevc")
        self.assertEqual(body["audio_codec"], "ac3")
        # ffmpeg is present, so a software encoder must be found.
        self.assertTrue(body["can_transcode"])
        self.assertTrue(body["transcode_encoder"])


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
