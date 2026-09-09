# Ratatoskr

Meshtastic node DB to WDGWars. Sibling to Heimdall (`meshcore-to-wdgwars`) and
Muninn (`adsb-to-wdgwars`): same HMAC envelope, same `/api/upload/` endpoint,
same mesh payload slot. Heimdall fills that slot with `network: "meshcore"`,
this fills it with `network: "meshtastic"`.

Stdlib only, except the serial reader.

## Three readers, one converter

Reading needs hardware. Everything after it runs against a saved dump, so a
capture can be re-converted and re-uploaded without the radio.

```bash
# the Meshtastic iOS app's own backup store, richest of the three
./read_ios_backup.py > ios-nodes.json

# a meshchat database, read-only, no downtime
python3 dump_meshchat.py > nodes.json

# or from the radio directly, which needs the serial port free
./ratatoskr.py --dump nodes.json

# several dumps merge; the freshest sighting wins and roles/rssi/hops
# are filled from whichever capture actually has them
./ratatoskr.py ios-nodes.json nodes.json --preview

# then, anywhere the API key lives
./ratatoskr.py nodes.json --preview
./ratatoskr.py nodes.json --dry-run
./ratatoskr.py nodes.json --probe      # POST exactly one record
./ratatoskr.py nodes.json              # upload
```

## meshchat owns the radio

The Meshtastic serial API permits one client on the port at a time, and on
nab9 that client is the `meshchat` container. `--dump` will fail with a busy
or timeout error while it is up. Stop the stack first, or use
`dump_meshchat.py`, which reads the database instead and needs no downtime.

The board is pinned to `/dev/meshtastic` by a udev rule. `--dump` prefers that
name and only falls back to autodetection. Never pass `/dev/ttyACM0`, it moves.

## What the sources differ on

| | serial | meshchat.db |
|---|---|---|
| roles | yes | **no role column** |
| coordinates | int32 scaled 1e7 | already decimal degrees |
| rssi | per node, when heard directly | recovered from the `messages` table |
| downtime | stop meshchat first | none |

`roles_available` in the dump tells the converter which case it is. On a
serial read an absent role decodes as `CLIENT`, because protobuf3 omits a
field equal to its default and role 0 is CLIENT. From the database an absent
role means unknown, and every record converts as `node_type: UNKNOWN` rather
than a guessed CLIENT that would label every router and repeater on the mesh
a client. The server coerces an unknown node_type rather than rejecting it.

## Contract notes

Three things differ from Heimdall and each one silently corrupts a feed if
carried across:

**Coordinates are scaled 1e7, not 1e6.** Reusing MeshCore's scale puts every
node 10x out with numbers that still look plausible. There is also a range
gate, because an unset int32 coordinate arrives as `INT32_MAX` and divides
down to a finite float that passes a naive nonzero test. That is not
hypothetical: it put a node on the map at longitude 2147 during the MeshCore
upload this tool was written after.

**`public_key` is never sent.** Meshtastic 2.5+ has an X25519 key, but a
Meshtastic `node_id` is a device number rather than a key prefix, so the
server's "node_id must prefix public_key" check rejects every record as
`key_prefix_mismatch`. This is also why the upstream docs say prefix merging
is MeshCore only: a Meshtastic id never gets merged upward later, so an 8 hex
id is final rather than provisional.

**`node_id` comes from `num`, not from parsing `user.id`.** The number is the
identity and `!xxxxxxxx` is a rendering of it. Masked to 32 bits and
zero-padded, so a low node number still clears the server's 8 hex floor.

`first_seen` is `lastHeard`, the reception, not `position.time`, which is when
the fix inside the packet was taken and can be far older. `hopsAway` maps to
`path_hops`, where 0 is meaningful and means direct.

## Open question

The record `type` constant is `"MESHCORE"` for that family. What a Meshtastic
record should carry, and whether it matters now that `network` is
authoritative, is not confirmed. `--envelope-type` sets it, default
`MESHTASTIC`, and `--probe` posts exactly one record so the server's verdict
is known before a full batch goes.

## A node with no position is never uploaded

Nodes without a fix are dropped as `no_gps`. This is what keeps the house off
the map: the nab9 node carries no position on purpose, because channel 0 has
`uplinkEnabled` and giving the radio a position would publish the house to the
public mesh. Confirmed on the current capture, the local node is absent from
the upload set. Do not add a fallback that fills a missing position from
`MESH_ORIGIN_LAT` / `MESH_ORIGIN_LON`, which exist for centring the map only.

## Key

`--key`, then `$WDGWARS_API_KEY`, then `~/.wdgwars/api_key`. The key stays on
whatever machine runs the upload, which is why the reader and the uploader are
separate: nab9 reads the radio, it never needs the credential.

## Licence

MIT. See `LICENSE`.

## The iOS app backup

The Meshtastic iOS app writes a CoreData/SwiftData SQLite database per
connected node into iCloud Drive, at
`~/Library/Mobile Documents/com~apple~CloudDocs/Meshtastic/<nodeNum>/Meshtastic.store`,
indexed by `backup-index.json`. `read_ios_backup.py` reads every store it finds,
read-only through a `file:...?mode=ro` URI, and merges them.

It is the best of the three sources. It carries an explicit **`ZVIAMQTT`** flag
per node, so MQTT provenance is *stated* rather than inferred from a missing
SNR, plus a real first-heard timestamp and position history (`ZLATEST = 1` is
current).

Do not bother with the app's "Application Logs" CSV export. Measured on a
442-line export: one position-bearing line shape, two instances, and no SNR
field anywhere, so nothing from it can satisfy the provenance gate.

Two traps, both silent:

- **Timestamps are Apple epoch (2001-01-01), not unix.** Read raw they land in
  1995 and fail every plausibility floor. Add `978307200`.
- **`ZROLE` is an integer**, on the `ZUSERENTITY` row rather than the node row,
  and an unrecognised value is left absent rather than guessed.

## Provenance: only what your own antenna heard

A node is uploaded only when this capture can show your radio measured a packet
from it. Two rules do that, and neither is optional:

- **No SNR, no upload** (`no_rf_measurement`). A node heard over the air has an
  SNR; one handed to you over the internet does not. A relayed packet still
  counts, your antenna did receive it, and the hop count rides along in
  `path_hops` so the server can discount it.
- **`viaMqtt` is rejected outright**, whatever SNR sits beside it.

`--allow-no-rf` waives the first. It exists for completeness and should stay
unused: uploading nodes you never heard turns your feed into a copy of the
public map.

## Never upload your own devices

See `own-nodes.example.txt`. Copy it to `own-nodes.txt`, which is gitignored and
read automatically. This is the one gate that is a privacy decision rather than
a data-quality one, so it lives in a list you control.
