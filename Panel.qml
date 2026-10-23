import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The omarchy-asus plugin: one control surface for an ASUS laptop's hardware.
// Written on the ROG Zephyrus G16 (GA605); every section hides when the laptop
// (or asusd) doesn't have it, so other ASUS models get what they support.
// For the LEDs, that's why
// this widget replaces the older Slash-only one rather than sitting beside it.
// The machine has exactly two user-controllable lights, and they are reached
// very differently:
//
//   Keyboard backlight -- a real kernel LED (/sys/class/leds/asus::kbd_backlight)
//     owned by asusd's xyz.ljones.Aura interface. Brightness is set per power
//     source (KBD_AC/KBD_BATTERY in omarchy-asus.conf; battery defaults to off,
//     about 2 W saved): the bridge re-applies
//     the level for the current source on every plug and unplug, which is why
//     it is presented as two levels rather than one slider. Colour and
//     power-state flags go straight to asusd.
//
//   Slash bar (lid) -- not a kernel LED at all; nothing for it appears under
//     /sys/class/leds. It is asusd's xyz.ljones.Slash interface on the same
//     object as Aura.
//
// The hardware reports SupportedBasicModes = [Static] and SupportedBasicZones =
// [], so the keyboard is single-zone, static-colour only. No breathe/rainbow
// section is offered because this keyboard cannot do them, whatever
// `asusctl aura effect` advertises.
Panel {
  id: root
  moduleName: "io.github.omarchy-asus.panel"
  ipcTarget: "io.github.omarchy-asus.panel"

  // All state and every process live in Service.qml, which the shell mounts
  // once (manifest kind "service"). With one bar per monitor this widget used
  // to exist twice, each copy running `g16ctl get` (~135 forks) every 10s.
  // Under a bar that does not hand out services, a local copy is started
  // after a grace period instead, so the widget still works on its own.
  // The service registers asynchronously; re-ask for it for a while.
  property int serviceTick: 0
  readonly property var svc: {
    var tick = root.serviceTick
    var host = root.bar ? root.bar.shell : null
    return host && typeof host.serviceFor === "function" ? host.serviceFor("io.github.omarchy-asus.panel") : null
  }
  readonly property var g: root.svc ? root.svc : localSvc.item

  Timer {
    interval: 500
    repeat: true
    running: root.svc === null && !localSvc.active
    onTriggered: {
      root.serviceTick++
      if (root.serviceTick >= 20 && root.svc === null) localSvc.active = true
    }
  }
  Loader {
    id: localSvc
    active: false
    visible: false
    source: "Service.qml"
  }
  onSvcChanged: if (root.svc && localSvc.active) {
    if (root.reportedTo === localSvc.item) { root.reportedOpen = false; root.reportedTo = null }
    localSvc.active = false
    root.syncOpen()
  }

  // Which service this widget told it is open, so a close always reaches the
  // same object even if the service is swapped underneath.
  property bool reportedOpen: false
  property var reportedTo: null
  function syncOpen() {
    var want = root.opened && !!root.g
    if (want === root.reportedOpen && (!want || root.reportedTo === root.g)) return
    if (root.reportedOpen && root.reportedTo) root.reportedTo.panelClosed()
    root.reportedOpen = false
    root.reportedTo = null
    if (want) { root.g.panelOpened(); root.reportedOpen = true; root.reportedTo = root.g }
  }
  onOpenedChanged: { syncOpen(); root.slashCursor = -1 }
  onGChanged: syncOpen()
  Component.onDestruction: if (root.reportedOpen && root.reportedTo) root.reportedTo.panelClosed()

  // ---- keyboard ----
  readonly property bool hasAura: g ? g.hasAura : false
  readonly property int kbdBrightness: g ? g.kbdBrightness : 0
  readonly property string kbdColor: g ? g.kbdColor : "FF00FF"
  // "" when not set: the bridge then leaves the level alone on that source.
  readonly property string kbdAC: g ? g.kbdAC : ""
  readonly property string kbdBattery: g ? g.kbdBattery : ""
  readonly property bool onBattery: g ? g.onBattery : false
  // USB-C chargers use the battery settings; the rows say which one is live.
  readonly property bool batterySettings: g ? g.batterySettings : false
  readonly property string powerSource: g ? g.source : "ac"
  readonly property bool kbdBoot: g ? g.kbdBoot : true
  readonly property bool kbdAwake: g ? g.kbdAwake : true
  readonly property bool kbdSleep: g ? g.kbdSleep : true
  readonly property bool kbdShutdown: g ? g.kbdShutdown : true

  // ---- slash ----
  readonly property bool hasSlash: g ? g.hasSlash : false
  readonly property bool slashOn: g ? g.slashOn : false
  readonly property int slashBrightness: g ? g.slashBrightness : 0
  readonly property int slashPattern: g ? g.slashPattern : 0
  readonly property string slashPatternName: g ? g.slashPatternName : ""
  readonly property bool showOnBoot: g ? g.showOnBoot : false
  readonly property bool showOnShutdown: g ? g.showOnShutdown : false
  readonly property bool showOnSleep: g ? g.showOnSleep : false
  readonly property bool showOnBattery: g ? g.showOnBattery : false
  readonly property bool showBatteryWarning: g ? g.showBatteryWarning : false
  readonly property bool showOnLidClosed: g ? g.showOnLidClosed : false
  readonly property var slashModes: g ? g.slashModes : []

  // A page that disappears (asusd gone, no Slash) sends the panel back to System.
  function checkTab() {
    if ((root.tab === 1 && !root.hasAura) || (root.tab === 2 && !root.hasSlash)) root.tab = 0
  }
  onHasAuraChanged: checkTab()
  onHasSlashChanged: checkTab()

  readonly property var levelOptions: [
    { value: "off",  label: "Off"  },
    { value: "low",  label: "Low"  },
    { value: "med",  label: "Med"  },
    { value: "high", label: "High" }
  ]

  readonly property var swatches: [
    "FFFFFF", "FF0000", "FF6A00", "FFD400", "6AFF00",
    "00FF88", "00E5FF", "0066FF", "8A2BE2", "FF00FF"
  ]

  // The live brightness: Fn+F2/F3 can move it away from the saved level.
  readonly property string effectiveLevel: levelOptions[Math.max(0, Math.min(3, kbdBrightness))].value
  readonly property bool kbdOn: kbdBrightness > 0
  readonly property bool anyOn: kbdOn || slashOn
  readonly property int slashPercent: Math.round(slashBrightness / 255 * 100)

  // 0 = System (front page), 1 = Keyboard, 2 = Lid slash.
  property int tab: 0

  // ---- display: the internal panel's refresh rate. The bridge sets it per
  // power source (or pinned here), and sets it again after a hotplug or
  // `hyprctl reload`, which re-apply monitors.lua and its native rate. ----
  readonly property int refreshHz: g ? g.refreshHz : 0
  // Shows the click at once; the bridge's next state confirms it.
  property string refreshPicked: ""
  readonly property string refreshOverride: refreshPicked !== "" ? refreshPicked
    : (g ? g.refreshOverride : "auto")
  readonly property bool refreshAvailable: root.refreshHz > 0
  readonly property var refreshOptions: [
    { value: "60",   label: "60Hz"  },
    { value: "240",  label: "240Hz" },
    { value: "auto", label: "Auto"  }
  ]

  // ---- battery charge ceiling (asusctl; sysfs is the source of truth) ----
  readonly property int chargeLimit: g ? g.chargeLimit : 0
  readonly property bool chargeAvailable: root.chargeLimit > 0
  readonly property var chargeOptions: [
    { value: "60",  label: "60%"  },
    { value: "80",  label: "80%"  },
    { value: "100", label: "100%" }
  ]

  // ---- Win-lock (firmware Fn+Super; helper + user service) ----
  readonly property bool hasWinlock: g ? g.hasWinlock : false
  readonly property string winlockMode: g ? g.winlockMode : "toast"
  readonly property string winlockService: g ? g.winlockService : "inactive"
  readonly property bool winlockAvailable: root.hasWinlock
  readonly property var winlockModeOptions: [
    { value: "toast",  label: "Toast"  },
    { value: "unlock", label: "Unlock" }
  ]

  function setLevel(scope, value) { if (g) g.setLevel(scope, value) }
  function toggleKeyboard() { if (g) g.toggleKeyboard() }
  function setColor(hex) { if (g) g.setColor(hex) }
  function setKbdPower(flag, value) { if (g) g.setKbdPower(flag, value) }
  function toggleSlash() { if (g) g.toggleSlash() }
  function setSlashPattern(index) { if (g) g.setSlashPattern(index) }

  // ---- keyboard: the Slash pattern grid (arrows or h/j/k/l move, Enter or
  // Space picks; Tab still switches panels, as everywhere in Omarchy) ----
  readonly property int slashColumns: 3
  // -1 until a key is pressed on the Slash tab; then it starts on the
  // current pattern. A click moves it too, so mouse and keys agree.
  property int slashCursor: -1

  // cursor-begin (pure; exercised by tests/cursor.test.js)
  // Next cursor position in a grid of `count` cells, `columns` wide. -1 (no
  // cursor yet) lands on `current`. Left/right step one cell and run on
  // across rows; up/down move a row. Never leaves the grid.
  function nextCursor(cursor, dx, dy, count, columns, current) {
    if (count <= 0) return -1
    if (cursor < 0 || cursor >= count) return Math.max(0, Math.min(count - 1, current))
    var next = cursor + dx + dy * columns
    if (next < 0 || next >= count) return dy !== 0 ? cursor : Math.max(0, Math.min(count - 1, next))
    return next
  }
  // cursor-end

  function moveCursor(dx, dy) {
    if (root.tab !== 2 || !root.hasSlash) return
    root.slashCursor = nextCursor(root.slashCursor, dx, dy, root.slashModes.length,
                                  root.slashColumns, root.slashPattern)
    root.revealCursor()
  }
  function activateCursor() {
    if (root.tab !== 2 || root.slashCursor < 0) return
    root.setSlashPattern(root.slashCursor)
  }
  // On the 1600 px internal panel the panel scrolls: keep the cursor's row in view.
  function revealCursor() {
    var cell = modeRepeater.itemAt(root.slashCursor)
    if (!cell || !panelScroll.interactive) return
    var y = cell.mapToItem(column, 0, 0).y
    if (y < panelScroll.contentY) panelScroll.contentY = y
    else if (y + cell.height > panelScroll.contentY + panelScroll.height)
      panelScroll.contentY = y + cell.height - panelScroll.height
  }
  onTabChanged: root.slashCursor = -1
  function applySlashBrightness() { if (g) g.applySlashBrightness() }
  function setSlashFlag(name, value) { if (g) g.setSlashFlag(name, value) }
  // Named setRefreshHz, not setRefresh: refresh() already means "re-read state".
  function setRefreshHz(value) {
    root.refreshPicked = value
    refreshPickedClear.restart()
    if (g) g.setRefreshHz(value)
  }
  function setChargeLimit(value) { if (g) g.setChargeLimit(value) }
  function setWinlockMode(value) { if (g) g.setWinlockMode(value) }
  function refresh() { if (g) g.refresh() }

  Timer { id: refreshPickedClear; interval: 4000; onTriggered: root.refreshPicked = "" }

  visible: hasAura || hasSlash || refreshAvailable || chargeAvailable || winlockAvailable
  implicitWidth: visible ? button.implicitWidth : 0
  implicitHeight: visible ? button.implicitHeight : 0

  Timer {
    id: slashThrottle
    interval: 120
    repeat: false
    onTriggered: root.applySlashBrightness()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    // Stays the lid-slash streak even though this widget now covers both
    // lights: the slash is this laptop model's own mark, so it reads as "the
    // LEDs on this machine" in a way a generic keyboard glyph does not.
    iconComponent: Component {
      Item {
        SlashMark {
          anchors.centerIn: parent
          width: Style.space(13)
          height: Style.space(13)
          lit: root.anyOn
        }
      }
    }
    active: root.anyOn
    tooltipText: {
      var parts = []
      if (root.hasAura)
        parts.push("Keyboard " + (root.kbdOn ? root.effectiveLevel : "off"))
      if (root.hasSlash)
        parts.push("Slash " + (root.slashOn ? root.slashPatternName : "off"))
      return parts.join("  ·  ")
    }
    onPressed: function(b) {
      if (b === Qt.RightButton) {
        if (root.hasAura) root.toggleKeyboard()
        else if (root.hasSlash) root.toggleSlash()
      } else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened && (root.hasAura || root.hasSlash || root.refreshAvailable
                          || root.chargeAvailable || root.winlockAvailable)
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onActivateRequested: root.activateCursor()

      Flickable {
        id: panelScroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        // Only interactive when there is something to scroll. The panel fits
        // whole on the 1440p external; it is the 1000px-tall internal panel
        // (2560x1600 at scale 1.6) that needs this.
        interactive: contentHeight > height

        Column {
          id: column
          width: panelScroll.width
          spacing: Style.space(14)

          // ---------- Tabs ----------
          // System leads: refresh and charge ceiling are the controls reached
          // for daily. The LEDs are deliberately one step back.
          ButtonGroup {
            id: tabs
            width: parent.width
            visible: root.hasAura || root.hasSlash
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            // Only pages that have content; checkTab() moves root.tab back to
            // System if the selected page disappears.
            options: {
              var o = [{ value: "system", label: "System" }]
              if (root.hasAura) o.push({ value: "keyboard", label: "Keyboard" })
              if (root.hasSlash) o.push({ value: "slash", label: "Lid slash" })
              return o
            }
            value: root.tab === 0 ? "system" : (root.tab === 1 ? "keyboard" : "slash")
            onChanged: function(v) {
              root.tab = (v === "system") ? 0 : (v === "keyboard" ? 1 : 2)
            }
          }

          // ================= KEYBOARD =================
          Column {
            width: parent.width
            spacing: Style.space(14)
            visible: root.hasAura && root.tab === 1

            PanelHero {
              width: parent.width
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              title: "Keyboard"
              meta: (root.kbdOn ? root.effectiveLevel : "off").toUpperCase()
              detail: root.powerSource === "usbc" ? "on USB-C"
                : root.powerSource === "weak" ? "on a weak barrel charger"
                : (root.onBattery ? "on battery" : "on the barrel charger")
              iconComponent: Component {
                KeyboardMark {
                  lit: root.kbdOn
                  color: root.bar ? root.bar.foreground : Commons.Color.foreground
                  implicitWidth: Style.space(34)
                  implicitHeight: Style.space(26)
                }
              }
              trailingControl: Component {
                ToggleSwitch {
                  checked: root.kbdOn
                  foreground: root.bar.foreground
                  onToggled: root.toggleKeyboard()
                }
              }
            }

            PanelSeparator { foreground: root.bar.foreground }

            // ---------- Level, per power source ----------
            Column {
              width: parent.width
              spacing: Style.space(8)

              PanelSectionHeader {
                text: "BRIGHTNESS"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
              }

              LevelRow {
                label: "On the barrel charger"
                value: root.kbdAC
                live: !root.batterySettings
                onPicked: function(v) { root.setLevel("ac", v) }
              }

              LevelRow {
                label: root.powerSource === "usbc" ? "On battery / USB-C (now: USB-C)" : "On battery / USB-C"
                value: root.kbdBattery
                live: root.batterySettings
                onPicked: function(v) { root.setLevel("battery", v) }
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: "The lights draw about 2 W at high, a big share of the laptop's "
                    + "idle draw, so on battery they stay off unless you pick a level. "
                    + "Applied again on every plug and unplug, so they are set here "
                    + "rather than as one live slider."
                color: Qt.darker(root.bar.foreground, 1.5)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            PanelSeparator { foreground: root.bar.foreground }

            // ---------- Colour ----------
            Column {
              width: parent.width
              spacing: Style.space(10)

              PanelSectionHeader {
                text: "COLOUR"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
              }

              Grid {
                id: swatchGrid
                width: parent.width
                columns: 10
                spacing: Style.space(6)

                readonly property real cell:
                  (width - spacing * (columns - 1)) / columns

                Repeater {
                  model: root.swatches
                  Rectangle {
                    required property var modelData
                    width: swatchGrid.cell
                    height: swatchGrid.cell
                    radius: width / 2
                    color: "#" + modelData
                    border.width: root.kbdColor.toUpperCase() === String(modelData).toUpperCase() ? 2 : 0
                    border.color: root.bar.foreground
                    opacity: root.kbdOn ? 1.0 : 0.55

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.setColor(String(modelData))
                    }

                    Behavior on opacity { NumberAnimation { duration: 150 } }
                  }
                }
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                visible: !root.kbdOn
                text: "Backlight is off, so a colour change will not be visible yet."
                color: Qt.darker(root.bar.foreground, 1.5)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            PanelSeparator { foreground: root.bar.foreground }

            // ---------- Power states ----------
            Column {
              width: parent.width
              spacing: Style.space(10)

              PanelSectionHeader {
                text: "KEEP LIT DURING"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
              }

              Grid {
                id: kbdPowerGrid
                width: parent.width
                columns: 4
                spacing: Style.space(6)

                readonly property real cellWidth:
                  (width - spacing * (columns - 1)) / columns

                FlagButton {
                  grid: kbdPowerGrid; label: "Boot"; state: root.kbdBoot
                  onToggle: root.setKbdPower("boot", !root.kbdBoot)
                }
                FlagButton {
                  grid: kbdPowerGrid; label: "Awake"; state: root.kbdAwake
                  onToggle: root.setKbdPower("awake", !root.kbdAwake)
                }
                FlagButton {
                  grid: kbdPowerGrid; label: "Sleep"; state: root.kbdSleep
                  onToggle: root.setKbdPower("sleep", !root.kbdSleep)
                }
                FlagButton {
                  grid: kbdPowerGrid; label: "Shutdown"; state: root.kbdShutdown
                  onToggle: root.setKbdPower("shutdown", !root.kbdShutdown)
                }
              }
            }
          }

          // ================= SLASH =================
          Column {
            width: parent.width
            spacing: Style.space(14)
            visible: root.hasSlash && root.tab === 2

            PanelHero {
              width: parent.width
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              title: "Lid slash"
              meta: (root.slashOn ? (root.slashPatternName || "ON") : "OFF").toUpperCase()
              detail: root.slashOn ? root.slashPercent + "% brightness" : ""
              iconComponent: Component {
                SlashMark {
                  lit: root.slashOn
                  color: root.bar ? root.bar.foreground : Commons.Color.foreground
                  implicitWidth: Style.space(26)
                  implicitHeight: Style.space(34)
                }
              }
              trailingControl: Component {
                ToggleSwitch {
                  checked: root.slashOn
                  foreground: root.bar.foreground
                  onToggled: root.toggleSlash()
                }
              }
            }

            PanelSeparator { foreground: root.bar.foreground }

            // ---------- Brightness ----------
            Column {
              width: parent.width
              spacing: Style.space(8)

              Item {
                width: parent.width
                implicitHeight: slashHeader.implicitHeight

                PanelSectionHeader {
                  id: slashHeader
                  text: "BRIGHTNESS"
                  foreground: root.bar.foreground
                  fontFamily: root.bar.fontFamily
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: slashHeader.verticalCenter
                  anchors.right: parent.right
                  text: root.slashPercent + "%"
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }
              }

              PanelSlider {
                id: slashSlider
                bar: root.bar
                width: parent.width
                minimum: 0
                maximum: 255
                step: 1
                integer: true
                value: root.slashBrightness
                onMoved: function(v) {
                  if (root.g) root.g.slashBrightness = Math.round(v)
                  slashThrottle.restart()
                }
                onReleased: function(v) {
                  if (root.g) root.g.slashBrightness = Math.round(v)
                  slashThrottle.stop()
                  root.applySlashBrightness()
                }
                // Keeps a re-read from yanking the handle mid-drag.
                onDraggingChanged: if (root.g) root.g.sliderDragging = dragging
              }
            }

            PanelSeparator { foreground: root.bar.foreground }

            // ---------- Pattern ----------
            Column {
              width: parent.width
              spacing: Style.space(10)

              PanelSectionHeader {
                text: "PATTERN"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
              }

              Grid {
                id: modeGrid
                width: parent.width
                columns: 3
                spacing: Style.space(6)

                readonly property real cellWidth:
                  (width - spacing * (columns - 1)) / columns

                Repeater {
                  id: modeRepeater
                  model: root.slashModes
                  Button {
                    required property var modelData
                    required property int index
                    width: modeGrid.cellWidth
                    text: String(modelData)
                    fontSize: Style.font.bodySmall
                    foreground: root.bar.foreground
                    fontFamily: root.bar.fontFamily
                    horizontalPadding: Style.spacing.controlPaddingX
                    verticalPadding: Style.spacing.controlPaddingY
                    bordered: true
                    active: root.slashPattern === index
                    hasCursor: root.tab === 2 && root.slashCursor === index
                    onClicked: { root.slashCursor = index; root.setSlashPattern(index) }
                  }
                }
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: "Static is the steady one. Most others animate, and a few sit dark between plays. "
                    + "Arrow keys (or h/j/k/l) move, Enter picks."
                color: Qt.darker(root.bar.foreground, 1.5)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            PanelSeparator { foreground: root.bar.foreground }

            // ---------- Event animations ----------
            Column {
              width: parent.width
              spacing: Style.space(10)

              PanelSectionHeader {
                text: "SHOW ANIMATION ON"
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
              }

              Grid {
                id: eventGrid
                width: parent.width
                columns: 3
                spacing: Style.space(6)

                readonly property real cellWidth:
                  (width - spacing * (columns - 1)) / columns

                FlagButton {
                  grid: eventGrid; label: "Boot"; state: root.showOnBoot
                  onToggle: root.setSlashFlag("ShowOnBoot", !root.showOnBoot)
                }
                FlagButton {
                  grid: eventGrid; label: "Shutdown"; state: root.showOnShutdown
                  onToggle: root.setSlashFlag("ShowOnShutdown", !root.showOnShutdown)
                }
                FlagButton {
                  grid: eventGrid; label: "Sleep"; state: root.showOnSleep
                  onToggle: root.setSlashFlag("ShowOnSleep", !root.showOnSleep)
                }
                FlagButton {
                  grid: eventGrid; label: "Battery"; state: root.showOnBattery
                  onToggle: root.setSlashFlag("ShowOnBattery", !root.showOnBattery)
                }
                FlagButton {
                  grid: eventGrid; label: "Low batt"; state: root.showBatteryWarning
                  onToggle: root.setSlashFlag("ShowBatteryWarning", !root.showBatteryWarning)
                }
                FlagButton {
                  grid: eventGrid; label: "Lid shut"; state: root.showOnLidClosed
                  onToggle: root.setSlashFlag("ShowOnLidClosed", !root.showOnLidClosed)
                }
              }
            }
          }

          // ================= DISPLAY =================
          Column {
            width: parent.width
            spacing: Style.space(8)
            visible: root.refreshAvailable && root.tab === 0

            PanelSectionHeader {
              text: "DISPLAY"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            LevelRow {
              label: "Refresh"
              options: root.refreshOptions
              value: root.refreshOverride
              live: true
              onPicked: function(v) { root.setRefreshHz(v) }
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "Now " + root.refreshHz + "Hz. Auto follows power (60 on battery with your "
                  + "defaults). Pin 60 or 240 anytime — including on battery — if you want to "
                  + "override and burn the watts."
              color: Qt.darker(root.bar.foreground, 1.5)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ================= BATTERY =================
          PanelSeparator {
            visible: root.chargeAvailable && root.refreshAvailable && root.tab === 0
            foreground: root.bar.foreground
          }

          Column {
            width: parent.width
            spacing: Style.space(8)
            visible: root.chargeAvailable && root.tab === 0

            PanelSectionHeader {
              text: "BATTERY"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            LevelRow {
              label: "Charge to"
              options: root.chargeOptions
              value: String(root.chargeLimit)
              live: true
              onPicked: function(v) { root.setChargeLimit(v) }
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "Stopping short of full is the single biggest lever on pack "
                  + "lifespan. 100% is right before a trip, not as a resting state."
              color: Qt.darker(root.bar.foreground, 1.5)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ================= WIN-LOCK =================
          PanelSeparator {
            visible: root.winlockAvailable && root.tab === 0
              && (root.refreshAvailable || root.chargeAvailable)
            foreground: root.bar.foreground
          }

          Column {
            width: parent.width
            spacing: Style.space(8)
            visible: root.winlockAvailable && root.tab === 0

            PanelSectionHeader {
              text: "SUPER"
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
            }

            LevelRow {
              label: "On Fn+Super"
              options: root.winlockModeOptions
              value: root.winlockMode
              live: true
              onPicked: function(v) { root.setWinlockMode(v) }
            }


            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: root.winlockMode === "unlock"
                ? "Unlock mode fights accidental Fn+Super: toast, then SetFeature unlock. Service: "
                  + root.winlockService + "."
                : "Toast mode shows an OSD when firmware Super-lock toggles. Service: "
                  + root.winlockService + "."
              color: Qt.darker(root.bar.foreground, 1.5)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // Where this comes from: omarchy-asus also fixes the rest of the
          // laptop (kernel, NVIDIA, Omarchy) as a signed pacman repo.
          Text {
            textFormat: Text.PlainText
            visible: root.tab === 0
            width: parent.width
            horizontalAlignment: Text.AlignRight
            text: "omarchy-asus · ASUS fixes for Omarchy"
            color: footerMouse.containsMouse ? root.bar.foreground : Qt.darker(root.bar.foreground, 1.8)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            font.underline: footerMouse.containsMouse
            MouseArea {
              id: footerMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: Qt.openUrlExternally("https://omarchy-asus.github.io")
            }
          }

        }
      }
    }
  }

  // A labelled segmented row. `live` marks the power source currently in force,
  // so it is obvious which of the two rows is the one you can see right now.
  component LevelRow: Item {
    id: row
    property string label: ""
    property string value: "off"
    property bool live: false
    // Defaults to the keyboard levels; Display and Battery pass their own.
    property var options: root.levelOptions
    signal picked(string value)

    width: parent ? parent.width : 0
    implicitHeight: Math.max(rowLabel.implicitHeight, group.implicitHeight)

    Text {
      textFormat: Text.PlainText
      id: rowLabel
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(76)
      text: row.label
      color: row.live ? root.bar.foreground : Qt.darker(root.bar.foreground, 1.4)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: row.live
      elide: Text.ElideRight
    }

    ButtonGroup {
      id: group
      anchors.left: rowLabel.right
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      foreground: root.bar.foreground
      fontFamily: root.bar.fontFamily
      fontSize: Style.font.bodySmall
      options: row.options
      value: row.value
      onChanged: function(v) { row.picked(v) }
    }
  }

  component FlagButton: Button {
    property Grid grid: null
    property string label: ""
    property bool state: false
    signal toggle()

    width: grid ? grid.cellWidth : implicitWidth
    text: label
    fontSize: Style.font.bodySmall
    foreground: root.bar.foreground
    fontFamily: root.bar.fontFamily
    horizontalPadding: Style.spacing.controlPaddingX
    verticalPadding: Style.spacing.controlPaddingY
    bordered: true
    active: state
    onClicked: toggle()
  }

  // Drawn rather than typeset, for the same reason the slash mark is: the bar
  // font has no keyboard glyph that reads correctly at 14px.
  component KeyboardMark: Item {
    id: kb
    property bool lit: false
    property color color: root.barForeground
    readonly property color ink: lit ? color : Qt.darker(color, 1.55)

    implicitWidth: Style.space(16)
    implicitHeight: Style.space(16)

    Rectangle {
      anchors.centerIn: parent
      width: kb.width
      height: Math.round(kb.width * 0.72)
      radius: Math.max(2, Math.round(kb.width * 0.14))
      color: "transparent"
      border.width: Math.max(1, Math.round(kb.width * 0.09))
      border.color: kb.ink

      Behavior on border.color { ColorAnimation { duration: 200 } }

      // Three key rows, the last one a single wide "spacebar".
      Column {
        anchors.centerIn: parent
        spacing: Math.max(1, Math.round(kb.width * 0.07))

        Repeater {
          model: 2
          Row {
            spacing: Math.max(1, Math.round(kb.width * 0.07))
            Repeater {
              model: 4
              Rectangle {
                width: Math.max(1, Math.round(kb.width * 0.11))
                height: Math.max(1, Math.round(kb.width * 0.09))
                radius: height / 2
                color: kb.ink
                Behavior on color { ColorAnimation { duration: 200 } }
              }
            }
          }
        }

        Rectangle {
          anchors.horizontalCenter: parent.horizontalCenter
          width: Math.round(kb.width * 0.44)
          height: Math.max(1, Math.round(kb.width * 0.09))
          radius: height / 2
          color: kb.ink
          Behavior on color { ColorAnimation { duration: 200 } }
        }
      }
    }
  }

  // The lid element is one bold diagonal streak of mini-LEDs. Carried over from
  // the older Slash-only widget unchanged; see that widget's notes on why it is drawn and why
  // the lean is 55.2 degrees (the GA605 lid is 354 x 246 mm).
  component SlashMark: Item {
    id: mark
    property bool lit: false
    property color color: root.barForeground
    property real thickness: 0.15
    property real lean: -55.2

    implicitWidth: Style.space(16)
    implicitHeight: Style.space(16)

    readonly property real _rad: Math.abs(mark.lean) * Math.PI / 180
    readonly property real _len: Math.min(mark.height * 1.35,
                                          mark.width * 0.82 / Math.max(0.25, Math.sin(_rad)))

    Rectangle {
      anchors.centerIn: parent
      width: Math.max(2, Math.round(mark.height * mark.thickness))
      height: Math.round(mark._len)
      radius: width / 2
      rotation: mark.lean
      color: mark.lit ? mark.color : Qt.darker(mark.color, 1.55)

      Behavior on color { ColorAnimation { duration: 200 } }
    }
  }
}
