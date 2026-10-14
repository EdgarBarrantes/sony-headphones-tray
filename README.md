# sony-headphones-tray

A small tray icon for Sony **WH-1000XM4**, **WH-1000XM3** and **WH-1000XM2**
headphones on Linux. It does the everyday things Sony's phone app does, without
the phone:

- noise cancelling / ambient sound / off (and wind noise reduction, where the
  model has it)
- ambient sound level and "focus on voice"
- Speak-to-Chat on/off (the XM4's "pause when I talk")
- DSEE Extreme, pause when taken off
- equalizer presets
- battery level, and a notification when it drops to 20%

The icon shows up when the headphones connect and goes away when they
disconnect. It works in any tray that speaks StatusNotifierItem / AppIndicator:
COSMIC's status area, KDE Plasma, XFCE, and GNOME with the
[AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).

It also installs `sony-headphones`, a command line tool for the same settings:

```
sony-headphones status
sony-headphones set speak-to-chat off
sony-headphones set nc ambient-sound        # noise-cancelling | ambient-sound | off
sony-headphones set ambient-level 12        # 0-20
sony-headphones set eq bass-boost
```

## Install

Dependencies (all from your distribution, no pip):

```
# Debian, Ubuntu, Pop!_OS
sudo apt install python3-gi python3-dbus gir1.2-ayatanaappindicator3-0.1
# Fedora
sudo dnf install python3-gobject python3-dbus libayatana-appindicator-gtk3
# Arch
sudo pacman -S python-gobject python-dbus libayatana-appindicator
```

Then, with the headphones paired and connected:

```
git clone https://github.com/EdgarBarrantes/sony-headphones-tray.git
cd sony-headphones-tray
./install.sh
```

This installs for your user only (no sudo): the programs in
`~/.local/share/sony-headphones-tray/`, the commands `sony-headphones` and
`sony-headphones-tray` in `~/.local/bin/`, and an entry in
`~/.config/autostart/` so the tray starts at login. `./install.sh --uninstall`
removes all of it.

Options (add them to the `Exec=` line of the autostart entry):

| Option | |
|---|---|
| `--always-show` | Keep the icon while the headphones are away, with a **Connect** item |
| `--label` | Show the battery percentage next to the icon (if your tray shows labels) |
| `--address AA:BB:...` | Only this headset, if you have more than one Sony device |
| `--demo` | A pretend WH-1000XM4, to try it without headphones (changes don't stick) |

## When things aren't connected

- **Headphones off or away:** the icon hides (or, with `--always-show`, says
  "not connected" and offers Connect).
- **Bluetooth off, or bluetoothd restarting:** same; it picks things up again
  by itself when Bluetooth comes back.
- **Connected, but the controls don't answer:** usually another device's app
  (Sony's app or Gadgetbridge on your phone) holds the control channel. The
  menu says so, greys out the controls, and retries after 5, 20 and 60 seconds,
  then every 10 minutes (or click Refresh).
- **WH-1000XM5 and newer:** these use Sony's newer v2 protocol, which isn't
  supported; the menu says "model not supported".

The tray never keeps a connection to the headphones' control channel open: each
read or change opens it, does its thing and closes it, so your phone can still
connect too. Settings live in the headphones, so a change made here holds on
your phone as well.

## How it works

`sony_headphones_tray.py` follows BlueZ over D-Bus to know when a paired Sony
headset (identified by Sony's control service UUID) connects, and runs
`sony-headphones` for every read and change, one at a time, in a worker thread.

`sony-headphones` is the helper from
[gabamnml/omarchy-sony-headphones](https://github.com/gabamnml/omarchy-sony-headphones)
(commit 5ed5bbb), a dependency-free Python implementation of Sony's v1
protocol over RFCOMM; it only talks to the headphones and `bluetoothctl`, never
the network. It is included unchanged except for one line, marked
`sony-headphones-tray:` in `direct()`: plain `status` printed "Headphones not
connected" even on success, because closing the link marked the state it was
about to print as disconnected. The protocol itself was reverse engineered by
[Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge).

## Tests

```
python3 tests/test_tray.py     # no headphones, GTK or Bluetooth needed
```

## On Android

[Gadgetbridge](https://gadgetbridge.org/) (F-Droid) controls the same
headphones without Google services.

## License

AGPL-3.0-or-later, like the helper and Gadgetbridge it builds on. See
[LICENSE](LICENSE). Not affiliated with Sony.
