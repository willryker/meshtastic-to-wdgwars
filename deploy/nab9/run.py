#!/usr/bin/env python3
"""Daily Ratatoskr feed on nab9: meshchat's node mirror -> WDGWars.

Run by ratatoskr.timer as willryker. WDGWARS_API_KEY reaches ratatoskr.py
through the unit's EnvironmentFile, so the key is never on a command line.

meshchat.db, not the radio, because the M7 keeps only 200 nodes and turned
them over in under a day (measured 2026-09-30), while meshchat never evicts.
The whole mirror is sent every run: re-sent nodes come back already_seen and
cost nothing, and a missed day heals itself.

The full log goes to the journal. Discord hears only failures and new points,
and only as counts: ratatoskr's output carries node names and positions (its
HELD BACK lines place Joe's own devices), and Discord is outside the lab.
"""
import json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUMP = HERE / "last-dump.json"


def notify(title, body, priority="default", tags="satellite", channel=None):
    # channel=None is the main lab channel. Only good news is routed elsewhere:
    # lab-notify never falls back between channels, so a broken per-channel
    # webhook must not be able to swallow a failure alert.
    opt = [f"--channel={channel}"] if channel else []
    subprocess.run(["/usr/local/bin/lab-notify", *opt, title, body, priority, tags])


def counts(obj, found):
    """Sum every integer field named like *imported* / *already_seen*, at any depth.

    The mesh reply's exact keys are unrecorded (ADS-B's are aircraft_imported
    and aircraft_already_seen), so match by name rather than guess one key.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, int) and not isinstance(v, bool):
                for tag in ("imported", "already_seen"):
                    if tag in k:
                        found[tag] = found.get(tag, 0) + v
            else:
                counts(v, found)
    elif isinstance(obj, list):
        for v in obj:
            counts(v, found)


def main():
    with DUMP.open("w") as f:
        r = subprocess.run([sys.executable, str(HERE / "dump_meshchat.py")],
                           stdout=f, stderr=subprocess.PIPE, text=True)
    if r.returncode:
        print(r.stderr, file=sys.stderr)
        notify("Ratatoskr FAILED", "Could not read meshchat.db. journalctl -u ratatoskr",
               "high", "warning")
        return 1

    # --allow-no-rf: MQTT-sourced nodes are uploaded too, by Joe's decision
    # 2026-10-01 (see meshtastic.md). Drop the flag to go back to antenna-only.
    r = subprocess.run([sys.executable, str(HERE / "ratatoskr.py"),
                        "--allow-no-rf", str(DUMP)],
                       capture_output=True, text=True)
    print(r.stdout + r.stderr)
    lines = r.stdout.splitlines()
    sent, found, bad = 0, {}, []
    for i, line in enumerate(lines):
        m = re.match(r"POST (\d+) records .*-> (\d+)$", line)
        if not m:
            continue
        sent += int(m.group(1))
        if m.group(2) != "200":
            bad.append(m.group(2))
            continue
        try:
            counts(json.loads(lines[i + 1]), found)
        except (IndexError, ValueError):
            pass

    if r.returncode or bad or not sent:
        why = f"HTTP {', '.join(bad)}" if bad else f"exit {r.returncode}, {sent} sent"
        notify("Ratatoskr FAILED", f"WDGWars mesh upload failed ({why}). journalctl -u ratatoskr",
               "high", "warning")
        return 1
    if "imported" not in found:
        notify("Ratatoskr: reply not understood",
               f"Sent {sent} mesh nodes, HTTP 200, but no imported count in the reply. "
               "journalctl -u ratatoskr", "default", "warning")
        return 0
    if found["imported"]:
        notify(f"Ratatoskr: +{found['imported']} mesh nodes",
               f"{found['imported']} new on WDGWars, {found.get('already_seen', 0)} already there, "
               f"{sent} sent.", channel="meshnetwork-node")
    return 0


if __name__ == "__main__":
    sys.exit(main())
