# Proposal: `ping_monitor` – high-frequency ICMP monitoring for Home Assistant

## 1. Goal

Continuously ping one or more internet hosts at roughly 1 packet per second and expose
detailed statistics in Home Assistant, mainly **packet loss** and **round-trip time
(RTT)**. Host, ICMP payload size and interval are configurable per target.

## 2. Existing solutions

| Solution | What it does | Gaps for this use case |
|---|---|---|
| **Core [Ping (ICMP)](https://www.home-assistant.io/integrations/ping)** | Polls every 30 s and sends a burst of `count` (default 5) echoes. Sensors for RTT min/avg/max/mdev, jitter and packet loss. Jitter and packet loss are disabled by default. | Sampling is burst-based, so outages between polls go unseen. Loss resolution is coarse (5 packets = 20 % steps). **Payload size is not configurable.** You can only change the interval by turning off polling and triggering from an automation. Until recently, packet loss went `unavailable` instead of `100 %` when a host was down ([#176826](https://github.com/home-assistant/core/issues/176826); fixes proposed in [#183744](https://github.com/home-assistant/core/pull/183744) and [#188394](https://github.com/home-assistant/core/pull/188394)). No percentiles, no rolling windows. |
| **[WANPulse](https://gh.r-mj.com/POLPROG-TECH/WANPulse)** (HACS) | Latency, jitter and loss for multiple targets, with dashboards. | Uses TCP, HTTP or DNS probes, not ICMP, so payload size is not a concept there. |
| **[Device Pulse](https://community.home-assistant.io/t/device-pulse-network-monitoring-for-ip-based-devices/959496)** (HACS) | Up/down availability of LAN devices. | Built on the core ping. Focused on availability, not statistics. |
| **[hanetprobe](https://github.com/toreamun/hanetprobe)** | Separate daemon that publishes RTT, jitter and loss over MQTT discovery. | Runs outside HA and needs MQTT. Limited statistics. |
| SmokePing, Prometheus blackbox_exporter, Telegraf `ping` + Grafana | Mature, high-quality latency and loss monitoring. | Separate stack. Getting the data into HA automations takes extra plumbing. |

**Conclusion:** none of these combines **continuous ~1 Hz ICMP probing, configurable payload
size, multiple targets and rich windowed statistics** inside HA. That justifies a small custom
integration distributed through HACS.

Contributing only "payload size" upstream to core `ping` is a reasonable side PR. However, a
continuous 1 Hz prober with rolling-window statistics does not fit core's polling and
data-coordinator conventions, so it belongs in a custom component.

## 3. Design overview

```
            ┌──────────────── per target (config entry) ────────────────┐
 icmplib    │  Prober task (asyncio)        SampleBuffer (ring)         │
 AsyncSocket│  send echo every `interval` ─► deque[(ts, seq, rtt|None)] │
 ──────────►│  match reply by id/seq        sized to largest window     │
            │  timeout ⇒ loss                                            │
            │                 │                                          │
            │                 ▼ every `publish_interval` (e.g. 10 s)     │
            │  StatsEngine: per-window loss %, RTT stats, jitter, bursts │
            │                 │                                          │
            │                 ▼                                          │
            │  Coordinator.async_set_updated_data() ─► sensors           │
            │  Outage tracker ─► binary_sensor + bus events              │
            └────────────────────────────────────────────────────────────┘
```

Key decisions:

1. **Decouple probing rate from state-write rate.** Probing runs at 1 Hz, but entities update
   every `publish_interval` (default 10 s). Writing ~15 sensors × N targets every second would
   bloat the recorder database and the event bus. Short spikes are not lost, because they show
   up in the window aggregates (max, p99, loss burst).
2. **One long-running asyncio task per target**, not a polling coordinator. A
   `DataUpdateCoordinator` with `update_interval=None` is used purely as a push hub
   (`async_set_updated_data`). Each probe is sent on its own schedule and not awaited
   serially, so a 2 s timeout never delays the next 1 s probe. Replies are matched by ICMP
   id and sequence number.
3. **Library: [`icmplib`](https://github.com/ValentinBELYN/icmplib)**, which core `ping`
   already uses, so there is no new dependency footprint. `ICMPv4Request`/`ICMPv6Request`
   support `payload_size` and `ttl`, and `AsyncSocket` gives per-packet control.
   - Runs **unprivileged** (`SOCK_DGRAM` ICMP) when `net.ipv4.ping_group_range` allows it,
     as on HA OS and most containers. It falls back to privileged raw sockets and reports a
     repair issue if neither works.
4. **One config entry per target.** This gives "multiple targets" for free: add the
   integration again for each host. Each target gets its own device, can be
   disabled/reloaded on its own, and gets its own diagnostics.

## 4. Configuration (UI config flow + options flow)

| Option | Default | Notes |
|---|---|---|
| `host` | – | Hostname, IPv4 or IPv6. Resolved at start and re-resolved periodically (e.g. every 5 min) so DNS changes are picked up. |
| `name` | host | Device name. |
| `payload_size` | 56 B | 0 – 65 500. Values above the path MTU minus 28 B (IPv4) cause fragmentation; the UI warns about this. |
| `interval` | 1.0 s | Minimum 0.2 s. |
| `timeout` | 2.0 s | A reply after the timeout counts as lost. |
| `windows` | 1 min, 5 min, 15 min | Rolling windows for statistics. Sensors are created per window. |
| `publish_interval` | 10 s | How often entities update. |
| `ip_family` | auto | auto / IPv4 / IPv6. |
| `ttl` | 64 | Optional. |
| `source` | – | Optional source address, for multi-homed hosts. |
| `down_after` | 5 consecutive losses | Threshold for the connectivity binary sensor. |
| `up_after` | 3 consecutive replies | Hysteresis for recovery. |

There is no YAML. Everything is in the config/options flow. Options changes reload only the
affected target.

## 5. Entities (per target)

Per window *W* (e.g. `5m`). The less important ones are disabled by default to keep the
entity count reasonable:

| Entity | Unit | state_class | Default |
|---|---|---|---|
| Packet loss (W) | % | measurement | ✅ |
| RTT average (W) | ms | measurement | ✅ |
| RTT median (W) | ms | measurement | ✅ |
| RTT min / max (W) | ms | measurement | ✅ |
| RTT p95 / p99 (W) | ms | measurement | p95 ✅, p99 ⬜ |
| RTT std deviation (W) | ms | measurement | ⬜ |
| Jitter (W) – mean of \|RTTₙ − RTTₙ₋₁\| (RFC 3550-style) | ms | measurement | ✅ |
| Longest loss burst (W) | packets | measurement | ⬜ |

Global (not per window):

| Entity | Notes |
|---|---|
| `binary_sensor` Connectivity | Uses the `down_after`/`up_after` hysteresis. Attributes: `last_reply`, `outage_started`. |
| Last RTT | Updated at publish time. Disabled by default. |
| Packets sent / received / lost | `total_increasing` counters, so you can build exact loss figures for any period with statistics or utility_meter. |
| Outages (count) | `total_increasing`. |
| Last outage duration | s |
| Resolved IP | diagnostic |

Because the sensors use `state_class: measurement`, HA's long-term statistics keep
hourly/5-minute min, mean and max without any extra work.

**Events** on the HA bus: `ping_monitor_outage_started` and `ping_monitor_outage_ended`,
with host, start, end, duration and lost packets. These are useful for automations such as
"notify when internet is down > 30 s" or "power-cycle the modem".

## 6. Extra features (phase 2)

- **Service `ping_monitor.get_samples`** returns raw `(ts, rtt)` samples for the last N
  seconds as response data, for ad-hoc analysis.
- **Diagnostics download** containing config, the current buffer summary and socket mode.
- **Packet-size sweep service**, which pings once with a range of payload sizes. It helps find
  MTU/fragmentation problems and justifies having payload size as a parameter at all.
- **Optional burst mode**, which reproduces core ping's behaviour for low-overhead targets.
- **Lovelace example**: a `statistics-graph` and an `apexcharts-card` config showing a
  SmokePing-style RTT band (min, median, p95, max) with loss colouring.

## 7. Implementation notes

- **Memory:** 1 Hz × 15 min window = 900 samples per target. That is negligible, even with
  dozens of targets.
- **Statistics computation:** recompute from the deque on each publish (O(n), n ≤ 900, every
  10 s). This is simpler and more correct than incremental algorithms, and with
  `statistics.quantiles` or a sort it costs microseconds. Optimise only if profiling says so.
- **Loss accounting edge cases:**
  - Late replies (arriving after the timeout) are counted as lost and logged at debug level.
  - Duplicate replies are ignored.
  - Probes still pending at window edges are excluded until they resolve or time out, so
    "in-flight" never counts as lost.
- **Host unreachable or DNS failure:** report loss as 100 % and RTT sensors as `unknown`.
  Do not mark the loss sensor `unavailable`, which is the exact problem core had.
- **Shutdown:** cancel prober tasks and close sockets in `async_unload_entry`.
- **Clock:** use `time.monotonic()` for RTT and window maths, and wall clock only for
  attributes and events.

## 8. Repository layout

```
custom_components/ping_monitor/
  __init__.py          # setup/unload, start prober
  manifest.json        # requirements: icmplib; iot_class: local_polling
  config_flow.py       # config + options flow
  const.py
  prober.py            # asyncio ICMP send/receive loop (no HA imports → unit-testable)
  stats.py             # pure functions over sample lists (no HA imports)
  coordinator.py       # push coordinator, outage tracker, events
  sensor.py
  binary_sensor.py
  diagnostics.py
  services.yaml
  strings.json / translations/en.json
tests/                 # pytest + pytest-homeassistant-custom-component
hacs.json
.github/workflows/     # hassfest, HACS validation, pytest, ruff
```

`prober.py` and `stats.py` have no HA dependency, so they can be unit-tested directly. They
could later be extracted into a small PyPI library, which is a prerequisite if this is ever
proposed for core.

## 9. Milestones

1. **MVP:** config flow (host, payload size, interval, timeout), prober, one 5-minute window,
   sensors for loss, RTT avg/min/max/p95 and jitter, a connectivity binary sensor, and tests
   for `stats.py`.
2. **Multiple windows**, counters, outage tracking with events, and the options flow.
3. **HACS packaging**, CI (hassfest, HACS action), README and a dashboard example.
4. **Phase 2 extras** (§6).

## 10. Open questions

1. Should the window set be fixed (1/5/15 min) or user-defined? The proposal is
   user-defined with those defaults.
2. Should all targets share one integration entry, or should each target be its own entry?
   The proposal is one entry per target, which is idiomatic in HA and simpler.
3. Do you need a raw per-packet history beyond the in-memory window (for example, writing
   to InfluxDB or a file)? Or are HA long-term statistics plus the window aggregates enough?
4. Is IPv6 needed in the MVP?
