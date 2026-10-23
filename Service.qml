import QtQuick
import Quickshell
import Quickshell.Io

// Shared state for the omarchy-asus plugin (io.github.omarchy-asus.panel). The shell mounts
// this once (manifest kind "service"); every bar's widget reads it, so two
// monitors still mean one bridge.
//
// Everything that touches the machine lives in one Python process,
// bridge/asus_bridge.py (standard library plus PyGObject's Gio, both on stock
// Omarchy). It follows asusd, UPower, Hyprland and the settings files by
// their own events and pushes a JSON "state" line whenever something
// changes; commands go to it as JSON lines on stdin. It also applies the
// per-power-source settings (keyboard level, the internal panel's refresh
// rate, panel overdrive) from ~/.config/omarchy/omarchy-asus.conf. Nothing
// here runs on a timer.
//
// Every launch is an absolute path with an argv array and a closed,
// allow-listed environment.
Item {
  id: svc
  visible: false

  // Set by the shell for service plugins; unused, declared so assignment works.
  property var shell: null
  property var manifest: null

  // ---- injectable for headless tests ----
  property string python3Bin: "/usr/bin/python3"
  property string bridgePath: decodeURIComponent(Qt.resolvedUrl("bridge/asus_bridge.py").toString().replace(/^file:\/\//, ""))
  // Extra environment for the bridge (tests point it at fake buses and files).
  property var extraEnv: ({})

  // ---- panel-open tracking (every bar's widget reports in) ----
  property int openCount: 0
  readonly property bool panelOpen: openCount > 0
  property bool sliderDragging: false

  property bool ready: false

  // ---- keyboard ----
  property bool hasAura: false
  property int kbdBrightness: 0
  property string kbdColor: "FF00FF"
  // Per power source; "" means not set (the bridge leaves it alone).
  property string kbdAC: ""
  property string kbdBattery: ""
  property bool onBattery: false
  // ac (barrel) | weak (a barrel that keeps dropping out) | usbc | battery.
  // USB-C uses the battery settings; a weak barrel the AC ones.
  property string source: "ac"
  readonly property bool batterySettings: source === "usbc" || source === "battery"
  property bool kbdBoot: true
  property bool kbdAwake: true
  property bool kbdSleep: true
  property bool kbdShutdown: true

  // ---- slash ----
  property bool hasSlash: false
  property bool slashOn: false
  property int slashBrightness: 0
  property int slashPattern: 0
  readonly property string slashPatternName: slashPattern >= 0 && slashPattern < slashModes.length
    ? String(slashModes[slashPattern]) : ""
  property bool showOnBoot: false
  property bool showOnShutdown: false
  property bool showOnSleep: false
  property bool showOnBattery: false
  property bool showBatteryWarning: false
  property bool showOnLidClosed: false
  property var slashModes: ["Static", "Bounce", "Slash", "Loading", "BitStream",
                            "Transmission", "Flow", "Flux", "Phantom", "Spectrum",
                            "Hazard", "Interfacing", "Ramp", "GameOver", "Start", "Buzzer"]

  // ---- display: the internal panel (0 when there is none) ----
  property int refreshHz: 0
  property string refreshOverride: "auto"
  property bool gaming: false

  // ---- battery ----
  property int chargeLimit: 0

  // ---- Win-lock ----
  property bool hasWinlock: false
  property string winlockMode: "toast"
  property string winlockService: "inactive"

  readonly property var levelOptions: ["off", "low", "med", "high"]

  // ---------------------------------------------------------------- bridge
  property int _nextId: 1
  property int _lastSent: 0
  property string _buf: ""
  property int _backoff: 2000
  property real _startedAt: 0

  // Allowlist only: clearEnvironment drops PYTHON*, LD_* and ambient PATH
  // before /usr/bin/python3 starts.
  function launchEnvironment() {
    var env = { PATH: "/usr/bin:/bin", HOME: Quickshell.env("HOME") || "",
                LANG: Quickshell.env("LANG") || "C.UTF-8" }
    var pass = ["USER", "LOGNAME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                "HYPRLAND_INSTANCE_SIGNATURE", "DBUS_SYSTEM_BUS_ADDRESS",
                "DBUS_SESSION_BUS_ADDRESS", "LC_ALL", "LC_CTYPE"]
    for (var i = 0; i < pass.length; i++) {
      var v = Quickshell.env(pass[i])
      if (v !== undefined && v !== null && String(v) !== "") env[pass[i]] = String(v)
    }
    for (var k in svc.extraEnv) env[k] = String(svc.extraEnv[k])
    return env
  }

  function startBridge() {
    if (bridge.running) return
    svc._buf = ""
    // A new bridge counts acks from 0 again: waiting for one past our last
    // command would drop every state it sends until the next click.
    svc._lastSent = 0
    bridge.environment = svc.launchEnvironment()
    bridge.running = true
  }

  function send(cmd, args) {
    if (!bridge.running) return
    var id = svc._nextId++
    svc._lastSent = id
    bridge.write(JSON.stringify({ id: id, cmd: cmd, args: args || [] }) + "\n")
  }

  function handleLine(line) {
    if (line.length > 65536) return
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (!msg || msg.type !== "state") return
    svc.ready = true
    // A state from before our latest command would put the old values back
    // over the optimistic ones; the next state includes the command.
    if ((msg.ack || 0) < svc._lastSent) return
    svc.applyState(msg)
  }

  function applyState(s) {
    svc.onBattery = s.onBattery === true
    svc.source = ["ac", "weak", "usbc", "battery"].indexOf(s.source) >= 0 ? s.source : "ac"
    svc.hasAura = s.hasAura === true
    if (svc.hasAura) {
      svc.kbdBrightness = Number(s.kbdBrightness) || 0
      svc.kbdColor = String(s.kbdColor || "FF00FF")
      svc.kbdBoot = s.kbdBoot === true
      svc.kbdAwake = s.kbdAwake === true
      svc.kbdSleep = s.kbdSleep === true
      svc.kbdShutdown = s.kbdShutdown === true
    }
    svc.kbdAC = String(s.kbdAC || "")
    svc.kbdBattery = String(s.kbdBattery || "")
    svc.hasSlash = s.hasSlash === true
    if (svc.hasSlash) {
      svc.slashOn = s.slashOn === true
      if (!svc.sliderDragging) svc.slashBrightness = Number(s.slashBrightness) || 0
      svc.slashPattern = Number(s.slashPattern) || 0
      svc.showOnBoot = s.showOnBoot === true
      svc.showOnShutdown = s.showOnShutdown === true
      svc.showOnSleep = s.showOnSleep === true
      svc.showOnBattery = s.showOnBattery === true
      svc.showBatteryWarning = s.showBatteryWarning === true
      svc.showOnLidClosed = s.showOnLidClosed === true
    } else {
      svc.slashOn = false
    }
    if (Array.isArray(s.slashModes) && s.slashModes.length > 0) svc.slashModes = s.slashModes
    svc.refreshHz = Number(s.refreshHz) || 0
    svc.refreshOverride = String(s.refreshOverride || "auto")
    svc.gaming = s.gaming === true
    svc.chargeLimit = Number(s.chargeLimit) || 0
    svc.hasWinlock = s.hasWinlock === true
    svc.winlockMode = String(s.winlockMode || "toast")
    svc.winlockService = String(s.winlockService || "inactive")
  }

  // ---------------------------------------------------------------- actions
  function refresh() { send("get") }

  // Optimistic local update: leaving a control on its old value until the
  // bridge reports back reads as a dropped click. A level for the active
  // source (or both) is also the live brightness.
  function setLevel(scope, value) {
    if (scope === "ac") svc.kbdAC = value
    else if (scope === "battery") svc.kbdBattery = value
    else { svc.kbdAC = value; svc.kbdBattery = value }
    if (scope === "both" || scope === (svc.batterySettings ? "battery" : "ac")) {
      var i = svc.levelOptions.indexOf(value)
      if (i >= 0) svc.kbdBrightness = i
    }
    send("kbd-level", [scope, value])
  }
  function toggleKeyboard() { setLevel("both", svc.kbdBrightness === 0 ? "med" : "off") }
  function setColor(hex) { svc.kbdColor = hex; send("kbd-color", [hex]) }
  function setKbdPower(flag, value) {
    if (flag === "boot") svc.kbdBoot = value
    else if (flag === "awake") svc.kbdAwake = value
    else if (flag === "sleep") svc.kbdSleep = value
    else if (flag === "shutdown") svc.kbdShutdown = value
    send("kbd-power", [flag, value ? "true" : "false"])
  }
  function toggleSlash() {
    svc.slashOn = !svc.slashOn
    send("slash-enabled", [svc.slashOn ? "true" : "false"])
  }
  function setSlashPattern(index) {
    if (index < 0 || index >= svc.slashModes.length) return
    svc.slashPattern = index
    send("slash-pattern", [String(index)])
  }
  function applySlashBrightness() { send("slash-brightness", [String(svc.slashBrightness)]) }
  function setSlashFlag(name, value) {
    var prop = ({ ShowOnBoot: "showOnBoot", ShowOnShutdown: "showOnShutdown",
                  ShowOnSleep: "showOnSleep", ShowOnBattery: "showOnBattery",
                  ShowBatteryWarning: "showBatteryWarning",
                  ShowOnLidClosed: "showOnLidClosed" })[name]
    if (prop) svc[prop] = value
    send("slash-flag", [name, value ? "true" : "false"])
  }
  function setRefreshHz(value) { svc.refreshOverride = value; send("refresh", [value]) }
  function setGaming(value) { send("gaming", [value]) }
  function setChargeLimit(value) {
    svc.chargeLimit = parseInt(value) || 0
    send("battery-limit", [String(value)])
  }
  function setWinlockMode(value) { svc.winlockMode = value; send("winlock-mode", [value]) }

  function panelOpened() { svc.openCount += 1 }
  function panelClosed() { svc.openCount = Math.max(0, svc.openCount - 1) }

  Process {
    id: bridge
    command: [svc.python3Bin, "-I", "-B", svc.bridgePath]
    clearEnvironment: true
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { svc.handleLine(line) } }
    // Its log lines ("omarchy-asus: ...") go to the shell's log, so they reach
    // the journal with the rest of the shell's output.
    stderr: SplitParser { onRead: function(line) { console.log(line.slice(0, 500)) } }
    onRunningChanged: if (running) svc._startedAt = Date.now()
    onExited: function(code) {
      svc.ready = false
      // A bridge that ran a while restarts fast; a crash loop backs off to 5 min.
      if (Date.now() - svc._startedAt > 60000) svc._backoff = 2000
      restart.interval = svc._backoff
      restart.start()
      svc._backoff = Math.min(300000, svc._backoff * 2)
    }
  }
  Timer { id: restart; onTriggered: svc.startBridge() }

  Component.onCompleted: startBridge()
}
