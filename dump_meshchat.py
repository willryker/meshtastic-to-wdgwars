#!/usr/bin/env python3
"""Emit meshchat's node table in the shape ratatoskr.convert() expects.

Runs on nab9. Reads the database READ-ONLY through a file: URI so the live
meshchat container is never written to, journalled or schema-upgraded by
being read, and so this needs no downtime: meshchat keeps the serial port,
which the Meshtastic API only lets one client hold at a time.

Two differences from a serial read, both declared in the output rather than
papered over:
  * `roles_available` is False. The nodes table has no role column, so every
    record converts with node_type UNKNOWN instead of a guessed CLIENT.
  * lat/lon are already decimal degrees here, not the int32-scaled-by-1e7
    the radio sends, so they are passed through and the 1e7 scale is not
    applied. Applying it would put every node in the Gulf of Guinea.

rssi is not in the nodes table. It is recovered per node from `messages`,
which records the rssi of packets actually received from that node, and is
left null for nodes we have never had a packet from directly.
"""
import json, sqlite3, sys, time

DB = sys.argv[1] if len(sys.argv) > 1 else "/opt/stacks/meshchat/data/meshchat.db"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# Strongest real rssi per sender. Strongest rather than latest because rssi
# varies packet to packet with no bearing on the node's position; the best
# reception is the one that actually demonstrates the link.
rssi = {}
for r in conn.execute("SELECT from_id, MAX(rssi) AS rssi FROM messages "
                      "WHERE direction='rx' AND rssi IS NOT NULL "
                      "GROUP BY from_id"):
    rssi[r["from_id"]] = r["rssi"]

nodes = {}
for r in conn.execute("SELECT * FROM nodes"):
    row = dict(r)
    nid = str(row.get("id") or "")
    try:
        num = int(nid.lstrip("!"), 16)
    except ValueError:
        continue
    nodes[nid] = {
        "num": num,
        "user": {
            "id": nid,
            "longName": row.get("long_name"),
            "shortName": row.get("short_name"),
            "hwModel": row.get("hw_model"),
            # no role column: deliberately absent, see roles_available
        },
        "position": {"latitude": row.get("lat"), "longitude": row.get("lon"),
                     "altitude": row.get("alt")},
        "snr": row.get("snr"),
        "rssi": rssi.get(nid),
        "hopsAway": row.get("hops"),
        "lastHeard": row.get("last_heard"),
    }
conn.close()

json.dump({"captured_at": int(time.time()), "source": "meshchat-db",
           "db": DB, "roles_available": False, "nodes": nodes},
          sys.stdout, indent=1)
