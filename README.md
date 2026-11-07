# Ping Monitor for Home Assistant

Continuous ICMP monitoring of internet (or LAN) hosts, about one echo request per
second, with detailed **packet loss** and **round-trip time** statistics over a rolling
window. Every target has its own host, payload size, interval and window.

The core [Ping (ICMP)](https://www.home-assistant.io/integrations/ping) integration
sends a burst of 5 pings every 30 s. This integration pings continuously, so short
outages aren't missed, and the payload size can be set. See [PROPOSAL.md](PROPOSAL.md)
for the full design and the comparison with existing integrations.

## Installation

**HACS:** add this repository as a custom repository (category *Integration*), install
**Ping Monitor** and restart Home Assistant.

**Manual:** copy `custom_components/ping_monitor` into your `config/custom_components/`
and restart.

Then go to **Settings → Devices & services → Add integration → Ping Monitor**. Add the
integration once per target.

## Options

| Option | Default | |
|---|---|---|
| Host | – | Host name or IPv4 address |
| Name | host | Device name |
| ICMP payload size | 56 B | 0 – 65 507. Above 1472 B, packets get fragmented on a 1500 B MTU path |
| Ping interval | 1 s | 0.2 – 60 s |
| Timeout | 2 s | Later replies count as lost |
| Statistics window | 300 s | 10 – 3600 s of history for all statistics |
| Update interval | 10 s | How often the sensors update. Probing is unaffected |
| TTL | 64 | |
| Down after / Up after | 5 / 3 | Consecutive lost / answered pings that flip *Connectivity* |

All options except the host can be changed later under **Configure**. A change reloads
only that target.

## Entities (per target)

| Entity | Enabled by default |
|---|---|
| Packet loss (%) | ✅ |
| RTT average / median / min / max / 95th percentile (ms) | ✅ |
| Jitter: mean difference between consecutive RTTs (ms) | ✅ |
| RTT 99th percentile, RTT standard deviation, longest loss burst | – |
| Packets lost, Outages (counters) | ✅ |
| Packets sent / received (counters), Last RTT | – |
| Last outage duration (s) | ✅ |
| Connectivity (binary sensor, updates immediately) | ✅ |
| Resolved address (diagnostic) | ✅ |

Window statistics carry a `window` attribute (in seconds). All measurement sensors have a
`state_class`, so Home Assistant keeps long-term min, mean and max statistics for them.

### Events

`ping_monitor_outage_started` and `ping_monitor_outage_ended` fire when *Connectivity*
changes. Both carry `entry_id`, `name`, `host`, `address` and `started`. The ended event
also carries `ended`, `duration` (s) and `lost_packets`.

```yaml
automation:
  - alias: Internet outage longer than 30 s
    triggers:
      - trigger: event
        event_type: ping_monitor_outage_ended
        event_data: { host: 1.1.1.1 }
    conditions: "{{ trigger.event.data.duration > 30 }}"
    actions:
      - action: notify.notify
        data:
          message: "Internet was down for {{ trigger.event.data.duration }} s
            ({{ trigger.event.data.lost_packets }} packets lost)."
```

## How it works

- Each target runs its own asyncio task that sends one echo request per interval over its
  own ICMP socket. Probes overlap, so a slow reply never delays the next probe. Replies
  are matched by sequence number.
- RTTs use **kernel receive timestamps** (`SO_TIMESTAMPNS`), so a busy Home Assistant event
  loop doesn't inflate the measured latency.
- Sensors update every *update interval* from an in-memory window. Nothing is written per
  packet, so the recorder database stays small.
- Packets still in flight are excluded from the statistics until they're answered or
  time out.
- If the host can't be resolved, every probe counts as lost (100 % loss) instead of the
  sensors going `unavailable`. Host names are re-resolved every 5 minutes.

### ICMP permissions

The integration first tries an unprivileged ICMP socket, which works when
`net.ipv4.ping_group_range` includes Home Assistant's group. That is the case on HA OS
and most Docker setups. If that fails, it falls back to a raw socket, which needs root or
`CAP_NET_RAW`. If neither works, setup fails with an explanatory error.

## Limitations

- IPv4 only. IPv6 is planned.
- Counters reset when Home Assistant restarts. They are `total_increasing`, so long-term
  statistics handle the reset.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

`icmp.py`, `prober.py` and `stats.py` don't import Home Assistant and can be tested on
their own.

## License

[MIT](LICENSE)
