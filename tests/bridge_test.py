#!/usr/bin/python3 -I
"""bridge/asus_bridge.py end to end, headless: two private dbus-daemons (one
standing in for the system bus) with fake asusd, UPower and systemd
(tests/fake_services.py), a fake Hyprland on its two IPC sockets, a fake
sysfs tree and a temp HOME. The real bridge runs as the shell would start it.

Run: tests/bridge_test.py   (needs dbus-daemon and python-gobject)
"""
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "..", "bridge", "asus_bridge.py")
FAKES = os.path.join(HERE, "fake_services.py")
AURA = "/xyz/ljones/aura/19b6_3_4"
OVERDRIVE = "/xyz/ljones/asus_armoury/panel_overdrive"
BAT = "/org/freedesktop/UPower/devices/battery_BAT1"
HELD = "omarchy-asus-charge-limit"

CONF = """# test
INTERNAL_OUTPUT=eDP-1
HZ_AC=240
HZ_BATTERY=60
HZ_GAMING=165
KBD_AC=med
KBD_BATTERY=off
PANEL_OVERDRIVE_AC=1
PANEL_OVERDRIVE_BATTERY=0
"""
HOLD = CONF + "CHARGE_HOLD_WEAK_BARREL=1\n"


class FakeHyprland:
    """.socket.sock answers j/monitors and records /eval modesets (which
    change the rate); .socket2.sock pushes events to every listener."""

    def __init__(self, d):
        self.dir = d
        os.makedirs(d)
        self.hz = 60
        self.giant = False      # answer j/monitors with 2 MB (a misbehaving compositor)
        self.evals = []
        self.listeners = []
        self.lock = threading.Lock()
        self.req = self._server(".socket.sock", self._request)
        self.ev = self._server(".socket2.sock", self._listener)

    def _server(self, name, handler):
        s = socket.socket(socket.AF_UNIX)
        s.bind(os.path.join(self.dir, name))
        s.listen(8)

        def loop():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                try:
                    handler(c)
                except OSError:
                    pass        # a client that hangs up mid-reply (the bridge's size cap)
        threading.Thread(target=loop, daemon=True).start()
        return s

    def _request(self, c):
        data = c.recv(65536).decode()
        with self.lock:
            if data == "j/monitors" and self.giant:
                out = "[" + " " * (2 * 1024 * 1024) + "]"
            elif data == "j/monitors":
                out = json.dumps([
                    {"name": "eDP-1", "description": "Samsung Display Corp. ATNA60DL04-0",
                     "width": 2560, "height": 1600, "x": 0, "y": 0, "scale": 1.6,
                     "refreshRate": float(self.hz), "currentFormat": "XRGB2101010"},
                    {"name": "HDMI-A-1", "description": "Monitor", "width": 2560, "height": 1440,
                     "x": 1600, "y": 0, "scale": 1.0, "refreshRate": 144.0, "currentFormat": "XRGB8888"}])
            elif data.startswith("/eval "):
                self.evals.append(data[6:])
                m = re.search(r'output = "eDP-1", mode = "2560x1600@(\d+)"', data)
                if m:
                    self.hz = int(m.group(1))
                out = "ok"
            else:
                out = "unknown request"
        c.sendall(out.encode())
        c.close()

    def _listener(self, c):
        with self.lock:
            self.listeners.append(c)

    def event(self, line, hz=None):
        with self.lock:
            if hz is not None:
                self.hz = hz
            for c in self.listeners:
                c.sendall((line + "\n").encode())

    def close(self):
        self.req.close()
        self.ev.close()


class Harness:
    def __init__(self, conf=CONF, asusd=True, on_battery=False, charge_mode=None, bright=None,
                 slash_getall_fails=False, state_files=None, before=None, giant_hyprland=False,
                 hold_quiet=None):
        self.tmp = tempfile.mkdtemp(prefix="g16b-")
        t = self.tmp
        self.home = os.path.join(t, "home")
        self.run_dir = os.path.join(t, "run")
        self.sys = os.path.join(t, "sys")
        for d in (self.home, self.run_dir, os.path.join(self.sys, "class", "power_supply", "BAT0"),
                  os.path.join(self.sys, "class", "leds", "asus::kbd_backlight")):
            os.makedirs(d)
        with open(os.path.join(self.sys, "class", "power_supply", "BAT0", "charge_control_end_threshold"), "w") as f:
            f.write("80\n")
        self.charge_mode = os.path.join(self.sys, "class", "firmware-attributes", "asus-armoury",
                                        "attributes", "charge_mode", "current_value")
        if charge_mode is not None:
            self.set_charge_mode(charge_mode)
        self.backlight = os.path.join(self.sys, "class", "backlight", "amdgpu_bl1")
        if bright is not None:
            # An nvidia_* backlight sorts first and must be skipped.
            for d, v in ((os.path.join(self.sys, "class", "backlight", "nvidia_0"), 50), (self.backlight, bright)):
                os.makedirs(d)
                with open(os.path.join(d, "max_brightness"), "w") as f:
                    f.write("100\n")
                with open(os.path.join(d, "brightness"), "w") as f:
                    f.write(f"{v}\n")
        self.led = os.path.join(self.sys, "class", "leds", "asus::kbd_backlight", "brightness_hw_changed")
        with open(self.led, "w") as f:
            f.write("1\n")
        self.conf = os.path.join(self.home, ".config", "omarchy", "omarchy-asus.conf")
        self.state_dir = os.path.join(self.home, ".local", "state", "omarchy")
        if conf is not None:
            os.makedirs(os.path.dirname(self.conf))
            with open(self.conf, "w") as f:
                f.write(conf)
        for name, text in (state_files or {}).items():
            os.makedirs(self.state_dir, exist_ok=True)
            with open(os.path.join(self.state_dir, name), "w") as f:
                f.write(text)
        if before:
            before(self)
        self.winlock = os.path.join(t, "g16-winlock")
        with open(self.winlock, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.winlock, 0o755)
        self.log = os.path.join(t, "fake.log")
        open(self.log, "w").close()
        self.daemons = []
        self.sysbus = self._bus("system.bus")
        self.sesbus = self._bus("session.bus")
        self.hypr = FakeHyprland(os.path.join(self.run_dir, "hypr", "SIG"))
        self.hypr.giant = giant_hyprland
        base = {"PATH": "/usr/bin:/bin", "HOME": self.home, "LANG": "C.UTF-8",
                "DBUS_SYSTEM_BUS_ADDRESS": self.sysbus, "DBUS_SESSION_BUS_ADDRESS": self.sesbus}
        fenv = dict(base, FAKE_LOG=self.log, FAKE_NO_ASUSD="0" if asusd else "1", FAKE_SYSFS=self.sys,
                    FAKE_SLASH_GETALL_FAILS="1" if slash_getall_fails else "0")
        self.fakes = subprocess.Popen([sys.executable, "-I", FAKES], env=fenv, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True)
        assert self.fakes.stdout.readline().strip() == "ready"
        if on_battery:
            self.fake({"set": ["/org/freedesktop/UPower", "org.freedesktop.UPower", "OnBattery", "b", True]})
        self.env = dict(base, XDG_RUNTIME_DIR=self.run_dir, HYPRLAND_INSTANCE_SIGNATURE="SIG",
                        G16_BRIDGE_SYSROOT=self.sys, G16_BRIDGE_WINLOCK=self.winlock)
        if hold_quiet is not None:
            self.env["G16_BRIDGE_HOLD_QUIET_S"] = str(hold_quiet)
        self.out = queue.Queue()
        self.states = []
        self.replies = {}
        self.next_id = 1
        self.err = open(os.path.join(t, "bridge.err"), "w")
        self.bridge = subprocess.Popen([sys.executable, "-I", "-B", BRIDGE], env=self.env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=self.err, text=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _bus(self, name):
        addr = "unix:path=" + os.path.join(self.tmp, name)
        p = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--address=" + addr,
                              "--print-address=1"], stdout=subprocess.PIPE, text=True)
        p.stdout.readline()
        self.daemons.append(p)
        return addr

    def _read(self):
        for line in self.bridge.stdout:
            self.out.put(json.loads(line))
        self.out.put(None)

    def pump(self, timeout):
        try:
            msg = self.out.get(timeout=timeout)
        except queue.Empty:
            return False
        if msg is None:
            return False
        if msg["type"] == "state":
            self.states.append(msg)
        elif msg["type"] == "reply":
            self.replies[msg["id"]] = msg
        return True

    def wait(self, pred, timeout=8.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            self.pump(min(0.2, max(0.01, end - time.time())))
        if pred():
            return
        raise AssertionError(f"timed out waiting for {what}; last state: {self.states[-1] if self.states else None}\n"
                             f"log: {self.calls()}\nstderr: {open(os.path.join(self.tmp, 'bridge.err')).read()[-2000:]}")

    @property
    def state(self):
        return self.states[-1] if self.states else {}

    def send(self, cmd, *args):
        cid = self.next_id
        self.next_id += 1
        self.bridge.stdin.write(json.dumps({"id": cid, "cmd": cmd, "args": list(args)}) + "\n")
        self.bridge.stdin.flush()
        self.wait(lambda: cid in self.replies, what=f"reply to {cmd}")
        self.wait(lambda: self.state.get("ack", 0) >= cid, what=f"state with ack {cid}")
        return self.replies[cid]

    def set_charge_mode(self, v):
        os.makedirs(os.path.dirname(self.charge_mode), exist_ok=True)
        with open(self.charge_mode, "w") as f:
            f.write(f"{v}\n")

    def brightness(self):
        with open(os.path.join(self.backlight, "brightness")) as f:
            return int(f.read())

    def adapter(self, online):
        # Only the AC adapter device: a weak adapter dropping out and back.
        self.fake({"set": ["/org/freedesktop/UPower/devices/line_power_ACAD",
                           "org.freedesktop.UPower.Device", "Online", "b", online]})

    def flap(self, times=3):
        for _ in range(times):
            self.adapter(False)
            self.adapter(True)

    def battery(self, prop, sig, value):
        self.fake({"set": [BAT, "org.freedesktop.UPower.Device", prop, sig, value]})

    def charge_sets(self):
        return [s[4] for s in self.sets("ChargeControlEndThreshold")]

    def state_file(self, name):
        path = os.path.join(self.state_dir, name)
        return open(path).read().strip() if os.path.exists(path) else None

    def stderr(self):
        with open(os.path.join(self.tmp, "bridge.err")) as f:
            return f.read()

    def switches(self):
        n = 0
        for task in os.listdir(f"/proc/{self.bridge.pid}/task"):
            with open(f"/proc/{self.bridge.pid}/task/{task}/status") as f:
                n += sum(int(x.split()[1]) for x in f if "ctxt_switches" in x)
        return n

    def power(self, on_battery):
        self.fake({"set": ["/org/freedesktop/UPower", "org.freedesktop.UPower", "OnBattery", "b", on_battery]})

    def curve_calls(self):
        return [c["call"][3] for c in self.calls() if "call" in c and c["call"][1] == "xyz.ljones.FanCurves"]

    def ppt_sets(self):
        return [(c["set"][0].rsplit("/", 1)[-1], c["set"][4]) for c in self.calls()
                if "set" in c and c["set"][2] == "CurrentValue" and "/asus_armoury/ppt_" in c["set"][0]
                or "set" in c and c["set"][0].endswith("nv_dynamic_boost")]

    def group_sets(self):
        return [c["set"][4:] for c in self.calls() if "set" in c and c["set"][2] == "EnablePptGroup"]

    def refused(self):
        return [c["refused"] for c in self.calls() if "refused" in c]

    def fake(self, msg):
        self.fakes.stdin.write(json.dumps(msg) + "\n")
        self.fakes.stdin.flush()
        assert self.fakes.stdout.readline().strip() == "done"

    def calls(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f if x.strip()]

    def sets(self, prop):
        return [c["set"] for c in self.calls() if "set" in c and c["set"][2] == prop]

    def close(self):
        for p in (self.bridge, self.fakes):
            if p.poll() is None:
                p.stdin.close()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        for p in self.daemons:
            p.terminate()
            p.wait()
            p.stdout.close()
        for p in (self.bridge, self.fakes):
            p.stdout.close()
        self.err.close()
        self.hypr.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class BridgeTest(unittest.TestCase):
    def harness(self, **kw):
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h

    def test_start_on_ac_applies_the_ac_settings_once(self):
        h = self.harness()
        h.wait(lambda: h.state.get("hasAura") and h.state.get("refreshHz") == 240, what="AC applied")
        self.assertEqual([s[4] for s in h.sets("Brightness") if s[1] == "xyz.ljones.Aura"], [2])
        self.assertEqual([s[4] for s in h.sets("CurrentValue")], [1])
        self.assertEqual(len(h.hypr.evals), 1)
        self.assertIn('mode = "2560x1600@240", position = "0x0", scale = 1.6, bitdepth = 10', h.hypr.evals[0])
        s = h.state
        self.assertEqual((s["kbdAC"], s["kbdBattery"], s["chargeLimit"], s["slashPattern"]), ("med", "off", 80, 2))
        self.assertEqual((s["kbdColor"], s["kbdSleep"], s["showOnBoot"], s["hasOverdrive"]), ("00FF88", False, True, True))
        self.assertEqual((s["winlockService"], s["hasWinlock"], s["refreshOverride"]), ("active", True, "auto"))

    def test_unplug_applies_battery_settings_and_replug_ac(self):
        h = self.harness()
        h.wait(lambda: h.state.get("refreshHz") == 240, what="AC applied")
        h.fake({"set": ["/org/freedesktop/UPower", "org.freedesktop.UPower", "OnBattery", "b", True]})
        h.wait(lambda: h.state.get("onBattery") and h.state.get("refreshHz") == 60, what="battery applied")
        h.wait(lambda: h.state.get("kbdBrightness") == 0, what="keyboard off")
        self.assertEqual([s[4] for s in h.sets("CurrentValue")], [1, 0])
        h.fake({"set": ["/org/freedesktop/UPower", "org.freedesktop.UPower", "OnBattery", "b", False]})
        h.wait(lambda: h.state.get("refreshHz") == 240 and h.state.get("kbdBrightness") == 2, what="AC again")
        self.assertEqual(len(h.hypr.evals), 3)

    def test_hyprland_reload_reapplies_the_refresh_rate(self):
        h = self.harness(on_battery=True)
        h.wait(lambda: h.state.get("refreshHz") == 60 and h.state.get("hasAura"), what="battery applied")
        n = len(h.hypr.evals)
        brightness_sets = len(h.sets("Brightness"))
        h.hypr.event("configreloaded>>", hz=240)   # monitors.lua put the native rate back
        h.wait(lambda: len(h.hypr.evals) == n + 1 and h.hypr.hz == 60, what="re-applied after reload")
        h.hypr.event("monitoraddedv2>>2,HDMI-A-1,Monitor", hz=240)
        h.wait(lambda: len(h.hypr.evals) == n + 2 and h.hypr.hz == 60, what="re-applied after hotplug")
        self.assertEqual(len(h.sets("Brightness")), brightness_sets, "a Hyprland event only touches the refresh rate")

    def test_pin_beats_gaming_beats_power_source(self):
        h = self.harness()
        h.wait(lambda: h.state.get("refreshHz") == 240, what="AC applied")
        r = subprocess.run([sys.executable, "-I", "-B", BRIDGE, "gaming", "on"], env=h.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        h.wait(lambda: h.hypr.hz == 165 and h.state.get("gaming"), what="gaming rate")
        self.assertTrue(h.send("refresh", "120")["ok"])
        h.wait(lambda: h.hypr.hz == 120 and h.state.get("refreshOverride") == "120", what="pinned rate")
        with open(os.path.join(h.state_dir, "omarchy-asus-hz")) as f:
            self.assertEqual(f.read().strip(), "120")
        self.assertTrue(h.send("refresh", "auto")["ok"])
        h.wait(lambda: h.hypr.hz == 165, what="back to gaming")
        self.assertTrue(h.send("gaming", "off")["ok"])
        h.wait(lambda: h.hypr.hz == 240 and not h.state.get("gaming"), what="back to AC")

    def test_kbd_level_for_the_other_source_is_saved_not_applied(self):
        h = self.harness(on_battery=True)
        h.wait(lambda: h.state.get("hasAura") and h.state.get("refreshHz") == 60, what="battery applied")
        before = len(h.sets("Brightness"))
        self.assertTrue(h.send("kbd-level", "ac", "high")["ok"])
        self.assertEqual(len(h.sets("Brightness")), before)
        self.assertIn("KBD_AC=high\n", open(h.conf).read())
        self.assertIn("# test\n", open(h.conf).read(), "other lines are kept")
        self.assertEqual(h.state["kbdAC"], "high")
        h.fake({"set": ["/org/freedesktop/UPower", "org.freedesktop.UPower", "OnBattery", "b", False]})
        h.wait(lambda: h.state.get("kbdBrightness") == 3, what="AC level applied on plug")
        self.assertTrue(h.send("kbd-level", "both", "low")["ok"])
        h.wait(lambda: h.state.get("kbdBrightness") == 1, what="both applied now")
        self.assertFalse(h.send("kbd-level", "ac", "blinding")["ok"])

    def test_writes_keep_the_dbus_types(self):
        h = self.harness()
        h.wait(lambda: h.state.get("hasAura"), what="asusd")
        for args in (("kbd-color", "#1A2B3C"), ("kbd-power", "sleep", "true"), ("slash-flag", "ShowOnSleep", "true"),
                     ("slash-brightness", "300"), ("slash-pattern", "5"), ("slash-enabled", "false"),
                     ("battery-limit", "60")):
            self.assertTrue(h.send(*args)["ok"], args)
        sets = {s[2] if s[2] != "Brightness" else s[1] + ".Brightness": s for s in h.sets("") or
                [c["set"] for c in h.calls() if "set" in c]}
        self.assertEqual(sets["LedModeData"][3:], ["(uu(yyy)(yyy)ss)", [0, 0, [26, 43, 60], [0, 0, 0], "Med", "Right"]])
        self.assertEqual(sets["LedPower"][3:], ["(a(ubbbb))", [[[1, True, True, True, True]]]])
        self.assertEqual(sets["ShowOnSleep"][3:], ["b", True])
        self.assertEqual(sets["xyz.ljones.Slash.Brightness"][3:], ["y", 255])
        self.assertEqual(sets["Interval"][3:], ["y", 5])
        self.assertEqual(sets["Enabled"][3:], ["b", False])
        self.assertEqual(sets["ChargeControlEndThreshold"][3:], ["y", 60])
        h.wait(lambda: h.state.get("kbdColor") == "1A2B3C" and h.state.get("chargeLimit") == 60
               and h.state.get("kbdSleep") and not h.state.get("slashOn"), what="signals reflected")
        for bad in (("kbd-color", "red"), ("slash-flag", "Bogus", "true"), ("battery-limit", "5"), ("nope",)):
            self.assertFalse(h.send(*bad)["ok"], bad)

    def test_no_settings_file_only_turns_the_keyboard_off_on_battery(self):
        h = self.harness(conf=None)
        h.wait(lambda: h.state.get("hasAura") and h.state.get("refreshHz") == 60, what="started")
        self.assertEqual([c for c in h.calls() if "set" in c], [], "nothing set on AC")
        h.fake({"set": ["/org/freedesktop/UPower", "org.freedesktop.UPower", "OnBattery", "b", True]})
        h.wait(lambda: h.state.get("onBattery"), what="battery")
        h.wait(lambda: h.state.get("kbdBrightness") == 0, what="keyboard off")
        h.send("get")
        self.assertEqual(h.hypr.evals, [])
        self.assertEqual([c["set"][2] for c in h.calls() if "set" in c], ["Brightness"])
        self.assertEqual((h.state["kbdAC"], h.state["kbdBattery"]), ("", "off"))

    def test_fn_keys_reach_the_state(self):
        h = self.harness()
        h.wait(lambda: h.state.get("kbdBrightness") == 2, what="AC level")
        with open(h.led, "w") as f:
            f.write("3\n")
        h.wait(lambda: h.state.get("kbdBrightness") == 3, what="brightness_hw_changed")

    def test_asusd_absent_then_appearing(self):
        h = self.harness(asusd=False)
        h.wait(lambda: h.state.get("refreshHz") == 240, what="started")
        self.assertFalse(h.state["hasAura"])
        self.assertEqual(h.state["chargeLimit"], 0, "no asusd: the charge limit can't be set, so it hides")
        self.assertFalse(h.send("kbd-color", "112233")["ok"])
        h.fake({"asusd": True})
        h.wait(lambda: h.state.get("hasAura") and h.state.get("hasSlash"), what="asusd found")
        h.wait(lambda: [s[4] for s in h.sets("Brightness") if s[1] == "xyz.ljones.Aura"] == [2], what="AC level applied")
        h.fake({"asusd": False})
        h.wait(lambda: not h.state.get("hasAura"), what="asusd gone")

    def test_winlock_mode_writes_the_dropin_and_restarts_the_unit(self):
        h = self.harness()
        h.wait(lambda: h.state.get("hasWinlock"), what="started")
        self.assertTrue(h.send("winlock-mode", "unlock")["ok"])
        dropin = os.path.join(h.home, ".config", "systemd", "user", "g16-winlock.service.d", "mode.conf")
        self.assertEqual(open(dropin).read(), "[Service]\nEnvironment=G16_WINLOCK_MODE=unlock\n")
        methods = [c["call"][2] for c in h.calls() if "call" in c and c["call"][1].startswith("org.freedesktop.systemd1")]
        self.assertEqual([m for m in methods if m != "GetUnit"], ["Reload", "EnableUnitFiles", "RestartUnit"])
        self.assertEqual(h.state["winlockMode"], "unlock")
        self.assertFalse(h.send("winlock-mode", "off")["ok"])

    def test_idle_bridge_never_wakes(self):
        # No timers: with nothing happening, no thread of the bridge runs.
        h = self.harness()
        h.wait(lambda: h.state.get("hasAura") and h.state.get("refreshHz") == 240, what="settled")
        # A real system bus: other services and UPower's battery device keep
        # emitting PropertiesChanged (the G16 saw ~10 wakeups a minute).
        h.fake({"noise": True})
        time.sleep(1.5)
        before = h.switches()
        time.sleep(6)
        self.assertEqual(h.switches() - before, 0, "the idle bridge woke up")

    def test_usb_c_uses_the_battery_settings(self):
        h = self.harness(charge_mode=2)   # bit 1: USB-C, bit 0 (barrel) clear
        h.wait(lambda: h.state.get("source") == "usbc" and h.state.get("refreshHz") == 60, what="USB-C applied")
        self.assertFalse(h.state["onBattery"])
        h.wait(lambda: [s[4] for s in h.sets("Brightness") if s[1] == "xyz.ljones.Aura"] == [0], what="battery kbd")
        h.set_charge_mode(1)
        h.power(True)
        h.power(False)                    # back on the barrel
        h.wait(lambda: h.state.get("source") == "ac" and h.state.get("refreshHz") == 240, what="barrel applied")

    def test_charger_classified_late_is_read_again(self):
        h = self.harness(charge_mode=1, on_battery=True)
        h.wait(lambda: h.state.get("source") == "battery" and h.state.get("refreshHz") == 60, what="battery")
        h.set_charge_mode(0)              # the firmware hasn't looked at the new adapter yet
        h.power(False)
        h.wait(lambda: h.state.get("source") == "ac", what="first guess")
        h.set_charge_mode(2)              # it turns out to be USB-C
        h.wait(lambda: h.state.get("source") == "usbc" and h.hypr.hz == 60, timeout=4, what="USB-C after the re-read")

    def test_backlight_dims_leaving_the_barrel_and_comes_back(self):
        h = self.harness(conf=CONF + "BRIGHT_BATTERY=30\n", charge_mode=1, bright=80)
        h.wait(lambda: h.state.get("refreshHz") == 240, what="started on the barrel")
        self.assertEqual(h.brightness(), 80, "the first apply only records the source")
        h.power(True)
        h.wait(lambda: h.brightness() == 30, what="dimmed to 30 %")
        with open(os.path.join(h.backlight, "..", "nvidia_0", "brightness")) as f:
            self.assertEqual(f.read().strip(), "50")
        h.power(False)
        h.wait(lambda: h.brightness() == 80, what="barrel level back")
        h.power(True)
        h.wait(lambda: h.brightness() == 30, what="dimmed again")
        with open(os.path.join(h.backlight, "brightness"), "w") as f:
            f.write("90\n")              # turned up by hand on battery
        h.power(False)
        # Four plugs in a few seconds look like a flapping charger: "weak", which
        # keeps the barrel (AC) settings.
        h.wait(lambda: h.state.get("source") in ("ac", "weak") and h.state.get("refreshHz") == 240, what="barrel")
        h.send("get")
        self.assertEqual(h.brightness(), 90, "a brighter manual level is kept")
        calls = [c["call"][3] for c in h.calls() if "call" in c and c["call"][2] == "SetBrightness"]
        self.assertEqual(calls, [["backlight", "amdgpu_bl1", 30], ["backlight", "amdgpu_bl1", 80],
                                 ["backlight", "amdgpu_bl1", 30]])

    def test_the_g16s_settings_file_works_as_is(self):
        # The live file from the G16 (tests/g16.conf): a desc: panel
        # match, keys from an older tool (UPOWER_EVENTS, POLL_SECS, HYPR_EVENTS),
        # quoted values and BRIGHT_BATTERY.
        with open(os.path.join(HERE, "g16.conf")) as f:
            conf = f.read()
        h = self.harness(conf=conf, charge_mode=1, bright=70)
        h.wait(lambda: h.state.get("hasAura") and h.state.get("refreshHz") == 240, what="barrel applied")
        self.assertIn("bitdepth = 10", h.hypr.evals[-1] if h.hypr.evals else "bitdepth = 10")
        h.power(True)
        h.wait(lambda: h.hypr.hz == 60 and h.brightness() == 30, what="battery: 60 Hz, dimmed to 30 %")
        self.assertEqual((h.state["kbdAC"], h.state["kbdBattery"]), ("med", "med"))

    def test_slash_state_when_getall_fails(self):
        # asusd 6.5.0 (zbus) fails a whole GetAll when one getter errors; on the
        # G16 the Slash section then showed nothing until it was toggled.
        h = self.harness(slash_getall_fails=True)
        h.wait(lambda: h.state.get("hasSlash"), what="slash found")
        s = h.state
        self.assertEqual((s["slashOn"], s["slashBrightness"], s["slashPattern"], s["showOnBoot"]),
                         (True, 100, 2, True))

    def test_slash_pattern_sets_interval_then_saves_mode(self):
        h = self.harness()
        h.wait(lambda: h.state.get("hasSlash"), what="slash found")
        self.assertTrue(h.send("slash-pattern", "5")["ok"])      # Transmission
        sets = [c["set"][2:5] for c in h.calls() if "set" in c and c["set"][1] == "xyz.ljones.Slash"]
        self.assertEqual(sets, [["Interval", "y", 5], ["Mode", "y", 0x1A]])
        h.wait(lambda: h.state.get("slashPattern") == 5, what="pattern from Mode")

    def test_usb_c_limits_turn_on_the_cpu_curve_then_write_ppt(self):
        h = self.harness(conf=CONF + 'PPT_USBC="25 30 40"\n', charge_mode=2)
        h.wait(lambda: len(h.ppt_sets()) == 3, what="USB-C limits")
        self.assertEqual(h.refused(), [], "asusd applied every write (the G16 saw them refused)")
        self.assertEqual(h.curve_calls(), [[0, "CPU", True]])
        self.assertEqual(h.group_sets(), [[True, "plugged", 0]])
        self.assertEqual(h.ppt_sets(), [("ppt_pl1_spl", 25), ("ppt_pl2_sppt", 30), ("ppt_pl3_fppt", 40)])
        order = [("curve" if "call" in c else c["set"][2]) for c in h.calls()
                 if "call" in c and c["call"][1] == "xyz.ljones.FanCurves" or "set" in c and c["set"][2] in ("EnablePptGroup", "CurrentValue")
                 and "/xyz/ljones" in c["set"][0] and "panel_overdrive" not in c["set"][0]]
        self.assertEqual(order[:3], ["curve", "EnablePptGroup", "CurrentValue"], "curve, group, then limits")
        state = lambda n: open(os.path.join(h.state_dir, n)).read().strip()
        self.assertEqual((state("omarchy-asus-ppt-curves"), state("omarchy-asus-ppt-groups")), ("0", "ac 0"))
        h.power(True)                       # unplugged: curve off; the AC group can't be reached on DC
        h.wait(lambda: h.curve_calls()[-1:] == [[0, "CPU", False]], what="curve off")
        self.assertEqual((state("omarchy-asus-ppt-curves"), state("omarchy-asus-ppt-groups")), ("", "ac 0"))
        self.assertEqual(h.group_sets(), [[True, "plugged", 0]])
        h.set_charge_mode(1)
        h.power(False)                      # the OEM barrel: the AC group is current again, so it goes off
        h.wait(lambda: h.group_sets()[-1:] == [[False, "plugged", 0]], what="group off")
        self.assertEqual(state("omarchy-asus-ppt-groups"), "")

    def test_no_limit_keys_touch_nothing(self):
        h = self.harness(charge_mode=2)
        h.wait(lambda: h.state.get("source") == "usbc" and h.state.get("hasAura"), what="USB-C")
        h.power(True)
        h.wait(lambda: h.state.get("source") == "battery", what="battery")
        h.send("get")
        self.assertEqual((h.curve_calls(), h.ppt_sets()), ([], []))

    def test_flapping_barrel_is_debounced_and_becomes_weak(self):
        h = self.harness(conf=CONF + 'PPT_WEAK_BARREL="45 55 65"\nBOOST_WEAK_BARREL=5\n', charge_mode=1)
        h.wait(lambda: h.state.get("source") == "ac" and h.hypr.hz == 240, what="barrel")
        evals = len(h.hypr.evals)
        for _ in range(3):                  # the adapter drops out three times in a second
            h.power(True)
            h.power(False)
        h.wait(lambda: h.state.get("source") == "weak", what="weak barrel")
        self.assertEqual(len(h.hypr.evals), evals, "no refresh-rate flips while it flaps")
        h.wait(lambda: len(h.ppt_sets()) == 4, what="weak-barrel limits")
        self.assertEqual(h.ppt_sets(), [("ppt_pl1_spl", 45), ("ppt_pl2_sppt", 55), ("ppt_pl3_fppt", 65),
                                        ("nv_dynamic_boost", 5)])
        self.assertEqual(h.curve_calls(), [[0, "CPU", True]])
        self.assertEqual(h.state["kbdBrightness"], 2, "a weak barrel keeps the AC settings")

    def test_a_flapping_adapter_is_weak_while_on_battery_stays_false(self):
        # The G16's SlimQ: ACAD dropped out 9 times in 46 s, OnBattery never moved.
        h = self.harness(conf=CONF + 'PPT_WEAK_BARREL="45 55 65"\nBOOST_WEAK_BARREL=5\nBRIGHT_BATTERY=30\n',
                         charge_mode=1, bright=80)
        h.wait(lambda: h.state.get("source") == "ac" and h.hypr.hz == 240, what="barrel")
        evals = len(h.hypr.evals)
        for _ in range(3):
            h.adapter(False)
            h.adapter(True)
        h.wait(lambda: h.state.get("source") == "weak", what="weak barrel")
        self.assertFalse(h.state["onBattery"])
        h.wait(lambda: len(h.ppt_sets()) == 4, what="weak-barrel limits")
        self.assertEqual(h.ppt_sets(), [("ppt_pl1_spl", 45), ("ppt_pl2_sppt", 55), ("ppt_pl3_fppt", 65),
                                        ("nv_dynamic_boost", 5)])
        self.assertEqual(len(h.hypr.evals), evals, "no refresh-rate flips")
        self.assertEqual(h.brightness(), 80, "a weak barrel keeps the AC settings: not dimmed")
        self.assertEqual(h.charge_sets(), [], "no CHARGE_HOLD_WEAK_BARREL: charging isn't held")

    # ---- charge hold on a weak barrel (#165) ----
    def test_a_weak_barrel_holds_charging_until_the_load_drops(self):
        # The G16 on a 140 W barrel, gaming while charging from 70 %: the
        # adapter dropped out about once a second and the EC clamped the CPU.
        h = self.harness(conf=HOLD, charge_mode=1, hold_quiet=3)
        h.wait(lambda: h.state.get("source") == "ac" and h.state.get("chargeLimit") == 80, what="barrel")
        h.flap()
        h.wait(lambda: h.state.get("chargeHeld") == 50, what="charging held at the charge")
        self.assertEqual(h.charge_sets(), [50])
        self.assertEqual(h.state["chargeLimit"], 80, "the panel still shows the user's limit")
        self.assertEqual(h.state_file(HELD), "80")
        h.battery("Percentage", "d", 48.0)          # the load outruns the adapter: the charge falls
        h.wait(lambda: h.state.get("chargeHeld") == 48, what="ceiling lowered with the charge")
        for _ in range(4):                          # discharging on AC: the load is still there
            time.sleep(1.2)
            h.battery("State", "u", 2)
        h.send("get")
        self.assertEqual(h.state["chargeHeld"], 48, "held while the battery helps carry the load")
        h.wait(lambda: h.state.get("chargeHeld") == 0, timeout=8, what="charging tried again")
        self.assertEqual(h.charge_sets(), [50, 48, 80])
        self.assertIsNone(h.state_file(HELD))
        self.assertIn("charging resumed (quiet for 3 s): limit 80%", h.stderr())
        # Released: the battery device is no longer followed.
        h.fake({"noise": True})
        time.sleep(1.5)
        before = h.switches()
        time.sleep(3)
        self.assertEqual(h.switches() - before, 0, "the battery device still wakes the bridge")

    def test_dropping_out_again_after_a_retry_holds_again_for_longer(self):
        h = self.harness(conf=HOLD, charge_mode=1, hold_quiet=2)
        h.wait(lambda: h.state.get("source") == "ac" and h.state.get("hasAura"), what="barrel")
        h.flap()
        h.wait(lambda: h.state.get("chargeHeld") == 50, what="held")
        h.wait(lambda: h.state.get("chargeHeld") == 0, timeout=6, what="charging tried again")
        h.flap(1)                                   # still too much: it drops out once more
        h.wait(lambda: h.state.get("chargeHeld") == 50, what="held again")
        self.assertEqual(h.charge_sets(), [50, 80, 50])
        self.assertIn("trying again in 4 s", h.stderr())

    def test_unplugging_a_held_weak_barrel_puts_the_limit_back(self):
        h = self.harness(conf=HOLD, charge_mode=1)  # 2 min quiet spell: only the unplug ends it here
        h.wait(lambda: h.state.get("source") == "ac" and h.state.get("hasAura"), what="barrel")
        h.flap()
        h.wait(lambda: h.state.get("chargeHeld") == 50, what="held")
        h.power(True)
        h.wait(lambda: h.state.get("chargeHeld") == 0, timeout=15, what="unplugged for good")
        self.assertEqual(h.charge_sets(), [50, 80])
        self.assertIsNone(h.state_file(HELD))

    def test_no_hold_at_or_above_the_limit(self):
        h = self.harness(conf=HOLD, charge_mode=1)
        h.wait(lambda: h.state.get("source") == "ac" and h.state.get("hasAura"), what="barrel")
        h.battery("Percentage", "d", 80.0)          # at the limit: not charging anyway
        h.flap()
        h.wait(lambda: h.state.get("source") == "weak", what="weak barrel")
        self.assertEqual((h.charge_sets(), h.state["chargeHeld"]), ([], 0))

    def test_a_limit_picked_while_held_is_the_one_that_comes_back(self):
        h = self.harness(conf=HOLD, charge_mode=1)
        h.wait(lambda: h.state.get("source") == "ac" and h.state.get("hasAura"), what="barrel")
        h.flap()
        h.wait(lambda: h.state.get("chargeHeld") == 50, what="held")
        self.assertTrue(h.send("battery-limit", "100")["ok"])
        self.assertEqual((h.state["chargeLimit"], h.state["chargeHeld"]), (100, 50))
        self.assertEqual((h.charge_sets(), h.state_file(HELD)), ([50], "100"), "still held")
        h.bridge.stdin.close()                      # the shell stops the bridge
        self.assertEqual(h.bridge.wait(5), 0)
        self.assertEqual(h.charge_sets(), [50, 100])
        self.assertIsNone(h.state_file(HELD))

    def test_a_limit_set_elsewhere_ends_the_hold(self):
        h = self.harness(conf=HOLD, charge_mode=1)
        h.wait(lambda: h.state.get("source") == "ac" and h.state.get("hasAura"), what="barrel")
        h.flap()
        h.wait(lambda: h.state.get("chargeHeld") == 50, what="held")
        h.fake({"set": ["/xyz/ljones", "xyz.ljones.Platform", "ChargeControlEndThreshold", "y", 90]})
        h.wait(lambda: h.state.get("chargeHeld") == 0 and h.state.get("chargeLimit") == 90, what="theirs stands")
        self.assertEqual(h.charge_sets(), [50])
        self.assertIsNone(h.state_file(HELD))

    def test_a_limit_left_held_by_a_crash_is_put_back(self):
        h = self.harness(state_files={HELD: "100\n"})
        h.wait(lambda: h.state.get("chargeLimit") == 100, what="the saved limit back")
        self.assertEqual(h.charge_sets(), [100])
        self.assertIsNone(h.state_file(HELD))

    def test_profile_change_moves_the_curve(self):
        h = self.harness(conf=CONF + 'PPT_USBC="25 30 40"\n', charge_mode=2)
        h.wait(lambda: h.curve_calls() == [[0, "CPU", True]], what="curve on, profile 0")
        h.fake({"set": ["/xyz/ljones", "xyz.ljones.Platform", "PlatformProfile", "u", 3]})
        h.wait(lambda: h.curve_calls()[1:] == [[0, "CPU", False], [3, "CPU", True]], what="curve moved to 3")
        with open(os.path.join(h.state_dir, "omarchy-asus-ppt-curves")) as f:
            self.assertEqual(f.read().strip(), "3")

    def test_a_group_the_user_enabled_is_left_alone(self):
        h = self.harness(conf=CONF + 'PPT_USBC="25 30 40"\n', charge_mode=1)
        h.wait(lambda: h.state.get("hasAura"), what="started on the barrel")
        h.fake({"set": ["/xyz/ljones", "xyz.ljones.Platform", "EnablePptGroup", "b", True]})
        h.set_charge_mode(2)                # the user's own group is on; USB-C uses it
        h.power(True)
        h.power(False)
        h.wait(lambda: len(h.ppt_sets()) == 3, what="USB-C limits")
        self.assertEqual(h.group_sets(), [], "it was already on: not touched")
        self.assertEqual(open(os.path.join(h.state_dir, "omarchy-asus-ppt-groups")).read().strip() if
                         os.path.exists(os.path.join(h.state_dir, "omarchy-asus-ppt-groups")) else "", "")

    def test_a_curve_left_on_by_a_crash_is_turned_off(self):
        h = self.harness(on_battery=True, state_files={"omarchy-asus-ppt-curves": "2\n"})
        h.wait(lambda: h.curve_calls() == [[2, "CPU", False]], what="stale curve off")
        with open(os.path.join(h.state_dir, "omarchy-asus-ppt-curves")) as f:
            self.assertEqual(f.read().strip(), "")

    # ---- the bridge's own file writes: no symlink ever redirects one ----
    def test_a_planted_link_at_a_state_file_is_not_followed(self):
        def plant(h):
            os.makedirs(h.state_dir)
            h.victim = os.path.join(h.tmp, "victim")
            with open(h.victim, "w") as f:
                f.write("precious\n")
            os.symlink(h.victim, os.path.join(h.state_dir, "omarchy-asus-hz"))
            os.symlink(h.victim, os.path.join(h.state_dir, "omarchy-asus-gaming"))
        h = self.harness(before=plant)
        h.wait(lambda: h.state.get("hasAura"), what="started")
        self.assertFalse(h.state["gaming"], "a link is not a gaming flag")
        self.assertEqual(h.state["refreshOverride"], "auto", "a link is not a pin")
        r = h.send("refresh", "120")
        self.assertFalse(r["ok"])
        self.assertIn("symbolic link", r["error"])
        self.assertFalse(h.send("gaming", "on")["ok"])
        self.assertEqual(open(h.victim).read(), "precious\n")
        self.assertTrue(os.path.islink(os.path.join(h.state_dir, "omarchy-asus-hz")))

    def test_a_linked_settings_file_is_neither_read_nor_written_through(self):
        def link(h):
            os.makedirs(os.path.dirname(h.conf))
            h.real_conf = os.path.join(h.tmp, "dotfiles-omarchy-asus.conf")
            with open(h.real_conf, "w") as f:
                f.write(CONF)
            os.symlink(h.real_conf, h.conf)
        h = self.harness(conf=None, before=link)
        h.wait(lambda: h.state.get("hasAura"), what="started")
        self.assertNotEqual(h.state.get("kbdAC"), "med", "nothing read through the link")
        r = h.send("kbd-level", "ac", "high")
        self.assertFalse(r["ok"])
        self.assertIn("symbolic link", r["error"])
        self.assertEqual(open(h.real_conf).read(), CONF, "the dotfile is untouched")
        self.assertTrue(os.path.islink(h.conf))

    def test_a_linked_state_dir_is_refused(self):
        def plant(h):
            h.elsewhere = os.path.join(h.tmp, "elsewhere")
            os.makedirs(h.elsewhere)
            os.makedirs(os.path.dirname(h.state_dir))
            os.symlink(h.elsewhere, h.state_dir)
        h = self.harness(before=plant)
        h.wait(lambda: h.state.get("hasAura"), what="started")
        self.assertFalse(h.send("refresh", "120")["ok"])
        self.assertEqual(os.listdir(h.elsewhere), [], "nothing written through the linked dir")

    def test_a_linked_ancestor_of_the_state_dir_is_refused(self):
        def plant(h):
            h.elsewhere = os.path.join(h.tmp, "elsewhere")
            os.makedirs(os.path.join(h.elsewhere, "state", "omarchy"))
            local = os.path.dirname(os.path.dirname(h.state_dir))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            os.symlink(h.elsewhere, local)
        h = self.harness(before=plant)
        h.wait(lambda: h.state.get("hasAura"), what="started")
        self.assertFalse(h.send("refresh", "120")["ok"])
        self.assertEqual(os.listdir(os.path.join(h.elsewhere, "state", "omarchy")), [],
                         "nothing written below the linked ancestor")

    def test_files_under_their_old_names_are_renamed_once(self):
        def old(h):
            os.makedirs(os.path.dirname(h.conf))
            with open(os.path.join(os.path.dirname(h.conf), "power-tune.conf"), "w") as f:
                f.write(CONF)
            os.makedirs(h.state_dir, exist_ok=True)
            with open(os.path.join(h.state_dir, "power-tune-hz"), "w") as f:
                f.write("60\n")
        h = self.harness(conf=None, before=old)
        h.wait(lambda: h.state.get("refreshOverride") == "60", what="the old pin read under its new name")
        self.assertEqual(h.state["kbdAC"], "med", "the old settings read under the new name")
        self.assertEqual(open(h.conf).read(), CONF)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(h.conf), "power-tune.conf")))
        self.assertTrue(os.path.exists(os.path.join(h.state_dir, "omarchy-asus-hz")))
        self.assertFalse(os.path.exists(os.path.join(h.state_dir, "power-tune-hz")))

    def test_writes_leave_no_temp_files(self):
        h = self.harness()
        h.wait(lambda: h.state.get("hasAura"), what="started")
        for args in (("refresh", "120"), ("gaming", "on"), ("kbd-level", "ac", "low"),
                     ("refresh", "auto"), ("gaming", "off"), ("winlock-mode", "unlock")):
            self.assertTrue(h.send(*args)["ok"], args)
        for d in (h.state_dir, os.path.dirname(h.conf)):
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [], d)
        self.assertEqual(oct(os.stat(os.path.join(h.state_dir, "omarchy-asus-hz")).st_mode & 0o777)
                         if os.path.exists(os.path.join(h.state_dir, "omarchy-asus-hz")) else "gone", "gone")

    def test_a_huge_hyprland_reply_is_capped(self):
        h = self.harness(giant_hyprland=True)
        h.wait(lambda: h.state.get("hasAura"), what="started")
        self.assertEqual((h.state["refreshHz"], h.state["hasPanel"]), (0, False))
        h.err.flush()
        with open(os.path.join(h.tmp, "bridge.err")) as f:
            self.assertIn("is over 1048576 bytes", f.read(), "refused by the cap, not parsed")
        h.hypr.giant = False
        h.send("get")                     # a re-read sees the panel again (at its current 60 Hz)
        h.wait(lambda: h.state.get("hasPanel") and h.state.get("refreshHz") == 60, what="normal again")

    def test_overlong_settings_values_are_ignored(self):
        h = self.harness(conf=CONF.replace("KBD_AC=med", "KBD_AC=" + "m" * 1000))
        h.wait(lambda: h.state.get("hasAura"), what="started")
        self.assertEqual(h.state["kbdAC"], "")

    def test_release_turns_everything_off_before_removal(self):
        h = self.harness(conf=CONF + 'PPT_USBC="25 30 40"\n', charge_mode=2)
        h.wait(lambda: len(h.ppt_sets()) == 3, what="USB-C limits")
        h.bridge.stdin.close()              # the plugin is being removed
        h.bridge.wait(5)
        r = subprocess.run([sys.executable, "-I", "-B", BRIDGE, "release"], env=h.env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("released", r.stdout)
        self.assertEqual(h.curve_calls()[-1:], [[0, "CPU", False]])
        self.assertEqual(h.group_sets()[-1:], [[False, "plugged", 0]])
        state = lambda n: open(os.path.join(h.state_dir, n)).read().strip()
        self.assertEqual((state("omarchy-asus-ppt-curves"), state("omarchy-asus-ppt-groups")), ("", ""))

    def test_closing_stdin_stops_the_bridge(self):
        h = self.harness()
        h.wait(lambda: h.state.get("hasAura"), what="started")
        h.bridge.stdin.close()
        self.assertEqual(h.bridge.wait(5), 0)


if __name__ == "__main__":
    if not shutil.which("dbus-daemon"):
        print("bridge tests skipped: no dbus-daemon")
        sys.exit(0)
    unittest.main(verbosity=2)
