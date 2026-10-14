#!/usr/bin/python3
"""Tests for the tray's logic and its use of the CLI. No GTK, D-Bus or headphones needed:
the CLI runs in its demo mode (a simulated WH-1000XM4)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sony_headphones_tray as t  # noqa: E402

XM4 = "AC:80:0A:00:00:01"
OTHER = "11:22:33:44:55:66"


def device(address, connected=True, sony=True, name="WH-1000XM4", paired=True):
    uuids = ["0000110b-0000-1000-8000-00805f9b34fb"] + ([t.SONY_UUID] if sony else [])
    return {"org.bluez.Device1": {"Address": address, "Alias": name, "Connected": connected,
                                  "Paired": paired, "UUIDs": uuids}}


def objects(*devices, powered=True):
    objs = {"/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": powered}}}
    for i, d in enumerate(devices):
        objs[f"/org/bluez/hci0/dev_{i}"] = d
    return objs


class Discovery(unittest.TestCase):
    def test_only_sony_devices(self):
        found = t.sony_devices(objects(device(XM4), device(OTHER, sony=False, name="Mouse")))
        self.assertEqual([d["address"] for d in found], [XM4])

    def test_connected_first(self):
        found = t.sony_devices(objects(device(OTHER, connected=False, name="A"), device(XM4, name="B")))
        self.assertEqual(found[0]["address"], XM4)

    def test_pick_preferred(self):
        found = t.sony_devices(objects(device(XM4), device(OTHER, name="WH-1000XM3")))
        self.assertEqual(t.pick_device(found, OTHER.lower())["address"], OTHER)
        self.assertIsNone(t.pick_device(found, "00:00:00:00:00:00"))
        self.assertIsNone(t.pick_device([], None))

    def test_adapter(self):
        self.assertTrue(t.adapter_powered(objects()))
        self.assertFalse(t.adapter_powered(objects(powered=False)))
        self.assertFalse(t.adapter_powered({}))  # bluetoothd not running


class Situations(unittest.TestCase):
    def test_situation_for(self):
        dev = {"connected": True}
        self.assertEqual(t.situation_for(False, dev), t.NO_BLUETOOTH)
        self.assertEqual(t.situation_for(True, None), t.NO_DEVICE)
        self.assertEqual(t.situation_for(True, {"connected": False}), t.DISCONNECTED)
        self.assertEqual(t.situation_for(True, dev), t.READING)

    def test_classify(self):
        self.assertEqual(t.classify({"connected": True, "battery": 50}, None), t.OK)
        self.assertEqual(t.classify({"connected": False, "battery": None}, "refused"), t.UNAVAILABLE)
        self.assertEqual(t.classify(None, "this device speaks Sony's v2 protocol, ..."), t.UNSUPPORTED)
        self.assertEqual(t.classify(None, None), t.UNAVAILABLE)

    def test_headline(self):
        self.assertEqual(t.headline(t.OK, "XM4", {"battery": 60, "charging": True}), "XM4  60% (charging)")
        self.assertEqual(t.headline(t.DISCONNECTED, "XM4", {}), "XM4: not connected")
        self.assertEqual(t.headline(t.NO_DEVICE, None, {}), "No Sony headphones paired")

    def test_low_battery(self):
        self.assertTrue(t.low_battery({"battery": 15, "charging": False}))
        self.assertFalse(t.low_battery({"battery": 15, "charging": True}))
        self.assertFalse(t.low_battery({"battery": None}))


class Cli(unittest.TestCase):
    def test_demo_status(self):
        code, state, error = t.call_cli(t.DEFAULT_CLI, XM4, ["status"], demo=True)
        self.assertEqual(code, 0, error)
        self.assertEqual(t.classify(state, error), t.OK)
        self.assertIn("speak-to-chat", state["features"])

    def test_demo_set(self):
        code, state, error = t.call_cli(t.DEFAULT_CLI, XM4, ["set", "speak-to-chat", "on"], demo=True)
        self.assertEqual(code, 0, error)
        self.assertTrue(state["speak_to_chat"])
        code, state, _ = t.call_cli(t.DEFAULT_CLI, XM4, ["set", "nc", "ambient-sound"], demo=True)
        self.assertEqual(state["nc_mode"], "ambient-sound")

    def test_missing_cli(self):
        code, state, error = t.call_cli("/nonexistent/sony-headphones", XM4, ["status"])
        self.assertNotEqual(code, 0)
        self.assertIsNone(state)
        self.assertTrue(error)

    def test_garbage_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "junk")
            with open(junk, "w") as fh:
                fh.write("import sys, time\nprint('not json')\nif 'slow' in sys.argv: time.sleep(5)\n")
            code, state, error = t.call_cli(junk, XM4, ["status"])
            self.assertIsNone(state)
            self.assertNotEqual(code, 0)
            code, state, error = t.call_cli(junk, XM4, ["slow"], timeout=0.5)
            self.assertEqual(error, "the headphones did not answer in time")


if __name__ == "__main__":
    unittest.main(verbosity=1)
