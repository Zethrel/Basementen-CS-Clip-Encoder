#!/usr/bin/env python3
# Copyright (c) 2026 Zethrel. All rights reserved. See LICENSE.
"""CS clip encoder.

Drives the installed CS Demo Manager command line (`csdm`) to record one
player's kills from a CS2 demo, then renames the clips to
`Map-Player-random.mkv` and moves them to the output folder.

It does not record or encode anything itself. Standard library only.

    python clipenc.py check     show the configuration and what is wrong with it
    python clipenc.py list      show the demos and which ones are already clipped
    python clipenc.py clip      clip the newest demo that is not clipped yet
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.2.2"
TESTED_CSDM_VERSION = "3.20.1"

APP_DIR = Path(__file__).resolve().parent
CONFIG_NAME = "config.json"
STATE_NAME = "state.json"
WORK_FOLDER_NAME = "_work"

# The limits the user chose. CSDM itself accepts any non-negative number, so
# the caps have to be enforced here.
MAX_SECONDS_BEFORE = 10
MAX_SECONDS_AFTER = 8

DEFAULT_CONFIG = {
    "csdm_path": "csdm",
    "demo_folder": "",
    "output_folder": "",
    "work_folder": "",
    "steam_id": "",
    "player_labels": {},
    "map_prefixes": {},
    "map_subfolders": True,
    "map_folders": {},
    "seconds_before": 4,
    "seconds_after": 3,
    "video": {
        "width": 1920,
        "height": 1080,
        "framerate": 30,
        "container": "mkv",
        "video_codec": "libx265",
        "audio_codec": "aac",
        "audio_bitrate": 512,
        "crf": 28,
        "recording_system": "HLAE",
    },
}

VIDEO_CONTAINERS = ("mkv", "mp4", "avi")
RECORDING_SYSTEMS = ("HLAE", "CS")

# Characters Windows does not allow in a file name.
FORBIDDEN_NAME_CHARS = '<>:"/\\|?*'
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    "%s%d" % (stem, number) for stem in ("COM", "LPT") for number in range(1, 10)
}
MAX_LABEL_LENGTH = 40
SUFFIX_LENGTH = 8


class ClipError(Exception):
    """A problem to show the user as one plain message, without a traceback."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config(path):
    """Read config.json and fill in every key it leaves out."""
    path = Path(path)
    if not path.is_file():
        raise ClipError(
            "No configuration file at %s.\n"
            "Copy config.example.json to config.json and fill it in." % path
        )
    try:
        with open(path, encoding="utf-8-sig") as handle:
            loaded = json.load(handle)
    except json.JSONDecodeError as error:
        raise ClipError("%s is not valid JSON: %s" % (path, error))
    if not isinstance(loaded, dict):
        raise ClipError("%s must hold one JSON object." % path)

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in loaded.items():
        if key == "video" and isinstance(value, dict):
            config["video"].update(value)
        else:
            config[key] = value
    return config


def check_padding(before, after):
    """Return the padding as numbers, or raise if it is outside the limits."""
    try:
        before = float(before)
        after = float(after)
    except (TypeError, ValueError):
        raise ClipError("The seconds before and after a kill must be numbers.")
    if not 0 <= before <= MAX_SECONDS_BEFORE:
        raise ClipError(
            "Seconds before a kill must be between 0 and %d, not %g."
            % (MAX_SECONDS_BEFORE, before)
        )
    if not 0 <= after <= MAX_SECONDS_AFTER:
        raise ClipError(
            "Seconds after a kill must be between 0 and %d, not %g."
            % (MAX_SECONDS_AFTER, after)
        )
    return before, after


def is_steam_id(value):
    return isinstance(value, str) and re.fullmatch(r"\d{17}", value) is not None


def config_problems(config):
    """Everything wrong with the configuration, as a list of sentences."""
    problems = []

    if not is_steam_id(config.get("steam_id")):
        problems.append("steam_id must be your SteamID64: 17 digits, in quotes.")

    demo_folder = config.get("demo_folder")
    if not demo_folder:
        problems.append("demo_folder is empty.")
    elif not Path(demo_folder).is_dir():
        problems.append("demo_folder does not exist: %s" % demo_folder)

    output_folder = config.get("output_folder")
    if not output_folder:
        problems.append("output_folder is empty. Set it to where finished clips should go.")
    elif not Path(output_folder).is_dir():
        problems.append("output_folder does not exist: %s" % output_folder)

    try:
        check_padding(config.get("seconds_before"), config.get("seconds_after"))
    except ClipError as error:
        problems.append(str(error))

    video = config.get("video", {})
    if video.get("container") not in VIDEO_CONTAINERS:
        problems.append("video.container must be one of: %s." % ", ".join(VIDEO_CONTAINERS))
    if video.get("recording_system") not in RECORDING_SYSTEMS:
        problems.append(
            "video.recording_system must be one of: %s." % ", ".join(RECORDING_SYSTEMS)
        )
    for key in ("width", "height", "framerate", "audio_bitrate", "crf"):
        if not isinstance(video.get(key), int) or isinstance(video.get(key), bool):
            problems.append("video.%s must be a whole number." % key)
    for key in ("video_codec", "audio_codec"):
        if not isinstance(video.get(key), str) or not video.get(key):
            problems.append("video.%s must be a codec name." % key)

    for key in ("player_labels", "map_prefixes", "map_folders"):
        if not isinstance(config.get(key), dict):
            problems.append("%s must be a JSON object." % key)
    if not isinstance(config.get("work_folder"), str):
        problems.append("work_folder must be a path in quotes, or \"\" for the default.")
    if not isinstance(config.get("map_subfolders"), bool):
        problems.append("map_subfolders must be true or false.")

    return problems


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

def sanitize_label(name, fallback):
    """Make a player or map name safe to use inside a Windows file name.

    The result can differ from the name shown in game. If nothing usable is
    left, the fallback (the Steam ID) is used instead.
    """
    text = "" if name is None else str(name)
    text = "".join(
        char for char in text if char not in FORBIDDEN_NAME_CHARS and ord(char) >= 32
    )
    text = " ".join(text.split())
    text = text[:MAX_LABEL_LENGTH].rstrip(" .")
    if not text:
        return str(fallback)
    if text.upper() in RESERVED_NAMES:
        text = "_" + text
    return text


def map_prefix(map_name, table):
    """Return (prefix, came_from_table) for a map such as `de_anubis`.

    The user's table wins. A map that is not in it gets a prefix derived from
    its name (`de_overpass` becomes `Overpass`), so a long recording is never
    thrown away over a missing table entry.
    """
    name = str(map_name or "").strip()
    for key, value in (table or {}).items():
        if str(key).lower() == name.lower() and str(value).strip():
            return sanitize_label(value, "Unknown"), True

    derived = name
    head, separator, tail = name.partition("_")
    if separator and tail and len(head) <= 3:
        derived = tail
    derived = derived.replace("_", " ").strip()
    derived = derived[:1].upper() + derived[1:]
    return sanitize_label(derived, "Unknown"), False


def map_folder_name(map_name, prefix, table):
    """The subfolder of the output folder that a map's clips go into.

    It is the map's prefix unless `map_folders` names another folder, which
    lets an existing folder keep its name (`de_dust_2`) while the clips get a
    nicer prefix (`Dust2`).
    """
    name = str(map_name or "").strip()
    for key, value in (table or {}).items():
        if str(key).lower() == name.lower() and str(value).strip():
            return sanitize_label(value, prefix)
    return prefix


def random_suffix(rng=random):
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(SUFFIX_LENGTH))


def new_clip_path(folder, prefix, label, extension, rng=random):
    """A `Map-Player-random.ext` path in `folder` that does not exist yet."""
    folder = Path(folder)
    for _ in range(1000):
        candidate = folder / ("%s-%s-%s.%s" % (prefix, label, random_suffix(rng), extension))
        if not candidate.exists():
            return candidate
    raise ClipError("Could not find a free file name in %s." % folder)


# --------------------------------------------------------------------------
# Demos and the list of finished ones
# --------------------------------------------------------------------------

def find_demos(folder):
    """The .dem files directly inside `folder`, newest first."""
    demos = [path for path in Path(folder).iterdir() if path.is_file() and path.suffix.lower() == ".dem"]
    demos.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return demos


def demo_key(path):
    """Identify a demo by name and size, which is cheap and stable enough."""
    path = Path(path)
    return "%s|%d" % (path.name, path.stat().st_size)


def load_state(path):
    path = Path(path)
    if not path.is_file():
        return {"version": 1, "demos": {}}
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except json.JSONDecodeError as error:
        raise ClipError(
            "%s is damaged (%s). Fix or delete it. Deleting it makes every demo count as new."
            % (path, error)
        )
    if not isinstance(state, dict) or not isinstance(state.get("demos"), dict):
        raise ClipError("%s does not look like a state file. Fix or delete it." % path)
    return state


def save_state(path, state):
    """Write the state file through a temporary file, so it is never half written."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


def done_entry(state, key, steam_id):
    return state["demos"].get(key, {}).get("players", {}).get(steam_id)


def record_done(state, demo, steam_id, map_name, clip_names, note="", folder="", sources=()):
    """Remember that a demo is finished for a player.

    `sources` holds the name CSDM gave each clip, in the same order as
    `clips`. That name carries the sequence number and the tick range, which
    the random clip name does not.
    """
    entry = state["demos"].setdefault(demo_key(demo), {"file": Path(demo).name, "players": {}})
    entry["map"] = map_name
    entry.setdefault("players", {})[steam_id] = {
        "clips": list(clip_names),
        "sources": list(sources),
        "folder": folder,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    }


# --------------------------------------------------------------------------
# Calling CSDM
# --------------------------------------------------------------------------

def resolve_csdm(csdm_path):
    """Find the csdm launcher. On Windows it is a `csdm.cmd` file."""
    found = shutil.which(str(csdm_path))
    if found:
        return found
    raise ClipError(
        "Could not find the CS Demo Manager command line (%s).\n"
        "Install CS Demo Manager, or set csdm_path in config.json to the full path of\n"
        "csdm.cmd in its install folder." % csdm_path
    )


def quote_for_windows(argument):
    """Quote one argument so it survives both cmd.exe and the program's own parsing."""
    if '"' in argument:
        raise ClipError("A double quote cannot be passed to CSDM: %s" % argument)
    if "%" in argument:
        raise ClipError(
            "A %% character cannot be passed to CSDM safely, because Windows would treat\n"
            "it as a variable: %s\nRename the folder or file." % argument
        )
    trailing_backslashes = len(argument) - len(argument.rstrip("\\"))
    return '"' + argument + "\\" * trailing_backslashes + '"'


def build_command(csdm, arguments, windows=None):
    """Return what to hand to subprocess: a list, or one string for a .cmd file.

    A batch file cannot be started directly with a list of arguments without
    Windows re-reading characters such as & in a path. Running it through
    `cmd /s /c "..."` with every argument quoted is the well-defined way.
    """
    if windows is None:
        windows = os.name == "nt"
    arguments = [str(argument) for argument in arguments]
    if windows and str(csdm).lower().endswith((".cmd", ".bat")):
        inner = " ".join(quote_for_windows(part) for part in [str(csdm)] + arguments)
        shell = os.environ.get("ComSpec") or "cmd.exe"
        return '"%s" /d /s /c "%s"' % (shell, inner)
    return [str(csdm)] + arguments


def run_csdm(csdm, arguments, dry_run=False):
    """Run one csdm command with its output going straight to the console."""
    print("> csdm " + " ".join(str(argument) for argument in arguments), flush=True)
    if dry_run:
        return 0
    try:
        completed = subprocess.run(build_command(csdm, arguments))
    except OSError as error:
        raise ClipError("Could not start csdm: %s" % error)
    return completed.returncode


def video_arguments(demo, steam_id, config, output_folder, before, after):
    """The `csdm video` arguments for one player's kills in one demo.

    Every encode and output option is pinned, because CSDM fills anything left
    out from the settings saved in its own window. X-ray and player voices are
    pinned off. The other look settings are inherited on purpose.
    """
    video = config["video"]
    return [
        "video", str(demo),
        "--mode", "player",
        "--steamids", steam_id,
        "--event", "kills",
        "--start-seconds-before", "%g" % before,
        "--end-seconds-after", "%g" % after,
        "--no-show-x-ray",
        "--no-player-voices",
        "--recording-system", video["recording_system"],
        "--recording-output", "video",
        "--encoder-software", "FFmpeg",
        "--width", str(video["width"]),
        "--height", str(video["height"]),
        "--framerate", str(video["framerate"]),
        "--ffmpeg-video-container", video["container"],
        "--ffmpeg-video-codec", video["video_codec"],
        "--ffmpeg-audio-codec", video["audio_codec"],
        "--ffmpeg-audio-bitrate", str(video["audio_bitrate"]),
        "--ffmpeg-crf", str(video["crf"]),
        "--no-concatenate-sequences",
        "--close-game-after-recording",
        "--output", str(output_folder),
    ]


def export_match(csdm, demo, json_folder):
    """Analyze the demo in CSDM and return its exported match data.

    `csdm analyze` exits with 0 even when the analysis failed, so success is
    judged by whether the export produced a file. The export is named after
    the match, not the demo file, so the folder is searched.
    """
    json_folder = Path(json_folder)
    json_folder.mkdir(parents=True, exist_ok=True)
    run_csdm(csdm, ["analyze", str(demo)])
    run_csdm(csdm, ["json", str(demo), "--output-folder", str(json_folder)])
    exports = sorted(json_folder.glob("*.json"))
    if len(exports) != 1:
        raise ClipError(
            "CSDM did not export the match data for %s (found %d files).\n"
            "Read its output above. The PostgreSQL service must be running."
            % (demo, len(exports))
        )
    try:
        with open(exports[0], encoding="utf-8") as handle:
            match = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ClipError("Could not read CSDM's export %s: %s" % (exports[0], error))
    if not isinstance(match, dict):
        raise ClipError("CSDM's export %s is not in the expected format." % exports[0])
    return match


def player_name(match, steam_id):
    for player in match.get("players") or []:
        if player.get("steamId") == steam_id:
            return player.get("name")
    return None


def player_in_match(match, steam_id):
    return any(player.get("steamId") == steam_id for player in match.get("players") or [])


def kill_count(match, steam_id):
    return sum(1 for kill in match.get("kills") or [] if kill.get("killerSteamId") == steam_id)


def clip_sort_key(path):
    found = re.search(r"sequence-(\d+)-", path.name)
    return (int(found.group(1)) if found else 10 ** 9, path.name)


# While recording, CSDM keeps each sequence's raw files in a folder such as
# `3-sequence`, and deletes them once the clip is finished.
RAW_SEQUENCE_FOLDER = re.compile(r"^\d+-sequence$")


def collect_clips(folder, container):
    """Every finished clip under `folder`, in sequence order.

    CSDM v3.20.1 writes the clips directly into the folder. Its unreleased
    code nests them in a video-ID subfolder, so the search is recursive.
    Anything still inside a raw `<n>-sequence` folder is a leftover that CSDM
    failed to clean up, not a clip, and is ignored.
    """
    folder = Path(folder)
    clips = []
    for path in folder.rglob("*.%s" % container):
        parents = path.relative_to(folder).parts[:-1]
        if path.is_file() and not any(RAW_SEQUENCE_FOLDER.match(part) for part in parents):
            clips.append(path)
    return sorted(clips, key=clip_sort_key)


# --------------------------------------------------------------------------
# Clipping one demo
# --------------------------------------------------------------------------

def work_base(config):
    """Where CSDM records before the clips are renamed.

    `work_folder` if it is set, otherwise `_work` inside the output folder.
    """
    if config.get("work_folder"):
        return Path(os.path.abspath(str(config["work_folder"])))
    return Path(os.path.abspath(str(config["output_folder"]))) / WORK_FOLDER_NAME


def new_work_folder(config, rng=random):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = work_base(config) / ("%s-%s" % (stamp, random_suffix(rng)[:4]))
    folder.mkdir(parents=True)
    return folder


def remove_work_folder(folder):
    shutil.rmtree(folder, ignore_errors=True)
    try:
        Path(folder).parent.rmdir()
    except OSError:
        pass


def clip_demo(demo, steam_id, config, state, state_path, csdm, before, after, rng=random):
    """Record, rename and move the clips for one player in one demo.

    Returns the list of finished clip paths. The demo is recorded as done when
    this returns, including when the player has no kills in it.
    """
    # Absolute paths, because CSDM is a separate program with its own idea of
    # the current folder.
    demo = Path(os.path.abspath(str(demo)))
    output_folder = Path(os.path.abspath(str(config["output_folder"])))
    work = new_work_folder(config, rng)
    video_folder = work / "video"
    video_folder.mkdir()

    print("\n== %s" % demo.name, flush=True)
    try:
        match = export_match(csdm, demo, work / "json")
    except ClipError:
        # Nothing was recorded yet, so there is nothing worth keeping.
        remove_work_folder(work)
        raise
    map_name = match.get("mapName") or "unknown"

    if not player_in_match(match, steam_id):
        print("Player %s is not in this demo. Marked as done with no clips." % steam_id)
        record_done(state, demo, steam_id, map_name, [], "player not in demo")
        save_state(state_path, state)
        remove_work_folder(work)
        return []

    kills = kill_count(match, steam_id)
    label = sanitize_label(
        config["player_labels"].get(steam_id) or player_name(match, steam_id), steam_id
    )
    if kills == 0:
        print("%s has no kills in this demo. Marked as done with no clips." % label)
        record_done(state, demo, steam_id, map_name, [], "no kills")
        save_state(state_path, state)
        remove_work_folder(work)
        return []

    prefix, from_table = map_prefix(map_name, config["map_prefixes"])
    if not from_table:
        print(
            'Map %s is not in map_prefixes, so the clips are named "%s". '
            "Add it to config.json to choose the name." % (map_name, prefix)
        )
    print(
        "%s on %s: %d kills. Recording now. CS2 will open and the PC is busy until it closes."
        % (label, map_name, kills),
        flush=True,
    )

    code = run_csdm(csdm, video_arguments(demo, steam_id, config, video_folder, before, after))
    if code != 0:
        raise ClipError(
            "CSDM stopped with exit code %d, so nothing was renamed.\n"
            "If the game failed to start, update HLAE from CSDM's video screen and try again.\n"
            "Anything it wrote is in %s" % (code, work)
        )

    container = config["video"]["container"]
    clips = collect_clips(video_folder, container)
    if not clips:
        raise ClipError(
            "CSDM reported success but left no .%s files in %s." % (container, video_folder)
        )

    # Only now, with clips in hand, is the map folder created.
    target_folder = output_folder
    if config["map_subfolders"]:
        target_folder = output_folder / map_folder_name(map_name, prefix, config["map_folders"])
        if not target_folder.is_dir():
            target_folder.mkdir(parents=True)
            print("Created the map folder %s" % target_folder)

    finished = []
    sources = []
    for clip in clips:
        target = new_clip_path(target_folder, prefix, label, container, rng)
        shutil.move(str(clip), str(target))
        finished.append(target)
        sources.append(clip.name)
        print("  %s  <-  %s" % (target.name, clip.name))

    folder = target_folder.name if config["map_subfolders"] else ""
    record_done(
        state, demo, steam_id, map_name, [path.name for path in finished], folder=folder, sources=sources
    )
    print("%d clip(s) are in %s" % (len(finished), target_folder))
    save_state(state_path, state)
    remove_work_folder(work)
    return finished


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def require_valid(config):
    problems = config_problems(config)
    if problems:
        raise ClipError(
            "The configuration has %d problem(s):\n- %s\nRun `python clipenc.py check` for the full picture."
            % (len(problems), "\n- ".join(problems))
        )


def player_label_for_display(config, steam_id):
    labels = config.get("player_labels")
    if isinstance(labels, dict) and labels.get(steam_id):
        return labels[steam_id]
    return steam_id or "(not set)"


def command_check(arguments):
    config = load_config(arguments.config)
    problems = config_problems(config)
    video = config["video"]

    print("clipenc %s (written against CS Demo Manager %s)" % (VERSION, TESTED_CSDM_VERSION))
    print("Config:        %s" % Path(arguments.config).resolve())
    try:
        print("csdm:          %s" % resolve_csdm(config["csdm_path"]))
    except ClipError as error:
        print("csdm:          NOT FOUND")
        problems.append(str(error).splitlines()[0])
    print("Demo folder:   %s" % (config["demo_folder"] or "(not set)"))
    print("Output folder: %s" % (config["output_folder"] or "(not set)"))
    print(
        "Player:        %s (named %s in clips)"
        % (config.get("steam_id") or "(not set)", player_label_for_display(config, config.get("steam_id")))
    )
    print("Work folder:   %s" % (work_base(config) if config["output_folder"] or config.get("work_folder") else "(not set)"))
    print(
        "Map folders:   %s"
        % ("one subfolder per map in the output folder" if config.get("map_subfolders") is True else "off, all clips in the output folder")
    )
    print("Padding:       %s s before, %s s after" % (config["seconds_before"], config["seconds_after"]))
    print(
        "Encode:        %s CRF %s, %sx%s, %s fps, %s, %s %s kbps, %s"
        % (
            video.get("video_codec"), video.get("crf"), video.get("width"), video.get("height"),
            video.get("framerate"), video.get("container"), video.get("audio_codec"),
            video.get("audio_bitrate"), video.get("recording_system"),
        )
    )

    if config["demo_folder"] and Path(config["demo_folder"]).is_dir() and is_steam_id(config.get("steam_id")):
        state = load_state(arguments.state)
        demos = find_demos(config["demo_folder"])
        waiting = [demo for demo in demos if done_entry(state, demo_key(demo), config["steam_id"]) is None]
        print("Demos:         %d found, %d not clipped yet" % (len(demos), len(waiting)))

    for key in ("demo_folder", "output_folder"):
        value = config.get(key)
        if isinstance(value, str) and any(ord(char) > 127 for char in value):
            print(
                "\nNote: %s has characters outside the basic Latin alphabet. CSDM checks demo\n"
                "paths for those and may refuse them. If a run fails, try a plain folder name." % key
            )

    if problems:
        print("\n%d problem(s):" % len(problems))
        for problem in problems:
            print("- %s" % problem)
        return 1
    print("\nEverything looks ready.")
    return 0


def command_list(arguments):
    config = load_config(arguments.config)
    require_valid(config)
    state = load_state(arguments.state)
    steam_id = arguments.player or config["steam_id"]
    demos = find_demos(config["demo_folder"])
    if not demos:
        print("No .dem files in %s" % config["demo_folder"])
        return 0

    print("Demos in %s for %s, newest first:" % (config["demo_folder"], player_label_for_display(config, steam_id)))
    waiting = 0
    for demo in demos:
        entry = done_entry(state, demo_key(demo), steam_id)
        when = datetime.fromtimestamp(demo.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if entry is None:
            status = "new"
            waiting += 1
        elif entry.get("clips"):
            status = "done, %d clips" % len(entry["clips"])
        else:
            status = "done, no clips (%s)" % (entry.get("note") or "nothing to clip")
        print("  %s  %-22s  %s" % (when, status, demo.name))
    print("%d demo(s), %d not clipped yet." % (len(demos), waiting))
    return 0


def command_clip(arguments):
    config = load_config(arguments.config)
    require_valid(config)
    steam_id = arguments.player or config["steam_id"]
    if not is_steam_id(steam_id):
        raise ClipError("--player must be a SteamID64: 17 digits.")
    before, after = check_padding(
        config["seconds_before"] if arguments.before is None else arguments.before,
        config["seconds_after"] if arguments.after is None else arguments.after,
    )
    state = load_state(arguments.state)

    if arguments.demo:
        demo = Path(arguments.demo)
        if not demo.is_file() or demo.suffix.lower() != ".dem":
            raise ClipError("Not a .dem file: %s" % demo)
        if done_entry(state, demo_key(demo), steam_id) is not None and not arguments.force:
            print("%s is already clipped for this player. Add --force to clip it again." % demo.name)
            return 0
        chosen, waiting_after = [demo], 0
    else:
        demos = find_demos(config["demo_folder"])
        candidates = [
            demo for demo in demos
            if arguments.force or done_entry(state, demo_key(demo), steam_id) is None
        ]
        if not candidates:
            print("Nothing to clip: all %d demo(s) in %s are done." % (len(demos), config["demo_folder"]))
            return 0
        limit = len(candidates) if arguments.all else arguments.limit
        if limit < 1:
            raise ClipError("--limit must be 1 or more.")
        chosen = candidates[:limit]
        waiting_after = len(candidates) - len(chosen)

    if arguments.dry_run:
        print("Dry run. These commands would be run, in a new work folder per demo:")
        for demo in chosen:
            run_csdm("csdm", ["analyze", str(demo)], dry_run=True)
            run_csdm("csdm", ["json", str(demo), "--output-folder", "<work>\\json"], dry_run=True)
            run_csdm(
                "csdm",
                video_arguments(demo, steam_id, config, "<work>\\video", before, after),
                dry_run=True,
            )
        return 0

    csdm = resolve_csdm(config["csdm_path"])
    total = 0
    for demo in chosen:
        total += len(clip_demo(demo, steam_id, config, state, arguments.state, csdm, before, after))

    print("\nDone: %d clip(s) from %d demo(s), under %s" % (total, len(chosen), config["output_folder"]))
    if waiting_after:
        print(
            "%d more demo(s) are waiting. Use --limit N or --all to clip more in one go."
            % waiting_after
        )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="clipenc",
        description="Record a player's kills from CS2 demos through CS Demo Manager and "
        "name the clips Map-Player-random.",
    )
    parser.add_argument("--version", action="version", version="clipenc %s" % VERSION)
    parser.add_argument(
        "--config", default=str(APP_DIR / CONFIG_NAME), help="path to config.json (default: next to clipenc.py)"
    )
    parser.add_argument(
        "--state", default=str(APP_DIR / STATE_NAME), help="path to the list of finished demos (default: next to clipenc.py)"
    )
    commands = parser.add_subparsers(dest="command")

    check = commands.add_parser("check", help="show the configuration and what is wrong with it")
    check.set_defaults(run=command_check)

    listing = commands.add_parser("list", help="show the demos and which ones are already clipped")
    listing.add_argument("--player", help="SteamID64 to list for (default: steam_id from the config)")
    listing.set_defaults(run=command_list)

    clip = commands.add_parser("clip", help="clip the newest demo that is not clipped yet")
    clip.add_argument("--demo", help="clip this .dem file instead of picking from the demo folder")
    clip.add_argument("--limit", type=int, default=1, help="how many new demos to clip, newest first (default: 1)")
    clip.add_argument("--all", action="store_true", help="clip every demo that is not clipped yet")
    clip.add_argument("--force", action="store_true", help="clip demos again even if they are already done")
    clip.add_argument("--player", help="SteamID64 whose kills to clip (default: steam_id from the config)")
    clip.add_argument("--before", type=float, help="seconds before a kill, 0 to %d (default: from the config)" % MAX_SECONDS_BEFORE)
    clip.add_argument("--after", type=float, help="seconds after a kill, 0 to %d (default: from the config)" % MAX_SECONDS_AFTER)
    clip.add_argument("--dry-run", action="store_true", help="show the csdm commands without running anything")
    clip.set_defaults(run=command_clip)
    return parser


def main(argv=None):
    # Player names can hold characters the Windows console cannot show.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not getattr(arguments, "run", None):
        parser.print_help()
        return 1
    try:
        return arguments.run(arguments)
    except ClipError as error:
        print("Error: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. A demo that was being recorded is not marked as done.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
