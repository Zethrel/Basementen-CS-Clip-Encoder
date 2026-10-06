# Basementen CS Clip Encoder

One step from a CS2 demo to finished kill clips named `Map-Player-random.mkv`,
for example `Overpass-Zethrel-hfowpifb.mkv`. It has a window and a command line.

It drives the command line of [CS Demo Manager](https://cs-demo-manager.com) (CSDM),
which launches CS2, records one player's kills and encodes them. This tool then
renames the clips and sorts them into one folder per map. It records and
encodes nothing itself, and no part of CSDM is included here.

- `clipenc.py` does the work and is the command line
- `clipgui.py` is the window on top of it
- Python standard library only
- Windows only, English only

## Needs

- Python 3.10 or later (tested on 3.10 and 3.13)
- CS Demo Manager 3.20.1, with its PostgreSQL service running
- Steam running, CS2 installed, and CS2 closed before a run

## Setup

1. Copy `config.example.json` to `config.json` and fill it in. The keys are
   explained below.
2. Check it:

   ```
   python clipenc.py check
   ```

   It prints the settings and lists anything that is wrong.

## The window

Build it once, and again after every change to the code:

```
build-exe.cmd
```

That makes `CSClipEncoder.exe` next to `config.json`, which is where it reads
its settings from. It needs PyInstaller, for building only:
`python -m pip install pyinstaller`. Without building, `python clipgui.py`
opens the same window.

- **Clip my new demos** clips your own kills from the newest demo that is not
  clipped yet, or from the newest three, or from all of them.
- **Click a demo** to see who played in it. CSDM reads the demo for this,
  which takes a few seconds the first time and does not start CS2. After that
  the players are remembered.
- **Tick players and press Clip.** Teammates and opponents can both be
  chosen. CS2 opens once for each player. A player without a kill cannot be
  ticked.
- **Stop run** closes CS2 at once. The demo that was being recorded stays
  unclipped, and its half-finished files are deleted. Demos that were already
  finished in the same run keep their clips.
- **Refresh** reads `config.json` and the demo folder again, for a demo that
  arrived or a setting that changed while the window was open.
- Settings are still edited in `config.json`. The window opens the file for
  you and has no settings screen of its own.

## The command line

```
python clipenc.py list
python clipenc.py clip
```

`list` shows the demos in your demo folder and which are already clipped.
`clip` takes the newest demo that is not clipped yet. CS2 opens, plays the demo
and closes again, and the PC is busy until it does.

| Command | What it does |
| --- | --- |
| `clip` | Clip the newest new demo |
| `clip --limit 3` | Clip the three newest new demos |
| `clip --all` | Clip every new demo |
| `clip --demo "D:\path\match.dem"` | Clip one named demo |
| `clip --force` | Clip again, even if the demo is already done |
| `clip --before 6 --after 4` | Change the padding for this run |
| `clip --player 76561190000000002` | Clip another player's kills, by their SteamID64 (this one is made up) |
| `clip --dry-run` | Show the CSDM commands without running anything |

## What you get

- **Clips:** kills within 10 seconds of each other share a clip. That is
  CSDM's grouping, kept on purpose so a multi-kill stays in one piece.
- **Names:** `Map-Player-random.mkv`, with 8 random lowercase letters.
  - The map part comes from `map_prefixes`. A map that is not listed gets a
    name derived from the map itself (`de_mirage` becomes `Mirage`).
  - The player part comes from `player_labels`, or else from the name in the
    demo. Characters Windows does not allow in file names are removed, so it
    can differ slightly from the name shown in game.
- **Folders:** each map's clips go into its own subfolder of the output
  folder, for example `MKV Final\Overpass`. A folder that does not exist yet
  is created when the first clip for that map is ready. Files already in a
  folder are left alone. The folder is named like the map part of the clip
  name, unless `map_folders` says otherwise.
- **Look:** X-ray and player voices are always off. HUD, assists, death notice
  duration and TrueView come from the settings saved in CSDM's own window.
- **Encode:** set in `video`. Every encode option is passed to CSDM on each
  run, so its saved encode settings are not used.

## config.json

| Key | Meaning |
| --- | --- |
| `csdm_path` | `csdm` if it is on your PATH, otherwise the full path to `csdm.cmd` in CSDM's install folder |
| `demo_folder` | Where your `.dem` files are. Steam's `replays` folder by default |
| `output_folder` | Where finished clips go. It must exist |
| `work_folder` | Where CSDM records before the clips are renamed. `""` means `_work` inside the output folder |
| `steam_id` | Your SteamID64, 17 digits, in quotes |
| `player_labels` | SteamID64 to the name used in clip names. Optional |
| `map_prefixes` | Map to the name used in clip names. Optional |
| `map_subfolders` | `true` sorts clips into one subfolder per map. `false` puts them all in the output folder |
| `map_folders` | Map to its subfolder name, for a folder that is not named like the prefix. Optional |
| `seconds_before` | Seconds before a kill, 0 to 10 |
| `seconds_after` | Seconds after a kill, 0 to 8 |
| `video` | Resolution, frame rate, container, codecs, audio bitrate, CRF and recording system (`HLAE` or `CS`) |

A file name or folder with a `%` in it cannot be passed to CSDM, and the tool
says so instead of trying.

## Switching to AV1

x265 at CRF 28 is the setting in use. AV1 was tested on 6 October 2026 and
works, so it stays available. To switch, change two values under `video` in
`config.json`:

```
"video_codec": "libsvtav1",
"crf": 35
```

To switch back, set them to `"libx265"` and `28`.

- **Tested:** SVT-AV1 at CRF 35 through CSDM 3.20.1. The clips import into
  DaVinci Resolve and play smoothly with sound.
- **File size:** at CRF 35 the AV1 clips were 65% larger than x265 at CRF 28,
  with no visible difference between the two. A higher CRF gives smaller
  files. The value that matches x265's size was not tested, and CSDM accepts
  51 at most.
- **Where it runs:** on the CPU, like x265. An RTX 30-series card can play
  AV1 but cannot encode it.
- **Existing clips:** nothing is re-encoded. A library can hold both kinds.

## Files it writes

- `state.json`, next to `clipenc.py` or the `.exe`: the demos already clipped,
  per player, and the map and players of every demo CSDM has read for you. It
  holds other players' names and Steam IDs, so it stays out of git. Delete it
  to make every demo count as new.
- The work folder (`work_folder`, or `_work` inside the output folder): where
  CSDM records before the clips are renamed. Each run gets its own subfolder.
  It is removed after a successful run and kept after a failed one, so you
  can look at what CSDM left. Delete it by hand afterwards.

## When a run fails

- **The game fails to start:** update HLAE from CSDM's video screen. A CS2
  update can break HLAE until HLAE catches up.
- **CSDM did not export the match data:** the PostgreSQL service is probably
  not running.
- **Steam is not running:** CSDM needs it to launch CS2.
- A failed demo is not marked as done, so the next `clip` tries it again.
- If you stop a command line run with Ctrl+C, CS2 may stay open. Close it
  yourself. The window's Stop button closes it for you.

## Not included yet

- X-ray on kills through smoke or a wall.
- A settings screen. Settings are edited in `config.json`.

## Tests

```
python -m unittest -v
```

The tests never start CSDM or CS2, and never close a running game. They use a
small stand-in `csdm` that writes the same kind of files. On Windows the
stand-in is a `csdm.cmd` file, like the real launcher. The window tests open
the real window for a moment and drive it against the same stand-in.

## Licence

All rights reserved. See `LICENSE`.
