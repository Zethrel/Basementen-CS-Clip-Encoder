# Copyright (c) 2026 Zethrel. All rights reserved. See LICENSE.
"""Tests for clipenc. Run with:  python -m unittest -v

The end-to-end tests never start CS Demo Manager or CS2. They build a small
stand-in `csdm` in a temporary folder that writes the same kind of files the
real one does, and they record every argument it was given. On Windows the
stand-in is a `csdm.cmd` file, like the real launcher, so the same quoting
path is exercised. The folder names hold spaces and an ampersand on purpose.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import clipenc

# Made-up IDs and names. Nothing here belongs to a real player.
STEAM_ID = "76561190000000001"
OTHER_ID = "76561190000000002"

STUB_SOURCE = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "control.json"), encoding="utf-8") as handle:
    control = json.load(handle)
arguments = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\n")
command = arguments[0] if arguments else ""
if os.environ.get("CLIPENC_TEST_QUIET"):
    # Run from the command line path, where this output would land in the
    # middle of the test report instead of being read by the tool.
    sys.stdout = open(os.devnull, "w")
if command == "analyze":
    print("1 demos to process", flush=True)
    if control.get("analyze_sleep"):
        time.sleep(control["analyze_sleep"])
    sys.exit(0)
if command == "json":
    folder = arguments[arguments.index("--output-folder") + 1]
    if control.get("match") is not None:
        with open(os.path.join(folder, "match730_fake.json"), "w", encoding="utf-8") as handle:
            json.dump(control["match"], handle)
    sys.exit(0)
if command == "video":
    print("Starting Counter-Strike...", flush=True)
    print("Recording in progress...", flush=True)
    if control.get("sleep"):
        time.sleep(control["sleep"])
    if control.get("video_exit", 0) != 0:
        print("Error while generating the video", flush=True)
        sys.exit(control["video_exit"])
    folder = arguments[arguments.index("--output") + 1]
    if control.get("layout") == "nested":
        folder = os.path.join(folder, "0b1c2d3e-video-id")
        os.makedirs(folder)
    container = arguments[arguments.index("--ffmpeg-video-container") + 1]
    for number, start, end in control.get("clips", []):
        name = "sequence-%d-tick-%d-to-%d.%s" % (number, start, end, container)
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(("clip %d" % number).encode())
        if control.get("leave_raw"):
            raw = os.path.join(folder, "%d-sequence" % number)
            os.makedirs(raw)
            with open(os.path.join(raw, "video." + container), "wb") as handle:
                handle.write(b"raw leftover")
    sys.exit(0)
sys.exit(2)
'''


def make_match(map_name="de_overpass", names=None, killers=()):
    names = names or {STEAM_ID: "Zethrel", OTHER_ID: "Teammate"}
    return {
        "mapName": map_name,
        "checksum": "0123456789abcdef",
        "players": [
            {"steamId": steam_id, "name": name, "teamName": "Team B" if steam_id == STEAM_ID else "Team A"}
            for steam_id, name in names.items()
        ],
        "kills": [
            {"killerSteamId": steam_id, "tick": 1000 + index * 700, "roundNumber": 1 + index}
            for index, steam_id in enumerate(killers)
        ],
    }


class FixedChoices:
    """A stand-in random source that hands out letters from a fixed list."""

    def __init__(self, letters):
        self.letters = list(letters)

    def choice(self, _population):
        return self.letters.pop(0)


class NamingTests(unittest.TestCase):
    def test_forbidden_characters_are_removed(self):
        self.assertEqual(clipenc.sanitize_label('a<b>:c"/\\|?*d', STEAM_ID), "abcd")

    def test_ordinary_and_non_latin_names_are_kept(self):
        self.assertEqual(clipenc.sanitize_label("pl4yer =DD", STEAM_ID), "pl4yer =DD")
        self.assertEqual(clipenc.sanitize_label("Игрок Один", STEAM_ID), "Игрок Один")

    def test_empty_result_falls_back_to_the_steam_id(self):
        self.assertEqual(clipenc.sanitize_label("???", STEAM_ID), STEAM_ID)
        self.assertEqual(clipenc.sanitize_label(None, STEAM_ID), STEAM_ID)
        self.assertEqual(clipenc.sanitize_label(" . ", STEAM_ID), STEAM_ID)

    def test_trailing_dots_spaces_and_long_names(self):
        self.assertEqual(clipenc.sanitize_label("name. ", STEAM_ID), "name")
        self.assertEqual(len(clipenc.sanitize_label("x" * 200, STEAM_ID)), clipenc.MAX_LABEL_LENGTH)

    def test_reserved_device_names_are_changed(self):
        self.assertEqual(clipenc.sanitize_label("con", STEAM_ID), "_con")
        self.assertEqual(clipenc.sanitize_label("LPT1", STEAM_ID), "_LPT1")

    def test_control_characters_and_extra_spaces(self):
        self.assertEqual(clipenc.sanitize_label("a\tb\n  c", STEAM_ID), "ab c")

    def test_map_prefix_from_the_table(self):
        self.assertEqual(clipenc.map_prefix("de_anubis", {"de_anubis": "Anubis"}), ("Anubis", True))
        self.assertEqual(clipenc.map_prefix("DE_ANUBIS", {"de_anubis": "Nubis"}), ("Nubis", True))

    def test_map_prefix_derived_when_missing(self):
        self.assertEqual(clipenc.map_prefix("de_overpass", {}), ("Overpass", False))
        self.assertEqual(clipenc.map_prefix("cs_office", {"de_anubis": "Anubis"}), ("Office", False))
        self.assertEqual(clipenc.map_prefix("de_dust2", {"de_dust2": " "}), ("Dust2", False))
        self.assertEqual(clipenc.map_prefix("workshop_map", {}), ("Workshop map", False))
        self.assertEqual(clipenc.map_prefix("", {}), ("Unknown", False))

    def test_map_folder_name(self):
        self.assertEqual(clipenc.map_folder_name("de_overpass", "Overpass", {}), "Overpass")
        self.assertEqual(clipenc.map_folder_name("de_dust2", "Dust2", {"DE_DUST2": "de_dust_2"}), "de_dust_2")
        self.assertEqual(clipenc.map_folder_name("de_dust2", "Dust2", {"de_dust2": " "}), "Dust2")
        self.assertEqual(clipenc.map_folder_name("de_dust2", "Dust2", {"de_dust2": "a/b:c"}), "abc")

    def test_random_suffix_shape(self):
        for _ in range(50):
            self.assertRegex(clipenc.random_suffix(), r"^[a-z]{8}$")

    def test_new_clip_path_skips_a_taken_name(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "Anubis-Zethrel-aaaaaaaa.mkv").write_bytes(b"x")
            rng = FixedChoices("a" * 8 + "b" * 8)
            path = clipenc.new_clip_path(folder, "Anubis", "Zethrel", "mkv", rng)
            self.assertEqual(path.name, "Anubis-Zethrel-bbbbbbbb.mkv")


class ConfigTests(unittest.TestCase):
    def test_padding_limits(self):
        self.assertEqual(clipenc.check_padding(4, 3), (4.0, 3.0))
        self.assertEqual(clipenc.check_padding(10, 8), (10.0, 8.0))
        self.assertEqual(clipenc.check_padding(0, 0), (0.0, 0.0))
        for before, after in ((10.5, 3), (4, 8.5), (-1, 3), (4, -1), ("x", 3)):
            with self.assertRaises(clipenc.ClipError):
                clipenc.check_padding(before, after)

    def test_empty_config_lists_what_is_missing(self):
        problems = "\n".join(clipenc.config_problems(json.loads(json.dumps(clipenc.DEFAULT_CONFIG))))
        self.assertIn("steam_id", problems)
        self.assertIn("demo_folder", problems)
        self.assertIn("output_folder", problems)

    def test_load_config_keeps_defaults_for_missing_video_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"steam_id": STEAM_ID, "video": {"crf": 24}}), encoding="utf-8")
            config = clipenc.load_config(path)
        self.assertEqual(config["video"]["crf"], 24)
        self.assertEqual(config["video"]["video_codec"], "libx265")
        self.assertEqual(config["seconds_before"], 4)

    def test_bad_values_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            config = json.loads(json.dumps(clipenc.DEFAULT_CONFIG))
            config.update(steam_id="123", demo_folder=folder, output_folder=folder, seconds_before=11)
            config["video"].update(container="webm", crf="28", recording_system="OBS")
            config.update(map_subfolders="yes", map_folders=[])
            problems = "\n".join(clipenc.config_problems(config))
        for expected in (
            "steam_id", "between 0 and 10", "video.container", "video.crf", "video.recording_system",
            "map_subfolders must be", "map_folders must be",
        ):
            self.assertIn(expected, problems)

    def test_missing_config_file_is_a_plain_error(self):
        with self.assertRaises(clipenc.ClipError):
            clipenc.load_config(Path(tempfile.gettempdir()) / "no-such-clipenc-config.json")


class CommandLineTests(unittest.TestCase):
    def test_video_arguments_are_pinned(self):
        config = json.loads(json.dumps(clipenc.DEFAULT_CONFIG))
        arguments = clipenc.video_arguments("D:\\d\\demo.dem", STEAM_ID, config, "D:\\out", 4.0, 3.0)
        self.assertEqual(
            arguments,
            [
                "video", "D:\\d\\demo.dem",
                "--mode", "player",
                "--steamids", STEAM_ID,
                "--event", "kills",
                "--start-seconds-before", "4",
                "--end-seconds-after", "3",
                "--no-show-x-ray",
                "--no-player-voices",
                "--recording-system", "HLAE",
                "--recording-output", "video",
                "--encoder-software", "FFmpeg",
                "--width", "1920",
                "--height", "1080",
                "--framerate", "30",
                "--ffmpeg-video-container", "mkv",
                "--ffmpeg-video-codec", "libx265",
                "--ffmpeg-audio-codec", "aac",
                "--ffmpeg-audio-bitrate", "512",
                "--ffmpeg-crf", "28",
                "--no-concatenate-sequences",
                "--close-game-after-recording",
                "--output", "D:\\out",
            ],
        )

    def test_fractional_padding_is_passed_as_written(self):
        config = json.loads(json.dumps(clipenc.DEFAULT_CONFIG))
        arguments = clipenc.video_arguments("d.dem", STEAM_ID, config, "out", 2.5, 0.0)
        self.assertEqual(arguments[arguments.index("--start-seconds-before") + 1], "2.5")
        self.assertEqual(arguments[arguments.index("--end-seconds-after") + 1], "0")

    def test_windows_quoting(self):
        self.assertEqual(clipenc.quote_for_windows("D:\\a & b\\demo.dem"), '"D:\\a & b\\demo.dem"')
        self.assertEqual(clipenc.quote_for_windows("D:\\out\\"), '"D:\\out\\\\"')
        with self.assertRaises(clipenc.ClipError):
            clipenc.quote_for_windows('say "hi"')
        with self.assertRaises(clipenc.ClipError):
            clipenc.quote_for_windows("D:\\100%\\demo.dem")

    def test_batch_launcher_goes_through_cmd_with_everything_quoted(self):
        command = clipenc.build_command("C:\\CS Demo Manager\\csdm.cmd", ["video", "D:\\a & b\\x.dem"], windows=True)
        self.assertIsInstance(command, str)
        self.assertTrue(command.endswith('/d /s /c ""C:\\CS Demo Manager\\csdm.cmd" "video" "D:\\a & b\\x.dem""'))

    def test_other_launchers_get_a_plain_list(self):
        self.assertEqual(clipenc.build_command("/usr/bin/csdm", ["analyze", "x.dem"], windows=False), ["/usr/bin/csdm", "analyze", "x.dem"])
        self.assertEqual(clipenc.build_command("C:\\x\\csdm.exe", ["analyze"], windows=True), ["C:\\x\\csdm.exe", "analyze"])

    def test_closing_the_game_is_windows_only(self):
        self.assertEqual(clipenc.close_game_commands(windows=False), [])
        self.assertEqual(
            clipenc.close_game_commands(windows=True),
            [["taskkill", "/IM", "cs2.exe", "/F"], ["taskkill", "/IM", "HLAE.exe", "/F"]],
        )

    def test_a_built_exe_keeps_its_files_next_to_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = os.path.join(folder, "CSClipEncoder.exe")
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
                self.assertEqual(clipenc.app_dir(), Path(os.path.abspath(folder)))
        self.assertEqual(clipenc.app_dir(), Path(clipenc.__file__).resolve().parent)

    def test_clip_order_follows_the_sequence_number(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("sequence-10-tick-9-to-10.mkv", "sequence-2-tick-3-to-4.mkv", "sequence-1-tick-1-to-2.mkv", "notes.txt"):
                (Path(folder) / name).write_bytes(b"x")
            raw = Path(folder) / "4-sequence"
            raw.mkdir()
            (raw / "video.mkv").write_bytes(b"x")
            names = [path.name for path in clipenc.collect_clips(folder, "mkv")]
        self.assertEqual(names, ["sequence-1-tick-1-to-2.mkv", "sequence-2-tick-3-to-4.mkv", "sequence-10-tick-9-to-10.mkv"])


class StubCsdmCase(unittest.TestCase):
    """A temporary demo folder, output folder, config and stand-in csdm.

    It holds no tests of its own. test_clipgui.py builds on it as well.
    """

    def setUp(self):
        # Stopping a recording closes CS2 by name. No test may ever do that
        # for real: these tests also run on a PC where the game can be open.
        patcher = mock.patch.object(clipenc, "close_game_commands", return_value=[])
        self.close_game = patcher.start()
        self.addCleanup(patcher.stop)

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "clip enc & test"
        self.demos = self.root / "demos & replays"
        self.output = self.root / "out put"
        self.tool = self.root / "fake csdm"
        for folder in (self.demos, self.output, self.tool):
            folder.mkdir(parents=True)

        stub = self.tool / "fake_csdm.py"
        stub.write_text(STUB_SOURCE, encoding="utf-8")
        if os.name == "nt":
            self.launcher = self.tool / "csdm.cmd"
            self.launcher.write_bytes(('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, stub)).encode("utf-8"))
        else:
            self.launcher = self.tool / "csdm"
            self.launcher.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, stub), encoding="utf-8")
            self.launcher.chmod(self.launcher.stat().st_mode | stat.S_IXUSR)

        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.config = {
            "csdm_path": str(self.launcher),
            "demo_folder": str(self.demos),
            "output_folder": str(self.output),
            "steam_id": STEAM_ID,
            "player_labels": {STEAM_ID: "Zethrel"},
            "map_prefixes": {"de_anubis": "Anubis", "de_overpass": "Overpass"},
        }
        self.write_config()
        self.control(match=make_match(killers=[STEAM_ID] * 4), clips=[[1, 65920, 66551], [2, 97922, 98723], [3, 140681, 141129]])
        self.demo = self.add_demo("match730_one.dem", age=100)

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def control(self, **values):
        (self.tool / "control.json").write_text(json.dumps(values), encoding="utf-8")

    def add_demo(self, name, age, size=10):
        path = self.demos / name
        path.write_bytes(b"d" * size)
        moment = 1_790_000_000 - age
        os.utime(path, (moment, moment))
        return path

    def calls(self):
        log = self.tool / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def run_tool(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        quiet = mock.patch.dict(os.environ, {"CLIPENC_TEST_QUIET": "1"})
        with quiet, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = clipenc.main(["--config", str(self.config_path), "--state", str(self.state_path)] + list(arguments))
        return code, out.getvalue(), err.getvalue()

    def clips(self):
        """Names of every finished clip, wherever under the output folder it is."""
        return sorted(path.name for path in self.clip_paths())

    def clip_paths(self):
        work = self.output / clipenc.WORK_FOLDER_NAME
        return [
            path for path in self.output.rglob("*")
            if path.is_file() and work not in path.parents
        ]

    def folders(self):
        return sorted(path.name for path in self.output.iterdir() if path.is_dir())

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))


class EndToEndTests(StubCsdmCase):
    def test_clip_renames_moves_and_records(self):
        code, out, err = self.run_tool("clip")
        self.assertEqual((code, err), (0, ""))

        names = self.clips()
        self.assertEqual(len(names), 3)
        for name in names:
            self.assertRegex(name, r"^Overpass-Zethrel-[a-z]{8}\.mkv$")
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists(), "work folder should be removed")

        entry = self.state()["demos"][clipenc.demo_key(self.demo)]
        self.assertEqual(entry["map"], "de_overpass")
        recorded = entry["players"][STEAM_ID]["clips"]
        self.assertEqual(sorted(recorded), names)
        # The state lists the clips in sequence order, and each file is the right clip.
        self.assertEqual(entry["players"][STEAM_ID]["folder"], "Overpass")
        self.assertEqual(
            entry["players"][STEAM_ID]["sources"],
            [
                "sequence-1-tick-65920-to-66551.mkv",
                "sequence-2-tick-97922-to-98723.mkv",
                "sequence-3-tick-140681-to-141129.mkv",
            ],
        )
        self.assertEqual(
            [(self.output / "Overpass" / name).read_bytes() for name in recorded],
            [b"clip 1", b"clip 2", b"clip 3"],
        )
        self.assertIn("3 clip(s) from 1 demo(s)", out)
        self.assertIn("Created the map folder", out)

    def test_clips_go_into_a_folder_per_map(self):
        self.run_tool("clip")
        self.assertEqual(self.folders(), ["Overpass"])
        self.assertEqual(len(list((self.output / "Overpass").iterdir())), 3)

    def test_an_existing_map_folder_is_used_and_its_files_are_left_alone(self):
        existing = self.output / "Overpass"
        existing.mkdir()
        (existing / "Sequence1tick5254to56381.mkv").write_bytes(b"old")
        _, out, _ = self.run_tool("clip")
        self.assertNotIn("Created the map folder", out)
        self.assertEqual(self.folders(), ["Overpass"])
        self.assertEqual((existing / "Sequence1tick5254to56381.mkv").read_bytes(), b"old")
        self.assertEqual(len(list(existing.iterdir())), 4)

    def test_a_map_folder_can_have_another_name_than_the_prefix(self):
        self.config["map_prefixes"]["de_dust2"] = "Dust2"
        self.config["map_folders"] = {"de_dust2": "de_dust_2"}
        self.write_config()
        (self.output / "de_dust_2").mkdir()
        self.control(match=make_match(map_name="de_dust2", killers=[STEAM_ID]), clips=[[1, 10, 20]])
        self.run_tool("clip")
        self.assertEqual(self.folders(), ["de_dust_2"])
        names = [path.name for path in (self.output / "de_dust_2").iterdir()]
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^Dust2-Zethrel-[a-z]{8}\.mkv$")
        self.assertEqual(self.state()["demos"][clipenc.demo_key(self.demo)]["players"][STEAM_ID]["folder"], "de_dust_2")

    def test_work_folder_can_be_somewhere_else(self):
        work = self.root / "recording"
        self.config["work_folder"] = str(work)
        self.write_config()
        self.run_tool("clip")
        video = [call for call in self.calls() if call[0] == "video"][0]
        self.assertTrue(video[video.index("--output") + 1].startswith(str(work)))
        self.assertEqual(self.folders(), ["Overpass"])
        self.assertEqual(len(self.clips()), 3)
        self.assertFalse(work.exists(), "an empty work folder is removed after a good run")

    def test_map_folders_can_be_turned_off(self):
        self.config["map_subfolders"] = False
        self.write_config()
        self.run_tool("clip")
        self.assertEqual(self.folders(), [])
        self.assertEqual(len([path for path in self.output.iterdir() if path.is_file()]), 3)
        self.assertEqual(self.state()["demos"][clipenc.demo_key(self.demo)]["players"][STEAM_ID]["folder"], "")

    def test_no_map_folder_is_made_when_nothing_was_recorded(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]], video_exit=1)
        self.run_tool("clip")
        self.assertEqual(self.folders(), [clipenc.WORK_FOLDER_NAME])

    def test_csdm_receives_the_exact_arguments(self):
        self.run_tool("clip")
        analyze, export, video = self.calls()
        self.assertEqual(analyze, ["analyze", str(self.demo)])
        self.assertEqual(export[:2], ["json", str(self.demo)])
        self.assertEqual(export[2], "--output-folder")
        work_video = video[video.index("--output") + 1]
        self.assertTrue(work_video.startswith(str(self.output / clipenc.WORK_FOLDER_NAME)))
        config = clipenc.load_config(self.config_path)
        self.assertEqual(video, clipenc.video_arguments(self.demo, STEAM_ID, config, work_video, 4.0, 3.0))

    def test_second_run_does_nothing(self):
        self.run_tool("clip")
        before = len(self.calls())
        code, out, _ = self.run_tool("clip")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to clip", out)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(len(self.clips()), 3)

    def test_force_clips_again(self):
        self.run_tool("clip")
        code, _, _ = self.run_tool("clip", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.clips()), 6)

    def test_named_demo_respects_done_and_force(self):
        self.run_tool("clip", "--demo", str(self.demo))
        code, out, _ = self.run_tool("clip", "--demo", str(self.demo))
        self.assertEqual(code, 0)
        self.assertIn("already clipped", out)
        self.assertEqual(len(self.clips()), 3)
        self.run_tool("clip", "--demo", str(self.demo), "--force")
        self.assertEqual(len(self.clips()), 6)

    def test_clips_in_a_video_id_subfolder_are_found(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]], layout="nested")
        code, _, _ = self.run_tool("clip")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.clips()), 1)

    def test_raw_files_csdm_left_behind_are_not_clips(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20], [2, 30, 40]], leave_raw=True)
        code, _, _ = self.run_tool("clip")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.clips()), 2)
        for path in self.clip_paths():
            self.assertNotEqual(path.read_bytes(), b"raw leftover")

    def test_raw_leftovers_in_a_video_id_subfolder_are_not_clips(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]], layout="nested", leave_raw=True)
        self.run_tool("clip")
        self.assertEqual(len(self.clips()), 1)

    def test_recording_failure_changes_nothing(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]], video_exit=1)
        code, _, err = self.run_tool("clip")
        self.assertEqual(code, 1)
        self.assertIn("exit code 1", err)
        self.assertEqual(self.clips(), [])
        self.assertIsNone(clipenc.done_entry(self.state(), clipenc.demo_key(self.demo), STEAM_ID))
        # And the demo is still offered on the next run.
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]])
        self.assertEqual(self.run_tool("clip")[0], 0)
        self.assertEqual(len(self.clips()), 1)

    def test_success_without_files_is_an_error(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[])
        code, _, err = self.run_tool("clip")
        self.assertEqual(code, 1)
        self.assertIn("left no .mkv files", err)
        self.assertIsNone(clipenc.done_entry(self.state(), clipenc.demo_key(self.demo), STEAM_ID))

    def test_missing_export_is_an_error(self):
        self.control(match=None)
        code, _, err = self.run_tool("clip")
        self.assertEqual(code, 1)
        self.assertIn("did not export", err)
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"])
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists(), "nothing recorded, nothing kept")

    def test_failed_recording_keeps_its_work_folder(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]], video_exit=1)
        self.run_tool("clip")
        self.assertTrue((self.output / clipenc.WORK_FOLDER_NAME).is_dir())

    def test_no_kills_is_done_without_recording(self):
        self.control(match=make_match(killers=[OTHER_ID, OTHER_ID]), clips=[[1, 10, 20]])
        code, out, _ = self.run_tool("clip")
        self.assertEqual(code, 0)
        self.assertIn("no kills", out)
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"])
        self.assertEqual(self.state()["demos"][clipenc.demo_key(self.demo)]["players"][STEAM_ID]["clips"], [])
        self.assertIn("Nothing to clip", self.run_tool("clip")[1])

    def test_player_not_in_demo_is_done_without_recording(self):
        self.control(match=make_match(names={OTHER_ID: "Teammate"}, killers=[OTHER_ID]), clips=[[1, 10, 20]])
        code, out, _ = self.run_tool("clip")
        self.assertEqual(code, 0)
        self.assertIn("not in this demo", out)
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"])

    def test_default_is_one_demo_newest_first(self):
        newest = self.add_demo("match730_two.dem", age=10, size=11)
        self.add_demo("match730_three.dem", age=500, size=12)
        code, out, _ = self.run_tool("clip")
        self.assertEqual(code, 0)
        self.assertEqual([call[1] for call in self.calls() if call[0] == "video"], [str(newest)])
        self.assertIn("2 more demo(s) are waiting", out)

    def test_limit_and_all(self):
        self.add_demo("match730_two.dem", age=10, size=11)
        self.add_demo("match730_three.dem", age=500, size=12)
        self.run_tool("clip", "--limit", "2")
        self.assertEqual(len([call for call in self.calls() if call[0] == "video"]), 2)
        self.run_tool("clip", "--all")
        self.assertEqual(len([call for call in self.calls() if call[0] == "video"]), 3)
        self.assertEqual(len(self.clips()), 9)

    def test_other_files_in_the_demo_folder_are_ignored(self):
        (self.demos / "match730_one.dem.info").write_bytes(b"i")
        (self.demos / "sub").mkdir()
        _, out, _ = self.run_tool("list")
        self.assertIn("1 demo(s), 1 not clipped yet", out)

    def test_unknown_map_gets_a_derived_prefix(self):
        self.control(match=make_match(map_name="de_mirage", killers=[STEAM_ID]), clips=[[1, 10, 20]])
        _, out, _ = self.run_tool("clip")
        self.assertRegex(self.clips()[0], r"^Mirage-Zethrel-[a-z]{8}\.mkv$")
        self.assertIn("not in map_prefixes", out)
        self.assertEqual(self.folders(), ["Mirage"])

    def test_label_comes_from_the_demo_when_no_label_is_set(self):
        self.config["player_labels"] = {}
        self.write_config()
        self.control(match=make_match(names={STEAM_ID: "Ze<th>rel: Кот"}, killers=[STEAM_ID]), clips=[[1, 10, 20]])
        self.run_tool("clip")
        self.assertRegex(self.clips()[0], r"^Overpass-Zethrel Кот-[a-z]{8}\.mkv$")

    def test_another_player_is_tracked_separately(self):
        self.run_tool("clip")
        self.control(match=make_match(killers=[OTHER_ID]), clips=[[1, 10, 20]])
        code, _, _ = self.run_tool("clip", "--player", OTHER_ID)
        self.assertEqual(code, 0)
        names = self.clips()
        self.assertEqual(len([name for name in names if name.startswith("Overpass-Teammate-")]), 1)
        self.assertEqual(len([name for name in names if name.startswith("Overpass-Zethrel-")]), 3)
        players = self.state()["demos"][clipenc.demo_key(self.demo)]["players"]
        self.assertEqual(sorted(players), sorted([STEAM_ID, OTHER_ID]))

    def test_padding_can_be_changed_within_the_limits(self):
        self.run_tool("clip", "--before", "10", "--after", "8")
        video = [call for call in self.calls() if call[0] == "video"][0]
        self.assertEqual(video[video.index("--start-seconds-before") + 1], "10")
        self.assertEqual(video[video.index("--end-seconds-after") + 1], "8")

    def test_padding_outside_the_limits_runs_nothing(self):
        code, _, err = self.run_tool("clip", "--before", "11")
        self.assertEqual(code, 1)
        self.assertIn("between 0 and 10", err)
        self.assertEqual(self.calls(), [])

    def test_dry_run_runs_nothing(self):
        code, out, _ = self.run_tool("clip", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("--no-show-x-ray", out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.clips(), [])
        self.assertFalse(self.state_path.exists())

    def test_bad_configuration_runs_nothing(self):
        self.config["output_folder"] = str(self.root / "missing")
        self.write_config()
        code, _, err = self.run_tool("clip")
        self.assertEqual(code, 1)
        self.assertIn("output_folder does not exist", err)
        self.assertEqual(self.calls(), [])

    def test_missing_csdm_is_a_plain_error(self):
        self.config["csdm_path"] = str(self.tool / "no-such-csdm")
        self.write_config()
        code, _, err = self.run_tool("clip")
        self.assertEqual(code, 1)
        self.assertIn("Could not find the CS Demo Manager command line", err)

    def test_list_shows_status(self):
        self.add_demo("match730_two.dem", age=10, size=11)
        self.run_tool("clip", "--demo", str(self.demo))
        code, out, _ = self.run_tool("list")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"done, 3 clips\s+match730_one\.dem")
        self.assertRegex(out, r"new\s+match730_two\.dem")
        self.assertIn("2 demo(s), 1 not clipped yet", out)

    def test_check_reports_ready_and_problems(self):
        code, out, _ = self.run_tool("check")
        self.assertEqual(code, 0)
        self.assertIn("Everything looks ready.", out)
        self.assertIn("1 found, 1 not clipped yet", out)

        self.config["steam_id"] = "abc"
        self.config["csdm_path"] = str(self.tool / "no-such-csdm")
        self.write_config()
        code, out, _ = self.run_tool("check")
        self.assertEqual(code, 1)
        self.assertIn("steam_id must be", out)
        self.assertIn("NOT FOUND", out)

    # -- what the window relies on ----------------------------------------

    def engine(self):
        config = clipenc.load_config(self.config_path)
        return config, clipenc.load_state(self.state_path), clipenc.resolve_csdm(config["csdm_path"])

    def test_runner_gets_csdm_output_and_the_stages_in_order(self):
        config, state, csdm = self.engine()
        lines, stages = [], []
        runner = clipenc.Runner(on_line=lines.append, on_stage=lambda name, details: stages.append((name, details)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            clips = clipenc.clip_demo(self.demo, STEAM_ID, config, state, self.state_path, csdm, 4.0, 3.0, runner=runner)
        self.assertEqual(len(clips), 3)
        self.assertEqual(out.getvalue(), "", "with a runner, nothing is printed")
        self.assertEqual([name for name, _ in stages], ["analyze", "record", "sort", "done"])
        self.assertEqual(stages[1][1]["label"], "Zethrel")
        self.assertEqual(stages[1][1]["kills"], 4)
        self.assertEqual(stages[3][1]["clips"], 3)
        text = "\n".join(lines)
        for expected in ("> csdm analyze", "1 demos to process", "> csdm video", "Starting Counter-Strike...", "Recording in progress...", "3 clip(s) are in"):
            self.assertIn(expected, text)

    def test_players_are_remembered_after_a_clip(self):
        self.run_tool("clip")
        info = clipenc.demo_info(self.state(), clipenc.demo_key(self.demo))
        self.assertEqual(info["map"], "de_overpass")
        self.assertEqual(info["checksum"], "0123456789abcdef")
        self.assertEqual(
            info["players"],
            [
                {"steam_id": STEAM_ID, "name": "Zethrel", "team": "Team B", "kills": 4},
                {"steam_id": OTHER_ID, "name": "Teammate", "team": "Team A", "kills": 0},
            ],
        )

    def test_players_can_be_read_without_recording(self):
        config, state, csdm = self.engine()
        lines = []
        summary = clipenc.load_demo_info(self.demo, config, state, self.state_path, csdm, clipenc.Runner(on_line=lines.append))
        self.assertEqual([player["name"] for player in summary["players"]], ["Zethrel", "Teammate"])
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"])
        saved = self.state()
        self.assertEqual(clipenc.demo_info(saved, clipenc.demo_key(self.demo))["map"], "de_overpass")
        self.assertIsNone(clipenc.done_entry(saved, clipenc.demo_key(self.demo), STEAM_ID), "reading players is not clipping")
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists())
        self.assertIn("1 demo(s), 1 not clipped yet", self.run_tool("list")[1])

    def test_stop_ends_csdm_and_marks_nothing(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]], sleep=60)
        config, state, csdm = self.engine()
        recording = threading.Event()
        runner = clipenc.Runner(
            on_line=lambda line: None,
            on_stage=lambda name, details: recording.set() if name == "record" else None,
        )
        outcome = {}

        def work():
            try:
                clipenc.clip_demo(self.demo, STEAM_ID, config, state, self.state_path, csdm, 4.0, 3.0, runner=runner)
                outcome["result"] = "finished"
            except clipenc.Stopped:
                outcome["result"] = "stopped"
            except Exception as error:  # pragma: no cover - shown by the assert below
                outcome["result"] = repr(error)

        thread = threading.Thread(target=work)
        started = time.monotonic()
        thread.start()
        self.assertTrue(recording.wait(20), "the recording step never started")
        time.sleep(0.5)
        runner.stop()
        thread.join(20)
        self.assertFalse(thread.is_alive(), "the run did not end after stop")
        self.assertLess(time.monotonic() - started, 40)
        self.assertEqual(outcome["result"], "stopped")
        self.assertEqual(self.clips(), [])
        self.assertIsNone(clipenc.done_entry(self.state(), clipenc.demo_key(self.demo), STEAM_ID))
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists(), "a stopped run leaves no work folder")
        self.close_game.assert_called_once_with()
        self.assertFalse(runner.recording)

    def test_stop_while_only_reading_a_demo_leaves_the_game_alone(self):
        self.control(match=make_match(killers=[STEAM_ID]), analyze_sleep=60)
        config, state, csdm = self.engine()
        reading = threading.Event()
        runner = clipenc.Runner(on_line=lambda line: reading.set() if "demos to process" in line else None)
        outcome = {}

        def work():
            try:
                clipenc.load_demo_info(self.demo, config, state, self.state_path, csdm, runner)
                outcome["result"] = "finished"
            except clipenc.Stopped:
                outcome["result"] = "stopped"
            except Exception as error:  # pragma: no cover - shown by the assert below
                outcome["result"] = repr(error)

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(reading.wait(20), "csdm analyze never started")
        runner.stop()
        thread.join(20)
        self.assertFalse(thread.is_alive(), "reading did not end after stop")
        self.assertEqual(outcome["result"], "stopped")
        self.close_game.assert_not_called()
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists())

    def test_a_stop_that_lands_while_csdm_is_starting_still_ends_it(self):
        self.control(match=make_match(killers=[STEAM_ID]), clips=[[1, 10, 20]], analyze_sleep=60)
        config, state, csdm = self.engine()
        runner = clipenc.Runner(on_line=lambda line: None)
        real_build = clipenc.build_command

        def build_then_stop(*arguments, **options):
            # The narrowest moment: after the "was it stopped?" check, before csdm exists.
            command = real_build(*arguments, **options)
            runner.stop()
            return command

        started = time.monotonic()
        with mock.patch.object(clipenc, "build_command", side_effect=build_then_stop):
            with self.assertRaises(clipenc.Stopped):
                clipenc.load_demo_info(self.demo, config, state, self.state_path, csdm, runner)
        self.assertLess(time.monotonic() - started, 30, "csdm was left running to its end")
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists())

    def test_a_stopped_runner_starts_nothing(self):
        config, state, csdm = self.engine()
        runner = clipenc.Runner(on_line=lambda line: None)
        runner.stop()
        with self.assertRaises(clipenc.Stopped):
            clipenc.clip_demo(self.demo, STEAM_ID, config, state, self.state_path, csdm, 4.0, 3.0, runner=runner)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists())

    def test_damaged_state_file_is_a_plain_error(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        code, _, err = self.run_tool("clip")
        self.assertEqual(code, 1)
        self.assertIn("is damaged", err)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
