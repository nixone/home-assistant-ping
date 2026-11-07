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
4. **One config entry per target** (see §4.1 for the trade-off). This gives "multiple
   targets" for free: add the integration again for each host. Each target gets its own
   device, can be disabled/reloaded on its own, and gets its own diagnostics.
5. **IPv4 only in the MVP.** The prober is written against an address-family parameter, so
   IPv6 can be added later without changing its structure.

## 4. Configuration (UI config flow + options flow)

| Option | Default | Notes |
|---|---|---|
| `host` | – | Hostname or IPv4 address. Resolved at start and re-resolved periodically (e.g. every 5 min) so DNS changes are picked up. |
| `name` | host | Device name. |
| `payload_size` | 56 B | 0 – 65 500. Values above the path MTU minus 28 B (IPv4) cause fragmentation; the UI warns about this. |
| `interval` | 1.0 s | Minimum 0.2 s. |
| `timeout` | 2.0 s | A reply after the timeout counts as lost. |
| `window` | 5 min | The one rolling window for this target's statistics, from 10 s to 1 h. Each target can use a different window. |
| `publish_interval` | 10 s | How often entities update. |
| `ttl` | 64 | Optional. |
| `source` | – | Optional source address, for multi-homed hosts. |
| `down_after` | 5 consecutive losses | Threshold for the connectivity binary sensor. |
| `up_after` | 3 consecutive replies | Hysteresis for recovery. |

There is no YAML. Everything is in the config/options flow. Options changes reload only the
affected target.

### 4.1 One entry per target vs. one entry for all targets

| | **A. One config entry per target** (recommended) | **B. One entry with a subentry per target** | **C. One entry with a target list in options** |
|---|---|---|---|
| UX | Settings → Integrations shows one row per host. Add a target with "Add entry". | One integration row. Targets appear as subentries with their own "Add target" button. | One row. Targets are edited in a long options form. |
| Reload when a target is edited | **Only that target.** Other targets keep their buffers and statistics. | HA reloads the **whole parent entry**, so every target's window starts over. | Whole entry, every target resets. |
| Shared settings (e.g. update interval, privileged mode) | Repeated per target. In practice the defaults are fine, so this rarely matters. | Natural place for them: the parent entry. | Natural. |
| Failure isolation | A broken target (DNS failure, socket error) can't affect the others. | Shared lifecycle, so one setup error can block everything. | Same as B. |
| Code complexity | Lowest. A standard config flow and options flow. | Medium. Subentry flows, which need HA ≥ 2025.3. | Highest. List editing in a flow is clumsy and error-prone. |
| Sockets | One ICMP socket per target. Harmless at a few targets; a shared socket can be added internally later if needed. | One shared socket is easy. | One shared socket is easy. |
| Device / entity model | Device per entry. Fits HA exactly. | Device per subentry. Also fits. | Devices must be created by hand. |

**Decision: A.** It is the simplest option and isolates targets fully. Most importantly,
editing one target doesn't reset the statistics window of every other target. B's
advantage, a shared place for global settings, is small here, since every setting is per
target anyway. If the number of targets grows large (more than ~10), moving to B later
is possible with a config-entry migration.

## 5. Entities (per target)

All statistics use the target's single configured window, so entity names have no window
suffix (e.g. `sensor.google_dns_packet_loss`). The window length is exposed as an
attribute. The less important entities are disabled by default:

| Entity | Unit | state_class | Default |
|---|---|---|---|
| Packet loss | % | measurement | ✅ |
| RTT average | ms | measurement | ✅ |
| RTT median | ms | measurement | ✅ |
| RTT min / max | ms | measurement | ✅ |
| RTT p95 / p99 | ms | measurement | p95 ✅, p99 ⬜ |
| RTT std deviation | ms | measurement | ⬜ |
| Jitter – mean of \|RTTₙ − RTTₙ₋₁\| (RFC 3550-style) | ms | measurement | ✅ |
| Longest loss burst | packets | measurement | ⬜ |

Global (not per window):

| Entity | Notes |
|---|---|
| `binary_sensor` Connectivity | Uses the `down_after`/`up_after` hysteresis. Attributes: `last_reply`, `outage_started`. |
| Last RTT | Updated at publish time. Disabled by default. |
| Packets sent / received / lost | `total_increasing` counters, so you can build exact loss figures for any period with statistics or utility_meter. |
| Outages (count) | `total_increasing`. |
| Last outage duration | s |
| Resolved IP | diagnostic |

**History comes from HA statistics only, with no raw per-packet storage.** Every sensor has
`state_class: measurement`, so the recorder keeps 5-minute and hourly min, mean and max
for each one indefinitely. Combined with the window aggregates (max, p95, loss) and the
`total_increasing` packet counters, this covers trend analysis without bloating the
database. Raw samples exist only in memory, for the length of the window.

**Events** on the HA bus: `ping_monitor_outage_started` and `ping_monitor_outage_ended`,
with host, start, end, duration and lost packets. These are useful for automations such as
"notify when internet is down > 30 s" or "power-cycle the modem".

## 6. Extra features (phase 2)

- **Diagnostics download** containing config, the current buffer summary and socket mode.
- **Packet-size sweep service**, which pings once with a range of payload sizes. It helps find
  MTU/fragmentation problems and justifies having payload size as a parameter at all.
- **IPv6 support.**
- **Optional burst mode**, which reproduces core ping's behaviour for low-overhead targets.
- **Lovelace example**: a `statistics-graph` and an `apexcharts-card` config showing a
  SmokePing-style RTT band (min, median, p95, max) with loss colouring.

## 7. Implementation notes

- **Memory:** 1 Hz × 1 h maximum window = 3 600 samples per target. That is negligible,
  even with dozens of targets.
- **Statistics computation:** recompute from the deque on each publish (O(n), n ≤ 3 600, every
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

1. **MVP:** IPv4 only. Config flow (host, payload size, interval, timeout, window), prober,
   sensors for loss, RTT avg/min/max/p95 and jitter, a connectivity binary sensor, and tests
   for `stats.py`.
2. **Options flow**, packet counters, outage tracking with events, and the remaining sensors.
3. **HACS packaging**, CI (hassfest, HACS action), README and a dashboard example.
4. **Phase 2 extras** (§6).

## 10. Decisions

1. **Statistics window:** one window per target, set by the user (default 5 min).
2. **Entry model:** one config entry per target (§4.1).
3. **History:** HA long-term statistics only. No raw per-packet export.
4. **IPv6:** not in the MVP. Planned for phase 2.
