#!/usr/bin/python3 -I
"""Fake asusd and UPower (on the bus in DBUS_SYSTEM_BUS_ADDRESS) and a fake
systemd user manager (on DBUS_SESSION_BUS_ADDRESS) for tests/bridge_test.py.

Shaped after asusd 6.x as the G16 widget uses it: the Aura and Slash
interfaces on one object, the charge limit on the platform object, and
asus-armoury attributes one object each. Every Set and method call is
appended to $FAKE_LOG as a JSON line. Commands arrive on stdin as JSON lines:
  {"set": [path, iface, prop, signature, value]}  change a value, emit PropertiesChanged
  {"asusd": false}                                 drop the asusd name (true: own it again)
  {"noise": true}                                  start unrelated PropertiesChanged traffic (NetworkManager,
                                                   UPower's battery device), as on a real system bus
FAKE_SLASH_GETALL_FAILS=1 makes GetAll on the Slash interface fail, the way asusd's
zbus fails the whole call when one getter errors.
Prints "ready" once every name is owned.
"""
import json
import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

LOG = os.environ["FAKE_LOG"]
AURA_PATH = "/xyz/ljones/aura/19b6_3_4"

ASUSD_XML = """
<node>
  <interface name="xyz.ljones.Aura">
    <property name="Brightness" type="u" access="readwrite"/>
    <property name="LedModeData" type="(uu(yyy)(yyy)ss)" access="readwrite"/>
    <property name="LedPower" type="(a(ubbbb))" access="readwrite"/>
  </interface>
  <interface name="xyz.ljones.Slash">
    <property name="Enabled" type="b" access="readwrite"/>
    <property name="Brightness" type="y" access="readwrite"/>
    <property name="Interval" type="y" access="readwrite"/>
    <property name="Mode" type="y" access="readwrite"/>
    <property name="ShowOnBoot" type="b" access="readwrite"/>
    <property name="ShowOnShutdown" type="b" access="readwrite"/>
    <property name="ShowOnSleep" type="b" access="readwrite"/>
    <property name="ShowOnBattery" type="b" access="readwrite"/>
    <property name="ShowBatteryWarning" type="b" access="readwrite"/>
    <property name="ShowOnLidClosed" type="b" access="readwrite"/>
  </interface>
  <interface name="xyz.ljones.Platform">
    <property name="ChargeControlEndThreshold" type="y" access="readwrite"/>
    <property name="PlatformProfile" type="u" access="readwrite"/>
    <property name="EnablePptGroup" type="b" access="readwrite"/>
  </interface>
  <interface name="xyz.ljones.FanCurves">
    <method name="SetProfileFanCurveEnabled">
      <arg type="u" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="in"/>
    </method>
    <method name="SetFanCurvesEnabled"><arg type="u" direction="in"/><arg type="b" direction="in"/></method>
  </interface>
  <interface name="xyz.ljones.AsusArmoury">
    <property name="Name" type="s" access="read"/>
    <property name="CurrentValue" type="i" access="readwrite"/>
    <property name="DefaultValue" type="i" access="read"/>
    <property name="MinValue" type="i" access="read"/>
    <property name="MaxValue" type="i" access="read"/>
  </interface>
</node>"""

LOGIND_XML = """
<node><interface name="org.freedesktop.login1.Session">
  <method name="SetBrightness">
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="u" direction="in"/>
  </method>
</interface></node>"""

UPOWER_XML = """
<node>
  <interface name="org.freedesktop.UPower">
    <method name="EnumerateDevices"><arg type="ao" direction="out"/></method>
    <property name="OnBattery" type="b" access="read"/>
  </interface>
  <interface name="org.freedesktop.UPower.Device">
    <property name="Type" type="u" access="read"/>
    <property name="Online" type="b" access="read"/>
    <property name="Percentage" type="d" access="read"/>
    <property name="State" type="u" access="read"/>
    <property name="EnergyRate" type="d" access="read"/>
  </interface>
</node>"""
ACAD = "/org/freedesktop/UPower/devices/line_power_ACAD"
BAT = "/org/freedesktop/UPower/devices/battery_BAT1"

SYSTEMD_XML = """
<node>
  <interface name="org.freedesktop.systemd1.Manager">
    <method name="GetUnit"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
    <method name="Reload"/>
    <method name="EnableUnitFiles">
      <arg type="as" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="in"/>
      <arg type="b" direction="out"/><arg type="a(sss)" direction="out"/>
    </method>
    <method name="RestartUnit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="o" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.systemd1.Unit">
    <property name="ActiveState" type="s" access="read"/>
  </interface>
</node>"""

# (path, iface) -> {prop: GLib.Variant}
values = {
    (AURA_PATH, "xyz.ljones.Aura"): {
        "Brightness": GLib.Variant("u", 1),
        "LedModeData": GLib.Variant("(uu(yyy)(yyy)ss)", (0, 0, (0, 255, 136), (0, 0, 0), "Med", "Right")),
        "LedPower": GLib.Variant("(a(ubbbb))", ([(1, True, True, False, True)],)),
    },
    (AURA_PATH, "xyz.ljones.Slash"): {
        "Enabled": GLib.Variant("b", True), "Brightness": GLib.Variant("y", 100),
        "Interval": GLib.Variant("y", 2), "Mode": GLib.Variant("y", 0x12), "ShowOnBoot": GLib.Variant("b", True),
        "ShowOnShutdown": GLib.Variant("b", False), "ShowOnSleep": GLib.Variant("b", False),
        "ShowOnBattery": GLib.Variant("b", False), "ShowBatteryWarning": GLib.Variant("b", True),
        "ShowOnLidClosed": GLib.Variant("b", False),
    },
    ("/xyz/ljones", "xyz.ljones.Platform"): {"ChargeControlEndThreshold": GLib.Variant("y", 80),
                                             "PlatformProfile": GLib.Variant("u", 0)},
    ("/xyz/ljones/asus_armoury/panel_overdrive", "xyz.ljones.AsusArmoury"): {
        "Name": GLib.Variant("s", "PanelOverdrive"), "CurrentValue": GLib.Variant("i", 0),
        "DefaultValue": GLib.Variant("i", -1), "MinValue": GLib.Variant("i", -1), "MaxValue": GLib.Variant("i", -1)},
    ("/org/freedesktop/UPower", "org.freedesktop.UPower"): {"OnBattery": GLib.Variant("b", False)},
    ("/org/freedesktop/UPower/devices/line_power_ACAD", "org.freedesktop.UPower.Device"): {
        "Type": GLib.Variant("u", 1), "Online": GLib.Variant("b", True)},
    ("/org/freedesktop/UPower/devices/battery_BAT1", "org.freedesktop.UPower.Device"): {
        "Type": GLib.Variant("u", 2), "Online": GLib.Variant("b", False), "Percentage": GLib.Variant("d", 50.0),
        "State": GLib.Variant("u", 1), "EnergyRate": GLib.Variant("d", 40.0)},
    ("/org/freedesktop/systemd1/unit/g16_2dwinlock_2eservice", "org.freedesktop.systemd1.Unit"): {
        "ActiveState": GLib.Variant("s", "active")},
}


# asus-armoury PPT attributes as asusd 6.5.0 reports them on the G16: the
# range and default follow the power source (AC 15-80 each; DC SPL 25-35,
# SPPT 31-44, FPPT 45-65; Dynamic Boost 5-15 on AC, 0 on battery).
RANGES = {"ppt_pl1_spl": ((15, 80, 80), (25, 35, 35)), "ppt_pl2_sppt": ((15, 80, 80), (31, 44, 44)),
          "ppt_pl3_fppt": ((15, 80, 80), (45, 65, 65)), "nv_dynamic_boost": ((5, 15, 15), (0, 0, 0))}
NAMES = {"ppt_pl1_spl": "PptPl1Spl", "ppt_pl2_sppt": "PptPl2Sppt", "ppt_pl3_fppt": "PptPl3Fppt",
         "nv_dynamic_boost": "NvDynamicBoost"}
for attr, (ac, dc) in RANGES.items():
    values[(f"/xyz/ljones/asus_armoury/{attr}", "xyz.ljones.AsusArmoury")] = {
        "Name": GLib.Variant("s", NAMES[attr]), "CurrentValue": GLib.Variant("i", ac[2]),
        "MinValue": GLib.Variant("i", ac[0]), "MaxValue": GLib.Variant("i", ac[1]),
        "DefaultValue": GLib.Variant("i", ac[2])}


# asusd's PPT tuning groups, one per (plugged, profile): while the current
# one is off, a PPT write is stored but not applied ("profile tuning is
# disabled for current profile and power state"); turning it on applies the
# stored values, turning it off re-applies the profile (limits back to default).
groups = {}       # (plugged, profile) -> bool
stored = {}       # (plugged, profile) -> {attr: value}


def plugged():
    return not values[("/org/freedesktop/UPower", "org.freedesktop.UPower")]["OnBattery"].unpack()


def profile():
    return values[("/xyz/ljones", "xyz.ljones.Platform")]["PlatformProfile"].unpack()


def ppt_path(attr):
    return f"/xyz/ljones/asus_armoury/{attr}"


def reset_ppt(conn):
    for attr in RANGES:
        v = values[(ppt_path(attr), "xyz.ljones.AsusArmoury")]
        if v["CurrentValue"].unpack() != v["DefaultValue"].unpack():
            v["CurrentValue"] = GLib.Variant("i", v["DefaultValue"].unpack())
            changed(conn, ppt_path(attr), "xyz.ljones.AsusArmoury", "CurrentValue")


def set_ranges(on_battery):
    for attr, (ac, dc) in RANGES.items():
        lo, hi, default = dc if on_battery else ac
        v = values[(f"/xyz/ljones/asus_armoury/{attr}", "xyz.ljones.AsusArmoury")]
        v["MinValue"], v["MaxValue"], v["DefaultValue"] = (GLib.Variant("i", x) for x in (lo, hi, default))


def log(entry):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def changed(conn, path, iface, prop):
    conn.emit_signal(None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                     GLib.Variant("(sa{sv}as)", (iface, {prop: values[(path, iface)][prop]}, [])))


def get_property(conn, sender, path, iface, prop):
    if iface == "xyz.ljones.Platform" and prop == "EnablePptGroup":
        return GLib.Variant("b", groups.get((plugged(), profile()), False))
    return values[(path, iface)][prop]


def set_property(conn, sender, path, iface, prop, value):
    key = (plugged(), profile())
    if iface == "xyz.ljones.Platform" and prop == "EnablePptGroup":
        log({"set": [path, iface, prop, "b", value.unpack(), "plugged" if key[0] else "battery", key[1]]})
        groups[key] = value.unpack()
        if value.unpack():
            for attr, v in stored.get(key, {}).items():
                values[(ppt_path(attr), "xyz.ljones.AsusArmoury")]["CurrentValue"] = GLib.Variant("i", v)
                changed(conn, ppt_path(attr), "xyz.ljones.AsusArmoury", "CurrentValue")
        else:
            reset_ppt(conn)
        return True
    attr = path.rsplit("/", 1)[-1]
    if prop == "CurrentValue" and attr in RANGES:
        stored.setdefault(key, {})[attr] = value.unpack()
        if not groups.get(key, False):
            log({"refused": [attr, value.unpack()]})
            return True
    log({"set": [path, iface, prop, value.get_type_string(), value.unpack()]})
    values[(path, iface)][prop] = value
    changed(conn, path, iface, prop)
    return True


def method_call(conn, sender, path, iface, method, params, invocation):
    log({"call": [path, iface, method, params.unpack() if params else None]})
    if method == "EnumerateDevices":
        invocation.return_value(GLib.Variant("(ao)", ([ACAD, BAT],)))
    elif method == "GetUnit":
        invocation.return_value(GLib.Variant("(o)", ("/org/freedesktop/systemd1/unit/g16_2dwinlock_2eservice",)))
    elif method == "EnableUnitFiles":
        invocation.return_value(GLib.Variant("(ba(sss))", (False, [])))
    elif method == "SetBrightness":
        # Like logind: write the backlight's brightness file.
        _sub, dev, value = params.unpack()
        with open(os.path.join(os.environ["FAKE_SYSFS"], "class", "backlight", dev, "brightness"), "w") as f:
            f.write(f"{value}\n")
        invocation.return_value(None)
    elif method == "RestartUnit":
        invocation.return_value(GLib.Variant("(o)", ("/org/freedesktop/systemd1/job/1",)))
    else:
        invocation.return_value(None)


def register(conn, xml, objects):
    node = Gio.DBusNodeInfo.new_for_xml(xml)
    ids = []
    for path, ifaces in objects:
        for iface in node.interfaces:
            if iface.name in ifaces:
                ids.append(conn.register_object(path, iface, method_call, get_property, set_property))
    return ids


system = Gio.bus_get_sync(Gio.BusType.SYSTEM)
session = Gio.bus_get_sync(Gio.BusType.SESSION)
# UPower and logind are other processes than asusd on a real system: own them
# on a connection of their own, so a sender filter can tell them apart.
upower = Gio.DBusConnection.new_for_address_sync(
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"],
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
asusd_objects = [(AURA_PATH, ["xyz.ljones.Aura", "xyz.ljones.Slash"]),
                 ("/xyz/ljones", ["xyz.ljones.Platform", "xyz.ljones.FanCurves"]),
                 ("/xyz/ljones/asus_armoury/panel_overdrive", ["xyz.ljones.AsusArmoury"])] + [
                 (f"/xyz/ljones/asus_armoury/{a}", ["xyz.ljones.AsusArmoury"]) for a in RANGES]
asusd_ids = register(system, ASUSD_XML, asusd_objects)
register(upower, UPOWER_XML, [("/org/freedesktop/UPower", ["org.freedesktop.UPower"]),
                              (ACAD, ["org.freedesktop.UPower.Device"]),
                              (BAT, ["org.freedesktop.UPower.Device"])])
register(upower, LOGIND_XML, [("/org/freedesktop/login1/session/auto", ["org.freedesktop.login1.Session"])])
register(session, SYSTEMD_XML, [("/org/freedesktop/systemd1", ["org.freedesktop.systemd1.Manager"]),
                                ("/org/freedesktop/systemd1/unit/g16_2dwinlock_2eservice",
                                 ["org.freedesktop.systemd1.Unit"])])


def own(conn, name):
    r = conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                       "RequestName", GLib.Variant("(su)", (name, 4)), None, 0, -1, None)
    assert r.unpack()[0] == 1, name


def release(conn, name):
    conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                   "ReleaseName", GLib.Variant("(s)", (name,)), None, 0, -1, None)


if os.environ.get("FAKE_SLASH_GETALL_FAILS") == "1":
    def getall_filter(conn, msg, incoming):
        if (incoming and msg.get_message_type() == Gio.DBusMessageType.METHOD_CALL
                and msg.get_interface() == "org.freedesktop.DBus.Properties"
                and msg.get_member() == "GetAll" and msg.get_body()
                and msg.get_body().unpack()[0] == "xyz.ljones.Slash"):
            conn.send_message(Gio.DBusMessage.new_method_error_literal(
                msg, "org.freedesktop.zbus.Error", "a getter failed"), Gio.DBusSendMessageFlags.NONE)
            return None
        return msg
    system.add_filter(getall_filter)

own(upower, "org.freedesktop.UPower")
own(upower, "org.freedesktop.login1")
own(session, "org.freedesktop.systemd1")
if os.environ.get("FAKE_NO_ASUSD") != "1":
    own(system, "xyz.ljones.Asusd")
print("ready", flush=True)

buf = b""
noisy = []


def stdin_cb(fd, cond):
    global buf
    d = os.read(fd, 65536)
    if not d:
        loop.quit()
        return False
    buf += d
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        msg = json.loads(line)
        if "set" in msg:
            path, iface, prop, sig, val = msg["set"]
            if sig.startswith("("):
                val = tuple(val)
            values[(path, iface)][prop] = GLib.Variant(sig, val)
            if prop == "EnablePptGroup":       # the user (asusctl) turning a group on or off
                groups[(plugged(), profile())] = bool(val)
                print("done", flush=True)
                continue
            if prop == "OnBattery":
                values[(path, iface)][prop] = GLib.Variant(sig, val)
                # A real plug or unplug moves the adapter too (a weak
                # adapter's drop-outs move only the adapter: set Online).
                values[(ACAD, "org.freedesktop.UPower.Device")]["Online"] = GLib.Variant("b", not val)
                changed(upower, ACAD, "org.freedesktop.UPower.Device", "Online")
                set_ranges(bool(val))
                if not groups.get((plugged(), profile()), False):
                    reset_ppt(system)     # the firmware's limits for the new source
            changed(upower if path.startswith("/org/freedesktop/UPower") else system, path, iface, prop)
        elif "noise" in msg:
            nm = Gio.DBusConnection.new_for_address_sync(
                os.environ["DBUS_SYSTEM_BUS_ADDRESS"],
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
            own(nm, "org.freedesktop.NetworkManager")
            noisy.append(nm)

            def tick():
                nm.emit_signal(None, "/org/freedesktop/NetworkManager", "org.freedesktop.DBus.Properties",
                               "PropertiesChanged", GLib.Variant("(sa{sv}as)", (
                                   "org.freedesktop.NetworkManager", {"Connectivity": GLib.Variant("u", 4)}, [])))
                upower.emit_signal(None, "/org/freedesktop/UPower/devices/battery_BAT1",
                                   "org.freedesktop.DBus.Properties", "PropertiesChanged", GLib.Variant(
                                       "(sa{sv}as)", ("org.freedesktop.UPower.Device",
                                                      {"Percentage": GLib.Variant("d", 50.0)}, [])))
                return True
            GLib.timeout_add(100, tick)
        elif "asusd" in msg:
            (own if msg["asusd"] else release)(system, "xyz.ljones.Asusd")
        print("done", flush=True)
    return True


loop = GLib.MainLoop()
GLib.io_add_watch(sys.stdin.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, stdin_cb)
loop.run()
