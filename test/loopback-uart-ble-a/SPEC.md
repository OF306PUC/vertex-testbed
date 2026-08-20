# Loopback test A — specification for implementation

Direction A: **the Pi advertises, the peer scans and reports over UART.**

```
   Pi  ──[HCI 0x2008]──►  advertise AdvData  ──BLE──►  peer scans
   Pi  ◄────────────── UART: r <AdvData, rssi, us> ───  peer reports
```

The UART is reliable and does not use the radio, so it is the **reference**: any
difference between what the Pi advertised and what came back over UART is
attributable to BLE. That is the whole reason for a second channel.

**The assertion is byte equality, not value equality.** The peer does not parse, so
comparing the reported hex against the AdvData we assembled checks element
structure, company ID, field order and endianness in a single comparison.

---

## 1. Modules to write

Three, with one responsibility each. Keeping the peer driver and the advertiser
apart matters: they fail for unrelated reasons, and a combined module makes a
serial fault look like a radio fault.

```
test/loopback-uart-ble-a/
├── peer.py             serial link + peer driver
├── radio/advertiser.py Pi-side BLE transmit, over vertex.radio
└── run_a.py            sequence, accounting, report
```

Everything they build on already exists and is tested: `vertex.serial.proto`,
`vertex.radio.hci`, `vertex.radio.ad`, `vertex.wire`.

---

## 2. `peer.py` — the serial peer driver

### 2.1 Link

```python
class Peer:
    def __init__(self, port: str = "/dev/ttyACM0", baud: int = 115200,
                 idle_timeout_s: float = 0.1) -> None
    def open(self) -> None
    def close(self) -> None            # idempotent
    def __enter__ / __exit__
```

`pyserial` with `timeout=0.01` on the read is enough — this is a test harness, not
an agent, so a simple read loop in a thread is fine and avoids asyncio entirely.

### 2.2 Receive loop

One thread, doing exactly this:

```python
data = self._ser.read(4096)             # returns b"" on timeout
if data:
    for frame in self._parser.feed(data):
        self._dispatch(frame)
    self._last_rx = time.monotonic()
elif self._parser.in_frame and time.monotonic() - self._last_rx > self.idle_timeout_s:
    self._parser.timeout()              # REQUIRED -- see below
```

**The `timeout()` call is not optional.** A mid-payload `0x7E` is deliberately not
treated as a frame start, so a truncated frame waits forever *and consumes the
following frames as payload* while it waits. Without the idle timeout, one
interrupted write makes the peer appear to go silent permanently. There is a test
for both halves of this in `tests/test_peer_proto.py`.

### 2.3 Dispatch

| Frame | Action |
|---|---|
| `r` ADV_REPORT | `decode_adv_report()`, timestamp **on arrival**, push to a queue |
| `k` ACK | resolve the pending command |
| `e` ERR | resolve as a failure, carrying `decode_ack()`'s status |
| `p` PONG | resolve, store `decode_pong()` |
| `q` STATS | resolve, store `decode_stats()` |

Timestamp inside the dispatch, before queueing. A timestamp taken after queueing
measures your own scheduler, not the link.

### 2.4 Commands

```python
def send(self, frame_type: int, payload: bytes = b"") -> None
def request(self, frame_type: int, payload: bytes = b"",
            timeout: float = 1.0) -> Frame       # waits for k/e/p/q
def set_radio(self, *, adv_min, adv_max, scan_interval, scan_window,
              active_scan=False, advertising=False) -> None
def ping(self) -> int                            # peer uptime_us
def stats(self) -> PeerStats
def reports(self) -> Iterator[AdvReport]         # drains the queue
```

`request()` should **raise on `e`**, naming the frame type and status. A silently
ignored rejection is how you end up sweeping a scan window the peer never applied.

Note `advertising=False` for direction A: the peer only scans here. Leaving its
advertiser on would put its own packets on the air and into your reports.

### 2.5 Robustness

- A malformed line or unknown frame type is **counted, not raised**. The peer logs
  on boot and on reset, and a stray frame must not end a 500-transmission run.
- Expose `parser.stats` so `crc_errors` and `timeouts` are visible in the report.
- `close()` must be safe to call twice — it runs from `__exit__` and from error paths.

---

## 3. `radio/advertiser.py` — Pi-side transmit

```python
class Advertiser:
    def __init__(self, device: int = 0,
                 interval_ms: float = 100.0,
                 channel_map: int = CHANNELS_ALL,
                 name: bytes = b"LABCTRL",
                 company_id: int = 0x0059) -> None
    def open(self) -> None
    def close(self) -> None            # idempotent; disable adv before closing
    def advertise(self, packet: StatePacket) -> bytes   # returns the AdvData sent
```

### 3.1 Setup, in this order

Parameters are only settable while the function is **disabled**, or the controller
returns `0x0C command disallowed`:

```python
sock.command(cmd_reset())
sock.command(cmd_le_set_adv_parameters(
    interval_min=ms_to_units(interval_ms),
    interval_max=ms_to_units(interval_ms),
    adv_type=ADV_NONCONN_IND,
    channel_map=channel_map))
sock.command(cmd_le_set_adv_data(ad))          # some payload must be set first
sock.command(cmd_le_set_adv_enable(True))
```

Use `sock.command()`, never `sock.send()`, for every setup step. It raises on a
non-zero status. A refused `set adv parameters` leaves the *previous* interval in
force, and the run then measures a configuration nobody chose.

### 3.2 Per transmission

```python
ad = build_ad(element(AD_NAME_COMPLETE, self.name),
              element(AD_MANUFACTURER,
                      manufacturer_value(self.company_id, packet.encode())))
self._sock.command(cmd_le_set_adv_data(ad))
return ad
```

`0x2008` alone — advertising need not stop for a **data** change, only for a
**parameter** change. Stopping and restarting would reset the advertising cadence
on every packet and make the interval sweep meaningless.

Return the AdvData so the caller can assert byte equality against it later. Do not
re-derive it at comparison time; that would compare an encoder against itself.

### 3.3 Teardown

`cmd_le_set_adv_enable(False)`, then close the socket. Bring the adapter back with
`hciconfig hci0 up` outside the process, or note in the report that it is down.

---

## 4. `run_a.py` — sequence and accounting

### 4.1 Parameters

```
--port /dev/ttyACM0     --device 0
--count 500             transmissions
--period 0.2            seconds between transmissions
--adv-interval 100      ms
--scan-interval 100     ms   (peer)
--scan-window 100       ms   (peer; == interval is 100% duty)
--channel-map 0x07
--node 200              Pi id      (201 = peer; both outside any real experiment)
--wire v1               or v0
```

### 4.2 Sequence

```
1. peer.open()
2. peer.set_radio(scan_interval=…, scan_window=…, advertising=False)
3. before = peer.stats()
4. advertiser.open()
5. for seq in 1..count:
       pkt = StatePacket.from_state(node, vstate=seq * 1e-6, seq=seq & 0xFFFF,
                                    tx_time_us=<monotonic µs>)
       ad = advertiser.advertise(pkt)
       sent[seq] = (ad, time.monotonic())
       drain peer.reports() into received
       sleep(period)
6. sleep(3 × adv_interval)          # let the last few reports arrive
7. drain peer.reports() again
8. after = peer.stats()
9. advertiser.close(); peer.close()
10. report
```

Step 6 matters: without it the last transmissions are counted as lost purely
because the run ended before they were reported.

`vstate = seq * 1e-6` makes every payload unique. Not strictly required here —
the peer reports duplicates — but it means the same harness works unchanged
against the coordination firmware, which filters them.

### 4.3 Matching reports to transmissions

Decode the manufacturer payload from each report and read its `seq`:

```python
payload = find_manufacturer(report.data, company_id)
if payload is None:
    stats.foreign += 1          # someone else's advertiser; expected, count it
    continue
pkt = decode_any(payload)
if pkt.node_id != node:
    stats.other_node += 1       # not ours
    continue
```

Then the two assertions:

```python
ad_sent, t_send = sent[pkt.seq]
assert report.data == ad_sent            # BYTE equality -- the primary result
rtt = t_recv - t_send
```

A `seq` not in `sent` is a **duplicate or a stale report**, not a delivery. Count
it separately; do not let it inflate the ratio.

### 4.4 Report

```
transmissions      500
delivered          487        (97.4%)
byte mismatches    0          <- must be zero; nonzero is a bug, not radio
duplicates         12
foreign adverts    143        (other devices nearby)
rtt                median 41 ms   min 8 ms   max 118 ms
peer internal      queue_dropped 0   tx_dropped 0   <- else the ratio is void
peer uart          partial_flushes 1042   crc_errors 0   timeouts 0
```

Four things this must make impossible to miss:

**Byte mismatches are a bug, never a radio condition.** Loss is expected and
measured; corruption is not. A nonzero count invalidates the run and means an
encoder, endianness or AD-structure fault.

**`queue_dropped` or `tx_dropped` nonzero voids the delivery ratio.** A report the
peer dropped internally is indistinguishable, in the final number, from a packet
lost over the air. Subtract `after − before` and say so loudly.

**`partial_flushes == 0` means the peer's UART is misconfigured.** It counts DMA
idle-timeout flushes, which only work with `CONFIG_UART_0_NRF_HW_ASYNC` and a free
timer. Zero after the host has sent frames means short frames are stuck in the DMA
buffer, and the whole run is suspect.

**RTT is not a radio latency.** It includes BLE transit, peer scan latency, UART
transit, and up to one advertising interval. Report it with those components named.
A clean one-way delay is not obtainable — the peer's `uptime_us` and the Pi's clock
share no origin (`docs/CLOCK_MODEL.md` §4). `ping()` bounds the offset to about a
UART round trip, ~1 ms, which is enough to separate interval effects and useless
for characterising the radio.

### 4.5 Recording

If logging through `RunLog`, use `units="scaled_int"` and put the full radio
configuration in `RunMeta.environment` — it cannot be reconstructed afterwards and
is the usual reason two runs disagree:

```python
environment = {
    "direction": "A", "wire_version": 1,
    "pi_adv_interval_ms": 100, "pi_channel_map": 0x07,
    "peer_scan_interval_ms": 100, "peer_scan_window_ms": 100,
    "duplicate_filtering": False,
    "wifi_channel": 11, "power_save": "off", "wifi_load": "idle",
}
```

---

## 5. Testable without hardware

Write these first; they are where the mistakes will be.

- **Report matching.** Feed synthesised `AdvReport`s — correct, foreign company,
  wrong node, unknown seq, duplicate seq — and assert each lands in the right
  counter. Pure logic over the structures.
- **Byte equality detection.** Flip one bit in a report's data and confirm the
  mismatch counter moves. If that test does not fail, the primary assertion is
  not wired up.
- **Peer dispatch.** Build frames with `build_frame()` and feed them to the
  dispatcher directly, with no serial port. Include a truncated frame followed by
  a good one, and assert recovery *after* `timeout()`.
- **Advertiser command sequence.** Inject a fake socket that records packets, and
  assert the exact byte sequence and its order. This catches a parameter change
  attempted while advertising is enabled — which on hardware is an opaque `0x0C`.

## 6. Prerequisites on the Pi

```bash
sudo hciconfig hci0 down          # release the adapter to HCI_CHANNEL_USER
ls -l /dev/ttyACM*                # confirm the peer's CDC device
bash scripts/radio_check.sh       # channel, power save, driver
```

Needs `CAP_NET_ADMIN` — run as root, or
`setcap cap_net_admin,cap_net_raw+eip $(readlink -f $(which python3))`.
