# omarchy-asus plugin

**One panel for your ASUS laptop's hardware, in the [Omarchy](https://omarchy.org) bar.**

> From **[omarchy-asus](https://omarchy-asus.github.io)**: ASUS fixes for Omarchy. A signed pacman repo carries the kernel, NVIDIA and Omarchy fixes that make an ASUS laptop work properly: the Fn+Super Win-lock keys, the touchpad, sleep with an HDMI monitor attached, the NVIDIA hibernate hang, USB-C charging profiles and more. This plugin is the control panel. **→ [omarchy-asus.github.io](https://omarchy-asus.github.io)** · [repository](https://github.com/omarchy-asus/omarchy-asus)

Written and tested on the ASUS ROG Zephyrus G16 (GA605). Everything goes through `asusd` and the kernel's ASUS drivers, and **each section hides when your laptop doesn't have it**, so other ASUS models get what they support. The lid Slash LEDs, for example, appear only on models that have them.

## What it does

- **Keyboard backlight:** a level for the barrel charger and one for battery / USB-C, applied on every plug and unplug; colour; and the boot/awake/sleep/shutdown lighting.
- **Lid Slash LEDs:** on/off, brightness, pattern (keyboard navigation: arrows or h/j/k/l, then Enter), and when it shows.
- **Internal panel refresh rate** per power source, a gaming rate, or pinned (60/240/Auto). It's set again after a monitor hotplug or `hyprctl reload`.
- **Screen backlight:** optionally dimmed on leaving the barrel charger, and restored on return.
- **CPU power limits per charger** (a USB-C charger; a weak third-party barrel that keeps dropping out), and panel overdrive per source.
- **Battery charge limit** (60/80/100 %).
- **Fn+Super (Win-lock) mode:** toast or auto-unlock, with the omarchy-asus Win-lock helper.

## Install

```bash
omarchy-plugin-add https://github.com/omarchy-asus/omarchy-asus-plugin --enable
```

Update with `omarchy-plugin-update io.github.omarchy-asus.panel`; the shell reloads it by itself.

**Needs:** stock Omarchy (Python and PyGObject are already there). Optional:
- `asusd` (`asusctl`): the keyboard, Slash, charge limit and power limits. Without it those sections hide.
- the Fn+Super switch: `asus-rog-nkey-winlock` and the Win-lock kernel patch, both from [omarchy-asus](https://omarchy-asus.github.io).

## Remove

```bash
/usr/bin/python3 -I ~/.config/omarchy/plugins/io.github.omarchy-asus.panel/bridge/asus_bridge.py release   # only if you set power limits or the charge hold
omarchy-plugin-remove io.github.omarchy-asus.panel
```

`release` turns off the fan curves and asusd PPT tuning groups the plugin turned on for power limits, so no limit outlives it, and puts back a charge limit a charge hold left behind. The plugin leaves your settings in place; delete them if you want a clean slate:
- `~/.config/omarchy/omarchy-asus.conf`;
- `~/.local/state/omarchy/omarchy-asus*`;
- `~/.config/systemd/user/g16-winlock.service.d/mode.conf`, if you changed the Fn+Super mode.

## What it touches

- **One process:** `/usr/bin/python3 -I -B bridge/asus_bridge.py`, started by the shell with a cleared, allow-listed environment. It spawns nothing and uses no network.
- **D-Bus (system bus):**
  - `asusd`: reads and sets the keyboard, Slash, charge limit, panel overdrive, PPT attributes, CPU fan-curve enable and the PPT tuning group. The last three only for power-limit keys you set.
  - `UPower`: read only.
  - `logind` `SetBrightness`: the screen backlight, only if you set `BRIGHT_BATTERY`.
- **D-Bus (session bus):** the systemd user manager, to reload, enable and restart `g16-winlock.service` when you change the Fn+Super mode.
- **Hyprland's IPC sockets:** the monitor list, `hl.monitor()` for the internal panel's refresh rate, and hotplug/reload events.
- **Files:** it reads and writes only `~/.config/omarchy/omarchy-asus.conf`, `~/.local/state/omarchy/` and the Win-lock drop-in above.
  - Directories are walked from `/` one component at a time without following symbolic links; each must be yours or a root-owned system directory.
  - Writes go to a new private temporary file that is atomically renamed into place.
  - A symbolic link is never read or written through: keep a real `omarchy-asus.conf` (copy it from your dotfiles), and the log says so if it is a link.
- **Nothing runs as root,** and the plugin installs nothing system-wide.

## Per power source: keyboard, refresh rate, panel overdrive

The widget applies these itself on every plug and unplug. They live in
`~/.config/omarchy/omarchy-asus.conf`, as plain `KEY=VALUE` lines (never run as
a script):

| key | values | what |
|---|---|---|
| `KBD_AC`, `KBD_BATTERY` | `off` `low` `med` `high` | keyboard backlight; the panel's "On AC" / "On battery" rows write these. Unset, `KBD_BATTERY` is `off`: the lights draw about 2 W at high, a big share of the idle draw on battery |
| `HZ_AC`, `HZ_BATTERY`, `HZ_GAMING` | Hz | the internal panel's refresh rate |
| `PANEL_OVERDRIVE_AC`, `PANEL_OVERDRIVE_BATTERY` | `0` `1` | asus-armoury panel overdrive, through asusd |
| `INTERNAL_OUTPUT` | `eDP-1`, or `desc:<EDID description>` | which output is the panel (default: the first `eDP`) |
| `BITDEPTH` | `8` `10` | carried on every modeset (default: the panel's current format) |
| `BRIGHT_BATTERY` | % | on leaving the barrel, dim the screen to this if it's brighter (once, not a cap); back on the barrel the old level returns unless you've gone brighter |
| `BRIGHT_DEVICE` | e.g. `amdgpu_bl1` | the backlight for that (default: the first one that isn't `nvidia_*`), set through logind |

| `PPT_AC`, `PPT_USBC`, `PPT_WEAK_BARREL` | `"SPL SPPT FPPT"` in W, e.g. `"25 30 40"` | CPU power limits on that source (asus-armoury, through asusd), clamped to the firmware's range |
| `BOOST_AC`, `BOOST_USBC`, `BOOST_WEAK_BARREL` | W | NVIDIA Dynamic Boost on that source |
| `CHARGE_HOLD_WEAK_BARREL` | `1` | on a weak barrel, hold charging while the charger can't carry the load and charge too (below) |

"AC" means the barrel charger. A USB-C charger gets the battery settings:
UPower calls both chargers AC, so the widget tells them apart by asus-armoury's
`charge_mode` (bit 0 barrel, bit 1 USB-C). A barrel that drops out three or
more times in 30 s (a third-party adapter that can't carry the load) is a
**weak barrel**: it keeps the AC settings and gets `PPT_WEAK_BARREL` until it's
unplugged for good. Power-source changes apply once the source has been stable
for 1.5 s, so a flapping adapter doesn't flip the refresh rate back and forth.

**Holding charging on a weak barrel** (`CHARGE_HOLD_WEAK_BARREL=1`). Heavy load
plus charging asks more of a weak barrel than it gives: it drops out about once
a second and the EC clamps the CPU (on the G16 with a 140 W barrel, to 735 MHz
while gaming). With this key, each time a weak barrel comes back online the
widget sets the charge limit to the current charge, which stops charging; the
panel says so, and your limit is kept in `~/.local/state/omarchy` and put back
after a crash. While charging is held the widget follows the battery: if the
charge falls under load the ceiling follows it down, and while the battery is
discharging on AC the hold stays. With charging held, the battery reads the same
under heavy and light load, so the widget tries charging again after two quiet
minutes; if the barrel drops out again it holds again and waits twice as long
next time (up to 30 minutes). Unplugging or removing the key puts your limit
back; a limit set elsewhere (`asusctl battery limit`) replaces it and ends the
hold. A limit picked in the panel while charging is held is the one that comes
back.

**Power limits and the fan curve.** On the GA605KM (BIOS 307) the firmware only
applies PPT limits while the CPU custom fan curve is on; in auto fan mode a
written limit is stored but ignored. So on a source with limits the widget
enables asusd's stored CPU curve for the current profile, then writes the
limits; leaving it turns the curve off and writes the defaults back. The fans
follow that curve while limits apply. The values the G16 used with rog-power:
`PPT_USBC="25 30 40"`, `PPT_WEAK_BARREL="45 55 65"`, `BOOST_WEAK_BARREL=5`
(the OEM barrel left at stock).

A key that isn't set is left alone, so with no file nothing is enforced except
the keyboard lights going off on battery (`KBD_BATTERY`). The
refresh pin (the panel's 60/240/Auto switch, stored in
`~/.local/state/omarchy/omarchy-asus-hz`) wins over gaming mode
(`omarchy-asus-gaming`), which wins over the power source. For a key binding:

```bash
/usr/bin/python3 -I ~/.config/omarchy/plugins/io.github.omarchy-asus.panel/bridge/asus_bridge.py gaming toggle
/usr/bin/python3 -I ~/.config/omarchy/plugins/io.github.omarchy-asus.panel/bridge/asus_bridge.py hz 60   # or: hz auto
```

A monitor hotplug or `hyprctl reload` re-applies `monitors.lua`, which puts the
panel back on its native rate; the widget sets the rate again after both.

The widget also works without asusd: the keyboard, Slash, charge limit and
power limit sections hide, and the refresh rate, screen dimming and Fn+Super
switch stay.

## How it works: one Python bridge, nothing polls

`Service.qml` (manifest kind `service`) holds the widget's state once, however
many bars show it. Everything that touches the machine is one process,
`bridge/asus_bridge.py`. It uses only the Python standard library and
PyGObject's Gio, both on stock Omarchy, and it spawns nothing. It talks to:
- asusd and UPower over D-Bus, and follows their `PropertiesChanged` signals;
- Hyprland over its IPC sockets, including the event socket;
- the keyboard LED's `brightness_hw_changed` (Fn+F2/F3) and the settings
  files, through inotify.

It sends a JSON state line whenever something changes and takes commands on
stdin. Under a shell that doesn't hand out services, each widget starts its own
copy after 10 s.


## Tests

```bash
tests/bridge_test.py      # the bridge against fake asusd, UPower, logind and systemd on private D-Bus buses, and a fake Hyprland
node tests/cursor.test.js # the Slash pattern grid's keyboard cursor
tests/service.test.sh     # Service.qml headless in quickshell (skips without qs)
```

## License

MIT. Part of [omarchy-asus](https://omarchy-asus.github.io).
