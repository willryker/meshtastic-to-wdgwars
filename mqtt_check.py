#!/usr/bin/env python3
"""Read ONLY the MQTT/uplink state off the node. Never `--info`.

`meshtastic --info` dumps the WiFi PSK, the MQTT password, the node's private
key and every channel PSK in one go. This prints an explicit allowlist of
non-secret fields instead, and reports credentials only as set/not-set, so it
is safe to run in a shared terminal or an agent session.
"""
from meshtastic.serial_interface import SerialInterface

iface = SerialInterface(devPath="/dev/meshtastic")
try:
    node = iface.localNode
    mq = node.moduleConfig.mqtt
    print("=== mqtt module config ===")
    for f in ("enabled", "tls_enabled", "json_enabled", "encryption_enabled",
              "proxy_to_client_enabled", "map_reporting_enabled", "root"):
        print(f"  {f:<24} = {getattr(mq, f, '(absent)')}")
    for f in ("address", "username", "password"):
        print(f"  {f:<24} = {'SET (value withheld)' if getattr(mq, f, '') else 'not set'}")

    print("=== channels: uplink publishes TO mqtt, downlink injects FROM it ===")
    for ch in node.channels:
        st = ch.settings
        if not st.name and ch.index and not st.uplink_enabled and not st.downlink_enabled:
            continue
        print(f"  ch{ch.index} name={st.name or '(primary/default)':<20} "
              f"role={ch.role} uplink={st.uplink_enabled} downlink={st.downlink_enabled}")

    net = node.localConfig.network
    print("=== network ===")
    for f in ("wifi_enabled", "eth_enabled"):
        print(f"  {f:<24} = {getattr(net, f, '(absent)')}")
    print(f"  {'wifi_ssid':<24} = {'SET (value withheld)' if getattr(net,'wifi_ssid','') else 'not set'}")
finally:
    iface.close()
