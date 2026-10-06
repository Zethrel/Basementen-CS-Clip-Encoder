#!/usr/bin/env python3
# Copyright (c) 2026 Zethrel. All rights reserved. See LICENSE.
"""CS clip encoder, the window.

A tkinter front end for clipenc.py: pick demos and players, start a run, and
watch CS Demo Manager work. It adds no behaviour of its own. Every recording
goes through the same code as `python clipenc.py clip`.

    python clipgui.py

Settings stay in config.json. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter import ttk

import clipenc

# The Basementen look: warm near-black, off-white text, and the logo yellow
# used as a fill with black on top. The same values as the pricecheck window.
THEME = {
    "plane": "#0e0e0c",      # the window behind everything
    "surface": "#181713",    # panels
    "ink": "#f2f0e6",
    "ink_2": "#b8b5a5",
    "muted": "#8d8a7c",
    "grid": "#2a2a22",       # hairlines and borders
    "axis": "#3a3a30",       # a shade up from grid: scrollbars, quiet buttons
    "accent": "#f9ec07",     # the logo yellow
    "accent_hi": "#fff37a",  # the same yellow under the pointer
    "accent_ink": "#0e0e0c", # what goes on top of the yellow. Black, always.
    "danger": "#f0946f",
}

POLL_MS = 80
LOG_LINES = 2000        # the log keeps this many lines and drops the oldest
ROW_HEIGHT = 30
WINDOW_SIZE = "1120x720"
STEPS = (
    ("analyze", "Read by CSDM"),
    ("record", "Recording in CS2"),
    ("sort", "Rename and sort into the map folder"),
)


# --------------------------------------------------------------------------
# What the window shows. Plain functions, so they can be tested without one.
# --------------------------------------------------------------------------

def plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


CONTROL_CODES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean_line(text):
    """A line a program wrote for a console, as plain text.

    Colour codes are dropped, and of a line redrawn in place (a progress
    counter) only the last version is kept.
    """
    return CONTROL_CODES.sub("", str(text)).rstrip("\r").rsplit("\r", 1)[-1]


def one_paragraph(text):
    """An engine message as running text. They are broken into lines for a console."""
    return " ".join(str(text).split())


def end_text(result):
    """The line that sums up a run: (text, whether it is an error)."""
    recordings = plural(result["finished"], "recording")
    clips = plural(result["clips"], "clip")
    if result["error"]:
        return "Stopped on an error after %s. See the last lines below." % recordings, True
    if result["stopped"] and result["finished"] == 0:
        return "Stopped by you. Nothing was saved.", False
    if result["stopped"]:
        return "Stopped by you after %s, %s saved." % (recordings, clips), False
    return "Done: %s from %s." % (clips, recordings), False


def clips_text(done):
    """How a finished (demo, player) pair reads in a list."""
    if done is None:
        return "New"
    if done.get("clips"):
        return plural(len(done["clips"]), "clip")
    if done.get("note") == "player not in demo":
        return "Not in demo"
    return "No kills"


def demo_rows(demos, state, config):
    """One row per demo, in the order given: what the demo list shows."""
    rows = []
    for demo in demos:
        key = clipenc.demo_key(demo)
        entry = state["demos"].get(key, {})
        done = clipenc.done_entry(state, key, config["steam_id"])
        map_name = entry.get("map")
        rows.append({
            "key": key,
            "path": Path(demo),
            "played": datetime.fromtimestamp(Path(demo).stat().st_mtime).strftime("%d %b %H:%M"),
            "map": clipenc.map_prefix(map_name, config["map_prefixes"])[0] if map_name else "Not analyzed yet",
            "clips": clips_text(done),
            "new": done is None,
        })
    return rows


def player_rows(info, state, key, config):
    """The player picker for one demo: groups of rows, your own team first.

    Returns [(heading, [row, ...]), ...]. Within a group the most kills come
    first, because those are the players worth clipping.
    """
    own = config["steam_id"]
    labels = config.get("player_labels") or {}
    rows = []
    for player in info.get("players") or []:
        steam_id = player["steam_id"]
        done = clipenc.done_entry(state, key, steam_id)
        rows.append({
            "steam_id": steam_id,
            "name": labels.get(steam_id) or player.get("name") or steam_id,
            "team": player.get("team") or "",
            "kills": int(player.get("kills") or 0),
            "status": "Not clipped" if done is None else clips_text(done),
            "done": done is not None,
        })
        if rows[-1]["kills"] == 0 and done is None:
            rows[-1]["status"] = "No kills"
    rows.sort(key=lambda row: (-row["kills"], row["name"].lower()))

    own_team = next((row["team"] for row in rows if row["steam_id"] == own), None)
    if own_team is not None:
        return [
            ("Your team", [row for row in rows if row["team"] == own_team]),
            ("Other team", [row for row in rows if row["team"] != own_team]),
        ]
    teams = sorted({row["team"] for row in rows})
    return [(team or "Players", [row for row in rows if row["team"] == team]) for team in teams]


def waiting_demos(demos, state, steam_id):
    """The demos not clipped for this player yet, in the order given."""
    return [demo for demo in demos if clipenc.done_entry(state, clipenc.demo_key(demo), steam_id) is None]


def count_choices(waiting):
    """The choices in the "My new demos" picker for a number of waiting demos."""
    choices = []
    if waiting >= 1:
        choices.append(("Newest 1", 1))
    if waiting >= 4:
        choices.append(("Newest 3", 3))
    if waiting >= 2:
        choices.append(("All %d" % waiting, waiting))
    return choices


def last_run_text(state, config):
    """One line about the most recent finished clip job, or an empty string."""
    latest = None
    for entry in state["demos"].values():
        for steam_id, done in (entry.get("players") or {}).items():
            if latest is None or str(done.get("finished") or "") > str(latest[2].get("finished") or ""):
                latest = (entry, steam_id, done)
    if latest is None:
        return ""
    entry, steam_id, done = latest
    try:
        when = datetime.fromisoformat(done["finished"]).astimezone().strftime("%d %b %H:%M")
    except (KeyError, ValueError):
        when = "unknown time"
    who = (config.get("player_labels") or {}).get(steam_id)
    if not who:
        info = entry.get("info") or {}
        who = next((p.get("name") for p in info.get("players") or [] if p.get("steam_id") == steam_id), None) or steam_id
    where = (" in %s" % done["folder"]) if done.get("folder") else ""
    return "Last run %s: %s for %s%s." % (when, clips_text(done).lower(), who, where)


def setup_problems(config_path):
    """Load the configuration. Returns (config or None, csdm path or None, problems)."""
    try:
        config = clipenc.load_config(config_path)
    except clipenc.ClipError as error:
        return None, None, [str(error)]
    problems = clipenc.config_problems(config)
    csdm = None
    try:
        csdm = clipenc.resolve_csdm(config["csdm_path"])
    except clipenc.ClipError as error:
        problems.append(str(error))
    return config, csdm, problems


def resource_path(name):
    """A file from the assets folder, also when running as a built .exe."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / "assets" / name


def build_stamp():
    """When this .exe was built, for the title bar. Empty when run from source."""
    if not getattr(sys, "frozen", False):
        return ""
    try:
        return time.strftime("%d %b %H:%M", time.localtime(os.path.getmtime(sys.executable)))
    except OSError:
        return ""


def open_path(path, editor=False):
    """Open a folder, or a file the way a double click would.

    With `editor` the file opens in Notepad on Windows. A .json file often has
    no program tied to it, and a double click would then open nothing.
    """
    try:
        if os.name == "nt" and editor and Path(path).is_file():
            subprocess.Popen(["notepad.exe", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # noqa: only exists on Windows
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        messagebox.showerror("CS Clip Encoder", "Could not open %s\n\n%s" % (path, error))


# --------------------------------------------------------------------------
# Look
# --------------------------------------------------------------------------

def pick_font(*candidates):
    """The first font family that exists on this machine."""
    available = {name.lower() for name in tkfont.families()}
    for name in candidates:
        if name.lower() in available:
            return name
    return candidates[-1]


def style_window(root):
    """Dark theme over ttk's `clam`, the only stock theme that lets every colour be set."""
    root.configure(bg=THEME["plane"])
    family = pick_font("Segoe UI", "Inter", "DejaVu Sans", "Helvetica")
    mono = pick_font("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New")
    fonts = {
        "h1": tkfont.Font(family=family, size=15, weight="bold"),
        "h2": tkfont.Font(family=family, size=11, weight="bold"),
        "body": tkfont.Font(family=family, size=10),
        "bold": tkfont.Font(family=family, size=10, weight="bold"),
        "head": tkfont.Font(family=family, size=8, weight="bold"),
        "meta": tkfont.Font(family=family, size=9),
        "mono": tkfont.Font(family=mono, size=9),
    }
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        return fonts

    plane, surface = THEME["plane"], THEME["surface"]
    style.configure(".", background=plane, foreground=THEME["ink"], fieldbackground=surface,
                    borderwidth=0, font=fonts["body"])
    style.configure("TFrame", background=plane)
    style.configure("Panel.TFrame", background=surface)

    style.configure("TLabel", background=plane, foreground=THEME["ink"])
    style.configure("H1.TLabel", font=fonts["h1"])
    style.configure("Sub.TLabel", foreground=THEME["ink_2"], font=fonts["meta"])
    for name, colour, font in (
        ("Panel.TLabel", THEME["ink"], fonts["body"]),
        ("PanelBold.TLabel", THEME["ink"], fonts["bold"]),
        ("PanelSoft.TLabel", THEME["ink_2"], fonts["body"]),
        ("PanelMuted.TLabel", THEME["muted"], fonts["meta"]),
        ("PanelHead.TLabel", THEME["muted"], fonts["head"]),
        ("PanelAccent.TLabel", THEME["accent"], fonts["bold"]),
        ("PanelDanger.TLabel", THEME["danger"], fonts["body"]),
        ("H2.TLabel", THEME["ink"], fonts["h2"]),
    ):
        style.configure(name, background=surface, foreground=colour, font=font)

    # The combobox popup is a plain Tk listbox, so no ttk style reaches it.
    root.option_add("*TCombobox*Listbox.background", surface)
    root.option_add("*TCombobox*Listbox.foreground", THEME["ink"])
    root.option_add("*TCombobox*Listbox.selectBackground", THEME["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", THEME["accent_ink"])
    root.option_add("*TCombobox*Listbox.font", fonts["body"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    style.configure("TCombobox", fieldbackground=surface, background=surface, foreground=THEME["ink"],
                    arrowcolor=THEME["ink_2"], bordercolor=THEME["axis"], lightcolor=THEME["axis"],
                    darkcolor=THEME["axis"], padding=7)
    style.map("TCombobox",
              fieldbackground=[("disabled", plane), ("readonly", surface)],
              foreground=[("disabled", THEME["muted"]), ("readonly", THEME["ink"])],
              bordercolor=[("focus", THEME["accent"])],
              arrowcolor=[("disabled", THEME["axis"]), ("active", THEME["accent"])])

    def button(name, fill, text, border, hover_fill, hover_text):
        style.configure(name, background=fill, foreground=text, bordercolor=border, lightcolor=fill,
                        darkcolor=fill, focuscolor=fill, padding=(16, 8), font=fonts["bold"], relief="raised",
                        borderwidth=1)      # clam draws no border at all on a flat button
        style.map(name,
                  background=[("disabled", THEME["grid"]), ("active", hover_fill)],
                  foreground=[("disabled", THEME["muted"]), ("active", hover_text)],
                  bordercolor=[("disabled", THEME["grid"]), ("active", hover_fill)],
                  lightcolor=[("disabled", THEME["grid"]), ("active", hover_fill)],
                  darkcolor=[("disabled", THEME["grid"]), ("active", hover_fill)])

    # One filled yellow button per window: the everyday action.
    button("Primary.TButton", THEME["accent"], THEME["accent_ink"], THEME["accent"],
           THEME["accent_hi"], THEME["accent_ink"])
    button("Outline.TButton", surface, THEME["accent"], THEME["accent"], THEME["accent"], THEME["accent_ink"])
    button("Quiet.TButton", surface, THEME["ink"], THEME["axis"], THEME["axis"], THEME["ink"])
    button("Danger.TButton", surface, THEME["danger"], THEME["danger"], THEME["danger"], THEME["accent_ink"])
    style.configure("Quiet.TButton", font=fonts["body"])

    style.configure("TCheckbutton", background=surface, foreground=THEME["ink"], font=fonts["body"],
                    indicatorbackground=surface, indicatorforeground=THEME["accent_ink"],
                    upperbordercolor=THEME["ink_2"], lowerbordercolor=THEME["ink_2"],
                    indicatormargin=(0, 0, 8, 0), padding=(0, 4), focuscolor=surface)
    style.map("TCheckbutton",
              background=[("active", surface)],
              foreground=[("disabled", THEME["muted"])],
              indicatorbackground=[("selected", THEME["accent"]), ("active", THEME["grid"])],
              upperbordercolor=[("selected", THEME["accent"])],
              lowerbordercolor=[("selected", THEME["accent"])])

    # Only the rows: clam's own layout wraps them in a field with a light border.
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview", background=surface, fieldbackground=surface, foreground=THEME["ink"],
                    rowheight=ROW_HEIGHT, borderwidth=0, font=fonts["body"])
    style.configure("Treeview.Heading", background=surface, foreground=THEME["muted"], relief="flat",
                    borderwidth=0, padding=(8, 8), font=fonts["head"])
    style.map("Treeview.Heading", background=[("active", surface)], foreground=[("active", THEME["muted"])])
    style.map("Treeview",
              background=[("selected", THEME["accent"])],
              foreground=[("selected", THEME["accent_ink"])])
    # A thin thumb in a trough, no arrow buttons and no grip lines.
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Vertical.TScrollbar", background=THEME["axis"], troughcolor=surface, bordercolor=surface,
                    lightcolor=THEME["axis"], darkcolor=THEME["axis"], arrowsize=10, gripcount=0, relief="flat")
    style.map("Vertical.TScrollbar",
              background=[("active", THEME["accent"])],
              lightcolor=[("active", THEME["accent"])],
              darkcolor=[("active", THEME["accent"])])
    style.configure("Run.Horizontal.TProgressbar", background=THEME["accent"], troughcolor=THEME["grid"],
                    bordercolor=THEME["grid"], lightcolor=THEME["accent"], darkcolor=THEME["accent"],
                    thickness=8)
    return fonts


def scrollbar_for(widget, parent):
    """A vertical scrollbar for `widget` that is only on show when there is something to scroll.

    The caller grids it once; after that it hides and shows itself.
    """
    bar = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)

    def follow(first, last):
        bar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            bar.grid_remove()
        else:
            bar.grid()

    widget.configure(yscrollcommand=follow)
    return bar


def draw_marker(canvas, kind):
    """The mark in front of a run step: done (a tick), now (a dot) or later (a ring)."""
    canvas.delete("all")
    if kind == "done":
        canvas.create_line(3, 8.5, 6.2, 11.7, 13, 4.8, fill=THEME["ink_2"], width=2,
                           capstyle="round", joinstyle="round")
    elif kind == "now":
        canvas.create_oval(3.5, 3.5, 12.5, 12.5, fill=THEME["accent"], outline=THEME["accent"])
    else:
        canvas.create_oval(3.5, 3.5, 12.5, 12.5, outline=THEME["muted"], width=2)


def panel(parent):
    """A flat surface with a hairline border."""
    return tk.Frame(parent, bg=THEME["surface"], highlightbackground=THEME["grid"],
                    highlightcolor=THEME["grid"], highlightthickness=1, bd=0)


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

class App:
    def __init__(self, root, config_path, state_path):
        self.root = root
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.events = queue.Queue()
        self.mode = "idle"          # idle, loading (reading players) or running
        self.runner = None
        self.worker = None
        self.jobs = []
        self.job_index = 0
        self.rows = []
        self.player_vars = []       # (BooleanVar, row) for the demo on show
        self.shown_key = None
        self.load_errors = {}       # demo key -> why CSDM could not read it
        self.side_shown = None      # players, run or setup
        self.step_state = {}        # run step -> done, now or later
        self.closing = False

        stamp = build_stamp()
        root.title("CS Clip Encoder" + ("   ·   build %s" % stamp if stamp else ""))
        root.geometry(WINDOW_SIZE)
        root.minsize(980, 660)
        self.fonts = style_window(root)
        self._set_icon()
        root.report_callback_exception = self._callback_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.config, self.csdm, self.problems = setup_problems(self.config_path)
        self.state = {"version": 1, "demos": {}}
        self._build()
        self.reload()
        self._poll_id = root.after(POLL_MS, self.poll)

    # -- building ---------------------------------------------------------

    def _set_icon(self):
        try:
            if os.name == "nt":
                self.root.iconbitmap(default=str(resource_path("icon.ico")))
            else:
                self._icon = tk.PhotoImage(file=str(resource_path("logo.png")))
                self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

    def _build(self):
        outer = ttk.Frame(self.root, padding=(22, 18, 22, 18))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=3, uniform="middle")
        outer.columnconfigure(1, weight=2, uniform="middle")
        outer.rowconfigure(1, weight=1)

        self._build_header(outer).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        self._build_demos(outer).grid(row=1, column=0, sticky="nsew", padx=(0, 7))

        self.side = ttk.Frame(outer)
        self.side.grid(row=1, column=1, sticky="nsew", padx=(7, 0))
        self.side.rowconfigure(0, weight=1)
        self.side.columnconfigure(0, weight=1)
        self.players_panel = self._build_players(self.side)
        self.run_panel = self._build_run(self.side)
        self.setup_panel = self._build_setup(self.side)
        for frame in (self.players_panel, self.run_panel, self.setup_panel):
            frame.grid(row=0, column=0, sticky="nsew")

        self._build_activity(outer).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))

    def _build_header(self, parent):
        header = ttk.Frame(parent)
        header.columnconfigure(1, weight=1)
        try:
            self._logo = tk.PhotoImage(file=str(resource_path("logo.png")))
            tk.Label(header, image=self._logo, bg=THEME["plane"], bd=0).grid(row=0, column=0, rowspan=2, padx=(0, 14))
        except tk.TclError:
            self._logo = None
        ttk.Label(header, text="CS Clip Encoder", style="H1.TLabel").grid(row=0, column=1, sticky="sw")
        self.subtitle = tk.StringVar()
        self.subtitle_label = ttk.Label(header, textvariable=self.subtitle, style="Sub.TLabel")
        self.subtitle_label.grid(row=1, column=1, sticky="nw")

        ttk.Label(header, text="My new demos", style="Sub.TLabel").grid(row=0, column=2, rowspan=2, padx=(0, 10))
        self.count_choice = tk.StringVar()
        self.count_box = ttk.Combobox(header, textvariable=self.count_choice, state="readonly", width=11)
        self.count_box.grid(row=0, column=3, rowspan=2, padx=(0, 10))
        self.clip_new_button = ttk.Button(header, text="Clip my new demos", style="Primary.TButton",
                                          command=self.clip_new_demos)
        self.clip_new_button.grid(row=0, column=4, rowspan=2)
        return header

    def _build_demos(self, parent):
        frame = panel(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        head = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 12, 14, 6))
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="Demos", style="H2.TLabel").grid(row=0, column=0, sticky="w")
        self.demos_hint = tk.StringVar(value="Newest first. Click one to see its players.")
        ttk.Label(head, textvariable=self.demos_hint, style="PanelMuted.TLabel").grid(row=0, column=1, sticky="e")

        columns = (("played", "PLAYED", 112, False), ("map", "MAP", 128, False),
                   ("clips", "YOUR CLIPS", 96, False), ("file", "DEMO FILE", 240, True))
        self.tree = ttk.Treeview(frame, columns=[name for name, *_ in columns], show="headings",
                                 selectmode="browse")
        for name, title, width, stretch in columns:
            self.tree.heading(name, text=title, anchor="w")
            self.tree.column(name, width=width, minwidth=60, stretch=stretch, anchor="w")
        self.tree.tag_configure("new", foreground=THEME["ink"])
        self.tree.tag_configure("done", foreground=THEME["ink_2"])
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(6, 6), pady=(0, 6))
        scrollbar_for(self.tree, frame).grid(row=1, column=1, sticky="ns", padx=(0, 4), pady=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        return frame

    def _build_players(self, parent):
        frame = panel(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        head = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 12, 14, 6))
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="Players", style="H2.TLabel").grid(row=0, column=0, sticky="w")
        self.players_where = tk.StringVar()
        ttk.Label(head, textvariable=self.players_where, style="PanelMuted.TLabel").grid(row=0, column=1, sticky="e")

        # The list sits on a canvas so that it scrolls when it does not fit:
        # a small window, or a demo with more than ten players.
        canvas = tk.Canvas(frame, bg=THEME["surface"], highlightthickness=0, bd=0, height=10)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar_for(canvas, frame).grid(row=1, column=1, sticky="ns", padx=(0, 4))
        self.players_body = ttk.Frame(canvas, style="Panel.TFrame", padding=(14, 0, 14, 6))
        self.players_body.columnconfigure(0, weight=1)
        inside = canvas.create_window(0, 0, anchor="nw", window=self.players_body)
        self.players_body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(inside, width=event.width))

        def wheel(event):
            if canvas.yview() != (0.0, 1.0):        # only when there is something to scroll
                canvas.yview_scroll(-1 if (event.delta > 0 or event.num == 4) else 1, "units")

        # The wheel belongs to whatever is under the pointer, so it is only
        # taken while the pointer is over the list.
        def take_wheel(_event):
            for name in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.bind_all(name, wheel)

        def give_wheel(_event):
            for name in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.unbind_all(name)

        canvas.bind("<Enter>", take_wheel)
        canvas.bind("<Leave>", give_wheel)
        self.players_canvas = canvas

        foot = tk.Frame(frame, bg=THEME["surface"], highlightbackground=THEME["grid"], highlightthickness=0)
        foot.grid(row=2, column=0, columnspan=2, sticky="ew")
        tk.Frame(foot, bg=THEME["grid"], height=1).pack(fill="x")
        inner = ttk.Frame(foot, style="Panel.TFrame", padding=(14, 10, 14, 12))
        inner.pack(fill="x")
        inner.columnconfigure(0, weight=1)
        self.selection_text = tk.StringVar()
        ttk.Label(inner, textvariable=self.selection_text, style="PanelSoft.TLabel").grid(row=0, column=0, sticky="w")
        self.clip_players_button = ttk.Button(inner, text="Clip players", style="Outline.TButton",
                                              command=self.clip_selected_players)
        self.clip_players_button.grid(row=0, column=1, sticky="e")
        return frame

    def _build_run(self, parent):
        frame = tk.Frame(parent, bg=THEME["surface"], highlightbackground=THEME["accent"],
                         highlightcolor=THEME["accent"], highlightthickness=1, bd=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        head = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 12, 14, 6))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="Running", style="H2.TLabel").grid(row=0, column=0, sticky="w")
        self.run_counter = tk.StringVar()
        ttk.Label(head, textvariable=self.run_counter, style="PanelSoft.TLabel").grid(row=0, column=1, sticky="e")

        body = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 6, 14, 10))
        body.grid(row=1, column=0, sticky="ew")
        body.columnconfigure(0, weight=1)
        self.job_title = tk.StringVar()
        ttk.Label(body, textvariable=self.job_title, style="PanelBold.TLabel").grid(row=0, column=0, sticky="w")
        self.job_status = tk.StringVar()
        ttk.Label(body, textvariable=self.job_status, style="PanelAccent.TLabel").grid(row=0, column=1, sticky="e")
        self.step_labels = {}
        self.step_marks = {}
        for index, (name, text) in enumerate(STEPS):
            line = ttk.Frame(body, style="Panel.TFrame")
            line.grid(row=1 + index, column=0, columnspan=2, sticky="w", pady=(8 if index == 0 else 3, 0))
            mark = tk.Canvas(line, width=16, height=16, bg=THEME["surface"], highlightthickness=0, bd=0)
            mark.pack(side="left", padx=(0, 8))
            label = ttk.Label(line, text=text, style="PanelMuted.TLabel")
            label.pack(side="left")
            self.step_labels[name] = label
            self.step_marks[name] = mark
            draw_marker(mark, "later")
        self.progress = ttk.Progressbar(body, mode="indeterminate", style="Run.Horizontal.TProgressbar")
        self.progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 8))
        ttk.Label(body, style="PanelMuted.TLabel", wraplength=360, justify="left",
                  text="CS2 has the screen while it records. CSDM reports no percentage for "
                       "this step, so the bar only shows that it is working.").grid(
            row=5, column=0, columnspan=2, sticky="w")

        waiting = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 0, 14, 0))
        waiting.grid(row=2, column=0, sticky="new")
        tk.Frame(waiting, bg=THEME["grid"], height=1).pack(fill="x", pady=(0, 10))
        self.waiting_text = tk.StringVar()
        ttk.Label(waiting, textvariable=self.waiting_text, style="PanelSoft.TLabel", justify="left").pack(anchor="w")

        foot = ttk.Frame(frame, style="Panel.TFrame")
        foot.grid(row=3, column=0, sticky="ew")
        tk.Frame(foot, bg=THEME["grid"], height=1).pack(fill="x")
        inner = ttk.Frame(foot, style="Panel.TFrame", padding=(14, 10, 14, 12))
        inner.pack(fill="x")
        inner.columnconfigure(0, weight=1)
        self.stop_note = tk.StringVar(value="Stopping closes CS2 and leaves this demo unclipped.")
        ttk.Label(inner, textvariable=self.stop_note, style="PanelSoft.TLabel", wraplength=230,
                  justify="left").grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(inner, text="Stop run", style="Danger.TButton", command=self.stop_run)
        self.stop_button.grid(row=0, column=1, sticky="e")
        return frame

    def _build_setup(self, parent):
        frame = panel(parent)
        frame.columnconfigure(0, weight=1)
        head = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 12, 14, 6))
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Setup needed", style="H2.TLabel").pack(anchor="w")
        body = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 4, 14, 14))
        body.grid(row=1, column=0, sticky="nsew")
        self.setup_text = tk.StringVar()
        ttk.Label(body, textvariable=self.setup_text, style="Panel.TLabel", wraplength=370,
                  justify="left").pack(anchor="w")
        ttk.Label(body, style="PanelMuted.TLabel", wraplength=370, justify="left",
                  text="Fix these in config.json, save it, then press Check again.").pack(anchor="w", pady=(12, 12))
        buttons = ttk.Frame(body, style="Panel.TFrame")
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Open settings file", style="Outline.TButton",
                   command=self.open_settings).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Check again", style="Quiet.TButton", command=self.reload).pack(side="left")
        return frame

    def _build_activity(self, parent):
        frame = panel(parent)
        frame.columnconfigure(0, weight=1)
        head = ttk.Frame(frame, style="Panel.TFrame", padding=(14, 10, 14, 6))
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="Activity", style="H2.TLabel").grid(row=0, column=0, sticky="w")
        self.activity_text = tk.StringVar()
        self.activity_label = ttk.Label(head, textvariable=self.activity_text, style="PanelSoft.TLabel")
        self.activity_label.grid(row=0, column=1, sticky="w", padx=(14, 10))
        self.refresh_button = ttk.Button(head, text="Refresh", style="Quiet.TButton", command=self.reload)
        self.refresh_button.grid(row=0, column=2, padx=(0, 8))
        ttk.Button(head, text="Open settings file", style="Quiet.TButton",
                   command=self.open_settings).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(head, text="Open clips folder", style="Quiet.TButton",
                   command=self.open_clips).grid(row=0, column=4)

        self.log = tk.Text(frame, height=6, wrap="none", bg=THEME["surface"], fg=THEME["ink_2"],
                           insertbackground=THEME["surface"], selectbackground=THEME["accent"],
                           selectforeground=THEME["accent_ink"], font=self.fonts["mono"], bd=0,
                           highlightthickness=0, padx=14, pady=4, state="disabled", cursor="arrow")
        self.log.tag_configure("command", foreground=THEME["ink"])
        self.log.tag_configure("error", foreground=THEME["danger"])
        self.log.tag_configure("hint", foreground=THEME["muted"])
        self.log.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        scrollbar_for(self.log, frame).grid(row=1, column=1, sticky="ns", padx=(0, 4), pady=(0, 8))
        self._log_line("What CSDM prints during a run shows up here.", tag="hint")
        return frame

    # -- state on show ----------------------------------------------------

    @property
    def ready(self):
        return self.config is not None and not self.problems

    def reload(self, settings=True):
        """Read the state file and the demo folder again and redraw the lists.

        With `settings`, config.json is read again too and demos CSDM could
        not read get another try. That is what the Refresh button does. The
        settings are left alone while something is running: a run keeps the
        settings it started with.
        """
        if settings and self.mode == "idle":
            self.config, self.csdm, self.problems = setup_problems(self.config_path)
            self.load_errors = {}
        demos = []
        if self.ready:
            try:
                self.state = clipenc.load_state(self.state_path)
                demos = clipenc.find_demos(self.config["demo_folder"])
            except (clipenc.ClipError, OSError) as error:
                self.problems = [str(error)]
        if not self.ready:
            self.rows = []
            self.tree.delete(*self.tree.get_children())
            self.setup_text.set("\n\n".join("•  " + one_paragraph(problem) for problem in self.problems))
            self.subtitle.set("Not ready yet. See the panel on the right.")
            self._show_side("setup")
            self._enable_actions()
            return
        self.rows = demo_rows(demos, self.state, self.config)

        selected = self.selected_key()
        self.tree.delete(*self.tree.get_children())
        for row in self.rows:
            self.tree.insert("", "end", iid=row["key"], tags=("new" if row["new"] else "done",),
                             values=(row["played"], row["map"], row["clips"], row["path"].name))
        if selected in {row["key"] for row in self.rows}:
            self.tree.selection_set(selected)

        waiting = sum(1 for row in self.rows if row["new"])
        if self.mode != "running":
            self.subtitle.set("%s in the demo folder, %d not clipped for you yet"
                              % (plural(len(self.rows), "demo"), waiting))
        choices = count_choices(waiting)
        self.count_box["values"] = [text for text, _ in choices]
        if self.count_choice.get() not in [text for text, _ in choices]:
            self.count_choice.set(choices[0][0] if choices else "")
        if self.mode == "idle" and not self.activity_text.get():
            self.activity_text.set(last_run_text(self.state, self.config))
        self._enable_actions()
        if self.mode != "running":
            self.show_selected()

    def _show_side(self, name):
        """Bring one of the three right-hand panels to the front."""
        self.side_shown = name
        {"players": self.players_panel, "run": self.run_panel, "setup": self.setup_panel}[name].tkraise()

    def _lock_demos(self, locked):
        """While a run is on, the demo list cannot be clicked and is dimmed."""
        self.tree.configure(selectmode="none" if locked else "browse")
        self.tree.tag_configure("new", foreground=THEME["muted"] if locked else THEME["ink"])
        self.tree.tag_configure("done", foreground=THEME["muted"] if locked else THEME["ink_2"])

    def selected_key(self):
        selection = self.tree.selection()
        return selection[0] if selection else None

    def row_for(self, key):
        return next((row for row in self.rows if row["key"] == key), None)

    def _enable_actions(self):
        idle = self.ready and self.mode == "idle"
        waiting = any(row["new"] for row in self.rows)
        self.clip_new_button.state(["!disabled"] if idle and waiting else ["disabled"])
        self.count_box.configure(state="readonly" if idle and waiting else "disabled")
        self.refresh_button.state(["!disabled"] if self.mode == "idle" else ["disabled"])
        self.update_selection_count()

    # -- the player picker ------------------------------------------------

    def on_select(self, _event=None):
        if self.mode == "running":
            return
        self.show_selected()

    def show_selected(self):
        """Show the players of the selected demo, asking CSDM for them if needed."""
        if not self.ready:
            return
        self._show_side("players")
        key = self.selected_key()
        row = self.row_for(key) if key else None
        if row is None:
            self.shown_key = None
            self.players_where.set("")
            self._players_message("Select a demo on the left to choose whose kills to clip.")
            return
        info = clipenc.demo_info(self.state, key)
        if info is None:
            self.shown_key = None
            self.players_where.set(row["played"])
            if key in self.load_errors:
                self._players_message("CSDM could not read this demo.\n\n%s" % one_paragraph(self.load_errors[key]),
                                      style="PanelDanger.TLabel", retry=key)
            elif self.mode == "idle":
                self.start_loading(row)
            elif self.mode == "loading":
                self._players_message("Reading players from CSDM. This takes a few seconds and does not start CS2.")
            return
        self.shown_key = key
        self.players_where.set("%s, %s" % (row["map"], row["played"]))
        self._players_list(player_rows(info, self.state, key, self.config))

    def _clear_players(self):
        for child in self.players_body.winfo_children():
            child.destroy()
        self.player_vars = []
        self.players_canvas.yview_moveto(0)

    def _players_message(self, text, style="PanelSoft.TLabel", retry=None):
        self._clear_players()
        ttk.Label(self.players_body, text=text, style=style, wraplength=370, justify="left").grid(
            row=0, column=0, sticky="w", pady=(10, 0))
        if retry is not None:
            ttk.Button(self.players_body, text="Try again", style="Quiet.TButton",
                       command=lambda: self.retry_loading(retry)).grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.update_selection_count()

    def retry_loading(self, key):
        self.load_errors.pop(key, None)
        self.show_selected()

    def _players_list(self, groups):
        self._clear_players()
        body = self.players_body
        body.columnconfigure(0, weight=1)
        line = 0
        for heading, rows in groups:
            if not rows:
                continue
            ttk.Label(body, text=heading.upper(), style="PanelHead.TLabel").grid(
                row=line, column=0, columnspan=3, sticky="w", pady=(8 if line else 2, 0))
            tk.Frame(body, bg=THEME["grid"], height=1).grid(row=line + 1, column=0, columnspan=3, sticky="ew",
                                                             pady=(3, 2))
            line += 2
            for row in rows:
                chosen = tk.BooleanVar(value=False)
                box = ttk.Checkbutton(body, text=row["name"], variable=chosen, command=self.update_selection_count)
                box.grid(row=line, column=0, sticky="w")
                if row["kills"] == 0:
                    box.state(["disabled"])     # nothing to clip
                ttk.Label(body, text=plural(row["kills"], "kill"), style="PanelSoft.TLabel").grid(
                    row=line, column=1, sticky="e", padx=(8, 16))
                ttk.Label(body, text=row["status"],
                          style="PanelSoft.TLabel" if row["done"] else "PanelMuted.TLabel").grid(
                    row=line, column=2, sticky="e")
                self.player_vars.append((chosen, row))
                line += 1
        if not self.player_vars:
            self._players_message("CSDM found no players in this demo.")
            return
        self.update_selection_count()

    def chosen_players(self):
        return [row for chosen, row in self.player_vars if chosen.get()]

    def update_selection_count(self):
        count = len(self.chosen_players())
        can_clip = self.ready and self.mode == "idle" and count > 0
        self.clip_players_button.configure(text="Clip %s" % plural(count, "player") if count else "Clip players")
        self.clip_players_button.state(["!disabled"] if can_clip else ["disabled"])
        if count:
            self.selection_text.set("%d selected. CS2 opens once for each." % count)
        elif self.player_vars:
            self.selection_text.set("Tick the players to clip.")
        else:
            self.selection_text.set("")

    def start_loading(self, row):
        self.mode = "loading"
        self._enable_actions()
        self._players_message("Reading players from CSDM. This takes a few seconds and does not start CS2.")
        self.runner = clipenc.Runner(on_line=lambda line: self.events.put(("line", line)))
        self.worker = threading.Thread(target=self._load_worker, args=(row["path"], row["key"]), daemon=True)
        self.worker.start()

    def _load_worker(self, demo, key):
        try:
            state = clipenc.load_state(self.state_path)
            clipenc.load_demo_info(demo, self.config, state, self.state_path, self.csdm, self.runner)
            self.events.put(("info", key, None))
        except clipenc.ClipError as error:
            self.events.put(("info", key, str(error)))
        except Exception as error:  # the window must hear about it, whatever it is
            self.events.put(("info", key, "Unexpected error: %r" % (error,)))

    # -- starting and following a run -------------------------------------

    def clip_new_demos(self):
        if not (self.ready and self.mode == "idle"):
            return
        demos = waiting_demos([row["path"] for row in self.rows], self.state, self.config["steam_id"])
        limits = dict(count_choices(len(demos)))
        limit = limits.get(self.count_choice.get(), 1)
        self.start_run([(demo, self.config["steam_id"]) for demo in demos[:limit]])

    def clip_selected_players(self):
        if not (self.ready and self.mode == "idle") or self.shown_key is None:
            return
        row = self.row_for(self.shown_key)
        chosen = self.chosen_players()
        if row is None or not chosen:
            return
        again = [player["name"] for player in chosen if player["done"]]
        if again and not messagebox.askyesno(
                "Clip again?",
                "%s already clipped from this demo: %s.\n\nClipping again adds a second set of "
                "clips next to the first. Continue?" % ("Player" if len(again) == 1 else "Players", ", ".join(again)),
                parent=self.root):
            return
        self.start_run([(row["path"], player["steam_id"]) for player in chosen])

    def job_text(self, job, label=None, map_name=None):
        demo, steam_id = job
        row = self.row_for(clipenc.demo_key(demo))
        info = clipenc.demo_info(self.state, clipenc.demo_key(demo)) or {}
        if label is None:
            label = (self.config.get("player_labels") or {}).get(steam_id) or next(
                (p.get("name") for p in info.get("players") or [] if p.get("steam_id") == steam_id), None) or steam_id
        if map_name is None:
            map_name = info.get("map")
        where = clipenc.map_prefix(map_name, self.config["map_prefixes"])[0] if map_name else Path(demo).name
        played = (", " + row["played"]) if row else ""
        return "%s%s  ·  %s" % (where, played, label)

    def start_run(self, jobs):
        if not jobs:
            return
        try:
            before, after = clipenc.check_padding(self.config["seconds_before"], self.config["seconds_after"])
        except clipenc.ClipError as error:
            self._finish_message(str(error), error=True)
            return
        self.jobs = list(jobs)
        self.job_index = 0
        self.mode = "running"
        self._enable_actions()
        self.demos_hint.set("Locked while recording")
        self._lock_demos(True)
        self.subtitle.set("A run is in progress. Demos and players are locked until it ends.")
        self.subtitle_label.configure(foreground=THEME["accent"])
        self.activity_text.set("CSDM output, as it prints it")
        self.activity_label.configure(style="PanelSoft.TLabel")
        self._clear_log()
        self.stop_button.state(["!disabled"])
        self.stop_note.set("Stopping closes CS2 and leaves this demo unclipped.")
        self._show_job(0)
        self._show_side("run")
        self.progress.start(14)
        self.runner = clipenc.Runner(
            on_line=lambda line: self.events.put(("line", line)),
            on_stage=lambda name, details: self.events.put(("stage", name, details)),
        )
        self.worker = threading.Thread(target=self._run_worker, args=(self.jobs, before, after), daemon=True)
        self.worker.start()

    def _run_worker(self, jobs, before, after):
        clips, finished, error, stopped = 0, 0, None, False
        for index, (demo, steam_id) in enumerate(jobs):
            self.events.put(("job", index))
            try:
                state = clipenc.load_state(self.state_path)
                made = clipenc.clip_demo(demo, steam_id, self.config, state, self.state_path, self.csdm,
                                         before, after, runner=self.runner)
            except clipenc.Stopped:
                stopped = True
                break
            except clipenc.ClipError as problem:
                error = str(problem)
                break
            except Exception as problem:  # the window must hear about it, whatever it is
                error = "Unexpected error: %r" % (problem,)
                break
            clips += len(made)
            finished += 1
        self.events.put(("end", {"clips": clips, "finished": finished, "total": len(jobs),
                                 "error": error, "stopped": stopped}))

    def _show_job(self, index, label=None, map_name=None):
        self.job_index = index
        self.run_counter.set("Recording %d of %d" % (index + 1, len(self.jobs)))
        self.job_title.set(self.job_text(self.jobs[index], label, map_name))
        rest = self.jobs[index + 1:]
        if rest:
            lines = ["Waiting:"] + ["    " + self.job_text(job) for job in rest[:4]]
            if len(rest) > 4:
                lines.append("    and %d more" % (len(rest) - 4))
            self.waiting_text.set("\n".join(lines))
        else:
            self.waiting_text.set("Nothing waiting after this one.")

    def _show_step(self, current):
        names = [name for name, _ in STEPS]
        position = names.index(current) if current in names else len(names)
        for index, (name, text) in enumerate(STEPS):
            kind = "done" if index < position else "now" if index == position else "later"
            style = {"done": "PanelSoft.TLabel", "now": "PanelBold.TLabel", "later": "PanelMuted.TLabel"}[kind]
            self.step_labels[name].configure(style=style)
            draw_marker(self.step_marks[name], kind)
            self.step_state[name] = kind
        self.job_status.set({"analyze": "Reading", "record": "Recording", "sort": "Sorting"}.get(current, ""))

    def stop_run(self, confirm=True):
        if self.mode != "running" or self.runner is None:
            return
        if confirm and not messagebox.askyesno(
                "Stop the run?",
                "CS2 will be closed at once and the demo being recorded stays unclipped.\n\n"
                "Demos already finished in this run keep their clips.", parent=self.root):
            return
        self.stop_button.state(["disabled"])
        self.stop_note.set("Stopping. Closing CS2.")
        self.job_status.set("Stopping")
        threading.Thread(target=self.runner.stop, daemon=True).start()

    # -- events from the worker thread ------------------------------------

    def poll(self):
        if self.closing:
            return
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self._poll_id = self.root.after(POLL_MS, self.poll)

    def handle(self, event):
        kind = event[0]
        if kind == "line":
            self._log_line(event[1])
        elif kind == "job":
            self._show_job(event[1])
            self._show_step("analyze")
        elif kind == "stage":
            name, details = event[1], event[2]
            if name == "record":
                self._show_job(self.job_index, details.get("label"), details.get("map"))
            if name in ("analyze", "record", "sort", "done"):
                self._show_step(name)
        elif kind == "info":
            key, problem = event[1], event[2]
            self.mode = "idle"
            self.worker = None
            if problem:
                self.load_errors[key] = problem     # shown instead of asking CSDM again
            self.reload(settings=False)
        elif kind == "end":
            self._end_run(event[1])

    def _end_run(self, result):
        self.progress.stop()
        self.mode = "idle"
        self.worker = None
        self.demos_hint.set("Newest first. Click one to see its players.")
        self._lock_demos(False)
        self.subtitle_label.configure(foreground=THEME["ink_2"])
        if result["error"]:
            self._log_line(result["error"], tag="error")
        self._finish_message(*end_text(result))
        self.reload(settings=False)

    def _finish_message(self, text, error=False):
        self.activity_text.set(text)
        self.activity_label.configure(style="PanelDanger.TLabel" if error else "PanelSoft.TLabel")

    # -- the log ----------------------------------------------------------

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log_line(self, text, tag=None):
        text = clean_line(text)
        if tag is None and text.startswith(("> csdm", "== ")):
            tag = "command"
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", (tag,) if tag else ())
        extra = int(self.log.index("end-1c").split(".")[0]) - 1 - LOG_LINES
        if extra > 0:
            self.log.delete("1.0", "%d.0" % (extra + 1))
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- the rest ---------------------------------------------------------

    def open_settings(self):
        open_path(self.config_path if self.config_path.exists() else self.config_path.parent, editor=True)

    def open_clips(self):
        if self.config and self.config.get("output_folder") and Path(self.config["output_folder"]).is_dir():
            open_path(self.config["output_folder"])
        else:
            messagebox.showinfo("CS Clip Encoder", "The clips folder is not set in config.json yet.",
                                parent=self.root)

    def on_close(self):
        if self.mode == "running" and self.runner is not None:
            if not messagebox.askyesno(
                    "A run is in progress",
                    "Closing stops the run: CS2 is closed at once and the demo being recorded "
                    "stays unclipped.\n\nClose anyway?", parent=self.root):
                return
            self.runner.stop()
        elif self.mode == "loading" and self.runner is not None:
            self.runner.stop()                  # ends csdm; no recording, so CS2 is not touched
        self.shut()

    def shut(self):
        """Close the window. Nothing of it may be touched afterwards."""
        self.closing = True
        try:
            self.root.after_cancel(self._poll_id)
            self.progress.stop()                # it keeps a timer of its own
            self.root.destroy()
        except tk.TclError:
            pass

    def _callback_error(self, kind, value, trace):
        """A bug in the window must be visible, also in an .exe with no console."""
        text = "".join(traceback.format_exception(kind, value, trace))
        try:
            self._log_line(text, tag="error")
            self._finish_message("Something went wrong in the window. See the lines below.", error=True)
        except tk.TclError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="clipgui", description="The CS clip encoder window.")
    parser.add_argument("--config", default=str(clipenc.APP_DIR / clipenc.CONFIG_NAME),
                        help="path to config.json (default: next to the program)")
    parser.add_argument("--state", default=str(clipenc.APP_DIR / clipenc.STATE_NAME),
                        help="path to the list of finished demos (default: next to the program)")
    arguments = parser.parse_args(argv)
    root = tk.Tk()
    App(root, arguments.config, arguments.state)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
