# Specification: serial + BLE-broadcast transports, and the loopback test

**Status:** specification for implementation. No code written; this is what to build
against.

**Purpose: exercising the Python transport modules.** Not an experiment. So the
nRF's job here is to be a *measuring instrument* — predictable, transparent, and
doing nothing else — rather than to run a coordination algorithm.

That is why §2 specifies a **new, purpose-built test firmware** instead of reusing
the coordination firmware. The existing firmware works, but as a test peer it
contributes four constraints and a running control law, all of which are confounds
when the thing under test is a codec and a socket. Its constraints are recorded in
Appendix A, because validating against it *is* worth doing — later, as an
integration test, once the Python side is known-good.

---

## 1. What the test is, and what it proves

One nRF52-DK on USB to one Raspberry Pi. Two independent channels between them:

```
        ┌───────────────── BLE 2.4 GHz (the channel under test) ──────────────┐
        │                                                                     │
   ┌────┴─────┐                                                        ┌──────┴───┐
   │   Pi     │                                                        │  nRF52   │
   │ (Python) │◄──────────── USB / UART (the reference channel) ───────►│   (C)    │
   └──────────┘                                                        └──────────┘
```

The UART is reliable, fast, and does not use the radio. That is what makes it the
**reference**: any discrepancy between what went over BLE and what came back over
UART is attributable to BLE. Without a second channel there is nothing to compare
against, and "did the packet arrive?" becomes unanswerable.

Two directions, tested separately, because they exercise different code:

| | Path | Exercises | Reference |
|---|---|---|---|
| **A** | Pi advertises → nRF scans → nRF reports over UART | Pi **TX** (new HCI code), nRF RX (existing) | the neighbour column of the nRF's `d` line |
| **B** | nRF advertises → Pi scans over HCI | nRF TX (existing), Pi **RX** (new HCI code) | the `vstate` column of the nRF's `d` line |

**Direction B is the more valuable one.** Pi-side scanning is what PLATFORM.md §3 A1
identified as unreachable through BlueZ: scan interval and window are absent from
the D-Bus API entirely. Direction B is the first thing that demonstrates the new
path actually reaches them.

What the test does *not* prove: coexistence, airtime effects, or anything about
convergence. It proves the transports move bytes correctly and lets you measure
delivery ratio against controlled adv/scan parameters.

---

## 2. The test peer firmware

A single-purpose Zephyr application: a **transparent bridge between UART and the BLE
advertising channel.** It parses nothing, filters nothing, and computes nothing.

```
   UART  "TX <hex>"  ──────►  advertise those exact bytes
   UART  "RX <hex>,<rssi>,<us>"  ◄──────  every advertising report, verbatim
```

### 2.1 Why transparent, rather than reusing the coordination firmware

Because the thing under test is the Python codec and the HCI path, and every layer of
interpretation on the peer is a place for the test to lie to you:

| Coordination firmware | Test firmware | Why it matters here |
|---|---|---|
| Manufacturer data must be **exactly 8 bytes** | any length | **v1 can be tested directly.** No need to fall back to v0 |
| Sender must be in a neighbour whitelist | no filtering | A wrong id fails visibly, not silently |
| Scans with `FILTER_DUPLICATE` | duplicates **reported** | Loss becomes measurable; identical payloads are legal |
| Reports only while the algorithm runs, aggregated per neighbour | reports **every** report, immediately | One advertisement in, one line out — countable |
| `vstate` moves as the law integrates | nothing moves unless commanded | The reference value is stable |

The last row is the one that matters most. With a transparent bridge the Pi
**commands exactly what goes on the air** and **sees exactly what came off it**, so
the wire format is verified byte-for-byte across the C/Python boundary. Given that
the two existing implementations of this project's control law disagree in four ways
(`docs/FIRMWARE_DIVERGENCE.md`), a byte-exact cross-implementation check is worth
having as a permanent fixture rather than a one-off.

### 2.2 Build target

A separate application, not a mode of the existing one:

```
firmware/
├── nordic/            coordination firmware (unchanged)
└── nordic-testpeer/   this
```

Keeping them separate means the instrument cannot drift when the experiment
firmware changes, and the experiment firmware carries no test code.

### 2.3 UART protocol

115200 8N1. Lines terminated `\n`. **Hex, not binary** — a text protocol survives a
partial line, is readable in a terminal, and cannot be confused with framing.

**Pi → nRF**

| Command | Meaning |
|---|---|
| `TX <hex>` | Advertise this AD payload verbatim. Hex is the **complete AD data**, so the Pi controls every byte including the element structure |
| `TXOFF` | Stop advertising |
| `ADV <interval_min> <interval_max> <chan_map>` | Set the nRF's advertising parameters (0.625 ms units, map bitmask) |
| `SCAN <interval> <window> <passive|active> <dup0|dup1>` | Set and restart scanning |
| `SCANOFF` | Stop scanning |
| `PING` | Reply `PONG <uptime_us>` — clock-offset estimation, §6.3 |
| `VER` | Reply `VER <fw> <bt_features_hex>` |

**nRF → Pi**

| Report | Meaning |
|---|---|
| `RX <hex>,<rssi>,<uptime_us>` | One advertising report. `hex` is the **raw AD bytes**, unparsed |
| `TXAT <seq>,<uptime_us>` | Emitted when a `TX` actually reaches the controller |
| `ERR <text>` | Anything rejected, with a reason |
| `PONG <uptime_us>` | Reply to `PING` |

Requirements:

- **Raise the buffers.** The coordination firmware's 64-byte RX buffer is the reason
  its configuration is split across three commands. A `TX` line for a 31-byte AD
  payload is 62 hex characters plus the command — set `RX_BUFF_SIZE` and
  `TX_BUFF_SIZE` to **256** and the fragmentation problem disappears.
- **No inactivity-timeout parsing.** Parse on `\n`, not on a 25 ms silence
  (`RX_TIMEOUT`). That removes the ≥ 50 ms inter-command gap the Pi currently has to
  respect, and with it a whole class of merged-command bugs.
- **Report every advertisement**, with no aggregation and no per-neighbour slot. If
  the UART cannot keep up, **drop and count**, and expose the drop count via `VER` or
  a `STAT` reply — a silently dropped report would be indistinguishable from BLE loss,
  which is the one confusion this instrument exists to prevent.
- **Timestamp in microseconds** at the moment of the scan callback, from
  `k_uptime_ticks()` converted with `k_ticks_to_us_floor64()`. Millisecond resolution
  is too coarse for advertising-event timing.

### 2.4 BLE implementation notes

- Advertise with `BT_LE_ADV_NCONN` (non-connectable undirected), payload set from the
  `TX` bytes. `bt_le_adv_update_data()` updates the payload without stopping.
- Scan with `BT_LE_SCAN_OPT_NONE` — **duplicate filtering off**, the opposite of the
  coordination firmware. Every report reaches the Pi.
- In the scan callback, take the raw bytes from `net_buf_simple` **before** any
  `bt_data_parse()`. Hex-encode `ad->data[0..ad->len]` and emit. Do not parse; parsing
  is the Python side's job and duplicating it here would hide a disagreement between
  the two.
- Set TX power explicitly, reusing `set_tx_power()` from `broadcaster.c`, and report
  the value the controller actually selected — it is often not the value requested.
- `prj.conf`: `CONFIG_BT_BROADCASTER=y`, `CONFIG_BT_OBSERVER=y`,
  `CONFIG_BT_EXT_ADV=y` if extended advertising is wanted later,
  `CONFIG_BT_DEVICE_NAME` is irrelevant since the Pi supplies the whole AD payload.

### 2.5 What this firmware makes possible that the coordination one does not

1. **Byte-exact wire-format verification.** `TX` a v1 payload from Python, read it
   back as `RX` hex, assert equality. Any encoder/decoder disagreement between C and
   Python surfaces immediately, at the byte level.
2. **Testing v1 now**, with no fallback to v0.
3. **Commanded content in both directions**, so the Pi's *scan* path is checked
   against known bytes rather than against a value the peer's algorithm is changing.
4. **Sweeping the peer's parameters too**, so adv interval and scan window can be
   varied on both sides and the results cross-checked.
5. **A permanent fixture.** Every future wire-format or transport change gets a
   byte-level conformance test against real silicon.

## 3. Serial transport specification (Pi side)

Two peers speak over UART, and the Python module must handle both — they are
different protocols:

| Peer | Protocol | Used for |
|---|---|---|
| **Test peer** (§2) | `TX`/`RX` hex lines, parsed on `\n` | this test |
| Coordination firmware | `n`/`a`/`p`/`t` commands, `d` data lines, 64-byte buffer, 25 ms timeout parsing | production BLE agents, and Appendix A's integration test |

Build the transport as a thin framed-line link with **two codecs on top**, rather
than one module that knows both dialects. The link handles the port, framing and
error counting; a codec turns lines into objects.

### 3.1 Link

| | Test peer | Coordination firmware |
|---|---|---|
| Device | `/dev/ttyACM0` (verify — CDC enumeration order varies with several boards) | same |
| Baud | 115200 8N1, no flow control | same |
| Terminator out | `\n` | `\n\r` (this order; it is what the firmware expects) |
| Max line out | 256 B | **64 B**, terminator included |
| Inter-command gap | none needed | **≥ 50 ms**, after draining |

The coordination firmware parses on a 25 ms inactivity timeout (`RX_TIMEOUT`), not on
a newline. Two commands sent back to back land in one buffer and `strtok` sees a
malformed line — that is why its configuration is split into three commands and why
the gap is mandatory. The test peer parses on `\n` and needs neither.

### 3.2 Python API to build

```python
class SerialLink:
    async def open(self) -> None
    async def close(self) -> None                       # idempotent
    async def send_line(self, line: str) -> None        # frames, drains, enforces limits
    def on_line(self, callback) -> None                 # per complete inbound line
    @property
    def stats(self) -> SerialStats                      # sent, received, malformed, dropped
```

Then a codec per peer:

```python
class TestPeerCodec:      # TX/TXOFF/ADV/SCAN/PING  ->  RX/TXAT/ERR/PONG
class CoordinationCodec:  # n/a/p/t                 ->  d
```

Requirements, in order of how likely they are to bite:

- **Enforce the outbound line limit and raise.** Silently over-long lines are the
  worst failure mode available here: the peer mis-parses and behaves plausibly.
- **A malformed inbound line is skipped and counted, never raised.** The UART emits
  boot banners, log output and partial lines on reset. One of those must not stop a
  500-transmission run.
- **Handle partial reads.** Lines arrive split across reads; buffer until `\n`.
  Test this explicitly by feeding a captured stream one byte at a time.
- **Do not block the event loop.** `pyserial-asyncio`, or a reader thread posting via
  `loop.call_soon_threadsafe`.
- **Timestamp on arrival**, inside the read callback, before queueing. A timestamp
  taken after queueing measures your own scheduler.
- Keep values as integers; convert with `vertex.numeric.dequantize` at the boundary.

### 3.3 Coordination-firmware command reference

For Appendix A's integration test. Field order is from `serial.c` and is
authoritative; all values are integers, state quantities scaled by 1e6.

```
n<enabled>,<node>,<neighbour>[,...]                              max 4 neighbours
a<clock>,<dt>,<state0>,<vstate0>,<vartheta0>,<eta>,<alpha>,<delta>,<avg_law>
p<dist_on>,<amplitude>,<offset>,<beta>,<A>,<frequency>,<phase>,<samples>
t<0|1>
```

`clock` and `dt` are milliseconds. `a` carries **nine** fields — the comment in
`serial.c` says "6 parameters" and is stale; the parser handles indices 0–8.

Data out, only while running, at the `clock` period:

```
d<timestamp_ms>,<state>,<vstate>,<vartheta>,<nb_vstate_1>[,...]
```

`timestamp_ms` is `k_uptime_get() - time0` — **the nRF's own clock**, sharing no
origin with the Pi's (`docs/CLOCK_MODEL.md` §4).

## 4. BLE broadcast transport specification

### 4.1 Socket

```python
AF_BLUETOOTH, BTPROTO_HCI, HCI_CHANNEL_USER = 31, 1, 1
sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
sock.bind((dev_id, HCI_CHANNEL_USER))
```

**`HCI_CHANNEL_USER`, not `HCI_CHANNEL_RAW`.** RAW (what `hci_open_dev()` in
`Mini-BLE-stack` uses) leaves BlueZ managing the controller, so the daemon can
overwrite advertising and scanning parameters underneath you — which is the original
problem restated. USER channel takes the device exclusively and requires it to be
**down** first:

```bash
sudo hciconfig hci0 down          # or: sudo btmgmt power off
```

Notes:

- Requires `CAP_NET_ADMIN` (root, or `setcap cap_net_admin,cap_net_raw+eip`).
- Verify Python's `bind()` accepts the 2-tuple on your version; if it rejects the
  channel, fall back to a `ctypes` bind with a hand-packed `sockaddr_hci`
  (`sa_family(2) | hci_dev(2) | hci_channel(2)`).
- BlueZ will not see the adapter while you hold it. Reverse with `hciconfig hci0 up`.
- **Coexistence is unaffected**: arbitration lives inside the combo chip between the
  WLAN and BT cores and does not depend on the host stack. Taking the device does
  not disable it.

### 4.2 Packet framing on the socket

| Direction | Layout |
|---|---|
| Command (write) | `0x01` \| opcode (2, LE) \| plen (1) \| params |
| Event (read) | `0x04` \| event code (1) \| plen (1) \| params |

Opcode is `(OGF << 10) | OCF`, little-endian on the wire.

### 4.3 Commands

| Command | Opcode | Parameters |
|---|---|---|
| Reset | `0x0C03` | none |
| LE Set Advertising Parameters | `0x2006` | `min_int(2) max_int(2) type(1) own_addr(1) peer_addr_type(1) peer_addr(6) chan_map(1) filter(1)` = 15 B |
| LE Set Advertising Data | `0x2008` | `len(1) data(31)` = 32 B, zero-padded |
| LE Set Advertise Enable | `0x200A` | `enable(1)` |
| LE Set Scan Parameters | `0x200B` | `type(1) interval(2) window(2) own_addr(1) filter(1)` = 7 B |
| LE Set Scan Enable | `0x200C` | `enable(1) filter_duplicates(1)` |

**These six are the whole point of the exercise** — the adv interval, channel map,
scan interval and scan window that §3 A1 showed are unreachable through BlueZ.

Parameter values for this test:

- Intervals and windows are in **0.625 ms units**. Interval 0x00A0 = 100 ms;
  0x0020 = 20 ms (the minimum for non-connectable advertising on 4.1+; older
  controllers floor at 0x00A0 — read the returned status rather than assuming).
- `adv type` = `0x03` (`ADV_NONCONN_IND`). Non-connectable undirected: we broadcast
  and never accept connections.
- `own_addr` = `0x00` (public) is simplest. `peer_addr_type`/`peer_addr` are unused
  for undirected advertising; zero them.
- `chan_map` = `0x07` (all three of 37/38/39). Worth varying deliberately later:
  `0x04` restricts to channel 39 at 2480 MHz, clear of Wi-Fi 1/6/11 — see the
  channel table in README §3.3.
- `scan type` = `0x00` (passive). Active scanning transmits scan requests, which
  costs airtime and is not needed to read advertising data.
- **`window` ≤ `interval`.** Setting them equal is a 100% duty-cycle scanner, which
  is the configuration to use when measuring maximum achievable delivery.
- **`filter_duplicates` = `0x00` on the Pi's scan.** The opposite of the nRF's
  setting, and deliberate: duplicate filtering makes loss unmeasurable, because a
  suppressed duplicate is indistinguishable from a lost packet.

### 4.4 Advertising data

AD elements are `len(1) | type(1) | value(len-1)`. Both wire versions fit; verified
against the codec constants (`BLE_AD_OVERHEAD + PAYLOAD_SIZE = 13 + 16 = 29 <= 31`):

```
v0  (8-byte manufacturer value, 19 B total)
08 09 4C 41 42 43 54 52 4C   09 FF 59 00 7F C8 01 00 00 00
└── name "LABCTRL" (9 B) ─┘  └── mfr element (10 B) ──────┘

v1  (18-byte manufacturer value, 29 B total)
08 09 4C 41 42 43 54 52 4C   13 FF 59 00 <16-byte v1 payload>
```

**The length byte counts type + value, not the element's total size.** The name
element occupies 9 bytes but its length byte is `0x08` (1 type + 7 name); the v1
manufacturer element occupies 20 bytes with a length byte of `0x13` = 19 (1 type +
2 company + 16 payload). Getting this off by one shifts every following element and
breaks parsing outright, so compute it — `bytes([len(value) + 1, ad_type]) + value` —
rather than writing a constant.

Company ID `0x0059` is little-endian on the wire: `59 00`. In v0, `0x7F` = enabled and
`0x70` = disabled.

**Against the test peer either version works**, because it does not inspect length.
That is the main reason for building it: v1 can be exercised on real silicon
immediately. The 8-byte restriction applies only to the coordination firmware —
Appendix A.

The name element is not required by anything here — the peer reports raw bytes and
the Pi filters on company ID — but keep it. It makes the packet identifiable in a
sniffer capture, and it matches what the coordination firmware advertises, so a
capture of one is comparable with a capture of the other.

Build the manufacturer value with `encode_manufacturer_data()` (v1) or the company ID
prepended to `encode_v0()`, and assemble the AD around it. Do not hand-roll the
payload layout a second time — the codec is tested and a second implementation is a
second thing to keep in step.

### 4.5 Receiving

Read from the socket, expect event packets, and dispatch on:

| | |
|---|---|
| `0x0E` Command Complete | `num_slots(1) opcode(2) status(1) [return params]` — **check status on every setup command** |
| `0x0F` Command Status | `status(1) num_slots(1) opcode(2)` |
| `0x3E` LE Meta Event | `subevent(1)` — `0x02` is **LE Advertising Report** |

LE Advertising Report payload:

```
num_reports(1)
  per report:  event_type(1) addr_type(1) addr(6) data_len(1) data(data_len) rssi(1, signed)
```

Parse `data` as AD elements, find type `0xFF`, verify the company ID is `0x0059`,
then decode with `decode_any()` — which already accepts v0 and v1, so this path does
not need to know which is on the air.

**Reject rather than guess.** A foreign company ID, a short element, or an unknown
version is a counted rejection, not a fallback to "whatever was there". `UdpStats`
is the model to copy: count `received`, `self_filtered`, `undecodable`, `delivered`.

**Self-filter by `node_id`, not by address.** Same reasoning as the UDP transport:
several agents share a host, and on this platform they are often each other's
neighbours.

### 4.6 Setup sequence

Order matters. Advertising and scanning parameters can only be set while the
respective function is **disabled**, or the controller returns
`Command Disallowed (0x0C)`:

```
1. Reset                                    0x0C03
2. LE Set Advertising Parameters             0x2006
3. LE Set Advertising Data                   0x2008
4. LE Set Scan Parameters                    0x200B
5. LE Set Scan Enable   (enable=1, dup=0)    0x200C
6. LE Set Advertise Enable (enable=1)        0x200A
```

To update the payload while advertising, `0x2008` alone is sufficient — advertising
does not need to be stopped for a data change, only for a *parameter* change. That
is the fast path the publish loop uses.

**Verify the controller supports simultaneous advertising and scanning** before
relying on it — the bridge role needs both at once. Read `LE Read Local Supported
Features` and, if it is available, `LE Read Number of Supported Advertising Sets`.
If concurrency is unsupported, the transport must time-slice, and that changes the
timing model enough to be worth knowing on day one rather than day thirty.

### 4.7 Interface to satisfy

It must be a drop-in `Transport` (`vertex/transports/base.py`):

```python
class BleBroadcastTransport(Transport):
    name = "ble"
    async def start(self, on_receive: ReceiveCallback) -> None
    async def stop(self) -> None                 # idempotent; restore the adapter
    async def publish(self, packet: StatePacket) -> None
```

- `publish` maps to `0x2008` with the new payload. It must not block the caller's
  period; the socket write is small but treat a slow write as loss, as UDP does.
- `on_receive` gets `Reception(packet, rx_time_us)`, with `rx_time_us` from the
  clock **at the moment the report is read**, not later.
- `stop` must be safe to call twice and should leave the adapter usable
  (`hciconfig hci0 up`), because it runs from signal handlers and failed starts.
- The callback must never raise into the socket read path.

Satisfying this interface is what makes the loopback test's result transferable: the
same agent, simulator and run log work over BLE with no changes.

---

## 5. Test procedure

### 5.1 Setup

```bash
sudo hciconfig hci0 down            # release the adapter to HCI_CHANNEL_USER
ls -l /dev/ttyACM*                  # confirm the test peer's CDC device
```

Use node ids **200 (Pi)** and **201 (peer)** so a stray packet can never be mistaken
for a real experiment's traffic.

### 5.2 Direction A — Pi TX, peer RX

Verifies the Pi's advertising path, byte for byte.

```
1. link.open(); peer.scan(interval=0x00A0, window=0x00A0, passive, dup=0)
2. ble.start()
3. for counter in 1..N:
       pkt  = StatePacket.from_state(200, vstate=counter * 1e-6, seq=counter)
       ad   = build_ad(pkt)                     # name + manufacturer elements
       await ble.publish(pkt)
       record (counter, bytes(ad), t_send)
4. for every `RX <hex>,<rssi>,<us>` line:
       record (bytes.fromhex(hex), rssi, peer_us, t_recv)
5. ble.stop(); peer.scanoff()
```

**The assertion is byte equality**, not value equality: the hex the peer reports must
equal the AD bytes the Pi assembled. That checks the AD structure, the company ID,
the field order and the endianness in one comparison — and it is only possible
because the peer does not parse.

### 5.3 Direction B — peer TX, Pi RX

Verifies the Pi's scanning path, which is the half BlueZ made unreachable.

```
1. peer.adv(interval_min=0x00A0, interval_max=0x00A0, chan_map=0x07)
2. ble.start()                                   # Pi scanning, dup filtering OFF
3. for counter in 1..N:
       ad = build_ad(StatePacket.from_state(201, vstate=counter * 1e-6, seq=counter))
       await peer.tx(ad.hex())                   # peer replies TXAT <seq>,<us>
       record (counter, bytes(ad), peer_tx_us)
       await sleep(period)
4. for every Reception the Pi decodes: record (packet, rx_time_us)
5. peer.txoff(); ble.stop()
```

Because the Pi dictates the payload, the reference is exact and involves no algorithm
and no clock comparison. **This is the direction to run the scan-window sweep on.**

### 5.4 Sweep

One variable at a time:

| Parameter | Values | Question |
|---|---|---|
| Pi adv interval | 20, 50, 100, 500, 1000 ms | Does delivery track the advertising rate? |
| **Pi scan window / interval** | 10%, 25%, 50%, 100% duty | **The parameter BlueZ would not expose. Does it behave as theory predicts?** |
| Pi channel map | `0x07` all, `0x04` ch 39 only | Does restricting to the Wi-Fi-clear channel change anything? |
| Wi-Fi load | idle, `iperf3` running | First real coexistence data point (A7) |
| Payload version | v0 (8 B), v1 (16 B) | Both decode; v1 needs no firmware concession here |

---

## 6. Metrics and acceptance criteria

### 6.1 Correctness — the primary result

Both directions: **every delivered advertisement must be byte-identical to what was
sent.** Not approximately, not after normalisation. A single mismatched byte is an
encoder, endianness or AD-structure defect, and it is far cheaper to find here than
in a 26-minute run across thirty nodes.

**Acceptance:** zero byte mismatches among delivered packets. Loss is expected and
measured separately; corruption is not, and a nonzero count is a bug, not a radio
condition.

### 6.2 Delivery ratio

```
sent      = advertisements commanded
delivered = advertisements received and decoded
lost      = sent - delivered            (seq gaps identify which)
ratio     = delivered / sent
```

Duplicate filtering is off on both sides, so a suppressed duplicate can never be
mistaken for a loss.

**Acceptance:** ≥ 0.95 at a 100 ms advertising interval with `window == interval`,
peer idle, no Wi-Fi traffic, over ≥ 500 transmissions. At centimetre range anything
much lower is a configuration fault, not a radio limit.

### 6.3 The result that actually validates the work

**Delivery ratio must fall measurably as the Pi's scan window shrinks**, in direction
B. Roughly, a scanner listening for a fraction *d* of the time on one of three
advertising channels sees each advertising event with probability approximately *d* —
so a 25% duty cycle should lose most of them.

If the ratio does **not** move when the window changes, the parameter is not taking
effect, and the whole HCI path has bought nothing over BlueZ. That single sweep is
the difference between believing the scan parameters are set and knowing it, and it
should be run before anything else is built on top.

### 6.4 Latency, and what is honestly obtainable

The peer's `uptime_us` and the Pi's clock share no origin, so a one-way BLE delay is
not directly measurable.

`PING`/`PONG` gives a bounded offset estimate: the Pi brackets the peer's reported
uptime between its own send and receive times, so the offset is known to within the
UART round trip — order **1 ms** at 115200 for a short line. Adequate for
distinguishing advertising-interval effects (tens of ms), useless for characterising
the radio itself (tens of µs).

Report loss and correctness as primary. If latency is reported, name the components
of what was measured — commanded-to-report includes UART transit both ways, scan
latency, and up to one advertising interval — and do not present it as a radio figure.

### 6.5 Recording

Log through `RunLog` with `units="scaled_int"`, and put the full radio configuration
in `RunMeta.environment`. It cannot be reconstructed afterwards and is the usual
reason two runs disagree:

```python
environment = {
    "peer_firmware": "nordic-testpeer",  "direction": "B",
    "pi_adv_interval_ms": 100, "pi_scan_interval_ms": 100, "pi_scan_window_ms": 100,
    "pi_channel_map": 0x07, "pi_tx_power_dbm": 8,
    "peer_adv_interval_ms": 100, "peer_tx_power_dbm": 8,
    "wire_version": 1, "duplicate_filtering": False,
    "wifi_channel": 11, "power_save": "off", "wifi_load": "idle",
}
```

`scripts/radio_check.sh` prints the Wi-Fi half.

## 7. Implementation checklist

**Test peer firmware** (`firmware/nordic-testpeer/`)

- [ ] RX/TX buffers 256 B; parse on `\n`, not on an inactivity timeout
- [ ] `TX <hex>` advertises the bytes verbatim; `bt_le_adv_update_data()` to change payload without stopping
- [ ] Scan with duplicate filtering **off**; emit `RX <hex>,<rssi>,<us>` per report
- [ ] Hex-encode the raw AD bytes **before** any `bt_data_parse()` — do not parse
- [ ] Microsecond timestamps from `k_uptime_ticks()`
- [ ] Count and expose dropped reports; never drop silently
- [ ] `ADV`/`SCAN` set the peer's own parameters; report what the controller actually selected
- [ ] `PING`/`PONG` and `VER`

**Serial (Pi)**

- [ ] Framed-line link + one codec per peer dialect; do not merge the two
- [ ] Enforce the outbound line limit and **raise** — never let a peer mis-parse
- [ ] Malformed inbound line: skip and count, never raise
- [ ] Handle partial reads; test by feeding a capture one byte at a time
- [ ] Timestamp inside the read callback, before queueing
- [ ] Do not block the event loop

**BLE (Pi)**

- [ ] `HCI_CHANNEL_USER`, with a clear error if the adapter is still up
- [ ] Check `Command Complete` status on **every** setup command
- [ ] Setup order per §4.6; parameters only while the function is disabled
- [ ] Duplicate filtering **off** on the scan
- [ ] Reject foreign company IDs and count rejections; never fall back to "whatever was there"
- [ ] Self-filter by `node_id`, not by address
- [ ] `stop()` idempotent, restores the adapter
- [ ] Verify concurrent adv + scan is supported before relying on it

**Harness**

- [ ] Ids 200/201
- [ ] **Assert byte equality**, not value equality
- [ ] Both directions measured separately
- [ ] Sweep the scan window and confirm delivery responds (§6.3) — do this first
- [ ] Full radio configuration in `environment`

## 8. What can be tested without hardware

Worth building first, because it is where the mistakes will be:

- **HCI command encoding.** Assert exact bytes for each of the six commands against
  hand-computed expectations. Opcode packing and the 0.625 ms unit conversion are the
  two places to get this wrong.
- **Advertising-report parsing.** Feed captured or synthesised event bytes and assert
  the decoded packet. Include a truncated report, a foreign company ID, an unknown AD
  type, and multiple reports in one event.
- **AD assembly.** Assert both layouts byte for byte — v0 at 19 B and v1 at 29 B, as
  printed in §4.4. Also assert the total stays within 31, which is the constraint that
  silently truncates a payload if a field is ever added.
- **Serial line parsing.** Table-driven, including short lines, extra fields,
  non-numeric fields, and partial lines split across reads.

All four are pure functions over bytes. If they pass, first-light on hardware is
about configuration rather than about logic.

---

## 9. Worked frames

Generated from the codec, not written by hand. Use these as fixtures for the offline
tests in §8 — if your encoder produces these bytes, first-light is a configuration
problem rather than a logic one.

Example packet throughout: **node 200, seq 1, vstate 1e-6, tx_time_us 1000000,
enabled**, wire v1.

### 9.1 The AD frame — carried identically in both directions

```
v1 (29 B)   08 09 4C 41 42 43 54 52 4C 13 FF 59 00 01 01 C8 00 01 00 01 00 00 00 40 42 0F 00 00 00
v0 (19 B)   08 09 4C 41 42 43 54 52 4C 09 FF 59 00 7F C8 01 00 00 00
```

```
08 09 4C 41 42 43 54 52 4C    name element:  len=08  type=09  "LABCTRL"
13 FF 59 00 ...               mfr element:   len=13  type=FF  company=0059 LE
   └ 0x13 = 19 = 1 type + 2 company + 16 payload
```

v1 payload, unpacked (`vertex/wire/codec.py`):

```
01        version
01        flags        bit0 enabled, bit1 disturbance_on
C8        node_id      200
00        reserved     must be 0
01 00     seq          uint16 LE = 1
01 00 00 00   vstate   int32  LE = 1   (scaled 1e-6)
40 42 0F 00 00 00   tx_time_us  uint48 LE = 1000000
```

### 9.2 Setup, both directions

```
Reset                     0x0C03   01 03 0C 00
LE Set Adv Parameters     0x2006   01 06 20 0F A0 00 A0 00 03 00 00 00 00 00 00 00 00 07 00
LE Set Scan Parameters    0x200B   01 0B 20 07 00 A0 00 A0 00 00 00
LE Set Scan Enable        0x200C   01 0C 20 02 01 00
LE Set Advertise Enable   0x200A   01 0A 20 01 01
```

Adv parameters decode as: interval_min `A0 00` = 0x00A0 = 160 x 0.625 ms = **100 ms**,
interval_max the same, type `03` = `ADV_NONCONN_IND`, own_addr `00` = public,
peer_addr_type + peer_addr zeroed (unused for undirected), chan_map `07` = all of
37/38/39, filter policy `00`.

Scan parameters: type `00` = passive, interval and window both 0x00A0 = 100 ms
(**100% duty**), own_addr `00`, filter `00`. Scan enable is `01 00` — enabled, and
**duplicate filtering off**, which is what makes loss countable.

### 9.3 Direction A — Pi TX, peer RX

Pi writes one HCI command per transmission; only the payload changes:

```
LE Set Advertising Data   0x2008   (36 B)
01 08 20 20 1D <31 B data field>
01 | 08 20 | 20 | 1D | <29 B AD + 2 B zero pad>
              │    └ significant length = 29
              └ parameter total length = 32 (1 + 31, always)
```

The data field is **always 31 bytes**, zero-padded. The length byte says how much of
it is significant. Sending a short parameter block is a common cause of
`Invalid HCI Command Parameters (0x12)`.

Peer reports over UART:

```
RX 08094c41424354524c13ff59000101c80001000100000040420f000000,-42,123456789
   └ raw AD, hex ─────────────────────────────────────────────┘  └rssi┘ └uptime_us┘
```

**Assertion:** `bytes.fromhex(hex_field) == ad_you_sent`. Byte equality, not value
equality.

### 9.4 Direction B — peer TX, Pi RX

Pi commands over UART:

```
TX 08094c41424354524c13ff59000101c80001000100000040420f000000
```

Peer replies `TXAT <seq>,<uptime_us>` when the payload reaches its controller, then
advertises. The Pi reads this from the HCI socket:

```
LE Advertising Report (44 B)
04 3E 29 02 01 03 01 8D 6E 37 5A E0 C4 1D ...
04 | 3E | 29 | 02 | 01 | 03 | 01 | <addr 6 B> | 1D | <29 B AD> | D6
 │    │    │     │    │    │    │                  │           │      └ rssi, int8 = -42
 │    │    │     │    │    │    └ addr_type 01 = random
 │    │    │     │    │    └ event_type 03 = ADV_NONCONN_IND
 │    │    │     │    └ num_reports = 1
 │    │    │     └ subevent 02 = LE Advertising Report
 │    │    └ parameter total length
 │    └ LE Meta Event
 └ HCI event packet
```

`num_reports` **may exceed 1** — the controller can batch several reports into one
event, and each has its own variable-length `data`, so they must be walked
sequentially rather than indexed at fixed offsets. A parser that assumes one report
per event drops traffic under load, which looks exactly like radio loss.

The address is little-endian on the wire: `8D 6E 37 5A E0 C4` is `C4:E0:5A:37:6E:8D`.

**Assertion:** the AD slice equals the bytes commanded via `TX`, and
`decode_any()` on the manufacturer value returns the original `StatePacket`.

---

## Appendix A: integration test against the coordination firmware

Worth doing **after** the Python side is known-good against the test peer, because it
validates the real production path — the one an actual BLE agent uses. Do not start
here: its constraints fail silently, so a Python bug and a constraint violation look
identical.

Four constraints, from `observer.c`, `serial.c` and `common.h`:

**A.1 Manufacturer data must be exactly 8 bytes.**

```c
if (data->data_len == CUSTOM_DATA_TYPE_SIZE) {      // == 8, exactly
```

`sizeof(custom_data_type)` is 8 — `uint16 manufacturer, uint8 netid_enabled,
uint8 node, int32 vstate`, no padding. Wire **v1**'s element is 18, so the comparison
fails and the packet is dropped **with no log line**. This integration test must
therefore advertise **v0**, or the firmware must be changed to accept both.

**A.2 The sender must be in the neighbour list.** `map_node_to_index()` returns `-1`
otherwise, and the packet is parsed and discarded. Send `n1,<peer>,<pi>` first.

**A.3 It scans with `BT_LE_SCAN_OPT_FILTER_DUPLICATE`.** Identical payloads are
reported once. Vary the payload every transmission — carry a counter in `vstate`,
which doubles as the sequence number since it is the field the firmware echoes back.

**A.4 It reports only while running.** `serial_log_coordination_task()` returns early
unless `coordination->running`, so `t1` is required, with valid enough parameters for
the loop to turn.

Also: send `p0,...` to disable the disturbance. It is the one setting that makes the
peer's `vstate` move unpredictably, and it sidesteps the firmware's disturbance
time-base defect (`docs/FIRMWARE_DIVERGENCE.md` §1) entirely.

The return path is the `d` line: `nb_vstate_1` is what the firmware received over
BLE from the Pi, and `vstate` is its own state, which it is also advertising. Because
that column holds only the *last* value received, count **distinct transitions**
rather than samples.

**What this test adds over §5:** it proves the production firmware and the Python
agent interoperate on the wire, including the v0 path a fleet mid-reflash depends on.
What it cannot do is isolate a fault, which is why it comes second.
