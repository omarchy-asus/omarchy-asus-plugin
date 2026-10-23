#!/usr/bin/python3 -I
"""Persistent bridge for the omarchy-asus plugin (io.github.omarchy-asus.panel).

Launched by Service.qml as `/usr/bin/python3 -I -B <this file>` with a closed
environment. It speaks line-delimited JSON on stdin/stdout: commands in
({"id": N, "cmd": "...", "args": [...]}), and out a "state" object whenever
something changes plus one "reply" per command. Every state object carries
"ack", the id of the last command handled, so the shell can tell a state that
predates its latest click from one that includes it.

What it follows, all pushed, nothing on a timer:
  - asusd (xyz.ljones.Asusd, system bus): the keyboard's Aura object
    (brightness, colour, power flags), the lid Slash bar, the battery charge
    limit and panel overdrive, through PropertiesChanged signals. asusd
    leaving or coming back is seen as a bus name change.
  - UPower's OnBattery (system bus).
  - Hyprland's event socket: a monitor hotplug or a config reload re-applies
    monitors.lua, which puts the internal panel back on its native refresh
    rate, so both re-apply the per-source refresh rate.
  - The keyboard LED's brightness_hw_changed (Fn+F2/F3 bypass asusd), and
    the settings and state files below, through inotify.

Per power source, from ~/.config/omarchy/omarchy-asus.conf (plain KEY=VALUE,
never sourced):
  KBD_AC / KBD_BATTERY                  off | low | med | high (battery default: off)
  HZ_AC / HZ_BATTERY / HZ_GAMING        refresh rate of the internal panel
  PANEL_OVERDRIVE_AC / _BATTERY         0 | 1
  INTERNAL_OUTPUT                       connector name, or desc:<EDID description>
  BITDEPTH                              carried on every modeset (default: the
                                        panel's current format, 10 or 8)
  BRIGHT_BATTERY                        % to dim the screen to on leaving the
                                        barrel (once, not a cap); empty = off
  BRIGHT_DEVICE                         backlight to use (default: the first
                                        one that isn't nvidia_*)
  PPT_AC / PPT_USBC / PPT_WEAK_BARREL     "SPL SPPT FPPT" in W for that source
  BOOST_AC / BOOST_USBC / BOOST_WEAK_BARREL
                                        NVIDIA Dynamic Boost (W) for that source
A USB-C charger counts as battery: barrel = full power, USB-C and battery
save. A barrel that drops out 3 or more times in 30 s (counted on UPower's
line-power device: a flapping adapter doesn't flip OnBattery) is a weak barrel (a
third-party adapter that can't carry the load): it keeps the AC settings and
gets the PPT_WEAK_BARREL limits until it's unplugged for good. Power-source
changes apply once the source has been stable for 1.5 s.

The PPT limits only take effect while the CPU custom fan curve is on (BIOS
307 measured: a limit written in auto fan mode is stored but ignored, and
applies the moment the curve is enabled). So a source with limits enables
asusd's stored CPU curve for the current profile and writes the limits,
clamped to the live firmware range. asusd only applies a PPT write while
its tuning group for the current (power state, profile) is on, so that is
turned on first (EnablePptGroup); turning it off makes asusd re-apply the
profile, which resets the limits. Leaving the source turns the curve off and
the group off where it's still current. A group left on by an unplug (the
power state has changed) is turned off the next time it's current; until
then the curve is off, and the firmware ignores stored limits without it.
The curves and groups it turned on are kept in ~/.local/state/omarchy
(omarchy-asus-ppt-curves, omarchy-asus-ppt-groups), so a crash can't leave either on. UPower reports both chargers as AC; asus-armoury's charge_mode tells
them apart (bit 0 barrel, bit 1 USB-C).
A key that is absent is left alone: with no file nothing is enforced. The
refresh pin (~/.local/state/omarchy/omarchy-asus-hz, an integer) wins over
gaming mode (omarchy-asus-gaming exists), which wins over the power source.

Only the Python standard library and PyGObject's Gio/GLib are used. Nothing
here spawns a process or uses a shell. `asus_bridge.py gaming on|off|toggle`
and `asus_bridge.py hz <N>|auto` only write the state files and exit; the
running bridge sees the change and applies it.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import errno  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import stat  # noqa: E402

# Local files and inotify only: never start gvfs (a session-bus daemon) for
# the file watches below.
os.environ["GIO_USE_VFS"] = "local"
os.environ["GIO_USE_VOLUME_MONITOR"] = "unix"

import gi  # noqa: E402

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

ASUSD = "xyz.ljones.Asusd"
AURA = "xyz.ljones.Aura"
SLASH = "xyz.ljones.Slash"
UPOWER = "org.freedesktop.UPower"
UPOWER_DEVICE = "org.freedesktop.UPower.Device"
LINE_POWER = 1  # UPower device type
PROPS = "org.freedesktop.DBus.Properties"
SYSTEMD = "org.freedesktop.systemd1"
LEVELS = ["off", "low", "med", "high"]
# The keyboard lights draw about 2 W at high, so on battery they are off
# unless the settings say otherwise.
LEVEL_DEFAULTS = {"KBD_BATTERY": "off"}
SLASH_MODES = ["Static", "Bounce", "Slash", "Loading", "BitStream", "Transmission",
               "Flow", "Flux", "Phantom", "Spectrum", "Hazard", "Interfacing",
               "Ramp", "GameOver", "Start", "Buzzer"]
# asusd's Mode property takes rog-slash's SlashMode codes (asusctl
# rog-slash/src/data.rs), in the order `asusctl slash list` prints them.
SLASH_CODES = [0x06, 0x10, 0x12, 0x13, 0x1D, 0x1A, 0x19, 0x25, 0x24, 0x26,
               0x32, 0x33, 0x34, 0x42, 0x43, 0x44]
SLASH_FLAGS = ["ShowOnBoot", "ShowOnShutdown", "ShowOnSleep", "ShowOnBattery",
               "ShowBatteryWarning", "ShowOnLidClosed"]
CHARGE_PROP = "ChargeControlEndThreshold"
OVERDRIVE_NAME = "panel_overdrive"
WINLOCK_BIN = "/usr/bin/g16-winlock"
WINLOCK_UNIT = "g16-winlock.service"
MAX_LINE = 4096
HYPR_MAX = 1024 * 1024
UNIT_STATES = {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance"}
CONF_KEYS = {"PPT_AC", "PPT_USBC", "PPT_WEAK_BARREL", "BOOST_AC", "BOOST_USBC",
             "BOOST_WEAK_BARREL", "KBD_AC", "KBD_BATTERY", "HZ_AC", "HZ_BATTERY", "HZ_GAMING",
             "PANEL_OVERDRIVE_AC", "PANEL_OVERDRIVE_BATTERY", "INTERNAL_OUTPUT",
             "BITDEPTH", "BRIGHT_BATTERY", "BRIGHT_DEVICE"}
LOGIND = "org.freedesktop.login1"
PLATFORM = "xyz.ljones.Platform"
FANCURVES = "xyz.ljones.FanCurves"
PPT_ATTRS = ["ppt_pl1_spl", "ppt_pl2_sppt", "ppt_pl3_fppt"]
BOOST_ATTR = "nv_dynamic_boost"
DEBOUNCE_MS = 1500
WEAK_EDGES, WEAK_WINDOW, WEAK_CLEAR_S = 3, 30.0, 10


def env(name, default=""):
    v = os.environ.get(name, "")
    return v if v else default


HOME = env("HOME", "/")
CONFIG_HOME = env("XDG_CONFIG_HOME", os.path.join(HOME, ".config"))
STATE_HOME = env("XDG_STATE_HOME", os.path.join(HOME, ".local", "state"))
CONF_NAME = "omarchy-asus.conf"
CONF_PATH = os.path.join(CONFIG_HOME, "omarchy", CONF_NAME)
STATE_DIR = os.path.join(STATE_HOME, "omarchy")
PIN_NAME, GAMING_NAME = "omarchy-asus-hz", "omarchy-asus-gaming"
HZ_PIN = os.path.join(STATE_DIR, PIN_NAME)
GAMING_FLAG = os.path.join(STATE_DIR, GAMING_NAME)
WINLOCK_DROPIN = os.path.join(CONFIG_HOME, "systemd", "user", WINLOCK_UNIT + ".d")
WINLOCK_MODE_CONF = os.path.join(WINLOCK_DROPIN, "mode.conf")
# The backlight step's edge-trigger state: the source class last applied, and
# the barrel backlight level saved when leaving it.
SEEN_NAME, SAVED_NAME = "omarchy-asus.source", "omarchy-asus.bright-ac"
CURVES_NAME, GROUPS_NAME = "omarchy-asus-ppt-curves", "omarchy-asus-ppt-groups"
# The names these files had before 1.6.0, renamed once (migrate_names).
OLD_CONF_NAMES = {"power-tune.conf": CONF_NAME}
OLD_STATE_NAMES = {"power-tune-hz": PIN_NAME, "power-tune-gaming": GAMING_NAME,
                   "power-tune.source": SEEN_NAME, "power-tune.bright-ac": SAVED_NAME,
                   "g16-ppt-curves": CURVES_NAME, "g16-ppt-groups": GROUPS_NAME}
# Tests point these at a temp tree; on a real system they are the defaults.
SYSROOT = env("G16_BRIDGE_SYSROOT", "/sys")
WINLOCK_PATH = env("G16_BRIDGE_WINLOCK", WINLOCK_BIN)


def log(msg):
    # Bounded and printable: the shell forwards these lines to its journal.
    text = re.sub(r"[^\x20-\x7e]", "?", str(msg))[:300]
    print(f"omarchy-asus: {text}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ safe files
# Every file this bridge writes is in the user's own XDG config or state dir.
# A planted symbolic link must never redirect a write:
#   - the base dir (the XDG config or state home) is walked from / one
#     component at a time with O_NOFOLLOW: a symbolic link anywhere on the way
#     is refused, and each step must be ours or a root-owned system dir;
#   - each dir below it is opened (or created) relative to that descriptor
#     with O_NOFOLLOW, and must be owned by us too;
#   - a write goes to a new private temporary file (O_CREAT|O_EXCL|O_NOFOLLOW,
#     random name) in that dir, is fsynced and renamed over the target; a
#     target that is a symbolic link is refused, not followed;
#   - reads are opened with O_NOFOLLOW, descriptor-checked (a regular file of
#     ours) and size-capped; a symbolic link is not read, and says so.
UID = os.getuid()
O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_FILE = 64 * 1024
MAX_LINE = 60 * 1024


class UnsafePath(OSError):
    pass


def _check_owner(fd, what):
    st = os.fstat(fd)
    if st.st_uid != UID:
        raise UnsafePath(f"{what} is not owned by this user")
    return st


class Dir:
    """A verified directory descriptor: base/parts..., created if missing."""

    def __init__(self, base, parts, create=True):
        # The base ($HOME, $XDG_*) is walked from / one component at a time,
        # never through a symbolic link: each step must be this user's, or
        # root's and not writable by others (sticky dirs such as /tmp aside).
        # Missing steps are created only inside a directory of ours.
        if not os.path.isabs(base):
            raise UnsafePath(f"{base} is not an absolute path")
        fd = os.open("/", O_DIR)
        try:
            mine = False
            for part in base.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    raise UnsafePath(f"{base} contains '..'")
                if create and mine:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    nfd = os.open(part, O_DIR, dir_fd=fd)
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        raise UnsafePath(f"{base}: {part} is a symbolic link") from None
                    raise
                os.close(fd)
                fd = nfd
                st = os.fstat(fd)
                mine = st.st_uid == UID
                if not mine and (st.st_uid != 0 or (st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX)):
                    raise UnsafePath(f"{base}: {part} is neither ours nor a safe system directory")
            _check_owner(fd, base)
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                nfd = os.open(part, O_DIR, dir_fd=fd)
                os.close(fd)
                fd = nfd
                _check_owner(fd, part)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def read(self, name):
        """The file's text, or None if it's missing, a symbolic link, not a
        regular file of ours, or larger than MAX_FILE."""
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=self.fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                log(f"{name} is a symbolic link; not read (put the file itself there)")
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != UID:
                return None
            data = os.read(fd, MAX_FILE + 1)
            if len(data) > MAX_FILE:
                log(f"{name} is larger than {MAX_FILE} bytes; ignored")
                return None
            return data.decode("utf-8", errors="replace")
        finally:
            os.close(fd)

    def exists(self, name):
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def is_file(self, name):
        try:
            return stat.S_ISREG(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except OSError:
            return False

    def write(self, name, text, mode=0o600):
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != UID:
                raise UnsafePath(f"{name} is a symbolic link or not a regular file of ours; not replacing it")
            mode = stat.S_IMODE(st.st_mode)
        except FileNotFoundError:
            pass
        tmp = f".{name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode, dir_fd=self.fd)
        try:
            data = text.encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except OSError:
                pass
            raise

    def remove(self, name):
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass


def config_dir(*parts, create=True):
    return Dir(CONFIG_HOME, ["omarchy", *parts], create)


def state_dir(create=True):
    return Dir(STATE_HOME, ["omarchy"], create)


def dropin_dir(create=True):
    return Dir(CONFIG_HOME, ["systemd", "user", WINLOCK_UNIT + ".d"], create)


def state_read(name):
    try:
        with state_dir(create=False) as d:
            return (d.read(name) or "").strip()
    except OSError:
        return ""


def state_write(name, text):
    with state_dir() as d:
        d.write(name, text + "\n")


def state_remove(name):
    try:
        with state_dir(create=False) as d:
            d.remove(name)
    except OSError:
        pass


def state_int(name):
    v = re.sub(r"[^0-9]", "", state_read(name))[:6]
    return int(v) if v else None


# ------------------------------------------------------------------ files
def read_conf():
    """KEY=VALUE lines; quotes stripped; anything else ignored (never sourced).
    Values are short tokens: anything longer than 64 characters is dropped."""
    out = {}
    try:
        with config_dir(create=False) as d:
            text = d.read(CONF_NAME) or ""
    except OSError:
        return out
    for line in text.splitlines():
        if len(line) > 512:
            continue
        m = re.match(r"^\s*([A-Z_]+)=([\"']?)(.*?)\2\s*(?:#.*)?$", line)
        if m and m.group(1) in CONF_KEYS and len(m.group(3)) <= 64:
            out[m.group(1)] = m.group(3).strip()
    return out


def write_conf_key(key, value):
    """Rewrite KEY in place, or append it; other lines are kept as they are."""
    if key not in CONF_KEYS or not re.fullmatch(r"[A-Za-z0-9_.:\- ]{0,64}", value):
        raise ValueError(f"bad conf value for {key}")
    with config_dir() as d:
        text = d.read(CONF_NAME)
        if text is None and d.is_file(CONF_NAME):
            raise OSError("the settings file can't be read")
        lines = text.splitlines() if text is not None else [
            "# Per power source settings for the omarchy-asus plugin.",
            "# A missing key is left alone."]
        done = False
        for i, line in enumerate(lines):
            if re.match(rf"^\s*{key}=", line):
                lines[i] = f"{key}={value}"
                done = True
        if not done:
            lines.append(f"{key}={value}")
        d.write(CONF_NAME, "\n".join(lines) + "\n", mode=0o644)


def read_int_file(path):
    """A small integer from a kernel (sysfs) attribute."""
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            v = re.sub(r"[^0-9]", "", f.read(32))[:9]
        return int(v) if v else None
    except OSError:
        return None


def pinned_hz():
    return state_int(PIN_NAME)


def gaming_on():
    try:
        with state_dir(create=False) as d:
            return d.is_file(GAMING_NAME)
    except OSError:
        return False


def set_pin(value):
    if value == "auto":
        state_remove(PIN_NAME)
    elif re.fullmatch(r"[0-9]{2,3}", value):
        state_write(PIN_NAME, value)
    else:
        raise ValueError(f"bad refresh value: {value}")


def set_gaming(value):
    on = {"on": True, "off": False, "toggle": not gaming_on()}.get(value)
    if on is None:
        raise ValueError(f"bad gaming value: {value}")
    if on:
        state_write(GAMING_NAME, "on")
    else:
        state_remove(GAMING_NAME)


def charge_limit_sysfs():
    base = os.path.join(SYSROOT, "class", "power_supply")
    try:
        names = sorted(n for n in os.listdir(base) if n.startswith("BAT"))
    except OSError:
        return 0
    for n in names:
        v = read_int_file(os.path.join(base, n, "charge_control_end_threshold"))
        if v is not None:
            return v
    return 0


def charge_mode():
    """asus-armoury charge_mode: bit 0 barrel, bit 1 USB-C; 0 until the
    firmware has looked at a new adapter."""
    return read_int_file(os.path.join(SYSROOT, "class", "firmware-attributes", "asus-armoury",
                                      "attributes", "charge_mode", "current_value"))


def backlight_device(conf):
    want = conf.get("BRIGHT_DEVICE", "")
    base = os.path.join(SYSROOT, "class", "backlight")
    if want and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", want):
        return want
    try:
        for n in sorted(os.listdir(base)):
            if not n.startswith("nvidia"):
                return n
    except OSError:
        pass
    return ""


def winlock_mode():
    m = "toast"
    try:
        with dropin_dir(create=False) as d:
            for line in (d.read("mode.conf") or "").splitlines():
                if line.startswith("Environment=G16_WINLOCK_MODE="):
                    m = line.split("=", 2)[2].strip().strip('"')
    except OSError:
        pass
    return m if m in ("toast", "unlock") else "toast"


# ------------------------------------------------------------------ hyprland
class Hyprland:
    """Hyprland's IPC: requests on .socket.sock ("<flags>/<command>", the way
    hyprctl sends them), events on .socket2.sock ("event>>data" lines)."""

    def __init__(self, on_event):
        sig = env("HYPRLAND_INSTANCE_SIGNATURE")
        run = env("XDG_RUNTIME_DIR")
        self.dir = os.path.join(run, "hypr", sig) if sig and run else ""
        self.on_event = on_event
        self.events = None
        self.buf = b""

    @property
    def available(self):
        return bool(self.dir) and os.path.exists(os.path.join(self.dir, ".socket.sock"))

    def request(self, cmd, timeout=2.0):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(os.path.join(self.dir, ".socket.sock"))
            s.sendall(cmd.encode())
            chunks, size = [], 0
            while True:
                d = s.recv(65536)
                if not d:
                    break
                size += len(d)
                if size > HYPR_MAX:
                    raise OSError(f"Hyprland's reply to {cmd.split()[0]} is over {HYPR_MAX} bytes")
                chunks.append(d)
            return b"".join(chunks).decode(errors="replace")
        finally:
            s.close()

    def monitors(self):
        try:
            data = json.loads(self.request("j/monitors"))
            return data if isinstance(data, list) else []
        except OSError as e:
            log(f"Hyprland monitors: {e}")
            return []
        except ValueError:
            return []

    def listen(self):
        if not self.dir or self.events:
            return
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(os.path.join(self.dir, ".socket2.sock"))
        except OSError as e:
            log(f"no Hyprland event socket: {e}")
            return
        s.setblocking(False)
        self.events = s
        GLib.io_add_watch(s.fileno(), GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                          self._readable)

    def _readable(self, _fd, cond):
        try:
            d = self.events.recv(65536)
        except BlockingIOError:
            return True
        except OSError:
            d = b""
        if not d or cond & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self.events.close()
            self.events = None
            GLib.timeout_add_seconds(5, lambda: (self.listen(), False)[1])
            return False
        self.buf += d
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            name = line.split(b">>", 1)[0].decode(errors="replace")
            self.on_event(name)
        if len(self.buf) > MAX_LINE:
            self.buf = b""
        return True


# ------------------------------------------------------------------ bridge
class Bridge:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        try:
            self.session = Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error:
            self.session = None
        self.ack = 0
        self.last_sent = None
        self.emit_pending = False
        self.on_battery = False
        self.kind = "ac"          # ac (barrel) | usbc | battery
        self.kind_tries = 0
        self.kind_timer = 0
        self.edges = []           # monotonic times of charger online/offline edges
        self.weak = False
        self.debounce_timer = 0
        self.weak_clear_timer = 0
        self.armoury = {}         # attribute name -> (path, iface)
        self.platform = None      # path of xyz.ljones.Platform
        self.fancurves = None     # path of xyz.ljones.FanCurves
        self.limits_on = False
        self.line_power = []      # UPower line-power device paths
        self.conf = read_conf()
        self.aura = None      # object path
        self.slash = None
        self.charge = None    # (path, interface)
        self.overdrive = None  # (path, interface, property)
        self.cache = {}       # (path, iface) -> {prop: python value}
        self.prop_names = {}  # (path, iface) -> property names, from introspection
        self.hypr = Hyprland(self.hypr_event)
        self.refresh_hz = 0
        self.output = ""
        self.inbuf = b""
        self.monitors_ = []
        self.apply_timer = 0
        self.file_timer = 0
        self.winlock_state = "inactive"

    # ---- output ----
    def send(self, obj):
        # Bounded here, at the producer: the shell never gets a line over
        # MAX_LINE (it drops longer ones too).
        line = json.dumps(obj, separators=(",", ":"))
        if len(line) > MAX_LINE:
            log(f"a {obj.get('type', '?')} message is over {MAX_LINE} bytes; not sent")
            return
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def state(self):
        aura = self.cache.get((self.aura, AURA), {}) if self.aura else {}
        slash = self.cache.get((self.slash, SLASH), {}) if self.slash else {}
        bright = aura.get("Brightness", 0)
        lmd = aura.get("LedModeData")
        color = "FF00FF"
        if isinstance(lmd, (list, tuple)) and len(lmd) >= 3 and len(lmd[2]) == 3:
            color = "".join(f"{int(c) & 255:02X}" for c in lmd[2])
        power = aura.get("LedPower")
        flags = [True, True, True, True]
        try:
            entry = power[0][0] if isinstance(power[0], (list, tuple)) and power[0] and isinstance(power[0][0], (list, tuple)) else power[0]
            flags = [bool(x) for x in entry[1:5]]
        except (TypeError, IndexError, KeyError):
            pass
        # Only asusd can set the limit, so without it the section hides (0).
        charge = 0
        if self.charge:
            charge = charge_limit_sysfs()
            charge = int(self.cache.get(self.charge, {}).get(CHARGE_PROP, charge) or charge)
        pin = pinned_hz()
        s = {
            "type": "state", "ack": self.ack,
            "onBattery": self.on_battery,
            "source": self.kind,
            "hasAura": bool(self.aura),
            "kbdBrightness": max(0, min(3, int(bright))) if isinstance(bright, int) else 0,
            "kbdColor": color,
            # Only values the panel knows; anything else from the file is "not set".
            "kbdAC": self.level_conf("KBD_AC"), "kbdBattery": self.level_conf("KBD_BATTERY"),
            "kbdBoot": flags[0], "kbdAwake": flags[1], "kbdSleep": flags[2], "kbdShutdown": flags[3],
            "hasSlash": bool(self.slash),
            "slashOn": bool(slash.get("Enabled", False)),
            "slashBrightness": int(slash.get("Brightness", 0) or 0),
            "slashPattern": self.slash_pattern(slash),
            "slashModes": SLASH_MODES,
            "chargeLimit": charge,
            "hasPanel": bool(self.output),
            "refreshHz": self.refresh_hz,
            "refreshOverride": str(pin) if pin else "auto",
            "gaming": gaming_on(),
            "hasOverdrive": bool(self.overdrive),
            "hasWinlock": os.access(WINLOCK_PATH, os.X_OK),
            "winlockMode": winlock_mode(),
            "winlockService": self.winlock_state,
        }
        for f in SLASH_FLAGS:
            s[f[0].lower() + f[1:]] = bool(slash.get(f, False))
        return s

    def level_conf(self, key):
        v = self.conf.get(key, "").lower()
        return v if v in LEVELS else LEVEL_DEFAULTS.get(key, "")

    def emit(self):
        if self.emit_pending:
            return
        self.emit_pending = True

        def go():
            self.emit_pending = False
            s = self.state()
            if s != self.last_sent:
                self.last_sent = s
                self.send(s)
            return False
        GLib.timeout_add(30, go)

    # ---- D-Bus helpers ----
    def call(self, bus, name, path, iface, method, args=None, reply=None, timeout=3000):
        return bus.call_sync(name, path, iface, method, args, reply and GLib.VariantType(reply),
                             Gio.DBusCallFlags.NONE, timeout, None)

    @staticmethod
    def slash_pattern(slash):
        """The saved pattern (Mode, a SlashMode code) when asusd reports one we
        know; otherwise Interval, which is what drives the live pattern."""
        mode = slash.get("Mode")
        if isinstance(mode, int) and mode in SLASH_CODES:
            return SLASH_CODES.index(mode)
        v = slash.get("Interval", 0)
        return int(v) if isinstance(v, int) and 0 <= v < len(SLASH_MODES) else 0

    def get_all(self, path, iface):
        """GetAll, or, if asusd fails it (zbus fails the whole call when one
        getter errors), each property on its own, keeping the ones that read."""
        try:
            v = self.call(self.system, ASUSD, path, PROPS, "GetAll",
                          GLib.Variant("(s)", (iface,)), "(a{sv})")
            self.cache[(path, iface)] = dict(v.unpack()[0])
            return
        except GLib.Error as e:
            log(f"GetAll {path} {iface}: {e.message}; reading properties one by one")
        got = {}
        for name in sorted(self.prop_names.get((path, iface), ())):
            try:
                got[name] = self.raw_prop(path, iface, name).unpack()
            except GLib.Error as e:
                log(f"Get {path} {iface}.{name}: {e.message}")
        self.cache[(path, iface)] = got

    def set_prop(self, path, iface, prop, variant):
        self.call(self.system, ASUSD, path, PROPS, "Set",
                  GLib.Variant("(ssv)", (iface, prop, variant)))

    def raw_prop(self, path, iface, prop):
        v = self.call(self.system, ASUSD, path, PROPS, "Get",
                      GLib.Variant("(ss)", (iface, prop)), "(v)")
        return v.get_child_value(0).get_variant()

    # ---- asusd discovery ----
    def discover(self):
        self.aura = self.slash = self.charge = self.overdrive = None
        self.cache = {}
        self.prop_names = {}
        self.armoury = {}
        self.platform = self.fancurves = None
        seen = 0
        stack = ["/"]
        while stack and seen < 256:
            path = stack.pop()
            seen += 1
            try:
                xml = self.call(self.system, ASUSD, path, "org.freedesktop.DBus.Introspectable",
                                "Introspect", None, "(s)").unpack()[0]
                node = Gio.DBusNodeInfo.new_for_xml(xml)
            except GLib.Error:
                continue
            for child in node.nodes:
                stack.append((path.rstrip("/") + "/" + child.path) if child.path else path)
            for iface in node.interfaces:
                names = {p.name for p in iface.properties}
                self.prop_names[(path, iface.name)] = names
                if iface.name == AURA and not self.aura:
                    self.aura = path
                elif iface.name == SLASH and not self.slash:
                    self.slash = path
                elif CHARGE_PROP in names and not self.charge:
                    self.charge = (path, iface.name)
                if iface.name == PLATFORM and not self.platform:
                    self.platform = path
                if iface.name == FANCURVES and not self.fancurves:
                    self.fancurves = path
                last = path.rstrip("/").rsplit("/", 1)[-1]
                if "CurrentValue" in names and last in PPT_ATTRS + [BOOST_ATTR]:
                    self.armoury[last] = (path, iface.name)
                if iface.name.startswith("xyz.ljones.") and not self.overdrive:
                    self.match_overdrive(path, iface.name, names)
        for key in [(self.aura, AURA), (self.slash, SLASH), self.charge,
                    self.overdrive[:2] if self.overdrive else None]:
            if key and key[0]:
                self.get_all(*key)
        log(f"asusd: aura={self.aura} slash={self.slash} charge={self.charge} overdrive={self.overdrive}")

    def match_overdrive(self, path, iface, names):
        # asus-armoury attributes: one object per attribute, named by its path
        # or a Name property; the value property is CurrentValue.
        if "CurrentValue" not in names:
            return
        if path.rstrip("/").endswith(OVERDRIVE_NAME):
            self.overdrive = (path, iface, "CurrentValue")
            return
        if "Name" in names:
            try:
                if str(self.raw_prop(path, iface, "Name").unpack()) == OVERDRIVE_NAME:
                    self.overdrive = (path, iface, "CurrentValue")
            except GLib.Error:
                pass

    def asusd_appeared(self, *_):
        self.discover()
        self.apply("asusd")
        self.emit()

    def asusd_vanished(self, *_):
        self.aura = self.slash = self.charge = self.overdrive = None
        self.cache = {}
        self.emit()

    def props_changed(self, _conn, _sender, path, _iface, _signal, params):
        iface, changed, invalidated = params.unpack()
        key = (path, iface)
        if path == "/org/freedesktop/UPower" and iface == UPOWER:
            if "OnBattery" in changed and bool(changed["OnBattery"]) != self.on_battery:
                self.on_battery = bool(changed["OnBattery"])
                # The adapter's own edges are counted where there is one.
                self.power_edge(count=not self.line_power)
            self.emit()
            return
        if iface == UPOWER_DEVICE and path in self.line_power:
            if "Online" in changed:
                self.power_edge(count=True)
            return
        if iface == PLATFORM and "PlatformProfile" in changed and self.limits_on:
            # Omarchy switches the profile on a charger change: the curve
            # gate belongs to the new profile now.
            self.apply_limits("profile change")
        if key in self.cache:
            self.cache[key].update(changed)
            if invalidated:
                self.get_all(path, iface)
            self.emit()

    # ---- power source ----
    def read_on_battery(self):
        try:
            v = self.call(self.system, UPOWER, "/org/freedesktop/UPower", PROPS, "Get",
                          GLib.Variant("(ss)", (UPOWER, "OnBattery")), "(v)")
            self.on_battery = bool(v.unpack()[0])
        except GLib.Error as e:
            log(f"UPower: {e.message}")

    # ---- the per-source policy ----
    def source(self):
        """Which settings apply: AC on the barrel (weak or not), BATTERY on
        USB-C and battery."""
        return "AC" if self.kind in ("ac", "weak") else "BATTERY"

    def read_kind(self):
        if self.on_battery:
            return "battery", True
        v = charge_mode()
        if v is None:
            return "ac", True          # no asus-armoury: every charger is AC
        if v == 0:
            return "ac", False         # the firmware hasn't classified it yet
        if not v & 1 and v & 2:
            return "usbc", True
        return ("weak" if self.weak else "ac"), True

    def find_line_power(self):
        """UPower's line-power (AC adapter) devices. Their Online property
        toggles each time a weak adapter drops out, while OnBattery may not."""
        out = []
        try:
            paths = self.call(self.system, UPOWER, "/org/freedesktop/UPower", UPOWER,
                              "EnumerateDevices", None, "(ao)").unpack()[0]
        except GLib.Error as e:
            log(f"UPower devices: {e.message}")
            return out
        for path in paths:
            try:
                t = self.call(self.system, UPOWER, path, PROPS, "Get",
                              GLib.Variant("(ss)", (UPOWER_DEVICE, "Type")), "(v)").unpack()[0]
            except GLib.Error:
                continue
            if t == LINE_POWER:
                out.append(path)
        return out

    def power_edge(self, count=True):
        """A charger went online or offline. Count it toward weak-barrel
        detection, and apply once the source has been stable for 1.5 s, so a
        flapping adapter doesn't re-apply (and modeset) on every edge."""
        now = GLib.get_monotonic_time() / 1e6
        self.edges = [t for t in self.edges if now - t <= WEAK_WINDOW] + ([now] if count else [])
        if not self.on_battery and len(self.edges) >= WEAK_EDGES and not self.weak:
            self.weak = True
            log(f"weak barrel: {len(self.edges)} charger edges in {WEAK_WINDOW:.0f} s")
        if self.weak_clear_timer:
            GLib.source_remove(self.weak_clear_timer)
            self.weak_clear_timer = 0
        if self.on_battery and self.weak:
            # Unplugged for good (10 s on battery) ends the weak-barrel state.
            def clear():
                self.weak_clear_timer = 0
                if self.on_battery:
                    self.weak = False
                    self.edges = []
                    log("weak barrel cleared")
                return False
            self.weak_clear_timer = GLib.timeout_add_seconds(WEAK_CLEAR_S, clear)
        if self.debounce_timer:
            GLib.source_remove(self.debounce_timer)

        def settled():
            self.debounce_timer = 0
            self.update_kind("power source")
            return False
        self.debounce_timer = GLib.timeout_add(DEBOUNCE_MS, settled)

    def update_kind(self, why):
        """Work out the source and apply. The firmware sets charge_mode a
        moment after an adapter comes online, so a 0 is read again every
        250 ms, at most 8 times, after a power event (never while idle)."""
        if self.kind_timer:
            GLib.source_remove(self.kind_timer)
            self.kind_timer = 0
        kind, sure = self.read_kind()
        self.kind = kind
        self.apply(why)
        self.emit()
        if not sure:
            self.kind_tries = 0

            def again():
                self.kind_tries += 1
                k, ok = self.read_kind()
                if ok or self.kind_tries >= 8:
                    self.kind_timer = 0
                    if k != self.kind:
                        self.kind = k
                        self.apply(why + " (charger type)")
                        self.emit()
                    return False
                return True
            self.kind_timer = GLib.timeout_add(250, again)

    def target_hz(self):
        pin = pinned_hz()
        if pin:
            return pin
        if gaming_on() and self.conf.get("HZ_GAMING", "").isdigit():
            return int(self.conf["HZ_GAMING"])
        v = self.conf.get("HZ_" + self.source(), "")
        return int(v) if v.isdigit() else None

    def apply(self, why, refresh_only=False):
        """Apply the settings for the current power source. Keys that are not
        set are left alone, except the keyboard on battery (off). Gaming mode
        uses the AC keyboard and overdrive."""
        src = "AC" if gaming_on() and not pinned_hz() else self.source()
        log(f"apply ({why}): {self.kind}")
        self.apply_refresh()
        if refresh_only:
            return
        self.adjust_brightness()
        level = self.level_conf("KBD_" + src)
        if level and self.aura:
            self.set_level_now(level)
        od = self.conf.get("PANEL_OVERDRIVE_" + src, "")
        if od in ("0", "1") and self.overdrive:
            self.set_overdrive(int(od))
        self.apply_limits(why)

    # ---- per-source power limits (#34) ----
    def wanted_limits(self):
        """(ppt values or None, boost or None) for the current source."""
        key = {"ac": "AC", "weak": "WEAK_BARREL", "usbc": "USBC"}.get(self.kind)
        if not key:
            return None, None
        ppt = self.conf.get("PPT_" + key, "").split()
        ppt = [int(x) for x in ppt] if len(ppt) == 3 and all(x.isdigit() for x in ppt) else None
        b = self.conf.get("BOOST_" + key, "")
        return ppt, (int(b) if b.isdigit() else None)

    def read_curves(self):
        try:
            return [int(x) for x in state_read(CURVES_NAME).split()[:8] if x.isdigit()]
        except ValueError:
            return []

    def set_curve(self, profile, on):
        # Only the CPU fan's curve gates the PPT; asusd persists the flag per
        # profile, so every one we turn on is recorded until it's off again.
        mgr = (self.fancurves, FANCURVES)
        try:
            self.call(self.system, ASUSD, mgr[0], mgr[1], "SetProfileFanCurveEnabled",
                      GLib.Variant("(usb)", (profile, "CPU", on)))
        except GLib.Error as e:
            log(f"CPU fan curve for profile {profile} -> {on}: {e.message}; trying all fans")
            self.call(self.system, ASUSD, mgr[0], mgr[1], "SetFanCurvesEnabled",
                      GLib.Variant("(ub)", (profile, on)))
        had = self.read_curves()
        now = sorted(set(had) | {profile}) if on else [p for p in had if p != profile]
        state_write(CURVES_NAME, " ".join(str(p) for p in now))

    def write_attr(self, name, value, default=False):
        """Write an asus-armoury attribute through asusd, clamped to its live
        range (asusd reports the AC or DC range for the current source)."""
        if name not in self.armoury:
            return
        path, iface = self.armoury[name]
        props = {}
        for p in ("CurrentValue", "MinValue", "MaxValue", "DefaultValue"):
            try:
                props[p] = self.raw_prop(path, iface, p)
            except GLib.Error:
                pass
        if "CurrentValue" not in props:
            return
        lo, hi = (int(props[p].unpack()) if p in props else -1 for p in ("MinValue", "MaxValue"))
        if default:
            value = int(props["DefaultValue"].unpack()) if "DefaultValue" in props else -1
            if value < 0:
                return
        if 0 <= lo <= hi and hi > 0:
            value = max(lo, min(hi, value))
        elif hi == 0 and value != 0:
            return                      # not adjustable on this source (e.g. boost on battery)
        if int(props["CurrentValue"].unpack()) != value:
            self.set_prop(path, iface, "CurrentValue",
                          GLib.Variant(props["CurrentValue"].get_type_string(), value))
            log(f"{name} -> {value}")

    def read_groups(self):
        out = []
        for line in state_read(GROUPS_NAME).splitlines()[:16]:
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("ac", "dc") and parts[1].isdigit():
                out.append((parts[0], int(parts[1])))
        return out

    def write_groups(self, groups):
        state_write(GROUPS_NAME, "\n".join(f"{p} {n}" for p, n in sorted(set(groups))))

    def power_state(self):
        # asusd keys its tuning groups on the AC adapter (USB-C counts).
        return "dc" if self.on_battery else "ac"

    def set_group(self, on):
        self.set_prop(self.platform, PLATFORM, "EnablePptGroup", GLib.Variant("b", on))

    def release_groups(self, profile):
        """Turn off every group we turned on that is current now; the rest wait
        (their curve is off, so their stored limits do nothing meanwhile)."""
        keep = []
        for g in self.read_groups():
            if g == (self.power_state(), profile):
                self.set_group(False)
                log(f"PPT tuning group {g[0]}/{g[1]} off")
            else:
                keep.append(g)
        self.write_groups(keep)

    def apply_limits(self, why):
        if not self.platform or not self.fancurves or not self.armoury:
            return
        ppt, boost = self.wanted_limits()
        try:
            stale = self.read_curves()
            profile = int(self.raw_prop(self.platform, PLATFORM, "PlatformProfile").unpack())
            if ppt is None and boost is None:
                if self.limits_on or stale or self.read_groups():
                    for p in stale:
                        self.set_curve(p, False)
                    self.release_groups(profile)
                    if self.limits_on:
                        log(f"power limits off ({why})")
                    self.limits_on = False
                return
            # Curve first, then the tuning group (asusd applies nothing without
            # it), then our limits, so ours land last.
            for p in stale:
                if p != profile:
                    self.set_curve(p, False)
            if profile not in stale:
                self.set_curve(profile, True)
            if not bool(self.raw_prop(self.platform, PLATFORM, "EnablePptGroup").unpack()):
                # Recorded only when we turn it on: a group the user enabled
                # themselves is theirs, and we never turn it off.
                self.set_group(True)
                self.write_groups(self.read_groups() + [(self.power_state(), profile)])
                log(f"PPT tuning group {self.power_state()}/{profile} on")
            for name, v in zip(PPT_ATTRS, ppt or []):
                self.write_attr(name, v)
            if boost is not None:
                self.write_attr(BOOST_ATTR, boost)
            self.limits_on = True
            log(f"power limits for {self.kind} ({why}): ppt={ppt} boost={boost} profile={profile}")
        except (GLib.Error, OSError) as e:
            log(f"power limits ({why}): {getattr(e, 'message', e)}")

    def adjust_brightness(self):
        """The backlight step, edge-triggered on the source class
        (ac / not ac) against the last apply: leaving the barrel dims to
        BRIGHT_BATTERY % if brighter (once, not a cap); back on the barrel
        the saved level returns unless it has since gone brighter. The first
        apply after a start only records the class."""
        cls = "ac" if self.kind in ("ac", "weak") else "battery"
        prev = state_read(SEEN_NAME)
        if prev == cls:
            return
        try:
            state_write(SEEN_NAME, cls)
        except OSError as e:
            log(f"cannot record the power source: {e}")
        pct = self.conf.get("BRIGHT_BATTERY", "")
        dev = backlight_device(self.conf)
        if not prev or not pct.isdigit() or not dev:
            return
        base = os.path.join(SYSROOT, "class", "backlight", dev)
        cur, top = read_int_file(os.path.join(base, "brightness")), read_int_file(os.path.join(base, "max_brightness"))
        if cur is None or not top:
            log(f"no backlight {dev}")
            return
        if prev == "ac" and cls != "ac":
            if cur * 100 // top > int(pct):
                state_write(SAVED_NAME, str(cur))
                self.set_backlight(dev, top * int(pct) // 100)
        elif prev != "ac" and cls == "ac":
            want = state_int(SAVED_NAME)
            state_remove(SAVED_NAME)
            if want is not None and cur < want:
                self.set_backlight(dev, want)

    def set_backlight(self, dev, value):
        # logind lets the session's user set it; no root, no brightnessctl.
        try:
            self.call(self.system, LOGIND, "/org/freedesktop/login1/session/auto",
                      LOGIND + ".Session", "SetBrightness",
                      GLib.Variant("(ssu)", ("backlight", dev, int(value))))
            log(f"backlight {dev} -> {value}")
        except GLib.Error as e:
            log(f"backlight {dev}: {e.message}")

    def set_level_now(self, level):
        cur = self.cache.get((self.aura, AURA), {}).get("Brightness")
        if cur == LEVELS.index(level):
            return
        try:
            v = self.raw_prop(self.aura, AURA, "Brightness")
            self.set_prop(self.aura, AURA, "Brightness",
                          GLib.Variant(v.get_type_string(), LEVELS.index(level)))
        except GLib.Error as e:
            log(f"keyboard level {level}: {e.message}")

    def set_overdrive(self, value):
        path, iface, prop = self.overdrive
        try:
            cur = self.raw_prop(path, iface, prop)
            if int(cur.unpack()) != value:
                self.set_prop(path, iface, prop, GLib.Variant(cur.get_type_string(), value))
        except GLib.Error as e:
            log(f"panel overdrive {value}: {e.message}")

    # ---- the internal panel's refresh rate ----
    def find_output(self, monitors):
        want = self.conf.get("INTERNAL_OUTPUT", "")
        if want.startswith("desc:"):
            for m in monitors:
                if m.get("description") == want[5:]:
                    return m
        for m in monitors:
            if want and m.get("name") == want:
                return m
        for m in monitors:
            if str(m.get("name", "")).startswith("eDP"):
                return m
        return None

    def read_monitors(self):
        mons = self.hypr.monitors() if self.hypr.available else []
        m = self.find_output(mons)
        self.output = m["name"] if m and re.fullmatch(r"[A-Za-z0-9-]+", str(m.get("name", ""))) else ""
        self.refresh_hz = int(round(float(m.get("refreshRate", 0)))) if m and self.output else 0
        return m

    def apply_refresh(self):
        m = self.read_monitors()
        hz = self.target_hz()
        if not m or not self.output or not hz or hz == self.refresh_hz:
            return
        depth = self.conf.get("BITDEPTH", "")
        if not depth.isdigit():
            depth = "10" if "2101010" in str(m.get("currentFormat", "")) else "8"
        try:
            scale = round(float(m.get("scale", 1)), 6)
            lua = ('hl.monitor({ output = "%s", mode = "%dx%d@%d", position = "%dx%d", scale = %s, bitdepth = %s })'
                   % (self.output, int(m["width"]), int(m["height"]), hz,
                      int(m["x"]), int(m["y"]), scale, depth))
        except (KeyError, TypeError, ValueError):
            return
        prev = self.refresh_hz
        log(f"{self.output} {prev}Hz -> {hz}Hz")
        try:
            self.hypr.request("/eval " + lua)
        except OSError as e:
            log(f"modeset: {e}")
            return

        # Check a second later, and put the old mode back rather than leave
        # the panel dark if the output vanished.
        def verify():
            m2 = self.read_monitors()
            if not m2 and prev:
                log(f"{self.output or 'internal panel'} vanished after the modeset; restoring {prev}Hz")
                try:
                    self.hypr.request("/eval " + lua.replace("@%d\"" % hz, "@%d\"" % prev))
                except OSError:
                    pass
            self.emit()
            return False
        GLib.timeout_add(1000, verify)

    def hypr_event(self, name):
        if name in ("monitoradded", "monitoraddedv2", "monitorremoved", "monitorremovedv2", "configreloaded"):
            # monitors.lua re-applies on these and puts the panel back on its
            # native rate: re-apply ours once things settle.
            if self.apply_timer:
                GLib.source_remove(self.apply_timer)

            def go():
                self.apply_timer = 0
                self.apply("hyprland " + name, refresh_only=True)
                self.emit()
                return False
            self.apply_timer = GLib.timeout_add(800, go)

    # ---- files ----
    def watch_files(self):
        self.monitors_ = []
        # GLib re-checks a watched file whose directory is missing every 4 s;
        # with the directories in place the watches are plain inotify.
        for opener in (config_dir, state_dir, dropin_dir):
            try:
                with opener():
                    pass
            except OSError as e:
                log(f"cannot prepare a settings dir: {e}")
        for path in (CONF_PATH, HZ_PIN, GAMING_FLAG, WINLOCK_MODE_CONF):
            mon = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
            mon.connect("changed", self.file_changed)
            self.monitors_.append(mon)
        led = os.path.join(SYSROOT, "class", "leds", "asus::kbd_backlight", "brightness_hw_changed")
        if os.path.exists(led):
            mon = Gio.File.new_for_path(led).monitor_file(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", self.led_changed)
            self.monitors_.append(mon)

    def file_changed(self, *_):
        if self.file_timer:
            GLib.source_remove(self.file_timer)

        def go():
            self.file_timer = 0
            new = read_conf()
            self.conf = new
            self.apply("settings changed")
            self.emit()
            return False
        self.file_timer = GLib.timeout_add(200, go)

    def led_changed(self, *_):
        led = os.path.join(SYSROOT, "class", "leds", "asus::kbd_backlight", "brightness_hw_changed")
        v = read_int_file(led)
        if v is not None and self.aura:
            self.cache.setdefault((self.aura, AURA), {})["Brightness"] = v
            self.emit()

    # ---- systemd (Win-lock mode) ----
    def unit_state(self):
        if not self.session:
            return "inactive"
        try:
            path = self.call(self.session, SYSTEMD, "/org/freedesktop/systemd1",
                             SYSTEMD + ".Manager", "GetUnit", GLib.Variant("(s)", (WINLOCK_UNIT,)),
                             "(o)", 1000).unpack()[0]
            v = self.call(self.session, SYSTEMD, path, PROPS, "Get",
                          GLib.Variant("(ss)", (SYSTEMD + ".Unit", "ActiveState")), "(v)", 1000)
            state = str(v.unpack()[0])
            return state if state in UNIT_STATES else "inactive"
        except GLib.Error:
            return "inactive"

    def set_winlock_mode(self, mode):
        if mode not in ("toast", "unlock"):
            raise ValueError(f"bad winlock mode: {mode}")
        with dropin_dir() as d:
            d.write("mode.conf", f"[Service]\nEnvironment=G16_WINLOCK_MODE={mode}\n", mode=0o644)
        if not self.session:
            return
        mgr = "/org/freedesktop/systemd1"
        self.call(self.session, SYSTEMD, mgr, SYSTEMD + ".Manager", "Reload", None, None, 10000)
        self.call(self.session, SYSTEMD, mgr, SYSTEMD + ".Manager", "EnableUnitFiles",
                  GLib.Variant("(asbb)", ([WINLOCK_UNIT], False, True)), None, 10000)
        self.call(self.session, SYSTEMD, mgr, SYSTEMD + ".Manager", "RestartUnit",
                  GLib.Variant("(ss)", (WINLOCK_UNIT, "replace")), None, 10000)

    # ---- commands ----
    def need(self, what):
        if not what:
            raise ValueError("no asusd object for this (is asusd running?)")

    def command(self, cmd, args):
        a = [str(x) for x in args]
        if cmd == "get":
            self.last_sent = None
        elif cmd == "kbd-level":
            scope, level = a[0], a[1].lower()
            if scope not in ("ac", "battery", "both") or level not in LEVELS:
                raise ValueError(f"bad kbd-level: {scope} {level}")
            for s in (["AC", "BATTERY"] if scope == "both" else [scope.upper()]):
                write_conf_key("KBD_" + s, level)
                self.conf["KBD_" + s] = level
            if scope == "both" or scope.upper() == self.source():
                self.need(self.aura)
                self.set_level_now(level)
        elif cmd == "kbd-color":
            self.need(self.aura)
            hexv = a[0].lstrip("#")
            if not re.fullmatch(r"[0-9A-Fa-f]{6}", hexv):
                raise ValueError(f"bad colour: {a[0]}")
            v = self.raw_prop(self.aura, AURA, "LedModeData")
            mode, zone, _c1, c2, speed, direction = v.unpack()
            rgb = tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))
            self.set_prop(self.aura, AURA, "LedModeData",
                          GLib.Variant(v.get_type_string(), (mode, zone, rgb, c2, speed, direction)))
        elif cmd == "kbd-power":
            self.need(self.aura)
            idx = {"boot": 1, "awake": 2, "sleep": 3, "shutdown": 4}.get(a[0])
            if idx is None or a[1] not in ("true", "false"):
                raise ValueError(f"bad kbd-power: {a}")
            v = self.raw_prop(self.aura, AURA, "LedPower")
            val = v.unpack()
            # (a(ubbbb)): one struct holding the per-zone list.
            outer = isinstance(val, tuple) and len(val) == 1 and isinstance(val[0], list)
            states = [list(x) for x in (val[0] if outer else val)]
            if not states:
                raise ValueError("asusd reported no LED power states")
            states[0][idx] = a[1] == "true"
            new = [tuple(x) for x in states]
            self.set_prop(self.aura, AURA, "LedPower",
                          GLib.Variant(v.get_type_string(), (new,) if outer else new))
        elif cmd in ("slash-enabled", "slash-brightness", "slash-flag", "slash-pattern"):
            self.need(self.slash)
            if cmd == "slash-enabled":
                prop, raw = "Enabled", a[0] == "true"
            elif cmd == "slash-brightness":
                prop, raw = "Brightness", max(0, min(255, int(a[0])))
            elif cmd == "slash-flag":
                if a[0] not in SLASH_FLAGS:
                    raise ValueError(f"bad slash flag: {a[0]}")
                prop, raw = a[0], a[1] == "true"
            else:
                prop, raw = "Interval", int(a[0])
                if not 0 <= raw < len(SLASH_MODES):
                    raise ValueError(f"bad slash pattern: {a[0]}")
            v = self.raw_prop(self.slash, SLASH, prop)
            self.set_prop(self.slash, SLASH, prop, GLib.Variant(v.get_type_string(), raw))
            if cmd == "slash-pattern" and "Mode" in self.prop_names.get((self.slash, SLASH), ()):
                # Interval changes the live pattern on this firmware; Mode is
                # what asusd saves (to the device and its config), as
                # `asusctl slash --mode` did for g16ctl.
                m = self.raw_prop(self.slash, SLASH, "Mode")
                self.set_prop(self.slash, SLASH, "Mode", GLib.Variant(m.get_type_string(), SLASH_CODES[raw]))
                self.cache.setdefault((self.slash, SLASH), {})["Mode"] = SLASH_CODES[raw]
        elif cmd == "refresh":
            set_pin(a[0])
            self.apply("refresh pin", refresh_only=True)
        elif cmd == "gaming":
            set_gaming(a[0])
            self.apply("gaming")
        elif cmd == "battery-limit":
            self.need(self.charge)
            n = int(a[0])
            if not 20 <= n <= 100:
                raise ValueError(f"bad charge limit: {n}")
            path, iface = self.charge
            v = self.raw_prop(path, iface, CHARGE_PROP)
            self.set_prop(path, iface, CHARGE_PROP, GLib.Variant(v.get_type_string(), n))
        elif cmd == "winlock-mode":
            self.set_winlock_mode(a[0])
        else:
            raise ValueError(f"unknown command: {cmd}")

    def stdin_readable(self, fd, cond):
        try:
            d = os.read(fd, 65536)
        except OSError:
            d = b""
        if not d:
            # The shell closed our stdin (or went away): stand down.
            self.loop.quit()
            return False
        self.inbuf += d
        while b"\n" in self.inbuf:
            line, self.inbuf = self.inbuf.split(b"\n", 1)
            self.handle_line(line)
        if len(self.inbuf) > MAX_LINE:
            self.inbuf = b""
        return True

    def handle_line(self, line):
        if not line.strip() or len(line) > MAX_LINE:
            return
        try:
            msg = json.loads(line)
            cid = int(msg.get("id", 0))
            cmd = str(msg.get("cmd", ""))
            args = msg.get("args", [])
            if not isinstance(args, list):
                raise ValueError("args must be a list")
        except (ValueError, AttributeError) as e:
            self.send({"type": "reply", "id": 0, "ok": False, "error": f"bad request: {e}"})
            return
        try:
            self.command(cmd, args)
            ok, err = True, ""
        except (ValueError, IndexError, GLib.Error, OSError) as e:
            ok, err = False, getattr(e, "message", None) or str(e)
            log(f"{cmd} {args}: {err}")
        self.ack = max(self.ack, cid)
        self.send({"type": "reply", "id": cid, "ok": ok, "error": err})
        if cmd in ("get", "winlock-mode"):
            # Not reported back over D-Bus: re-read now.
            self.read_monitors()
            self.winlock_state = self.unit_state()
        self.emit()

    # ---- run ----
    def run(self):
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, s, lambda *_: (self.loop.quit(), False)[1])
        # Only asusd's signals, and UPower's daemon object (not its battery
        # devices, which change every few seconds): with no sender filter every
        # PropertiesChanged on the system bus would wake the bridge.
        self.system.signal_subscribe(ASUSD, PROPS, "PropertiesChanged", None, None,
                                     Gio.DBusSignalFlags.NONE, self.props_changed)
        self.system.signal_subscribe(UPOWER, PROPS, "PropertiesChanged", "/org/freedesktop/UPower",
                                     None, Gio.DBusSignalFlags.NONE, self.props_changed)
        # The AC adapter devices one by one: they change only on plug events.
        self.line_power = self.find_line_power()
        for path in self.line_power:
            self.system.signal_subscribe(UPOWER, PROPS, "PropertiesChanged", path, None,
                                         Gio.DBusSignalFlags.NONE, self.props_changed)
        self.read_on_battery()
        Gio.bus_watch_name_on_connection(self.system, ASUSD, Gio.BusNameWatcherFlags.NONE,
                                         self.asusd_appeared, self.asusd_vanished)
        self.watch_files()
        self.hypr.listen()
        self.read_monitors()
        self.winlock_state = self.unit_state()
        self.update_kind("start")
        GLib.io_add_watch(sys.stdin.fileno(), GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                          self.stdin_readable)
        self.emit()
        self.loop.run()


def cli(argv):
    """The commands people bind to keys; the running bridge
    notices the state file change and applies it."""
    if argv[0] == "gaming":
        set_gaming(argv[1] if len(argv) > 1 else "toggle")
        print("gaming:", "on" if gaming_on() else "off")
    elif argv[0] == "hz":
        if len(argv) > 1 and argv[1] != "get":
            set_pin(argv[1])
        pin = pinned_hz()
        print("hz override:", pin if pin else "auto")
    elif argv[0] == "release":
        # Before removing the plugin: turn off every fan curve and PPT tuning
        # group the bridge turned on, so no power limit outlives it.
        b = Bridge()
        b.read_on_battery()
        b.discover()
        b.conf, b.limits_on = {}, True
        b.apply_limits("release")
        left = b.read_groups()
        if left:
            print("still on (for another power state or profile; inert while their fan curve is off):",
                  " ".join(f"{p}/{n}" for p, n in left))
        else:
            print("released: no fan curve or PPT tuning group left on")
    else:
        print("usage: asus_bridge.py [gaming on|off|toggle | hz <N>|auto|get | release]", file=sys.stderr)
        return 2
    return 0


def migrate_names():
    """Rename the settings and state files from their pre-1.6.0 names, once:
    only where the old name exists and the new one doesn't. A rename never
    follows a symbolic link (a linked file is moved as a link, then refused)."""
    for opener, names in ((config_dir, OLD_CONF_NAMES), (state_dir, OLD_STATE_NAMES)):
        try:
            with opener(create=False) as d:
                for old, new in names.items():
                    if d.exists(old) and not d.exists(new):
                        os.rename(old, new, src_dir_fd=d.fd, dst_dir_fd=d.fd)
                        log(f"renamed {old} to {new}")
        except OSError:
            pass


def main():
    migrate_names()
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    try:
        Bridge().run()
    except Exception as e:  # one bounded line, not a traceback
        log(f"bridge stopped: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
