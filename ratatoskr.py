#!/usr/bin/env python3
"""Ratatoskr. Meshtastic node DB -> WDGWars, over USB serial or TCP.

Sibling to Heimdall (meshcore-to-wdgwars) and Muninn (adsb-to-wdgwars):
same HMAC envelope, same /api/upload/ endpoint, same mesh payload slot.
Heimdall fills that slot with `network: "meshcore"`; this fills it with
`network: "meshtastic"`, which LOCOSP's 2026-08-12 contract made the
authoritative field rather than having the server guess from role casing.

The read and the convert/upload halves are deliberately separate.
Reading needs the `meshtastic` package and a device on a cable or on wifi;
everything after that is stdlib and runs against a saved dump, so a capture
can be re-converted, re-previewed and re-uploaded without the radio present.

    ./ratatoskr.py --dump nodes.json          # read the radio, save the dump
    ./ratatoskr.py --dump roof.json --host IP # ...or one reachable only by wifi
    ./ratatoskr.py nodes.json --preview       # see the records
    ./ratatoskr.py nodes.json --dry-run       # sign, don't POST
    ./ratatoskr.py nodes.json --probe         # POST exactly one record
    ./ratatoskr.py nodes.json                 # upload
"""
from __future__ import annotations

import argparse, base64, collections, datetime, hashlib, hmac, json, os
import pathlib, re, secrets, sys, time
import urllib.request, urllib.error

__version__ = "0.2.0"

DEFAULT_ENDPOINT = "https://wdgwars.pl/api/upload/"
ME_ENDPOINT = "https://wdgwars.pl/api/me"
BATCH_SIZE = 1000
USER_AGENT = f"ratatoskr/{__version__}"

# Meshtastic packs lat/lon as int32 scaled by 1e7. MeshCore uses 1e6, and
# reusing that constant here would put every node 10x out with coordinates
# that still look superficially plausible. Kept as its own named constant so
# the two can never be confused.
COORD_SCALE = 10_000_000

# INT32_MAX / COORD_SCALE. An unset int32 coordinate arrives as INT32_MAX and
# divides down to a finite float that passes a naive nonzero test, which is
# exactly how a node reached wdgwars.pl at longitude 2147 during the MeshCore
# upload this tool was written after. The range gate below catches it; this
# constant exists so the drop can be reported by name rather than as a
# generic out-of-range.
INT32_SENTINEL = 2147483647 / COORD_SCALE

TS_FLOOR = 1_577_836_800        # 2020-01-01, before any of this shipped
TS_SKEW_AHEAD = 86_400          # tolerate a day of clock skew

# wdgwars.pl gates node_id at 8-16 lowercase hex. A Meshtastic node number is
# a 32-bit device number, so it renders as exactly 8 hex and sits on the floor.
NODE_ID_GATE = re.compile(r"^[0-9a-f]{8,16}$")

# Role 0 is CLIENT, and protobuf3 omits a field equal to its default, so a
# `user` record with no `role` key means CLIENT rather than unknown. That is a
# decode of the wire format, not a guess, which is why this one default is
# allowed where Heimdall refuses to default an unrecognised MeshCore type
# integer. A role we have genuinely never seen still rides through verbatim:
# the 2026-08-12 contract asks feeders to send the captured role and let the
# server map it, and an unknown node_type coerces server-side rather than
# rejecting.
DEFAULT_ROLE = "CLIENT"


# ---------------------------------------------------------------------------
# Reading the radio (the only part that needs the meshtastic package)
# ---------------------------------------------------------------------------

def read_radio(port: str | None, host: str | None = None) -> dict:
    """Return the radio's node DB as plain JSON-able dicts.

    Imported lazily and reported plainly on failure: the convert and upload
    paths are stdlib, and a missing package or absent radio must not stop
    someone re-running a saved dump.
    """
    try:
        from meshtastic.serial_interface import SerialInterface
        from meshtastic.tcp_interface import TCPInterface
        import meshtastic.util
    except ImportError as e:
        raise SystemExit(
            f"the 'meshtastic' package is needed to read a radio ({e}).\n"
            f"  python3 -m venv .venv && .venv/bin/pip install meshtastic\n"
            f"Converting and uploading a saved dump needs no packages at all."
        ) from e

    # A node reached over wifi has no serial port at all. The roof repeater is
    # the case this exists for: it is up a ladder, and its API on :4403 is a
    # LOCAL connection, so it needs no admin key (remote admin over LoRa does,
    # and that one is stranded). Try the network before planning a climb.
    if host:
        print(f"[..] connecting to {host}", file=sys.stderr)
        iface = TCPInterface(hostname=host)
        try:
            nodes = json.loads(json.dumps(iface.nodes, default=str))
            my = json.loads(json.dumps(getattr(iface, "myInfo", None), default=str))
        finally:
            # The node drops the socket as it is told to disconnect, so close()
            # raises BrokenPipeError AFTER the nodedb is already in hand. It is
            # noise on the way out, not a failed read, and a traceback here
            # reads as one.
            try:
                iface.close()
            except OSError:
                pass
        print(f"[OK] read {len(nodes)} nodes from {host}", file=sys.stderr)
        return {"captured_at": int(time.time()), "source": "tcp", "host": host,
                "roles_available": True, "my_info": my, "nodes": nodes}

    if not port:
        # nab9 pins the board to /dev/meshtastic with a udev rule precisely
        # because /dev/ttyACM0 moves. Prefer the stable name when it exists.
        if pathlib.Path("/dev/meshtastic").exists():
            port = "/dev/meshtastic"
    if not port:
        found = list(meshtastic.util.findPorts(True))
        if not found:
            raise SystemExit(
                "no Meshtastic device found on USB. Plug the radio in and "
                "re-run, or name the port with --port /dev/cu.usbserial-XXXX")
        if len(found) > 1:
            raise SystemExit(
                "more than one serial device present, name one with --port:\n  "
                + "\n  ".join(found))
        port = found[0]

    print(f"[..] opening {port}", file=sys.stderr)
    iface = SerialInterface(devPath=port)
    try:
        nodes = json.loads(json.dumps(iface.nodes, default=str))
        my = json.loads(json.dumps(getattr(iface, "myInfo", None), default=str))
    finally:
        iface.close()
    print(f"[OK] read {len(nodes)} nodes from {port}", file=sys.stderr)
    return {"captured_at": int(time.time()), "source": "serial", "port": port,
            "roles_available": True, "my_info": my, "nodes": nodes}


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _node_id(entry: dict) -> str:
    """8 lowercase hex from the node number.

    Derived from `num` rather than parsed out of `user.id`, because the number
    is the identity and the `!xxxxxxxx` string is a rendering of it. Masked and
    zero-padded so a low number still produces the 8 hex the server's floor
    needs instead of a short string that would come back bad_node_id.
    """
    num = entry.get("num")
    if num is None:
        user_id = str((entry.get("user") or {}).get("id") or "").lstrip("!")
        return user_id.lower() if user_id else ""
    try:
        return f"{int(num) & 0xFFFFFFFF:08x}"
    except (TypeError, ValueError):
        return ""


def _plausible_fix(lat: float, lon: float) -> bool:
    """Both coordinates real and in range.

    Requires both nonzero rather than either: a record with one real
    coordinate and one zero is null-island, not a fix, and the server rejects
    it as no_gps. The range check catches INT32_SENTINEL and anything else
    outside the globe.
    """
    if not lat or not lon:
        return False
    return abs(lat) <= 90 and abs(lon) <= 180


def _position(entry: dict) -> tuple[float, float]:
    pos = entry.get("position") or {}
    lat_i, lon_i = pos.get("latitudeI"), pos.get("longitudeI")
    if lat_i is None and "latitude" in pos:      # already-decoded degrees
        return float(pos.get("latitude") or 0), float(pos.get("longitude") or 0)
    try:
        return int(lat_i or 0) / COORD_SCALE, int(lon_i or 0) / COORD_SCALE
    except (TypeError, ValueError):
        return 0.0, 0.0


def _heard_at(entry: dict) -> int | None:
    """When our radio last heard this node.

    `lastHeard` is the reception, `position.time` is when the fix inside the
    packet was taken and can be much older. first_seen is a claim about the
    sighting, so the reception wins and the fix time is only a fallback.
    """
    for key, src in (("lastHeard", entry), ("time", entry.get("position") or {})):
        try:
            ts = int(src.get(key))
        except (TypeError, ValueError):
            continue
        if ts:
            return ts
    return None


def convert(dump: dict, since_days: float | None = None,
            envelope_type: str = "MESHTASTIC", require_rf: bool = True
            ) -> tuple[list[dict], collections.Counter]:
    """Node DB -> WDGWars mesh records, with every drop counted by reason.

    Two readers feed this, and they differ in one way that matters. A serial
    read carries `role`, so an absent role decodes as CLIENT (protobuf omits
    the default). The meshchat database has no role column at all, so absence
    there means unknown, and defaulting it to CLIENT would label every router
    and repeater on the mesh a client. `roles_available` in the dump says
    which case this is; without it, roles are assumed unavailable, because
    guessing wrong is worse than an honest UNKNOWN that the server coerces.
    """
    nodes = dump.get("nodes") or {}
    entries = list(nodes.values()) if isinstance(nodes, dict) else list(nodes)
    drop: collections.Counter = collections.Counter()
    ceiling = int(time.time()) + TS_SKEW_AHEAD
    cutoff = time.time() - since_days * 86_400 if since_days is not None else None
    roles_available = bool(dump.get("roles_available"))

    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            drop["malformed"] += 1
            continue

        node_id = _node_id(entry)
        if not NODE_ID_GATE.match(node_id):
            drop["bad_node_id"] += 1
            continue

        user = entry.get("user")
        if not isinstance(user, dict):
            # Heard, but no NodeInfo yet.
            # Heard, but no NodeInfo yet, so there is no role to report and
            # nothing to name it. Dropped rather than sent as a guessed
            # CLIENT: the protobuf-omission reasoning behind DEFAULT_ROLE
            # only holds when a user record actually exists.
            drop["no_user_record"] += 1
            continue

        # PROVENANCE GATE. A node whose data reached us over the air has an
        # SNR, because the radio measured the packet it received. A node
        # injected through MQTT has none: nothing was received, the entry was
        # handed to us over the internet. WDGWars is a game about what your
        # own hardware heard, so an unmeasured node is not ours to claim.
        #
        # This is not theoretical. Measured 2026-09-07 on the live nodedb: 14
        # of 157 nodes had no SNR and `lastHeard` 0, i.e. never heard at all.
        # All 14 happened to be excluded anyway, 13 for having no position and
        # 1 for having no timestamp, so the first upload was clean BY LUCK
        # rather than by rule. One of them with a position and a clock would
        # have gone straight up. Hence an explicit gate.
        #
        # It does not cost real nodes: of 67 positioned nodes on the same
        # capture, 66 had an SNR.
        if require_rf and not _has_rf(entry):
            drop["no_rf_measurement"] += 1
            continue

        lat, lon = _position(entry)
        if not _plausible_fix(lat, lon):
            drop["no_gps_sentinel" if INT32_SENTINEL in (abs(lat), abs(lon))
                 else "no_gps"] += 1
            continue

        heard = _heard_at(entry)
        if heard is None:
            drop["no_timestamp"] += 1
            continue
        if not (TS_FLOOR <= heard <= ceiling):
            drop["implausible_timestamp"] += 1
            continue
        if cutoff is not None and heard < cutoff:
            drop["older_than_since_days"] += 1
            continue

        name = str(user.get("longName") or user.get("shortName") or "").strip()
        record = {
            "node_id": node_id,
            "node_type": str(user.get("role")
                             or (DEFAULT_ROLE if roles_available else "UNKNOWN")),
            "name": name or node_id,
            "lat": lat,
            "lon": lon,
            # Absent rather than 0.0: the node DB carries rssi only for nodes
            # heard directly, and a zero would read as a real measurement.
            "rssi": _num_or_none(entry.get("rssi")),
            "first_seen": datetime.datetime.fromtimestamp(
                heard, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "type": envelope_type,
            "network": "meshtastic",
        }
        # public_key is deliberately never sent. Meshtastic 2.5+ has an X25519
        # key, but node_id is a device number rather than a key prefix, so the
        # server's "node_id must prefix public_key" check would reject every
        # record as key_prefix_mismatch. Same reason the upstream docs say
        # prefix merging is MeshCore only.
        hops = entry.get("hopsAway")
        if hops is not None:
            try:
                record["path_hops"] = int(hops)   # 0 is meaningful: direct
            except (TypeError, ValueError):
                pass
        out.append(record)

    uniq: dict[str, dict] = {}
    for record in out:
        uniq.setdefault(record["node_id"], record)
    drop["collapsed_repeats"] = len(out) - len(uniq)
    return list(uniq.values()), drop


def merge_records(sets: list[list[dict]]) -> tuple[list[dict], collections.Counter]:
    """Union several converted captures, field by field.

    The two readers are complementary rather than redundant, measured on a
    real capture: the radio's own node DB was a strict subset of meshchat's
    (66 of 75), because meshchat keeps nodes the radio has since aged out,
    while only the radio carries roles and only meshchat had recovered any
    rssi. Positions agreed to the digit across every shared node, which is
    also the cross-check that the 1e7 scaling is right.

    So the freshest sighting wins the record, and three fields are then
    filled from whichever capture actually has them:

      node_type  a known role beats UNKNOWN, always. This is the whole
                 reason to read the radio at all.
      rssi       a real measurement beats None.
      path_hops  present beats absent.

    Nothing is invented: every field still comes from a capture that
    recorded it, this only decides which capture to take it from.
    """
    merged: dict[str, dict] = {}
    stats: collections.Counter = collections.Counter()
    for records in sets:
        for rec in records:
            prior = merged.get(rec["node_id"])
            if prior is None:
                merged[rec["node_id"]] = dict(rec)
                stats["new"] += 1
                continue
            keep, other = ((rec, prior) if rec["first_seen"] > prior["first_seen"]
                           else (prior, rec))
            out = dict(keep)
            if out["node_type"] == "UNKNOWN" and other["node_type"] != "UNKNOWN":
                out["node_type"] = other["node_type"]
                stats["role_recovered"] += 1
            if out["rssi"] is None and other["rssi"] is not None:
                out["rssi"] = other["rssi"]
                stats["rssi_recovered"] += 1
            if "path_hops" not in out and "path_hops" in other:
                out["path_hops"] = other["path_hops"]
            merged[rec["node_id"]] = out
            stats["merged"] += 1
    return list(merged.values()), stats


def _has_rf(entry: dict) -> bool:
    """True when our radio actually measured a packet carrying this node.

    SNR is the evidence, not RSSI: the node DB carries SNR for anything heard
    over the air and rssi only for a subset. A relayed packet still counts,
    the antenna genuinely received it, and WDGWars accepts hopped sightings
    explicitly, trusting them less for position via `path_hops`.
    """
    if entry.get("viaMqtt"):
        # Stated by the source rather than inferred. The iOS backup carries
        # ZVIAMQTT per node; when it says the packet came over MQTT the node
        # was heard by the internet, not by an antenna, and no SNR sitting
        # beside it changes that.
        return False
    return entry.get("snr") not in (None, 0)


def _num_or_none(value):
    try:
        return float(value) if value not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Envelope + transport
# ---------------------------------------------------------------------------

def load_key(cli_key: str | None) -> str:
    """--key, then $WDGWARS_API_KEY, then the shared key file on this Mac."""
    if cli_key:
        return cli_key.strip()
    env = os.environ.get("WDGWARS_API_KEY")
    if env:
        return env.strip()
    path = pathlib.Path.home() / ".wdgwars" / "api_key"
    if path.is_file():
        return path.read_text().strip()
    raise SystemExit(
        "no API key. Pass --key, set WDGWARS_API_KEY, or put it in "
        f"{path} (chmod 0600).")


def build_envelope(records: list[dict], api_key: str) -> dict[str, str]:
    payload = {"networks": [], "aircraft": [], "meshcore_nodes": records}
    # Compact separators are load-bearing: the server recomputes the HMAC over
    # these exact bytes, so any added whitespace fails the signature check.
    data_b64 = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode()
    nonce = secrets.token_hex(8)
    sig = hmac.new(api_key.encode(), (nonce + data_b64).encode(),
                   hashlib.sha256).hexdigest()
    return {"data": data_b64, "nonce": nonce, "sig": sig}


def _request(url: str, api_key: str, body: bytes | None = None):
    req = urllib.request.Request(
        url, data=body, method="POST" if body else "GET",
        headers={"Content-Type": "application/json", "X-API-Key": api_key,
                 "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return 0, f"connection failed: {e.reason}"


def post_records(records: list[dict], api_key: str, endpoint: str,
                 dry_run: bool = False) -> int:
    failures = 0
    for i in range(0, len(records), BATCH_SIZE):
        chunk = records[i:i + BATCH_SIZE]
        env = build_envelope(chunk, api_key)
        body = json.dumps(env).encode()
        if dry_run:
            print(f"dry-run: {len(chunk)} records, {len(body)} bytes, "
                  f"sig={env['sig'][:12]}...")
            continue
        status, text = _request(endpoint, api_key, body)
        print(f"POST {len(chunk)} records ({len(body)} bytes) -> {status}")
        print(f"  {text}")
        if status != 200:
            failures += 1
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Meshtastic node DB -> WDGWars.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dump", nargs="*", default=[],
                   help="saved node-DB dump(s) to convert. Give more than "
                        "one to merge them, see merge_records()")
    p.add_argument("--dump", dest="dump_out", metavar="FILE",
                   help="read the radio and save to FILE (USB serial, "
                        "or TCP with --host)")
    p.add_argument("--port", help="serial port (auto-detected when omitted)")
    p.add_argument("--host", metavar="ADDR",
                   help="read the radio over TCP :4403 instead of USB serial, "
                        "for a node reachable only over wifi")
    p.add_argument("--preview", action="store_true",
                   help="print the converted records and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="build and sign the envelope but do not POST")
    p.add_argument("--probe", action="store_true",
                   help="POST exactly one record, to learn the server's "
                        "verdict before committing a full batch")
    p.add_argument("--whoami", action="store_true",
                   help="validate the API key against /api/me")
    p.add_argument("--since-days", type=float, metavar="N",
                   help="skip nodes not heard in the last N days")
    p.add_argument("--envelope-type", default="MESHTASTIC",
                   help="the record `type` constant (default: MESHTASTIC). "
                        "`network` is the authoritative field; this is the "
                        "one part of the contract not yet confirmed, which "
                        "is what --probe is for.")
    p.add_argument("--exclude-file", metavar="FILE",
                   help="file of node_ids never to upload, one per line, '#' "
                        "comments allowed. Defaults to own-nodes.txt beside "
                        "this script when it exists, so the safe behaviour is "
                        "automatic rather than remembered.")
    p.add_argument("--no-exclude-file", action="store_true",
                   help="ignore the default exclusion file")
    p.add_argument("--exclude", action="append", default=[], metavar="NODE_ID",
                   help="hold a node back from the upload. Repeatable. For "
                        "nodes you do not want published under your own "
                        "account regardless of what the mesh heard, e.g. a "
                        "device you carry that reports a real GPS fix.")
    p.add_argument("--allow-no-rf", action="store_true",
                   help="upload nodes our radio never measured (no SNR). "
                        "These are typically MQTT-injected, i.e. heard by "
                        "the internet rather than by your antenna. Off by "
                        "default on purpose.")
    p.add_argument("--key", help="WDGWars API key")
    p.add_argument("--api-url", default=DEFAULT_ENDPOINT)
    p.add_argument("--version", action="version", version=f"ratatoskr {__version__}")
    args = p.parse_args(argv)

    if args.whoami:
        status, text = _request(ME_ENDPOINT, load_key(args.key))
        print(status, text[:400])
        return 0 if status == 200 else 1

    if args.dump_out:
        dump = read_radio(args.port, args.host)
        pathlib.Path(args.dump_out).write_text(json.dumps(dump, indent=1))
        print(f"[OK] wrote {args.dump_out} ({len(dump['nodes'])} nodes)")
        # Dumping is not uploading. Stop here unless the caller also asked
        # for a conversion step, so `--dump` on a machine that happens to
        # hold an API key can never post unprompted.
        if not args.dump:
            print(f"next: ./ratatoskr.py {args.dump_out} --preview")
            return 0

    if not args.dump:
        p.error("give a dump file to convert, or --dump FILE to read the radio")

    sets, total, drop = [], 0, collections.Counter()
    for path in args.dump:
        dump = json.loads(pathlib.Path(path).read_text())
        recs, d = convert(dump, args.since_days, args.envelope_type,
                          require_rf=not args.allow_no_rf)
        print(f"{path}: {len(dump.get('nodes') or {})} entries -> {len(recs)} records "
              f"(roles_available={bool(dump.get('roles_available'))})")
        sets.append(recs)
        total += len(dump.get("nodes") or {})
        drop.update(d)
    records, mstats = merge_records(sets)
    # The standing list is applied unless explicitly waived. Catching a personal
    # device by eye worked twice and is not a control; a file is.
    ex_path = args.exclude_file or (pathlib.Path(__file__).parent / "own-nodes.txt")
    if not args.no_exclude_file and pathlib.Path(ex_path).is_file():
        for line in pathlib.Path(ex_path).read_text().splitlines():
            tok = line.split("#", 1)[0].strip()
            if tok:
                args.exclude.append(tok)
    if args.exclude:
        held = {e.lower().lstrip("!") for e in args.exclude}
        kept = [r for r in records if r["node_id"] not in held]
        for r in records:
            if r["node_id"] in held:
                print(f"HELD BACK    : {r['node_id']} {r['name']!r} "
                      f"at {r['lat']},{r['lon']}")
        records = kept
    if len(sets) > 1:
        print(f"merge        : {dict(mstats)}")

    print(f"node DB      : {total} entries across {len(sets)} capture(s)")
    for reason, n in sorted(drop.items()):
        if n:
            print(f"  dropped    : {n} {reason}")
    print(f"records      : {len(records)}")
    print(f"  with rssi  : {sum(1 for r in records if r['rssi'] is not None)}")
    print(f"  with hops  : {sum(1 for r in records if 'path_hops' in r)}")
    print(f"  roles      : {dict(collections.Counter(r['node_type'] for r in records))}")

    if not records:
        print("nothing to send.")
        return 1
    if args.preview:
        print(json.dumps(records[:6], indent=1))
        return 0

    api_key = load_key(args.key)
    if args.probe:
        print("\nprobing with ONE record to confirm the `type` constant:")
        print(json.dumps(records[0], indent=1))
        return 1 if post_records(records[:1], api_key, args.api_url) else 0
    return 1 if post_records(records, api_key, args.api_url, args.dry_run) else 0


if __name__ == "__main__":
    sys.exit(main())
