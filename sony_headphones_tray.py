#!/usr/bin/python3
# sony-headphones-tray: a tray icon for Sony WH-1000XM2/XM3/XM4 headphones.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tray icon (StatusNotifierItem / AppIndicator) for Sony's v1-protocol headphones.

The icon follows BlueZ over D-Bus: it appears when a paired Sony headset connects
and goes away when it disconnects (or stays, greyed, with --always-show). Every
read and change runs the bundled `sony-headphones` CLI in a worker thread, one at
a time. No control link is held open between clicks, so a phone app can still
connect to the headphones.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import signal
import subprocess
import sys
import threading

VERSION = "0.1.0"
SONY_UUID = "96cc203e-5068-46ad-b32d-e316f5e069ba"  # Sony's control service
HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_CLI = os.path.join(HERE, "sony-headphones")

POLL_S = 600              # re-read battery and settings while connected
FIRST_READ_S = 4          # a headset that just connected needs a moment
RETRY_S = (5, 20, 60)     # when the control channel refuses, then back to POLL_S
CLI_TIMEOUT_S = 45
LOW_BATTERY = 20

MODES = [
    ("noise-cancelling", "Noise cancelling"),
    ("wind-noise-reduction", "Wind noise reduction"),
    ("ambient-sound", "Ambient sound"),
    ("off", "Off"),
]
LEVELS = [0, 5, 10, 15, 20]
EQS = ["off", "bass-boost", "treble-boost", "vocal", "speech",
       "bright", "excited", "mellow", "relaxed", "manual"]

# Situations, from nothing to fully working.
NO_BLUETOOTH = "no-bluetooth"
NO_DEVICE = "no-device"
DISCONNECTED = "disconnected"
READING = "reading"
UNAVAILABLE = "unavailable"   # connected, but the control channel did not answer
UNSUPPORTED = "unsupported"   # v2 models (WH-1000XM5 and newer)
OK = "ok"

# ---------------------------------------------------------------------------
# Plain logic (no GTK or D-Bus needed; covered by tests/test_tray.py)
# ---------------------------------------------------------------------------


def sony_devices(objects: dict) -> list[dict]:
    """Sony headsets in BlueZ's managed objects, connected ones first."""
    devices = []
    for path, interfaces in objects.items():
        dev = interfaces.get("org.bluez.Device1")
        if not dev:
            continue
        if SONY_UUID not in [str(u).lower() for u in dev.get("UUIDs", [])]:
            continue
        address = str(dev.get("Address", ""))
        devices.append({
            "path": str(path),
            "address": address,
            "name": str(dev.get("Alias") or dev.get("Name") or address),
            "connected": bool(dev.get("Connected", False)),
            "paired": bool(dev.get("Paired", False)),
        })
    return sorted(devices, key=lambda d: (not d["connected"], not d["paired"], d["name"]))


def adapter_powered(objects: dict) -> bool:
    return any(bool(i["org.bluez.Adapter1"].get("Powered", False))
               for i in objects.values() if "org.bluez.Adapter1" in i)


def pick_device(devices: list[dict], preferred: str | None = None) -> dict | None:
    if preferred:
        return next((d for d in devices if d["address"].lower() == preferred.lower()), None)
    return devices[0] if devices else None


def situation_for(powered: bool, device: dict | None) -> str:
    if not powered:
        return NO_BLUETOOTH
    if device is None:
        return NO_DEVICE
    return READING if device["connected"] else DISCONNECTED


def classify(state: dict | None, error: str | None) -> str:
    """What a CLI answer means for a connected headset."""
    if state and state.get("connected") and state.get("battery") is not None:
        return OK
    if "v2 protocol" in (error or ""):
        return UNSUPPORTED
    return UNAVAILABLE


def headline(situation: str, name: str | None, state: dict) -> str:
    name = name or "Sony headphones"
    if situation == NO_BLUETOOTH:
        return "Bluetooth is off"
    if situation == NO_DEVICE:
        return "No Sony headphones paired"
    if situation == DISCONNECTED:
        return f"{name}: not connected"
    if situation == READING:
        return f"{name}: reading..."
    if situation == UNAVAILABLE:
        return f"{name}: controls unavailable"
    if situation == UNSUPPORTED:
        return f"{name}: model not supported"
    text = name
    if state.get("battery") is not None:
        text += f"  {state['battery']}%" + (" (charging)" if state.get("charging") else "")
    return text


def low_battery(state: dict) -> bool:
    battery = state.get("battery")
    return battery is not None and battery <= LOW_BATTERY and not state.get("charging")


def call_cli(cli: str, address: str | None, args: list[str], demo: bool = False,
             timeout: float = CLI_TIMEOUT_S) -> tuple[int, dict | None, str | None]:
    """Run the CLI with --json. Returns (exit code, state or None, error or None)."""
    cmd = [sys.executable, "-I", cli, "--json"]
    if address and not demo:
        cmd += ["--address", address]
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": os.path.expanduser("~")}
    for key in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if demo:
        env["SONY_HEADPHONES_DEMO"] = "1"
    try:
        done = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 1, None, "the headphones did not answer in time"
    except OSError as exc:
        return 1, None, f"cannot run {cli}: {exc}"
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    try:
        state = json.loads(lines[-1])
    except (IndexError, ValueError):
        stderr = done.stderr.strip().splitlines()
        return done.returncode or 1, None, stderr[-1] if stderr else "no answer"
    if not isinstance(state, dict):
        return 1, None, "unexpected answer"
    error = state.get("error")
    if done.returncode and not error:
        error = done.stderr.strip() or "the headphones refused that"
    return done.returncode, state, error


# ---------------------------------------------------------------------------
# The tray
# ---------------------------------------------------------------------------

Gtk = GLib = AI = dbus = None


def load_gui() -> None:
    global Gtk, GLib, AI, dbus
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as indicator
    except (ValueError, ImportError):
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as indicator
    from gi.repository import GLib as glib, Gtk as gtk
    import dbus as dbus_module
    from dbus.mainloop.glib import DBusGMainLoop
    DBusGMainLoop(set_as_default=True)
    Gtk, GLib, AI, dbus = gtk, glib, indicator, dbus_module


class Tray:
    def __init__(self, args):
        self.args = args
        self.device: dict | None = None
        self.situation = NO_BLUETOOTH
        self.state: dict = {}
        self.message: str | None = None
        self.retry = 0
        self.timer = None
        self.rescan_timer = None
        self.warned_low = False
        self.syncing = False
        self.jobs: queue.Queue = queue.Queue()
        self.pending_status = False
        threading.Thread(target=self.worker, daemon=True).start()

        self.ind = AI.Indicator.new("sony-headphones-tray", "audio-headphones-symbolic",
                                    AI.IndicatorCategory.HARDWARE)
        self.ind.set_menu(self.build_menu())

        if args.demo:
            self.set_device(True, {"path": "", "address": "00:00:00:00:00:00",
                                   "name": "WH-1000XM4 (demo)", "connected": True, "paired": True})
        else:
            self.bus = dbus.SystemBus()
            self.bus.add_signal_receiver(self.on_properties, "PropertiesChanged",
                                         "org.freedesktop.DBus.Properties", "org.bluez")
            for member in ("InterfacesAdded", "InterfacesRemoved"):
                self.bus.add_signal_receiver(lambda *_: self.schedule_rescan(), member,
                                             "org.freedesktop.DBus.ObjectManager", "org.bluez")
            self.bus.add_signal_receiver(lambda *_: self.schedule_rescan(), "NameOwnerChanged",
                                         "org.freedesktop.DBus", arg0="org.bluez")
            self.rescan()
        GLib.timeout_add_seconds(POLL_S, self.poll)

    # -- menu -----------------------------------------------------------------

    def build_menu(self):
        menu = Gtk.Menu()
        self.header = Gtk.MenuItem(label="")
        self.header.set_sensitive(False)
        menu.append(self.header)
        self.detail = Gtk.MenuItem(label="")
        self.detail.set_sensitive(False)
        menu.append(self.detail)
        self.connect_item = Gtk.MenuItem(label="Connect")
        self.connect_item.connect("activate", lambda _: self.connect_device())
        menu.append(self.connect_item)
        menu.append(Gtk.SeparatorMenuItem())

        self.controls = []
        self.mode_items = {}
        group = None
        for key, label in MODES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = item
            item.connect("toggled", self.on_radio, ["set", "nc", key])
            self.mode_items[key] = item
            menu.append(item)
            self.controls.append(item)

        self.ambient = Gtk.MenuItem(label="Ambient level")
        submenu, group = Gtk.Menu(), None
        self.level_items = {}
        for level in LEVELS:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, str(level))
            group = item
            item.connect("toggled", self.on_radio, ["set", "ambient-level", str(level)])
            self.level_items[level] = item
            submenu.append(item)
        self.voice = Gtk.CheckMenuItem(label="Focus on voice")
        self.voice.connect("toggled", self.on_check, "focus-on-voice")
        submenu.append(Gtk.SeparatorMenuItem())
        submenu.append(self.voice)
        self.ambient.set_submenu(submenu)
        menu.append(self.ambient)
        self.controls.append(self.ambient)
        menu.append(Gtk.SeparatorMenuItem())

        self.checks = {}
        for key, label, feature in (("speak-to-chat", "Speak-to-Chat", "speak-to-chat"),
                                    ("dsee", "DSEE Extreme", "dsee"),
                                    ("pause-when-taken-off", "Pause when taken off",
                                     "pause-when-taken-off")):
            item = Gtk.CheckMenuItem(label=label)
            item.connect("toggled", self.on_check, key)
            self.checks[key] = (item, feature)
            menu.append(item)
            self.controls.append(item)

        self.eq = Gtk.MenuItem(label="Equalizer")
        submenu, group = Gtk.Menu(), None
        self.eq_items = {}
        for preset in EQS:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, preset.replace("-", " ").capitalize())
            group = item
            item.connect("toggled", self.on_radio, ["set", "eq", preset])
            self.eq_items[preset] = item
            submenu.append(item)
        self.eq.set_submenu(submenu)
        menu.append(self.eq)
        self.controls.append(self.eq)
        menu.append(Gtk.SeparatorMenuItem())

        self.refresh_item = Gtk.MenuItem(label="Refresh")
        self.refresh_item.connect("activate", lambda _: self.request_status())
        menu.append(self.refresh_item)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        menu.append(quit_item)
        menu.show_all()
        return menu

    def update_ui(self) -> None:
        s, sit = self.state, self.situation
        name = self.device["name"] if self.device else None
        text = headline(sit, name, s)
        self.header.set_label(text)
        self.ind.set_title(text)
        self.detail.set_label(self.message or "")
        self.detail.set_visible(bool(self.message))
        self.connect_item.set_visible(sit == DISCONNECTED)
        self.refresh_item.set_visible(sit in (OK, UNAVAILABLE, READING))
        self.refresh_item.set_sensitive(sit != READING)

        has_state = sit == OK
        features = s.get("features") or []
        self.syncing = True
        try:
            for item in self.controls:
                item.set_visible(sit in (OK, UNAVAILABLE, READING))
                item.set_sensitive(has_state)
            self.mode_items["wind-noise-reduction"].set_visible(bool(s.get("supports_wind")))
            if has_state:
                if s.get("nc_mode") in self.mode_items:
                    self.mode_items[s["nc_mode"]].set_active(True)
                self.ambient.set_sensitive(s.get("nc_mode") in ("ambient-sound", "off"))
                level = s.get("ambient_level")
                if level is not None:
                    self.level_items[min(LEVELS, key=lambda x: abs(x - level))].set_active(True)
                self.voice.set_active(bool(s.get("focus_on_voice")))
                state_keys = {"speak-to-chat": "speak_to_chat", "dsee": "dsee",
                              "pause-when-taken-off": "pause_when_taken_off"}
                for key, (item, feature) in self.checks.items():
                    item.set_visible(feature in features)
                    item.set_active(bool(s.get(state_keys[key])))
                self.eq.set_visible("equalizer" in features)
                if s.get("eq_preset") in self.eq_items:
                    self.eq_items[s["eq_preset"]].set_active(True)
        finally:
            self.syncing = False

        if sit == NO_BLUETOOTH:
            self.ind.set_icon_full("bluetooth-disabled-symbolic", text)
        else:
            self.ind.set_icon_full("audio-headphones-symbolic", text)
        if self.args.label:
            label = f"{s['battery']}%" if has_state and s.get("battery") is not None else ""
            self.ind.set_label(label, "100%")
        shown = self.args.always_show or sit in (READING, OK, UNAVAILABLE, UNSUPPORTED)
        self.ind.set_status(AI.IndicatorStatus.ACTIVE if shown else AI.IndicatorStatus.PASSIVE)

    # -- clicks ---------------------------------------------------------------

    def on_radio(self, item, args):
        if item.get_active() and not self.syncing:
            self.submit(args)

    def on_check(self, item, key):
        if not self.syncing:
            self.submit(["set", key, "on" if item.get_active() else "off"])

    def connect_device(self):
        if not self.device:
            return
        self.message = "Connecting..."
        self.update_ui()

        def failed(err):
            self.message = f"Could not connect: {err.get_dbus_message() or err.get_dbus_name()}"
            self.update_ui()
        try:
            proxy = self.bus.get_object("org.bluez", self.device["path"])
            proxy.Connect(dbus_interface="org.bluez.Device1", timeout=30,
                          reply_handler=lambda: None, error_handler=failed)
        except dbus.DBusException as exc:
            failed(exc)

    # -- the CLI, one call at a time -------------------------------------------

    def submit(self, args):
        if not self.device:
            return
        if args == ["status"]:
            if self.pending_status:
                return
            self.pending_status = True
        self.jobs.put((self.device["address"], args))

    def request_status(self):
        self.submit(["status"])

    def worker(self):
        while True:
            address, args = self.jobs.get()
            if args == ["status"]:
                self.pending_status = False
            result = call_cli(self.args.cli, address, args, demo=self.args.demo)
            GLib.idle_add(self.on_result, address, args, result)

    def on_result(self, address, args, result):
        code, state, error = result
        if not self.device or self.device["address"] != address or not self.device["connected"]:
            return False  # the answer is about a headset that has gone since
        verdict = classify(state, error)
        if verdict == OK:
            self.state = state
            self.situation = OK
            self.retry = 0
            self.message = f"Could not change that: {error}" if code and args[0] == "set" else None
            self.check_battery()
        elif args[0] == "set" and self.situation == OK:
            self.message = f"Could not change that: {error or 'no answer'}"
        else:
            self.situation = verdict
            if verdict == UNSUPPORTED:
                self.message = None
            elif error and error.startswith("cannot run"):
                self.message = error
            else:
                self.message = "Busy or out of reach (is a phone app connected?)"
            if verdict == UNAVAILABLE:
                self.schedule_status(RETRY_S[min(self.retry, len(RETRY_S) - 1)]
                                     if self.retry < len(RETRY_S) else POLL_S)
                self.retry += 1
        self.update_ui()
        return False

    def check_battery(self):
        if not low_battery(self.state):
            self.warned_low = False
            return
        if self.warned_low:
            return
        self.warned_low = True
        try:
            notify = dbus.Interface(dbus.SessionBus().get_object(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications"),
                "org.freedesktop.Notifications")
            notify.Notify("Sony headphones", 0, "battery-low-symbolic",
                          f"{self.device['name']} battery low",
                          f"{self.state['battery']}% left", [], {}, -1)
        except Exception:  # noqa: BLE001  (a notification is a nicety, never fatal)
            pass

    # -- timers ---------------------------------------------------------------

    def schedule_status(self, seconds):
        if self.timer:
            GLib.source_remove(self.timer)

        def fire():
            self.timer = None
            self.request_status()
            return False
        self.timer = GLib.timeout_add_seconds(seconds, fire)

    def poll(self):
        if self.situation == OK:
            self.request_status()
        return True

    # -- Bluetooth --------------------------------------------------------------

    def on_properties(self, interface, changed, _invalidated):
        if interface in ("org.bluez.Device1", "org.bluez.Adapter1") and \
                set(changed) & {"Connected", "Powered", "UUIDs", "Paired", "Alias"}:
            self.schedule_rescan()

    def schedule_rescan(self):
        if self.rescan_timer is None:
            self.rescan_timer = GLib.timeout_add(500, self.rescan)

    def rescan(self):
        self.rescan_timer = None
        try:
            manager = dbus.Interface(self.bus.get_object("org.bluez", "/"),
                                     "org.freedesktop.DBus.ObjectManager")
            objects = manager.GetManagedObjects()
        except dbus.DBusException:
            objects = {}
        self.set_device(adapter_powered(objects),
                        pick_device(sony_devices(objects), self.args.address))
        return False

    def set_device(self, powered, device):
        before = self.device
        self.device = device
        situation = situation_for(powered, device)
        newly_connected = situation == READING and not (
            before and before["connected"] and before["address"] == device["address"]
            and self.situation in (OK, UNAVAILABLE, UNSUPPORTED))
        if situation != READING:
            self.situation, self.state, self.message, self.retry = situation, {}, None, 0
            self.warned_low = False
            if self.timer:
                GLib.source_remove(self.timer)
                self.timer = None
        elif newly_connected:
            self.situation, self.state, self.message, self.retry = READING, {}, None, 0
            self.schedule_status(0 if self.args.demo else FIRST_READ_S)
        self.update_ui()


def single_instance() -> object | None:
    directory = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    path = os.path.join(directory, f"sony-headphones-tray-{os.getuid()}.lock")
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return handle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sony-headphones-tray", description=__doc__.splitlines()[0])
    parser.add_argument("--address", help="only this headset (Bluetooth MAC)")
    parser.add_argument("--always-show", action="store_true",
                        help="keep the icon while disconnected (with a Connect item)")
    parser.add_argument("--label", action="store_true",
                        help="show the battery percentage next to the icon (if the tray supports labels)")
    parser.add_argument("--demo", action="store_true", help="a pretend WH-1000XM4, no Bluetooth needed")
    parser.add_argument("--cli", default=DEFAULT_CLI, help="path to the sony-headphones CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)

    lock = single_instance()
    if lock is None:
        print("sony-headphones-tray is already running", file=sys.stderr)
        return 0
    try:
        load_gui()
    except (ImportError, ValueError) as exc:
        print(f"missing a dependency ({exc}); see the README", file=sys.stderr)
        return 1
    try:
        Tray(args)
    except dbus.DBusException as exc:
        print(f"cannot reach D-Bus: {exc}", file=sys.stderr)
        return 1
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: Gtk.main_quit() or False)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
