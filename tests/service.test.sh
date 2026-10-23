#!/bin/bash
# Service.qml headless (#145, #156): quickshell on the offscreen platform loads
# it with a stub bridge, a Python script that speaks the bridge's JSON-lines
# protocol and logs every command. Checks:
#   - the first state reaches the properties;
#   - a click updates the property at once, and a state from before that
#     click (ack lower than the command id) is ignored;
#   - commands reach the bridge with their arguments;
#   - a bridge that exits is started again, and its states (acks from 0)
#     are applied.
# Run: tests/service.test.sh   (skips without quickshell's `qs`)
set -u
command -v qs >/dev/null || { echo "service tests skipped: no qs"; exit 0; }
SVC=$(realpath "$(dirname "$0")/../Service.qml")
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

cat >"$T/stub.py" <<'EOF'
import json, os, sys
log = open(os.environ["STUB_LOG"], "a")
starts = os.path.join(os.path.dirname(os.environ["STUB_LOG"]), "starts")
with open(starts, "a") as f:
    f.write("x")
n = len(open(starts).read())
# Like the bridge, every state carries the current colour: a restarted
# bridge reports its own (and counts acks from 0 again).
color = "ABCDEF" if n > 1 else "00FF88"
def state(ack, **kw):
    s = {"type": "state", "ack": ack, "onBattery": False, "hasAura": True, "kbdBrightness": 1,
         "kbdColor": color, "kbdAC": "med", "kbdBattery": "off", "kbdBoot": True,
         "kbdAwake": True, "kbdSleep": False, "kbdShutdown": True, "hasSlash": True,
         "slashOn": True, "slashBrightness": 100, "slashPattern": 2, "chargeLimit": 80,
         "refreshHz": 240, "refreshOverride": "auto", "gaming": False, "hasWinlock": True,
         "winlockMode": "toast", "winlockService": "active", "starts": n}
    s.update(kw)
    print(json.dumps(s), flush=True)
state(0)
for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps([msg["cmd"]] + msg["args"]) + "\n"); log.flush()
    if msg["cmd"] == "kbd-color":
        state(msg["id"] - 1)     # stale: from before the click
        color = msg["args"][0]
        state(msg["id"])
    elif msg["cmd"] == "crash":
        sys.exit(3)
    else:
        state(msg["id"])
EOF

cat >"$T/shell.qml" <<EOF
import QtQuick
import Quickshell
ShellRoot {
  id: r
  property var s: null
  property var out: []
  function ok(name, cond) { out.push((cond ? "ok - " : "not ok - ") + name) }
  Component.onCompleted: {
    var c = Qt.createComponent("file://$SVC")
    if (c.status !== Component.Ready) { console.log("LOAD ERROR " + c.errorString()); Qt.exit(2); return }
    r.s = c.createObject(r, { python3Bin: "/usr/bin/python3", bridgePath: "$T/stub.py",
                              extraEnv: { STUB_LOG: "$T/log" } })
  }
  Timer { interval: 700; running: true; onTriggered: {
    ok("first state: hasAura, slash, charge, refresh", r.s.ready && r.s.hasAura && r.s.slashOn && r.s.chargeLimit === 80 && r.s.refreshHz === 240)
    ok("first state: per-source levels", r.s.kbdAC === "med" && r.s.kbdBattery === "off")
    r.s.setColor("1A2B3C")
    ok("optimistic colour", r.s.kbdColor === "1A2B3C")
    r.s.setLevel("ac", "high")
    ok("AC level live on AC", r.s.kbdBrightness === 3 && r.s.kbdAC === "high")
    r.s.setKbdPower("sleep", true)
    r.s.setRefreshHz("60")
    ok("optimistic pin", r.s.refreshOverride === "60")
  } }
  Timer { interval: 1500; running: true; onTriggered: {
    ok("stale state ignored, colour kept", r.s.kbdColor === "1A2B3C")
    r.s.send("crash", [])
  } }
  Timer { interval: 5500; running: true; onTriggered: {
    ok("bridge started again after it exited", r.s.ready)
    ok("the new bridge's state is applied", r.s.kbdColor === "ABCDEF")
    for (var i = 0; i < r.out.length; i++) console.log(r.out[i])
    Qt.exit(0)
  } }
}
EOF

out=$(cd "$T" && QT_QPA_PLATFORM=offscreen timeout 30 qs -p "$T/shell.qml" 2>&1 |
  grep -oE '(not ok|ok) - .*|LOAD ERROR.*')
echo "$out"
fail=0
[[ $(grep -c '^ok - ' <<<"$out") -eq 8 ]] && ! grep -q '^not ok\|LOAD' <<<"$out" || fail=1
expect='["kbd-color", "1A2B3C"]
["kbd-level", "ac", "high"]
["kbd-power", "sleep", "true"]
["refresh", "60"]
["crash"]'
if [[ $(cat "$T/log" 2>/dev/null) == "$expect" ]]; then echo "ok - commands reached the bridge in order"
else echo "not ok - commands: $(cat "$T/log" 2>/dev/null)"; fail=1; fi
[[ $(cat "$T/starts") == xx ]] && echo "ok - two bridge starts" || { echo "not ok - starts: $(cat "$T/starts")"; fail=1; }
((fail)) && { echo "FAILED"; exit 1; }
echo "service tests ok"
