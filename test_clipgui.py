# Copyright (c) 2026 Zethrel. All rights reserved. See LICENSE.
"""Tests for the window. Run with:  python -m unittest -v

The first half tests what the window shows, as plain functions. The second
half opens the real window against the stand-in csdm from test_clipenc.py and
drives it: select a demo, tick players, start a run, stop a run. Nothing here
starts CS Demo Manager or CS2, and nothing can close a running game.

The whole file is skipped where Python has no tkinter. The window tests are
skipped where there is no screen to open a window on.
"""
from __future__ import annotations

import gc
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk
    import clipgui
except ImportError as error:  # Python built without tkinter
    raise unittest.SkipTest("tkinter is not available: %s" % error)

import clipenc
from test_clipenc import OTHER_ID, STEAM_ID, StubCsdmCase

# Made-up IDs and names. Nothing here belongs to a real player.
MATE_ID = "76561190000000003"
QUIET_ID = "76561190000000004"
RIVAL_ID = "76561190000000011"
NAMES = {
    STEAM_ID: "Zethrel",
    MATE_ID: "Teammate",
    QUIET_ID: "Quiet one",
    OTHER_ID: "Opponent",
    RIVAL_ID: "Rival",
}
OWN_TEAM = (STEAM_ID, MATE_ID, QUIET_ID)


def make_match(map_name="de_overpass"):
    """Three on the user's team, one of them without a kill, and two opponents."""
    killers = [STEAM_ID] * 4 + [MATE_ID] * 2 + [OTHER_ID] * 5 + [RIVAL_ID] * 3
    return {
        "mapName": map_name,
        "checksum": "0123456789abcdef",
        "players": [
            {"steamId": steam_id, "name": name, "teamName": "Team A" if steam_id in OWN_TEAM else "Team B"}
            for steam_id, name in NAMES.items()
        ],
        "kills": [
            {"killerSteamId": steam_id, "tick": 1000 + index * 700, "roundNumber": 1 + index}
            for index, steam_id in enumerate(killers)
        ],
    }


CONFIG = {
    "steam_id": STEAM_ID,
    "player_labels": {STEAM_ID: "Zethrel"},
    "map_prefixes": {"de_overpass": "Overpass"},
}


class TextTests(unittest.TestCase):
    def test_plural(self):
        self.assertEqual(clipgui.plural(1, "clip"), "1 clip")
        self.assertEqual(clipgui.plural(0, "clip"), "0 clips")
        self.assertEqual(clipgui.plural(12, "demo"), "12 demos")

    def test_clips_text(self):
        self.assertEqual(clipgui.clips_text(None), "New")
        self.assertEqual(clipgui.clips_text({"clips": ["a", "b"]}), "2 clips")
        self.assertEqual(clipgui.clips_text({"clips": [], "note": "no kills"}), "No kills")
        self.assertEqual(clipgui.clips_text({"clips": [], "note": "player not in demo"}), "Not in demo")

    def test_count_choices(self):
        self.assertEqual(clipgui.count_choices(0), [])
        self.assertEqual(clipgui.count_choices(1), [("Newest 1", 1)])
        self.assertEqual(clipgui.count_choices(2), [("Newest 1", 1), ("All 2", 2)])
        self.assertEqual(clipgui.count_choices(3), [("Newest 1", 1), ("All 3", 3)])
        self.assertEqual(clipgui.count_choices(7), [("Newest 1", 1), ("Newest 3", 3), ("All 7", 7)])

    def test_assets_are_found_from_source_and_from_a_built_exe(self):
        from_source = clipgui.resource_path("logo.png")
        self.assertEqual(from_source, Path(clipgui.__file__).resolve().parent / "assets" / "logo.png")
        self.assertTrue(from_source.is_file(), "assets/logo.png is missing")
        self.assertTrue(clipgui.resource_path("icon.ico").is_file(), "assets/icon.ico is missing")
        with mock.patch.object(clipgui.sys, "_MEIPASS", "/unpacked", create=True):
            self.assertEqual(clipgui.resource_path("logo.png"), Path("/unpacked") / "assets" / "logo.png")

    def test_build_stamp_is_empty_when_run_from_source(self):
        self.assertEqual(clipgui.build_stamp(), "")

    def test_clean_line(self):
        self.assertEqual(clipgui.clean_line("plain"), "plain")
        self.assertEqual(clipgui.clean_line("\x1b[32mdone\x1b[0m"), "done")
        self.assertEqual(clipgui.clean_line("10%\r50%\r100%"), "100%")
        self.assertEqual(clipgui.clean_line("line\r"), "line")

    def test_one_paragraph(self):
        self.assertEqual(clipgui.one_paragraph("first line.\nsecond   line."), "first line. second line.")

    def test_end_text(self):
        def result(**values):
            return dict({"clips": 0, "finished": 0, "total": 2, "error": None, "stopped": False}, **values)

        self.assertEqual(clipgui.end_text(result(clips=5, finished=2)), ("Done: 5 clips from 2 recordings.", False))
        self.assertEqual(clipgui.end_text(result(stopped=True)), ("Stopped by you. Nothing was saved.", False))
        self.assertEqual(
            clipgui.end_text(result(stopped=True, clips=3, finished=1)),
            ("Stopped by you after 1 recording, 3 clips saved.", False),
        )
        text, is_error = clipgui.end_text(result(error="CSDM stopped with exit code 1", finished=1))
        self.assertTrue(is_error)
        self.assertIn("after 1 recording", text)

    def test_players_are_grouped_with_the_own_team_first_and_most_kills_on_top(self):
        info = clipenc.match_summary(make_match())
        state = {"version": 1, "demos": {"k": {"players": {MATE_ID: {"clips": ["a.mkv"], "note": ""}}}}}
        groups = clipgui.player_rows(info, state, "k", CONFIG)

        self.assertEqual([heading for heading, _ in groups], ["Your team", "Other team"])
        own, other = groups[0][1], groups[1][1]
        self.assertEqual([row["name"] for row in own], ["Zethrel", "Teammate", "Quiet one"])
        self.assertEqual([row["kills"] for row in own], [4, 2, 0])
        self.assertEqual([row["status"] for row in own], ["Not clipped", "1 clip", "No kills"])
        self.assertEqual([row["done"] for row in own], [False, True, False])
        self.assertEqual([(row["name"], row["kills"]) for row in other], [("Opponent", 5), ("Rival", 3)])

    def test_players_fall_back_to_team_names_when_the_user_is_not_in_the_demo(self):
        info = clipenc.match_summary(make_match())
        config = dict(CONFIG, steam_id="76561190000000099")
        groups = clipgui.player_rows(info, {"version": 1, "demos": {}}, "k", config)
        self.assertEqual([heading for heading, _ in groups], ["Team A", "Team B"])
        self.assertEqual(len(groups[0][1]) + len(groups[1][1]), 5)

    def test_a_label_from_the_settings_wins_over_the_name_in_the_demo(self):
        info = clipenc.match_summary(make_match())
        config = dict(CONFIG, player_labels={STEAM_ID: "Zethrel", OTHER_ID: "Nemesis"})
        groups = clipgui.player_rows(info, {"version": 1, "demos": {}}, "k", config)
        self.assertEqual(groups[1][1][0]["name"], "Nemesis")


class ListTests(StubCsdmCase):
    """What the demo list shows, without opening a window."""

    def test_demo_rows_and_waiting_demos(self):
        newer = self.add_demo("match730_two.dem", age=50, size=11)
        config = clipenc.load_config(self.config_path)
        state = clipenc.load_state(self.state_path)
        clipenc.remember_demo(state, self.demo, clipenc.match_summary(make_match()))
        clipenc.record_done(state, self.demo, STEAM_ID, "de_overpass", ["a.mkv", "b.mkv"], folder="Overpass")

        demos = clipenc.find_demos(config["demo_folder"])
        rows = clipgui.demo_rows(demos, state, config)
        self.assertEqual([row["path"].name for row in rows], ["match730_two.dem", "match730_one.dem"])
        self.assertEqual([row["map"] for row in rows], ["Not analyzed yet", "Overpass"])
        self.assertEqual([row["clips"] for row in rows], ["New", "2 clips"])
        self.assertEqual([row["new"] for row in rows], [True, False])
        self.assertEqual(clipgui.waiting_demos(demos, state, STEAM_ID), [newer])
        self.assertEqual(clipgui.waiting_demos(demos, state, MATE_ID), demos)

    def test_last_run_text(self):
        config = clipenc.load_config(self.config_path)
        state = clipenc.load_state(self.state_path)
        self.assertEqual(clipgui.last_run_text(state, config), "")
        clipenc.remember_demo(state, self.demo, clipenc.match_summary(make_match()))
        clipenc.record_done(state, self.demo, MATE_ID, "de_overpass", ["a.mkv"], folder="Overpass")
        text = clipgui.last_run_text(state, config)
        self.assertTrue(text.startswith("Last run "), text)
        self.assertTrue(text.endswith(": 1 clip for Teammate in Overpass."), text)

    def test_setup_problems_names_what_is_wrong(self):
        config, csdm, problems = clipgui.setup_problems(self.config_path)
        self.assertEqual(problems, [])
        self.assertEqual(Path(csdm).name, self.launcher.name)

        self.config["csdm_path"] = str(self.tool / "not-here")
        self.config["steam_id"] = "123"
        self.write_config()
        config, csdm, problems = clipgui.setup_problems(self.config_path)
        self.assertIsNone(csdm)
        self.assertEqual(len(problems), 2)

        config, csdm, problems = clipgui.setup_problems(self.root / "missing.json")
        self.assertIsNone(config)
        self.assertEqual(len(problems), 1)


class WindowTests(StubCsdmCase):
    """The real window, driven the way a user would, against the stand-in csdm."""

    def setUp(self):
        super().setUp()
        self.control(match=make_match(), clips=[[1, 100, 200], [2, 300, 400], [3, 500, 600]])
        self.second = self.add_demo("match730_two.dem", age=50, size=11)    # newer than self.demo
        self.answers = {"askyesno": True}
        self.asked = []
        for name in ("askyesno", "showinfo", "showerror"):
            patcher = mock.patch.object(clipgui.messagebox, name, side_effect=self.answer(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.window = tk.Tk()
        except tk.TclError as error:
            self.skipTest("no screen to open a window on: %s" % error)
        self.addCleanup(self.close_window)
        self.app = clipgui.App(self.window, self.config_path, self.state_path)
        self.window.update()

    def answer(self, name):
        def reply(title, message=None, **_options):
            self.asked.append((name, title))
            return self.answers.get(name)
        return reply

    def close_window(self):
        app = self.app
        if app.runner is not None:
            app.runner.stop()
        if app.worker is not None:
            app.worker.join(20)
        app.shut()
        # Tk may only be torn down by the thread that made it. Drop the window
        # here and now, so that no later test's worker thread ends up doing it.
        self.app = self.window = app = None
        gc.collect()

    def wait(self, condition, what, seconds=20):
        """Keep the window alive until `condition()` holds."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.window.update()
            if condition():
                self.window.update()
                return
            time.sleep(0.01)
        self.fail("timed out waiting for " + what)

    def select(self, demo):
        self.app.tree.selection_set(clipenc.demo_key(demo))
        self.window.update()

    def show_players(self, demo):
        self.select(demo)
        self.wait(lambda: self.app.shown_key == clipenc.demo_key(demo) and self.app.mode == "idle", "the player list")

    def tick(self, *names):
        for chosen, row in self.app.player_vars:
            chosen.set(row["name"] in names)
        self.app.update_selection_count()

    def log_text(self):
        return self.app.log.get("1.0", "end")

    def disabled(self, widget):
        return widget.instate(["disabled"])

    # -- what is on show ---------------------------------------------------

    def test_start_lists_the_demos_newest_first(self):
        app = self.app
        self.assertEqual(app.side_shown, "players")
        rows = [app.tree.item(item, "values") for item in app.tree.get_children()]
        self.assertEqual([values[3] for values in rows], ["match730_two.dem", "match730_one.dem"])
        self.assertEqual([values[1:3] for values in rows], [("Not analyzed yet", "New")] * 2)
        self.assertEqual(app.subtitle.get(), "2 demos in the demo folder, 2 not clipped for you yet")
        self.assertEqual(list(app.count_box["values"]), ["Newest 1", "All 2"])
        self.assertEqual(app.count_choice.get(), "Newest 1")
        self.assertFalse(self.disabled(app.clip_new_button))
        self.assertTrue(self.disabled(app.clip_players_button), "nothing is ticked yet")
        self.assertEqual(self.calls(), [], "opening the window must not start csdm")

    def test_selecting_a_demo_reads_its_players_without_recording(self):
        self.show_players(self.demo)
        app = self.app
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"])
        self.assertEqual(
            [(row["name"], row["kills"], row["status"]) for _, row in app.player_vars],
            [("Zethrel", 4, "Not clipped"), ("Teammate", 2, "Not clipped"), ("Quiet one", 0, "No kills"),
             ("Opponent", 5, "Not clipped"), ("Rival", 3, "Not clipped")],
        )
        self.assertEqual(app.tree.item(clipenc.demo_key(self.demo), "values")[1], "Overpass")
        self.assertEqual(app.players_where.get().split(",")[0], "Overpass")
        self.assertIsNone(clipenc.done_entry(self.state(), clipenc.demo_key(self.demo), STEAM_ID))

        # Selecting it again asks CSDM nothing: the players are remembered.
        self.select(self.second)
        self.wait(lambda: app.shown_key == clipenc.demo_key(self.second) and app.mode == "idle", "the second demo")
        self.select(self.demo)
        self.wait(lambda: app.shown_key == clipenc.demo_key(self.demo), "the first demo again")
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"] * 2)

    def test_a_player_without_kills_cannot_be_ticked(self):
        self.show_players(self.demo)
        boxes = [child for child in self.app.players_body.winfo_children() if child.winfo_class() == "TCheckbutton"]
        by_name = {box.cget("text"): box for box in boxes}
        self.assertEqual(sorted(by_name), ["Opponent", "Quiet one", "Rival", "Teammate", "Zethrel"])
        self.assertTrue(self.disabled(by_name["Quiet one"]))
        self.assertFalse(self.disabled(by_name["Opponent"]), "opponents are selectable")

    # -- running -----------------------------------------------------------

    def test_clipping_two_ticked_players_runs_once_for_each(self):
        self.show_players(self.demo)
        self.tick("Teammate", "Opponent")
        app = self.app
        self.assertEqual(app.clip_players_button.cget("text"), "Clip 2 players")
        self.assertFalse(self.disabled(app.clip_players_button))

        app.clip_players_button.invoke()
        self.assertEqual(app.mode, "running")
        self.assertEqual(app.side_shown, "run")
        self.assertTrue(self.disabled(app.clip_new_button), "nothing else can start during a run")
        self.assertEqual(str(app.tree.cget("selectmode")), "none", "the demo list is locked during a run")
        self.wait(lambda: app.mode == "idle", "the run to end")

        self.assertEqual(app.side_shown, "players")
        self.assertEqual(str(app.tree.cget("selectmode")), "browse")
        self.assertEqual(app.activity_text.get(), "Done: 6 clips from 2 recordings.")
        names = self.clips()
        self.assertEqual(len(names), 6)
        self.assertEqual(sum(name.startswith("Overpass-Teammate-") for name in names), 3)
        self.assertEqual(sum(name.startswith("Overpass-Opponent-") for name in names), 3)
        videos = [call for call in self.calls() if call[0] == "video"]
        self.assertEqual([call[call.index("--steamids") + 1] for call in videos], [MATE_ID, OTHER_ID])
        self.assertIn("> csdm video", self.log_text())
        self.assertIn("Recording in progress...", self.log_text())

        saved = self.state()
        key = clipenc.demo_key(self.demo)
        self.assertEqual(len(clipenc.done_entry(saved, key, MATE_ID)["clips"]), 3)
        self.assertEqual(len(clipenc.done_entry(saved, key, OTHER_ID)["clips"]), 3)
        self.assertIsNone(clipenc.done_entry(saved, key, STEAM_ID), "clipping others does not mark the user's own")
        statuses = {row["name"]: row["status"] for _, row in app.player_vars}
        self.assertEqual(statuses["Teammate"], "3 clips")
        self.assertEqual(statuses["Zethrel"], "Not clipped")
        self.assertEqual(app.tree.item(key, "values")[2], "New", "the demo is still new for the user")

    def test_clip_my_new_demos_takes_the_newest_first(self):
        app = self.app
        app.clip_new_button.invoke()
        self.wait(lambda: app.mode == "idle", "the run to end")
        self.assertEqual(app.activity_text.get(), "Done: 3 clips from 1 recording.")
        saved = self.state()
        self.assertIsNotNone(clipenc.done_entry(saved, clipenc.demo_key(self.second), STEAM_ID))
        self.assertIsNone(clipenc.done_entry(saved, clipenc.demo_key(self.demo), STEAM_ID))
        self.assertEqual(list(app.count_box["values"]), ["Newest 1"])
        self.assertEqual(app.subtitle.get(), "2 demos in the demo folder, 1 not clipped for you yet")
        self.assertEqual(app.tree.item(clipenc.demo_key(self.second), "values")[1:3], ("Overpass", "3 clips"))

    def test_clip_my_new_demos_can_take_all(self):
        app = self.app
        app.count_choice.set("All 2")
        app.clip_new_button.invoke()
        self.wait(lambda: app.mode == "idle", "the run to end")
        self.assertEqual(app.activity_text.get(), "Done: 6 clips from 2 recordings.")
        self.assertTrue(self.disabled(app.clip_new_button), "nothing is left to clip")
        self.assertEqual(app.subtitle.get(), "2 demos in the demo folder, 0 not clipped for you yet")

    def test_the_run_panel_follows_the_steps(self):
        self.control(match=make_match(), clips=[[1, 100, 200]], sleep=60)
        app = self.app
        app.clip_new_button.invoke()
        self.wait(lambda: app.step_state.get("record") == "now", "the recording step")
        self.assertEqual(app.step_state, {"analyze": "done", "record": "now", "sort": "later"})
        self.assertEqual(app.job_status.get(), "Recording")
        self.assertEqual(app.run_counter.get(), "Recording 1 of 1")
        self.assertIn("Zethrel", app.job_title.get())
        self.assertTrue(app.job_title.get().startswith("Overpass"), app.job_title.get())
        self.assertTrue(app.runner.recording)
        app.stop_run(confirm=False)
        self.wait(lambda: app.mode == "idle", "the run to stop")

    def test_stop_ends_the_run_and_leaves_the_demo_unclipped(self):
        self.control(match=make_match(), clips=[[1, 100, 200]], sleep=60)
        app = self.app
        app.count_choice.set("All 2")
        app.clip_new_button.invoke()
        self.wait(lambda: app.job_status.get() == "Recording", "the recording step")
        started = time.monotonic()

        app.stop_button.invoke()
        self.assertEqual(self.asked, [("askyesno", "Stop the run?")])
        self.wait(lambda: app.mode == "idle", "the run to stop")

        self.assertLess(time.monotonic() - started, 30, "stop must not wait for csdm to finish")
        self.assertEqual(app.activity_text.get(), "Stopped by you. Nothing was saved.")
        self.assertEqual(app.side_shown, "players")
        self.assertEqual(self.clips(), [])
        # At most one: the stand-in may be ended before it has noted its own start.
        self.assertLessEqual(len([call for call in self.calls() if call[0] == "video"]), 1, "the second demo never started")
        saved = self.state()
        for demo in (self.demo, self.second):
            self.assertIsNone(clipenc.done_entry(saved, clipenc.demo_key(demo), STEAM_ID))
        self.assertFalse((self.output / clipenc.WORK_FOLDER_NAME).exists(), "a stopped run leaves no work folder")
        self.close_game.assert_called_once_with()
        self.assertFalse(self.disabled(app.clip_new_button), "a new run can start after a stop")

    def test_answering_no_keeps_the_run_going(self):
        self.control(match=make_match(), clips=[[1, 100, 200]], sleep=1)
        self.answers["askyesno"] = False
        app = self.app
        app.clip_new_button.invoke()
        self.wait(lambda: app.job_status.get() == "Recording", "the recording step")
        app.stop_button.invoke()
        self.assertFalse(app.runner.stop_requested)
        self.wait(lambda: app.mode == "idle", "the run to end")
        self.assertEqual(app.activity_text.get(), "Done: 1 clip from 1 recording.")
        self.close_game.assert_not_called()

    def test_a_failed_recording_is_reported_and_marks_nothing(self):
        self.control(match=make_match(), clips=[], video_exit=1)
        app = self.app
        app.count_choice.set("All 2")
        app.clip_new_button.invoke()
        self.wait(lambda: app.mode == "idle", "the run to end")
        self.assertTrue(app.activity_text.get().startswith("Stopped on an error after 0 recordings"))
        self.assertIn("CSDM stopped with exit code 1", self.log_text())
        self.assertEqual(len([call for call in self.calls() if call[0] == "video"]), 1, "a run stops at the first error")
        self.assertIsNone(clipenc.done_entry(self.state(), clipenc.demo_key(self.second), STEAM_ID))
        self.assertFalse(self.disabled(app.clip_new_button))

    def test_clipping_a_player_again_asks_first(self):
        self.show_players(self.demo)
        self.tick("Teammate")
        self.app.clip_players_button.invoke()
        self.wait(lambda: self.app.mode == "idle", "the first run")
        videos = len([call for call in self.calls() if call[0] == "video"])

        self.answers["askyesno"] = False
        self.tick("Teammate")
        self.app.clip_players_button.invoke()
        self.assertEqual(self.asked, [("askyesno", "Clip again?")])
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(len([call for call in self.calls() if call[0] == "video"]), videos)

        self.answers["askyesno"] = True
        self.app.clip_players_button.invoke()
        self.wait(lambda: self.app.mode == "idle", "the second run")
        self.assertEqual(len(self.clips()), 6, "a second set of clips sits next to the first")

    # -- when things are wrong -------------------------------------------------

    def test_a_demo_csdm_cannot_read_is_asked_for_once_not_in_a_loop(self):
        self.control(match=None)
        app = self.app
        key = clipenc.demo_key(self.demo)
        self.select(self.demo)
        self.wait(lambda: key in app.load_errors and app.mode == "idle", "the error")
        for _ in range(20):                # give a retry loop every chance to show itself
            self.window.update()
            time.sleep(0.01)
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"])
        self.assertIn("did not export the match data", app.load_errors[key])
        self.assertIsNone(app.shown_key)
        self.assertTrue(self.disabled(app.clip_players_button))

        # "Try again" asks CSDM once more, and now it works.
        self.control(match=make_match(), clips=[[1, 100, 200]])
        app.retry_loading(key)
        self.wait(lambda: app.shown_key == key and app.mode == "idle", "the player list")
        self.assertEqual([call[0] for call in self.calls()], ["analyze", "json"] * 2)
        self.assertEqual(len(app.player_vars), 5)

    def test_broken_settings_show_the_setup_panel_until_they_are_fixed(self):
        good = dict(self.config)
        self.config["steam_id"] = "123"
        self.config["demo_folder"] = str(self.root / "missing")
        self.write_config()
        app = self.app
        app.reload()
        self.assertEqual(app.side_shown, "setup")
        self.assertIn("steam_id", app.setup_text.get())
        self.assertIn("demo_folder", app.setup_text.get())
        self.assertEqual(app.tree.get_children(), ())
        self.assertTrue(self.disabled(app.clip_new_button))
        self.assertTrue(self.disabled(app.clip_players_button))

        self.config = good
        self.write_config()
        app.reload()                        # the "Check again" button
        self.assertEqual(app.side_shown, "players")
        self.assertEqual(len(app.tree.get_children()), 2)
        self.assertFalse(self.disabled(app.clip_new_button))

    def test_the_log_drops_its_oldest_lines(self):
        app = self.app
        with mock.patch.object(clipgui, "LOG_LINES", 5):
            for number in range(12):
                app._log_line("line %d" % number)
        self.assertEqual(self.log_text().split(), ["line", "7", "line", "8", "line", "9", "line", "10", "line", "11"])

    def test_refresh_picks_up_a_new_demo(self):
        app = self.app
        self.add_demo("match730_three.dem", age=10, size=12)
        self.assertEqual(len(app.tree.get_children()), 2)
        app.refresh_button.invoke()
        self.window.update()
        rows = [app.tree.item(item, "values")[3] for item in app.tree.get_children()]
        self.assertEqual(rows, ["match730_three.dem", "match730_two.dem", "match730_one.dem"])
        self.assertEqual(list(app.count_box["values"]), ["Newest 1", "All 3"])

    def test_closing_during_a_run_asks_and_then_stops_it(self):
        self.control(match=make_match(), clips=[[1, 100, 200]], sleep=60)
        app = self.app
        app.clip_new_button.invoke()
        self.wait(lambda: app.job_status.get() == "Recording", "the recording step")

        self.answers["askyesno"] = False
        app.on_close()
        self.assertFalse(app.closing, "answering no keeps the window open")
        self.assertEqual(app.mode, "running")

        self.answers["askyesno"] = True
        worker = app.worker
        app.on_close()
        self.assertTrue(app.closing)
        worker.join(20)
        self.assertFalse(worker.is_alive(), "closing the window must end the run")
        self.close_game.assert_called_once_with()
        self.assertEqual(self.clips(), [])


if __name__ == "__main__":
    unittest.main()
