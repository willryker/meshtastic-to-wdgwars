#!/usr/bin/env python3
"""Read the Meshtastic iOS app's backup store into ratatoskr's dump format.

The app writes a CoreData/SwiftData SQLite database per connected node under
iCloud Drive (`Meshtastic/<nodeNum>/Meshtastic.store`, indexed by
`backup-index.json`). That store is a far better capture than the app's
"Application Logs" CSV export, which carries only two parseable position
lines and no SNR at all.

Three things this source has that neither the serial read nor the meshchat
mirror does:

  ZVIAMQTT      an explicit per-node flag saying the node reached the app over
                MQTT rather than the air. Provenance stated by the app instead
                of inferred from a missing SNR.
  ZFIRSTHEARD   a real first-heard timestamp alongside last-heard.
  position      history in ZPOSITIONENTITY, of which ZLATEST=1 is current.

TWO TRAPS, both silent:

  * TIMESTAMPS ARE APPLE EPOCH (2001-01-01), not unix. Read raw they land in
    1995 and every record fails a plausibility floor. Add 978307200.
  * ZROLE IS AN INTEGER, and the enum is not the same shape as MeshCore's.
    Mapped here; an unrecognised value is left absent rather than guessed, so
    the record converts as UNKNOWN instead of a wrong role.

Opened read-only through a file: URI, so an iCloud copy is never written to,
journalled or schema-upgraded by being read.
"""
import json, sqlite3, sys, time, pathlib

APPLE_EPOCH = 978_307_200

# Config.DeviceConfig.Role. Verified against meshtastic 2.7.11's protobuf.
ROLES = {
    0: "CLIENT", 1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER_CLIENT",
    4: "REPEATER", 5: "TRACKER", 6: "SENSOR", 7: "TAK", 8: "CLIENT_HIDDEN",
    9: "LOST_AND_FOUND", 10: "TAK_TRACKER", 11: "ROUTER_LATE", 12: "CLIENT_BASE",
}

QUERY = """
SELECT n.ZNUM num, n.ZSNR snr, n.ZRSSI rssi, n.ZHOPSAWAY hops,
       n.ZVIAMQTT via_mqtt, n.ZFIRSTHEARD first_heard, n.ZLASTHEARD last_heard,
       u.ZLONGNAME lname, u.ZSHORTNAME sname, u.ZHWMODEL hw, u.ZROLE role,
       u.Z_PK upk,
       p.ZLATITUDEI lat_i, p.ZLONGITUDEI lon_i, p.ZTIME ptime,
       p.ZPRECISIONBITS prec
  FROM ZNODEINFOENTITY n
  LEFT JOIN ZUSERENTITY u     ON u.ZUSERNODE    = n.Z_PK
  LEFT JOIN ZPOSITIONENTITY p ON p.ZNODEPOSITION = n.Z_PK AND p.ZLATEST = 1
"""


def _apple(ts):
    """Apple epoch -> unix seconds. None/0 stay falsy."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    return ts + APPLE_EPOCH if ts else None


def read_store(path):
    conn = sqlite3.connect(f"file:{pathlib.Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(QUERY)]
    finally:
        conn.close()
    out = {}
    for r in rows:
        num = r["num"]
        if num is None:
            continue
        nid = f"!{int(num) & 0xFFFFFFFF:08x}"
        user = None
        if r["upk"] is not None:
            user = {"id": nid, "longName": r["lname"], "shortName": r["sname"],
                    "hwModel": r["hw"]}
            role = ROLES.get(r["role"])
            if role:
                user["role"] = role
        pos = {}
        if r["lat_i"] or r["lon_i"]:
            pos = {"latitudeI": r["lat_i"], "longitudeI": r["lon_i"],
                   "time": _apple(r["ptime"]), "precisionBits": r["prec"]}
        out[nid] = {
            "num": int(num),
            "user": user,
            "position": pos,
            "snr": r["snr"],
            "rssi": r["rssi"] or None,      # 0 here means "not recorded"
            "hopsAway": r["hops"],
            "lastHeard": _apple(r["last_heard"]),
            "firstHeard": _apple(r["first_heard"]),
            # Stated by the app, not inferred. ratatoskr's RF gate rejects it
            # outright: a node the app says arrived over MQTT was heard by the
            # internet, not by an antenna, whatever SNR happens to sit beside it.
            "viaMqtt": bool(r["via_mqtt"]),
        }
    return out


def main(argv):
    if not argv:
        base = pathlib.Path.home() / ("Library/Mobile Documents/"
                                      "com~apple~CloudDocs/Meshtastic")
        argv = [str(p) for p in sorted(base.glob("*/Meshtastic.store"))]
        if not argv:
            raise SystemExit(f"no Meshtastic.store found under {base}")
    merged, seen = {}, {}
    for path in argv:
        nodes = read_store(path)
        print(f"[..] {path}: {len(nodes)} nodes", file=sys.stderr)
        for nid, node in nodes.items():
            prior = merged.get(nid)
            # Freshest reception wins the record; a node seen over the air in
            # ANY backup is not re-flagged as MQTT by a staler one that only
            # saw it relayed.
            if prior is None or (node["lastHeard"] or 0) > (prior["lastHeard"] or 0):
                if prior is not None and not prior["viaMqtt"]:
                    node = dict(node, viaMqtt=False)
                merged[nid] = node
            elif not node["viaMqtt"]:
                merged[nid]["viaMqtt"] = False
            seen[nid] = seen.get(nid, 0) + 1
    print(f"[OK] {len(merged)} unique nodes from {len(argv)} backup(s); "
          f"{sum(1 for n in merged.values() if n['viaMqtt'])} flagged viaMQTT",
          file=sys.stderr)
    json.dump({"captured_at": int(time.time()), "source": "ios-app-backup",
               "stores": argv, "roles_available": True, "nodes": merged},
              sys.stdout, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
