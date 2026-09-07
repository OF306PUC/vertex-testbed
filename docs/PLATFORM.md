# VERTEX platform

An experimental testbed for finite-time adaptive coordination controllers over a
heterogeneous radio network. Raspberry Pi 4 nodes each host up to three logical
agents (`ble`, `wifi`, `bridge`); each Pi is paired over USB with an nRF52-DK. A
hub on a laptop orchestrates runs.

The point of the platform is to make the *transport* a controlled variable: the
same control law, the same initial conditions, the same disturbance stream, run
over BLE advertising and over Wi-Fi, so that differences in convergence are
attributable to the network rather than to the algorithm.

---

## 1. Design principles

1. **Real links are the product.** Anything a pure simulator can do is not the
   differentiator. Per-link delivery ratio, one-way latency, and radio
   coexistence behavior are first-class outputs, not diagnostics.
2. **Algorithm, transport, and topology are all pluggable.** Today all three are
   hardcoded.
3. **Every run is reproducible from a manifest + a git hash.** Partly true today.
4. **The same controller code runs on hardware, in simulation, and in analysis.**
5. **Prefer traffic engineering over register tweaking** when both solve the
   problem — cheaper, portable, and doesn't depend on closed firmware.

---


## 2. Settled decisions

Three decisions are premises, not open questions.

1. **The Pi code is Python** The driving reason is raw HCI User
   Channel access: stdlib in Python, a native binding in Node. BlueZ's default scan window/interval duty cycle is unknown and unreachable today

2. **The onboard CYW43455 serves both BLE and WLAN.** 

3. **The Wi-Fi transport is UDP broadcast, not HTTP pull.** TCP retransmission
   inflates TX airtime, which blanks this node's own BLE receiver, which causes
   more loss — a positive feedback loop UDP breaks.

---

## 3. Radio access and coexistence

### A1. CYW43455 coexistence — what the datasheet claims, and what it means here

Datasheet (quoted by JI):

> Support is provided for platforms that share a single antenna between Bluetooth
> and WLAN. Dual-antenna applications are also supported. The CYW43455 radio
> architecture allows for lossless simultaneous Bluetooth and WLAN **reception**
> for shared antenna applications. This is possible only via an integrated
> solution (shared LNA and joint AGC algorithm). It has superior performance
> versus implementations that need to arbitrate between Bluetooth and WLAN
> reception.

This is real and it is good news for us — but read the scope precisely.

**What it does say.** One LNA feeds both receive paths; the RF is split after the
LNA and downconverted separately for WLAN and BT, with a joint AGC so neither
desensitizes the other. There is therefore **no RX/RX conflict.** A BLE scanner
at 100% duty cycle does not have to give up airtime to WLAN *reception*. An
external-switch design would have to arbitrate; this one does not.

**What it does not say — and this is the operative limit.** It says
*reception*. Simultaneous **transmission** on a single shared antenna at 2.4 GHz
is physically not on offer: one antenna port, and a transmitting PA both drives
that port and would saturate the co-located receive front end. So:

- **RX + RX → simultaneous, lossless.** (The datasheet's claim.)
- **TX + RX → mutually exclusive.** When WLAN transmits, the T/R switch hands
  the antenna to the PA and the shared LNA is isolated; the BLE receiver is deaf
  for that frame plus turnaround. And vice versa.
- **TX + TX → mutually exclusive.** Arbitrated by the on-chip coexistence engine.

**Therefore:** residual interference between our BLE and Wi-Fi agents is
proportional to **local transmit airtime**, not to total traffic. That reframes
the whole dongle question — see A2.

**Host interfaces are already separate** and are not a bottleneck: on Pi 4, WLAN
is on SDIO, BT is on a PL011 UART at 3 Mbaud (~6600 HCI advertising reports/sec
ceiling — far above our scale, but worth remembering if we ever scan dense
environments).

**Coexistence survives HCI User Channel.** Coex arbitration lives inside the
combo chip between the WLAN and BT cores; it does not depend on the Linux
Bluetooth host stack. So taking exclusive control of `hci0` (A5) does **not**
disable coexistence. These two workstreams compose.

### A2. The airtime argument — this is probably why the dongle was needed

Given A1, the honest hypothesis is that the dongle is a workaround for a
**traffic engineering problem, not an RF problem.**

Today the Wi-Fi agent does `axios.get()` per neighbor per tick — HTTP/1.1 over
TCP. Even with keep-alive, that's request + response + ACKs, each frame carrying
802.11 preamble + MAC/LLC/IP/TCP headers, DIFS/backoff, and possible retries at
a low MCS. Rough order of magnitude at 3–4 neighbors: **~1–2.5% transmit duty
cycle per node**, and every one of those transmit events blanks that node's own
BLE receiver.

A 16-byte binary UDP datagram at the same rate is **one** frame, ~300 µs of
airtime including contention → **~0.3% at ten nodes on a channel**, roughly two
orders of magnitude less local TX airtime.

**The deeper reason UDP wins is not the byte count — it's that its airtime does
not grow under contention.** With TCP, a blanked reception causes a loss →
retransmission → more airtime → more blanking → more loss. Positive feedback,
worst exactly in the loaded case we care about. With UDP a blanked datagram is
just a lost datagram and airtime stays flat. (This is also why C4's sequence
numbers matter: under UDP, loss becomes something we *measure* rather than
something the stack hides from us by retrying.)

#### A2.1 Three independent mechanisms — do not conflate them

Reducing airtime only addresses the first. Ranking them is what **A7** is for.

1. **Self-blanking (coexistence).** *My* WLAN TX deafens *my* BLE RX, and vice
   versa. Scales with **local transmit airtime**. → fixed by UDP.

2. **Co-channel interference (plain 2.4 GHz collision).** Another node's WLAN
   frame collides in the air with a BLE adv packet at our antenna. Nothing to do
   with coexistence. → **fixed for free by WLAN channel planning:**

   | BLE adv ch | Freq | WLAN ch 1 (2402–2422) | ch 6 (2427–2447) | ch 11 (2452–2472) |
   |---|---|---|---|---|
   | 37 | 2402 MHz | **collides** (lower edge) | clear | clear |
   | 38 | 2426 MHz | clear | **collides** (lower edge) | clear |
   | 39 | 2480 MHz | clear | clear | clear |

   2402 / 2426 / 2480 were chosen to sit in the guard regions around WLAN 1/6/11.
   **Put the LAN on channel 11 and all three adv channels are clear.** Check what
   the router is actually on — if 1 or 6, every frame from every node is colliding
   with adv 37 or 38. In the nRF52480, one can not set the **adv_channel_map** to specific channels. 


### A3. Coexistence knobs

Even with low airtime, the firmware's default coex *policy* may still deprioritize
BLE scanning and advertising (typical Broadcom policy privileges WLAN's own
traffic and BT SCO/eSCO/connection events; scan and adv are low priority). These
are the "specific registers" that were previously out of reach:

- **`btc_mode` iovar** — coexistence arbitration mode. `0` disables arbitration
  entirely (not what we want — that's mutual destruction), `1` is the default.
- **`btc_params <index> <value>` iovars** — the coex priority/threshold register
  bank. This is the real target. **Indices must be read out of the
  brcmfmac / nexmon sources; do not trust remembered values.**
- **Access path:** `nexutil` (from Nexmon) can get/set arbitrary brcmfmac iovars
  on the 43455. Broadcom's proprietary `wl` utility is the alternative where
  available.
- **Board-level NVRAM** — coex and board flags live in the firmware NVRAM text
  file. Verify locations on Bookworm:
  ```bash
  ls -l /lib/firmware/brcm/ | grep -i 43455   # WLAN fw + clm_blob + NVRAM .txt
  ls -l /lib/firmware/brcm/BCM4345C0.hcd      # BT firmware
  ```
  `boardflags` / `boardflags2` / `boardflags3` carry BTCOEX bits. This file is
  editable — it is the most "register-level" configuration surface available
  without patching firmware.
- **PHY-level Wi-Fi** (monitor mode, injection, CSI) → **Nexmon** patched
  firmware; `nexmon_csi` supports the 43455c0 used on Pi 3B+/4.

Separately, and independent of coex, the plain nl80211 knobs matter and are
easy (`pyroute2`, or `iw`): **`iw dev wlan0 set power_save off`** (power save adds
tens of ms of non-deterministic latency — check this on current hardware, it may
already be polluting collected data), fixed MCS/rate, TX power, channel and
bandwidth, A-MPDU aggregation (a jitter source), and WMM access category via
DSCP / `SO_PRIORITY` so state packets land in the Voice AC.

### A4. Reaching the BLE parameters: raw HCI User Channel

`socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)` + `bind((dev_id, HCI_CHANNEL_USER))`
takes exclusive control of `hci0` (BlueZ must release it first: `hciconfig hci0 down`)
and speaks HCI directly:

| Command | OGF/OCF | Unlocks |
|---|---|---|
| LE Set Advertising Parameters | 0x08/0x0006 | interval min/max, adv type, **channel map**, filter policy |
| LE Set Advertising Data | 0x08/0x0008 | payload (replaces the `expect` script) |
| LE Set Scan Parameters | 0x08/0x000B | passive/active, **scan interval + window** (window == interval → 100% duty) |
| LE Set Extended Adv Parameters | 0x08/0x0036 | 2M / Coded PHY, secondary channel, per-set TX power |
| LE Set Extended Scan Parameters | 0x08/0x0041 | per-PHY scan config |

Two structural wins beyond the parameters: we receive **raw HCI LE Advertising
Report events at the moment of reception** (real timestamps, real packet counts —
no cache, no freshness guesswork), and there is **extra layers of control like D-Bus**, so the match-rule
exhaustion bug cannot occur.

#### A4.1 The existing C++ min-BLE stack — port, sidecar, or bind? *(OPEN)*

One of JI's developers has already written a **minimal BLE stack in C++ over
`socket()`**. That is exactly this layer, already built. Three ways to use it,
and the choice matters for the whole deployment story:

| Option | Cost | When it's right |
|---|---|---|
| **a) Port the knowledge, not the binary** — read it as the reference spec, reimplement in pure Python | one-time reading effort; no build system | **default choice.** The stack is ~200 lines of `socket`+`struct` territory; the *valuable* part is the HCI command sequencing, controller quirks, and event-parsing edge cases it already encodes. Extract those, drop the C++ |
| **b) Sidecar daemon** — keep the C++ as a separate process, speak msgpack/lines over a unix socket | small protocol to define | the stack is large and battle-tested, or it does something genuinely timing-critical. Gains: crash isolation (a segfault doesn't kill the agent), no GIL interaction, no build coupling, and it drops straight into the `Transport` ABC (C2) as `BleHciTransport` → local daemon |
| **c) In-process binding** — pybind11 / nanobind / cffi | ARM build or wheel on every Pi, ABI + lifetime management, two-language debugging | **last resort.** Only with a *measured* latency reason |

#### A4.2 The connection-oriented option *(future, JI 2026-08-26)*

Separate from *how* to reuse the stack: what it would let the platform do. A
connection-oriented BLE transport is the BLE analogue of the UDP unicast proposal.

**The run-start stall.** Connectionless advertising has no
acknowledgement, so a link that goes silent is indistinguishable from one that is
merely lossy, and nothing recovers it. A connection has link-layer acknowledgement,
retransmission and a supervision timeout: the same fault would either self-recover
or drop the connection and reconnect, and either way it becomes an **observable
event** rather than a hole in the data. Both configuration hypotheses for that
stall (duplicate filtering, advertising-interval collision) have been eliminated
by experiment, so a transport-level fix is now the more promising direction than
further parameter search.

**Note on the mechanism.** §6.4's explanation was corrected on 2026-08-26: the
asymmetry is the 83x difference between BLE receive duty (100%, continuous
scanning) and BLE transmit duty (1.2%), not an inability of PTA to arbitrate
receives. A connection-oriented transport changes this picture substantially,
because a connection's receive windows are *scheduled* rather than continuous --
the receiver knows when the peer will transmit, so its front-end request drops
from 100% to the connection's own duty cycle. That is arguably a stronger argument
for connections than the retransmission one.

**The cost, and it is not small.** Connections add retransmission, and
retransmission hides the raw loss this platform exists to characterise. Every
delivery figure is a *link* measurement precisely because nothing retries;
under connections those numbers would measure the retry policy instead, and would
not be comparable with anything collected so far. Airtime also becomes
O(degree), exactly as C2.2 notes for unicast. So this is a second transport to
compare against broadcast, not a replacement for it -- which is the same
conclusion C2.2 reached, and for the same reason.

### A5. Escalation: own the controller (nRF52840 as HCI radio)

Flash an nRF52840 dongle with Zephyr's `hci_uart` sample; plug into each Pi as
its BLE radio over USB CDC. The Pi then has a controller whose firmware **we**
compile. It frees the onboard chip
for Wi-Fi-only duty, adds 2M and Coded PHY, and makes exotic behavior a firmware
change in code we already build (per-packet `RADIO`→`TIMER` capture
timestamping, custom adv scheduling, channel-map hopping, connectionless CTE for
AoA). ~$10–20/node.

Beyond that, for *characterized* rather than *realistic* channels: nRF `RADIO`
in proprietary mode (Nordic ESB or custom) — deterministic TDMA slots,
µs timestamps. Keep as a third transport, not a replacement.

---

## 4. Transports

### C2.1 UDP transport — design consequences *(decided; details open)*

Switching Wi-Fi from HTTP/TCP to UDP is decided. It is not a drop-in
substitution — it changes four things that need deciding together.

**1. It inverts the communication model: pull → push.** Today the fetcher *pulls*
(`axios.get('/getVState')`). UDP naturally *pushes*: each agent broadcasts its own
state at its own rate and neighbors listen. **This is what makes the Wi-Fi agent
structurally identical to the BLE agent**, which has always been push/broadcast —
and it is the whole point of the C2 confound fix, so take the push model
deliberately rather than emulating request/response over UDP.

Consequences to handle:
- `/getVState` leaves the hot path. Keep the endpoint for diagnostics only.
- **The `clock` parameter changes meaning** — from "how often I fetch" to "how
  often I publish." Same number, different semantics; logged runs before and after
  are not directly comparable on this axis. Bump the log schema version (D4).
- `neighborReceived` gets *truer*: "a fresh packet arrived from j since my last
  step" rather than "my fetch call succeeded."
- **The neighbor table becomes the primary mechanism, not a fallback.** Both
  transports collapse to one structure: `{neighbor_id → (state, seq, rx_time)}`.
  `_neighborStateCache` in `edge.js` is currently a fallback for failed fetches;
  under push it *is* the design, shared by BLE and Wi-Fi alike. This deletes a
  whole class of special-casing.

**2. Broadcast vs. unicast — broadcast wins, and by more than it looks.** One
frame reaching all neighbors makes airtime **O(1) per node instead of
O(degree)**. Rough comparison at degree 4:

| | Airtime per publish |
|---|---|
| 4× unicast @ high MCS | ~4 × 150 µs ≈ **600 µs** (each with ACK + SIFS + DIFS/backoff) |
| 1× broadcast @ 6 Mbps basic rate | ~**250 µs** (no ACK) |

Broadcast still wins despite the low basic rate, and it grows better with degree.
It also matches BLE semantics exactly — broadcast to all, filter by sender ID in
the payload — which is the apples-to-apples comparison we want.

**3. No ACKs means loss is real and must be measured, not hidden.** This is the
upside — see A3's feedback-loop argument — but it hard-requires **C4's sequence
numbers**. Without them we've traded a retry mechanism for nothing observable.
**C4 is a prerequisite for C2.1, not an optional companion.**

**4. Scientific-validity note worth stating in write-ups.** Under broadcast, every
node hears every node; the topology becomes a *software* filter over a physical
broadcast medium. A "link failure" is therefore enforced in software, not
physically. This is already true of the BLE agent (all nodes are in radio range),
so it isn't a regression — but it should be explicit in any paper: **the platform
studies logical topologies over a shared physical medium.** Genuine physical link
failure needs attenuation or distance, which this hardware setup doesn't provide.
Conversely it's a feature: topology reconfiguration without touching hardware.

**Open:** wire format (fold into C4 — one versioned binary layout for both
transports), publish rate vs. controller `dt` decoupling, and whether the
receiver applies a freshness deadline (max age before a neighbor counts as
disabled — the current 2 s `NEIGHBOR_CACHE_MAX_AGE_MS` becomes a real protocol
parameter and should move into the manifest).

- **C3. Versioned binary payload with sequence number + TX timestamp.** Current
  BLE payload is 6 bytes `[flag | node | int32 vstate]`; the adv packet allows 31.
  Adding `uint16 seq` + `uint32 tx_ts` (+6 bytes) yields **per-link delivery ratio
  and true one-way delay for free**, on both transports, with no extra
  instrumentation. For a platform whose selling point is real links, those two
  numbers are the headline product. Under UDP there are no ACKs, so this is the only loss signal we have.
  One layout, versioned, shared by BLE adv and UDP alike.
- **C4. Simulation mode.** N agents, mock transport, one process, 100× real time.
  Validate an algorithm in seconds before touching hardware.
- **C5. Topology validation** via `networkx`: connectivity, strong connectivity,
  balance, spectral gap λ₂ — reported *before* the run, next to the convergence
  rate it predicts.
- **C6. Zeroconf/mDNS discovery** instead of hardcoded IPs.
- **C7. Unicast-per-neighbour as a selectable UDP mode.** *Proposed, not decided.*
  See C2.2 below — broadcast's airtime advantage is real, but it costs 153.6 ms of
  DTIM latency on infrastructure WLAN, measured. Making the mode selectable turns a
  fixed disadvantage into an experimental variable.

---

### C2.2 The latency cost of broadcast — measured, and a proposed option

C2.1 chose subnet broadcast and its airtime argument stands: one frame reaching all
neighbours is **O(1) per node instead of O(degree)**. What it could not know is the
price.

**Measured on `oficina_v2` (BSSID `04:d9:f5:b2:ba:80`), 2026-08-21.** UDP one-way
delay: median 171-175 ms, minimum 16-21 ms, zero duplication, on stations whose own
`power_save` is **off**. The AP advertises a 100 TU beacon (102.4 ms) and
**DTIM period 3**, so its broadcast buffer is 307.2 ms and a frame arriving at a
uniform point in that cycle waits `U(0, 307.2)` — median 153.6 ms. Therefore
`median - min` should equal 153.6 ms on every link, and it does:

```
     link     min   median  median-min   vs W/2
   12->11   20.80   175.36      154.56    +0.96
   22->11   20.70   173.13      152.43    -1.17
   11->12   16.50   171.39      154.89    +1.29
   21->12   17.71   171.66      153.95    +0.35
```

Four links within 1.3 ms — 0.8% — of a figure derived from nothing but the AP's
beacon interval and DTIM period. **Broadcast and multicast through an AP are
buffered to the next DTIM beacon whenever any associated station is dozing**,
including stations that are not ours: turning our own `power_save` off changed
nothing.

Three consequences worth stating plainly.

**It is structural, not congestion.** It is present at low duty with no
contention to speak of. Any claim that the Wi-Fi path degrades under heavier
traffic must be made *on top of* a 153.6 ms offset that has nothing to do with
traffic — and if an earlier analysis attributed this to congestion, it was
attributing a DTIM cycle.

**It inverts the assumed transport ordering.** With a 200 ms publish period, Wi-Fi
neighbour data is nearly a full period stale while BLE is about half that:

| | delivery | median delay | min delay |
|---|---|---|---|
| BLE | 89-90% | 104-108 ms | 5-6 ms |
| UDP broadcast | 98-99% | 171-175 ms | 16-21 ms |

BLE is ~9 points worse on delivery and ~65 ms *better* on latency. Neither
dominates, and each figure has a mechanism: BLE's latency is the advertising
interval, UDP's is the DTIM cycle.

**It explains why degree does not vary traffic under broadcast**, which C2.1 point 4
already implied without drawing the conclusion. Broadcast airtime is
degree-independent, so a ring with degree 4 puts exactly as much traffic on the
medium as one with degree 2 — a degree comparison measures connectivity and
receive-side load, not communication load.

#### The proposal

Make the UDP send mode selectable — `broadcast` (today) or `unicast`, one datagram
per neighbour — as a manifest field beside the radio parameters, recorded in
`RunMeta` like everything else that changes what a run measures.
`UdpTransport` already takes `send_to`, so the change is small; the value is not in
the code but in what becomes measurable.

*Benefit.* Unicast is not DTIM-buffered, so the 153.6 ms term should disappear and
leave the ~18 ms base hop. That gives the platform a Wi-Fi transport usable for
latency-sensitive work, and a *controlled* comparison of the two modes on identical
hardware — which is a result in itself, not merely a fix.

*And it supplies the traffic knob.* Unicast airtime is **O(degree)**. With it, a
degree-2 versus degree-4 comparison genuinely varies communication load, so the
question C2.1 answered on airtime grounds and the G1/G2 question collapse into one
change.

*Risk.* Unicast needs each neighbour's address, so the transport gains a dependency
on the manifest's `ip` mapping that broadcast does not have; a stale address becomes
a silent per-link failure rather than a whole-transport one. And it re-opens the
scientific-validity note in C2.1 point 4 from the other side: under unicast the
topology is enforced by *addressing* rather than by a software filter over a
physical broadcast, which is arguably more faithful and is certainly different.
Say which one a run used.

*Falsifiable prediction, so the experiment can fail.* Broadcast: median ~171 ms,
distribution roughly uniform from 18 to 325 ms. Unicast: median ~18 ms. **If unicast
returns ~170 ms, DTIM is not the mechanism and this section is wrong.**

*Cheaper first step.* Ask the network owner to set DTIM 1, or point the experiment
LAN at an AP under our control. That would cut the buffer from 307 ms to 102 ms
without any code, and it tests the mechanism just as well. It does not remove the
dependency on someone else's AP configuration, which is the deeper reason to want
the unicast option.

---


---

## 5. Results on the new platform

All figures below are from `n6-ring`/`n6-fast`: 6 agents on 2 hosts, 25 Hz
dynamics, 120 s runs, 10 repeats with the run index held fixed so initial
conditions and disturbance streams are bit-identical across the set and the
network realisation is the only variable. `JOURNAL.md` has the full record.

### 5.1 The transport trade-off, with error bars

The measurement the platform was built to make. Ten repeats, `n6-fast`:

| path | delivery | median one-way delay |
|---|---|---|
| UDP (Wi-Fi broadcast) | **0.985 ± 0.008** | **171 ± 9 ms** |
| BLE nRF→nRF | 0.964 ± 0.007 | not measured |
| BLE Pi→nRF | 0.934 ± 0.066 | not measured |
| BLE nRF→Pi | **0.832 ± 0.022** | **107 ± 2 ms** |

Convergence: **13.70 ± 0.17 s**, identical initial conditions.

The trade-off is real and it is not in the direction a naive reading would
suggest. **UDP is the more reliable transport and the slower one**; BLE delivers
~60 ms faster and loses 3–17× more. Neither is uniformly better, which is the
result — and note the sd on `Pi→nRF` (0.066) is an order of magnitude above its
neighbours, driven by outlier runs rather than spread (§6.5).

Delay is only measured where a Pi is the receiver: the nRF does not report arrival
timestamps (no STATS frame — §8.1). "Not measured" above is a gap in the
instrumentation, not a zero. The plots render those bars at zero, which is
misleading and should be hatched.

### 5.2 The 171 ms UDP delay is the access point's DTIM cycle

Not congestion, and not the control loop. The AP (BSSID `04:d9:f5:b2:ba:80`)
beacons every 102.4 ms with **DTIM period 3**, so buffered broadcast frames are
released every 307.2 ms. A packet arriving at a uniformly random point in that
cycle waits **153.6 ms** on average.

Measured `median − min` across the UDP links: **152.4–154.9 ms** against a
predicted 153.6 — agreement to **0.8%**.

This matters more than the number. It says the dominant term in Wi-Fi latency here
is a property of the *infrastructure*, fixed by the AP's configuration, and it is
not reduced by sending less or sending faster. It also means UDP latency figures
from this testbed do not transfer to a different AP without restating its DTIM
period. Power save was ruled out separately by re-running with it disabled.

### 5.3 The advertising interval is a delivery ceiling

`broadcaster_update()` and `cmd_le_set_adv_data` rewrite the advertising *payload*;
neither changes how often the controller radiates. A neighbour therefore observes
at most one distinct value per advertising interval, and publishing faster than
that overwrites values before they are ever transmitted:

```
delivery <= min(1, T_pub / T_adv)
```

That is **undersampling at the transmitter**, and it is indistinguishable from
packet loss in any statistic that counts distinct sequence numbers.

Measured over a 4-point × 10-repeat publish-rate sweep with `T_adv` fixed at
100 ms:

| publish | ceiling | nRF→nRF observed | obs/ceiling |
|---|---|---|---|
| 2.5 Hz | 1.000 | 0.997 | 0.997 |
| 5.0 Hz | 1.000 | 0.962 | 0.962 |
| 12.5 Hz | 0.800 | 0.655 | **0.818** |
| 25.0 Hz | 0.400 | 0.331 | **0.826** |

The two capped points normalise to the same number. The apparent collapse from
0.997 to 0.331 is the ceiling, not the medium. UDP over the same sweep is flat
(0.987 → 0.978), because every publish is its own datagram.

#### The ceiling cannot always be lifted: the controller floor

The obvious fix — match `T_adv` to `T_pub` — has a hard limit. **The CYW43455
rejects `ADV_NONCONN_IND` below 100 ms**, returning *invalid HCI command
parameters* on opcode `0x2006`. Bluetooth 4.x required
`Advertising_Interval_Min >= 0x00A0` for non-connectable and scannable undirected
advertising; 5.0 dropped the restriction; this controller kept it. Measured with
`scripts/adv_floor.py` after it cost 10 runs of a sweep.

So **no bridge agent can publish faster than 10 Hz without capping its own
delivery**, whatever the manifest asks for. Above that rate the ceiling is a
property of the hardware and the only honest response is to report it:

| publish | adv (clamped) | ceiling |
|---|---|---|
| 2.5 Hz | 400 ms | 1.00 |
| 5.0 Hz | 200 ms | 1.00 |
| 12.5 Hz | 100 ms | **0.80** |
| 25.0 Hz | 100 ms | **0.40** |

`radio_for()` clamps rather than raising, and the reason is symmetry: the same
value reaches the nRF and the Pi, so both agent classes sit on the *same* ceiling.
Letting the nRF advertise at its own floor while the Pi is pinned at 100 ms would
give the two classes different ceilings — which is exactly the defect §5 identifies
in the JS platform. A platform limit shared by both classes is a measurement
constraint; one that falls on a single class is a confound.

**Consequence: no delivery ratio from this platform is interpretable without
`T_pub / T_adv` stated alongside it.** The sweep that produced this table was
therefore confounded and is being re-run with the interval pinned to the publish
period (§8.2). Two guards now prevent a silent recurrence: `validate.check()` warns
per node, and `vertex.hub --publish-period` refuses outright.

This is also the answer to the reviewer question about mismatched advertising
intervals between agent classes, and it applies retrospectively to the JS platform
— see §5, seventh bug.

### 5.4 nRF→Pi is the weak direction, and the receiver is why

`nRF→Pi` is the worst link class in every measurement, at every rate. The
explanation that fits is JI's, with a correction to its mechanism made 2026-08-26.

The nRF52 is a single-protocol radio with no Packet Traffic Arbitration, and needs
none. The CYW43455 has PTA because WLAN and BLE share one front-end there.

**An earlier version of this section said PTA "can only arbitrate transmissions the
chip itself originates". That is wrong: PTA arbitrates the shared front-end for
transmit and receive alike, on both radios.** The asymmetry is not TX versus RX in
principle -- it is **duty cycle**:

| BLE role on a Pi | share of time the front-end is needed |
|---|---|
| transmit (advertising) | one ~1.2 ms event per 100 ms = **1.2%** |
| receive (scanning) | `scan_window == scan_interval` = **100%** |

An 83x difference in exposure. When WLAN wants the antenna it almost never collides
with the 1.2% transmit request and *always* collides with the 100% receive request.
The receiving direction loses because it asks for the resource continuously, not
because arbitration is unavailable to it.

A second, independent asymmetry compounds it: a transmit can be deferred a few ms
and still fall inside its advertising event's own slack, whereas a receive
opportunity is set by the **remote** transmitter's clock and cannot be moved at
all. That part of the original argument stands.

Corollary, and it is testable: shortening `scan_window` below `scan_interval` cuts
the receive-side exposure proportionally, at the cost of missing advertisements
that fall outside the window. **Varied for the first time in §5.4a, which finds the
corollary only half right: cutting the window cuts reception on both radios, but
raising it raises reception only on the nRF.**

The RSSI distributions localise it to the Pi's receive path. Geometry is fixed and
the transmitter is the same board in each pair, so a spread difference is a
receiver property:

| case | p5..p95 | span | min | delivery |
|---|---|---|---|---|
| nRF → nRF, neither end shares a front-end | −38..−31 | **7 dB** | −41 | 0.964 |
| Pi → nRF, receiver is the nRF | −44..−37 | **7 dB** | −50 | 0.936 |
| nRF → Pi, receiver is the CYW43455 | −59..−31 | **28 dB** | −91 | 0.824 |
| nRF → Pi, receiver is the CYW43455 | −58..−32 | **26 dB** | −85 | 0.842 |

Four times the spread and a tail 40 dB below the median, on exactly the two links
where the shared front-end receives.

Two readings are excluded by this. **Weak signal**: the medians are within a few dB
across all four, so it is not path loss — what changes is the variance, the
signature of a receiver whose sensitivity is modulated by something other than the
incoming signal. The CYW43455's shared LNA and joint AGC give that a mechanism (§3,
A2). **Pure TX blanking**: blanking loses packets the receiver never hears, leaving
the ones it does hear normal; a 40 dB low tail means the Pi *is* hearing marginal
packets, so its sensitivity is varying rather than being switched off.

The publish-rate sweep supports the asymmetry while refuting a simpler version of
it. Normalised by the §6.3 ceiling, `Pi→nRF` and `nRF→nRF` agree to within 0.02 at
every point, while `nRF→Pi` sits below both and the gap widens 0.03 → 0.23. So the
*level* is the advertising ceiling; the *asymmetry* is the Pi's receive path. Any
claim that falling delivery under load is "coexistence" needs §8.2's experiment,
because `nRF→nRF` — no CYW43455 at either end — fell just as far.

### 5.4a Scan duty is granted, not requested: the two BLE modules have different gain

`n18-50hz` and `n18-50hz-scan100` differ in one field, `scan_window_ms` (11.25 vs
20.0 against a 20 ms interval, so 56.25% vs 100% receive duty). Same 18-cycle, same
seed, same controller, same 100-120 ms advertising dither in both arms, so neither
advertising phase locking nor channel-rotation rate is a variable. Raising the window is
a **1.78x increase in the requested receive duty**. What each receiver actually got:

| receiver | metric | 56.25% | 100% | ratio |
|---|---|---|---|---|
| nRF52840, 8 links | raw arrivals | 3707 | 7363 | **1.99x** |
| CYW43455, bridge 21 | HCI advertising reports | 17762 | 18087 | **1.02x** |
| CYW43455, bridge 26 | HCI advertising reports | 16615 | 19054 | 1.15x |
| UDP, 22 links | delivery | 0.769-0.983 | 0.769-0.995 | 1.03x |

The nRF converts the request into reception one-for-one: 1.78x asked, 1.99x
received. The CYW43455 does not. Bridge 21 saw **2% more advertising reports for a
78% larger window**, and per-link delivery follows: nRF receivers went 0.34-0.41 ->
0.67-0.82 (mean 1.98x) while Pi receivers went 0.457/0.465 -> 0.546/0.598
(1.20x/1.29x).

**The extra window was requested and not granted, which is why UDP is untouched.**
This is the reading the null supports. Had BLE actually held the front-end for
78% longer, WLAN would have contended for it and UDP delivery would have moved;
it did not move (1.03x over 22 links, and the clean links sit at 0.98 in both
arms). So BT never got the antenna for longer. PTA gives WLAN priority and hands
BLE scanning whatever is left, largely independent of the window the host asks
for.

That refines §5.4 rather than contradicting it. §5.4's exposure argument explains
which direction *loses* an arbitration; it does not predict that the losing side's
request is ignored. On the Pi, `scan_window` is close to advisory above some
threshold this pair does not locate.

**Consequence for the platform: the two modules have different delivery-vs-duty
gain**, so BLE reception is not one number.

| receiver | limited by | behaviour |
|---|---|---|
| nRF52840 (single-protocol) | the requested duty | scales ~linearly; at 100% it lands near the 0.909 dither ceiling |
| CYW43455 (shared front-end) | the arbiter | saturates; `nRF→Pi` sits at ~0.6 regardless |

The `nRF→Pi` saturation is stable across configurations: 0.647 in the undithered
100%-scan baseline, 0.546/0.598 here at 100% scan with dither. Duty does not lift
it, which is consistent with §5.4's RSSI evidence localising the limit to the
CYW43455's receive path rather than to airtime.

The BLE network is therefore asymmetric in **both** directions of a link, for two
unrelated hardware reasons. On transmit, the nRF's controller applies the spec's
random `advDelay` to every advertising event and the CYW43455 appears not to, so a
Pi advertiser presents a steady phase and can sit inside an nRF's own advertising
window -- which blanks its receiver -- for as long as the two crystals take to
drift apart. Measured in the undithered `n18-50hz-0`: `26→1` delivered 7 of ~1200
packets with 41 s gaps at -53 dBm while `1→26` over the same pair was steady, and
`21→6` ran perfectly for 96 s and then stopped; a 100-120 ms range removes both.
On receive, the asymmetry is the one above. Neither is a property of the medium,
and both are invisible in an aggregate delivery figure.

**What this does not settle.** Both arms converged: final spread 4.68e-4 at 56.25%
and 5.57e-4 at 100%, against a dead-band of 0.01 -- twenty times inside the band,
so accuracy was never delivery-limited here and the difference between those two
numbers is noise. What 100% duty buys is convergence *rate* through the middle of
the run (spread at t=60 s: 0.488 vs 0.725, ~1.5x). n=1 against n=1, and measured
run-to-run spread on delivery is several points, so the ratios above need
replicates before they carry error bars. The threshold at which the CYW43455 stops
honouring the window is also unmeasured; this pair brackets it between 56.25% and
100% and says nothing about where inside that range it lies.

### 5.5 The completed publish-rate sweep: airtime is not the variable

*(2026-08-25. Four points x 10 repeats x 120 s, 39 of 40 runs usable, identical
initial conditions throughout. Advertising interval matched to the publish period
where the controller allowed it, clamped at the 100 ms floor otherwise.)*

| point | publish | adv | BLE duty | ceiling | conv (s) | nRF→nRF | Pi→nRF | nRF→Pi | UDP |
|---|---|---|---|---|---|---|---|---|---|
| p400 | 2.5 Hz | 400 ms | 0.86% | 1.00 | 16.50 ± 0.33 | 0.885 | 0.854 | 0.678 | 0.991 |
| p200 | 5 Hz | 200 ms | 1.73% | 1.00 | 14.98 ± 0.75 | 0.883 | 0.868 | 0.684 | 0.990 |
| p080 | 12.5 Hz | 100 ms | 3.46% | 0.80 | **13.27 ± 0.14** | 0.703 | 0.678 | 0.518 | 0.973 |
| p040 | 25 Hz | 100 ms | 3.46% | 0.40 | 15.80 ± 0.52 | 0.347 | 0.344 | 0.256 | 0.972 |

#### Normalised by the ceiling, delivery is flat

| link | p400 | p200 | p080 | p040 | spread |
|---|---|---|---|---|---|
| nRF→nRF | 0.885 | 0.883 | 0.879 | 0.868 | **0.017** |
| Pi→nRF | 0.854 | 0.868 | 0.848 | 0.860 | **0.020** |
| nRF→Pi | 0.678 | 0.684 | 0.648 | 0.640 | 0.044 |

Across a **4x airtime range**, per-transmission success is constant to within 2%.
**BLE loss on this testbed is not contention in this range.** It is a fixed
per-transmission failure probability — 0.12 nRF→nRF, 0.15 Pi→nRF, 0.34 nRF→Pi —
multiplied by how many chances each value gets.

p080 and p040 make that a controlled result rather than an inference: identical
airtime (3.46%, both at the floor), different ceilings, and normalised delivery
agrees to 0.011. The ceiling model holds with airtime held fixed.

#### Matching the advertising interval to the publish period was the wrong advice

Earlier guidance here said to pin `T_adv = T_pub` so the ceiling sits at 1.0. That
is necessary but badly incomplete: the ceiling is 1.0 for **any** `T_adv <= T_pub`,
and the number of advertising events carrying one published value is
`k = T_pub / T_adv`. Matching them sets `k = 1`, the minimum, so a single lost
advertisement is a lost value.

Measured at 5 Hz publish, both configurations at ceiling 1.0:

| | nRF→nRF | Pi→nRF | nRF→Pi |
|---|---|---|---|
| `adv` 200 ms, k=1 (`sweep-p200`) | 0.883 | 0.868 | 0.684 |
| `adv` 100 ms, k=2 (`n6-fast`) | 0.964 | 0.934 | 0.832 |
| gain | +0.081 | +0.066 | **+0.148** |

For nothing but advertising twice as often. Independent losses would predict 0.986
/ 0.983 / 0.900; the observed values are all lower, so losses are **correlated** —
a second transmission is worth less than an independent retry, and still worth a
lot.

**Rule: advertise as fast as the controller allows, always.** `radio_for()` now
defaults to the 100 ms floor, and an explicit interval is required to vary airtime
deliberately (the sweep manifests do that, and pay `k = 1` for it).

#### Convergence has an optimum, and it is not the fastest publish rate

16.50 → 14.98 → **13.27** → 15.80 s. Faster publishing helps until it does not.

p080 and p040 deliver the *same* information rate — 8.79 and 8.67 distinct values
per second, both saturated at `adv_rate x p_success` = 10 x 0.87 = 8.7 — yet
convergence differs by 2.5 s. The cause is the staleness window:
`max_neighbor_age_s` defaults to `3 x publish_period`, so it shrinks with the
publish rate while the achievable arrival gap does not.

| point | window (3 x T_pub) | mean gap between delivered values | window / gap |
|---|---|---|---|
| p400 | 1.200 s | 452 ms | 2.65x |
| p200 | 0.600 s | 226 ms | 2.65x |
| p080 | 0.240 s | 114 ms | 2.11x |
| p040 | **0.120 s** | 115 ms | **1.04x** |

At p040 the window is barely one arrival gap, so a large share of values are
already stale when they are used, and the controller discards data it did
receive. It starves itself.

**This is a coupling bug, not a radio result.** The staleness window is sized to
what the agent *intends* to publish; it must be sized to what the medium can
*carry*, which is bounded by the advertising rate. A floor of
`3 x max(T_pub, T_adv)` would fix it. Not changed yet: it alters effective
coupling and therefore invalidates comparison with everything collected so far —
see §8.4.

### 5.6 `n6-50hz` confirms the model, and BLE latency turns out to be `T_adv/2`

*(2026-08-25. 50 Hz dynamics, 10 Hz publish, 2 Hz sine, 10 repeats, 10/10 usable.)*

Predicted from §6.5 before running: convergence at or slightly better than p080's
13.27 s, and delivery near 0.88 / 0.85 / 0.66 because `k = 1` at this
configuration. Measured:

| | predicted | measured |
|---|---|---|
| convergence | ≤ 13.27 s | **13.24 ± 0.21 s** |
| nRF→nRF | ~0.88 | **0.877 ± 0.010** |
| Pi→nRF | ~0.85 | **0.857 ± 0.014** |
| nRF→Pi | ~0.66 | **0.663 ± 0.020** |
| UDP | — | 0.984 ± 0.006 at 177 ± 3 ms |

Delivery predicted to within 0.007 on all three BLE classes. The per-transmission
success model of §6.5 — a fixed failure probability times the number of chances —
is now a predictive model, not a fit.

Convergence equals p080's while carrying a **ceiling of 1.0 instead of 0.80**, which
was the point of the configuration: the same information rate with no undersampling
and a 2.6x staleness margin.

#### BLE one-way delay is `T_adv/2` plus 6 ms, and at `k < 1` it is selection-biased

`nRF→Pi` delay fell from `n6-fast`'s 107 ms to 56 ms. Not an improvement in the
radio — a different point on a simple law. A published value waits for the next
advertising event, so:

* `k >= 1`: the wait is uniform on `[0, T_adv]` → mean `T_adv/2`.
* `k < 1`: only some values are ever advertised, and they are precisely the ones
  published shortly *before* an event → mean `T_adv * k / 2`.

| config | `T_adv` | k | predicted | measured | residual |
|---|---|---|---|---|---|
| sweep-p400 | 400 | 1.00 | 200 ms | 205 ms | +5 |
| sweep-p200 | 200 | 1.00 | 100 ms | 106 ms | +6 |
| n6-50hz | 100 | 1.00 | 50 ms | 56 ms | +6 |
| sweep-p080 | 100 | 0.80 | 40 ms | 46 ms | +6 |
| sweep-p040 | 100 | 0.40 | 20 ms | 26 ms | +6 |

A constant +6 ms offset across a 10x range of predicted delay — fixed processing
cost, not model error.

**So p040's 26 ms is survivorship bias, not low latency.** At `k < 1` the delivered
values are exactly the ones that waited least; the 74% that waited longer were
overwritten and never measured. Any table quoting delay for a `k < 1` configuration
must say so, or it reports the fastest quarter of the traffic as if it were all of
it. `n6-fast`'s 107 ms is the opposite bias: at `k = 2` some values arrive only on
the retry, which pushes the mean above `T_adv/2`.

This also gives the platform a genuine **latency/reliability knob**: `k` trades them
against each other. `k = 1` at the 100 ms floor is the minimum-latency configuration
that is not undersampled (56 ms, delivery 0.66 on the weak link); `k = 2` buys
+0.17 delivery for +51 ms.

### 5.7 Nine agents: a real airtime effect, mostly hidden behind a worse node

*(2026-08-25. 9 agents on 3 hosts, `n9-50hz`, 10 repeats, 10/10 complete, one
excluded from statistics — see below.)*

Two predictions were recorded before the run. One held, one was wrong.

| | predicted | measured |
|---|---|---|
| convergence | ~28 s (lambda_2 scaling) | **24.07 ± 0.26 s** |
| per-link delivery | unchanged from `n6-50hz` | **fell** |

Convergence scaled by 1.82x against the 2.14x that lambda_2 implies. Expected: the
lambda_2 ratio governs the asymptotic *linear* rate, and this law's coupling is a
signed square root, so it is not the right scaling — useful as a sanity bound, not
as a prediction.

#### The airtime effect is real, and smaller than the aggregate suggests

Aggregate delivery fell on every BLE class (`nRF→nRF` 0.877 → 0.797,
`nRF→Pi` 0.663 → 0.606). Reading that as a 1.5x-airtime effect would be wrong:
`n6-*` is pinned to pi2+pi4 while `n9-*` uses all three hosts in order, so the two
experiments do not share their physical links, and **pi1 is a materially worse
node**. Within `n9-50hz` alone, comparing host pairs:

| class | pi2-pi4 | pi1-pi2 | pi1 penalty |
|---|---|---|---|
| nRF→Pi | 0.636 | 0.574 | 0.062 |
| Pi→nRF | 0.812 | 0.688 | 0.124 |

The `pi2-pi4` pair exists in both experiments, so it is the only like-for-like
comparison — same boards, same geometry, same `k = 1`, only advertiser count
differing:

| class | n6 (4 adv, 3.46%) | n9 (6 adv, 5.18%) | delta | Welch t |
|---|---|---|---|---|
| nRF→Pi | 0.663 ± 0.020 | 0.636 ± 0.019 | **−0.028** | **3.62** |
| Pi→nRF | 0.857 ± 0.014 | 0.812 ± 0.078 | −0.045 | 1.80 |

So: **the first genuine contention effect this platform has measured** — but
−0.028, not the −0.057 the aggregate implies. Roughly half the apparent drop is a
property of one Raspberry Pi.

This qualifies §6.5 rather than overturning it. That section found delivery flat
across a 4x airtime range, but produced that range by varying the advertising
interval, which moves `k` at the same time; normalising by the ceiling removed
both. Here `k` is fixed and only the advertiser count changes, and a 1.5x rise in
duty costs about 4% of delivery on the weakest link class. The correct statement is
now "airtime is a weak variable below ~5% duty", not "airtime is not the variable".

**Do not compare `n6-*` and `n9-*` aggregates without conditioning on host pair.**
The host-ordering convention (§7.4) puts node 1 on pi2 in the two-host manifests and
on pi1 in the three-host ones by design, so class means are not comparable across
them.

### 5.8 Duplicate filtering costs 0.44 delivery on the Pi, and the answer is asymmetric

*(2026-08-25. `n9-k2` vs `n9-k2-dupfilter`: 9 agents, k = 2, 10 repeats each,
differing in `radio.filter_duplicates` alone.)*

A BLE controller can suppress repeated advertising reports below the host. The
feature exists for device *discovery* -- an advertiser repeating 10x a second
should be reported once, not 600 times a minute. Here an advertisement is a data
packet whose payload changes every publish, so the feature is being applied to a
channel it was not designed for.

| receiver | link | k=1 | k=2 filter OFF | k=2 filter ON | delta | t |
|---|---|---|---|---|---|---|
| **Pi** | nRF→Pi | 0.605 | **0.768 ± 0.089** | **0.331 ± 0.023** | **−0.437** | **21.3** |
| **Pi** | Pi→Pi | 0.898 | 0.976 ± 0.008 | 0.958 ± 0.021 | −0.019 | 3.8 |
| nRF | nRF→nRF | 0.796 | 0.949 ± 0.010 | 0.946 ± 0.013 | −0.003 | 1.1 |
| nRF | Pi→nRF | 0.750 | 0.935 ± 0.074 | 0.924 ± 0.139 | −0.012 | 0.3 |

The nRF rows are the control: its scanner filters in firmware in **both** variants,
and neither moves (t = 1.1 and 0.3). So the effect is entirely on the Pi's receive
path, which is what the experiment varied.

**It is far worse than losing the retry.** The prediction was that filtering would
suppress the duplicate copy and push `nRF→Pi` back toward its k=1 value of 0.605.
It went to **0.331** -- roughly half of k=1, not equal to it. So the CYW43455 is not
merely dropping the byte-identical repeat; it is suppressing reports whose payload
has changed. Whatever its filter keys on, it is not the advertising data.

**`Pi→Pi` is protected, and that is informative rather than contradictory.** A
`bridge` carries both media, so `MultiTransport` sends every packet over BLE *and*
UDP. When the BLE copy is filtered away the UDP copy still arrives, so the link
barely moves. `ble→bridge` has only BLE and takes the full hit. The redundancy that
makes a bridge expensive in airtime (C2.2) is also what makes it robust here.

**The two stacks do not behave alike.** Zephyr's controller on the nRF has
filtering enabled and delivers 0.949 -- it is evidently data-sensitive. The
CYW43455's is destructive. "Duplicate filtering" names two different behaviours,
and the platform's asymmetry was therefore never removable by matching the flag:
setting it on both ends would have crippled one end only.

**Conclusion: `filter_duplicates` must stay `False` on the Pi**, which is what every
run to date used. The residual asymmetry with the nRF is real, is not fixable by
configuration, and is benign in the direction it exists.

#### What it does not affect

Convergence: 26.62 ± 2.74 s filtered off, 26.72 ± 2.79 s filtered on. Losing 44
points of delivery on one link class did not move it. The ring keeps enough other
paths, and most of the graph is UDP, so the control law absorbed a change that
looks catastrophic per link. Worth remembering when reading any per-link figure as
though it predicted control performance.

Both sets contain one slow run each -- `n9k2-r4` at 34.4 s and `n9k2dup-r1` at
34.6 s against ~25.8 s -- the same collapse signature as §6.9, now six occurrences.


### 5.9 The run-start collapses: diagnosed to a receive-side stall, not loss

Six occurrences across ~110 runs (~5%): `n6-fast` r6 and r9, `sweep-p200` r1,
`n950` r1, `n9k2` r4, `n9k2dup` r1. Characterised 2026-08-25.

**It is not elevated packet loss.** In `n9k2-r4` the worst link's whole-run
delivery is 0.678, entirely normal for that configuration, yet convergence took
34.4 s against ~25.7 s. Averaged delivery hides it because the fault is confined to
the start of the run.

**It is a link that is silent from the trigger and then recovers.** Time from run
start until a neighbour is first heard, against a normal median of ~0.2 s:

| run | link | first heard |
|---|---|---|
| `n9k2-r4` | 21→2 | **26.0 s** |
| `n9k2dup-r1` | 22→3 | **14.7 s** |
| `n9k2dup-r1` | 21→2 | 4.3 s |
| `n950-r1` | 21→2 | heard, then silent for the rest of the run |

**The transmitter is fine.** Every collapsed run shows `published: 600, failed: 0`
on the silent link's sender, identical to healthy runs. The Pi transmits; the nRF
does not register it.

**The direction is always the same.** Every late-starting link is `bridge -> ble`:
the nRF receiving from a Pi. Never the reverse, and never a UDP link.

That is exactly where the nRF's duplicate filter operates, and §6.8 has just shown
that a duplicate filter can suppress far more than byte-identical repeats. The
nRF's accept path was audited and contains no time-dependent rejection --
`on_data_parse_after_device_found` decodes, maps the node id and stores, with
counters for every rejection class -- so the packets are not arriving at the host
at all.

**Action taken:** `BT_LE_SCAN_OPT_FILTER_DUPLICATE` removed from `observer.c`
(2026-08-25), which both removes the last configuration asymmetry between the two
receive paths and tests this hypothesis. Requires reflashing every board.

**Prediction:** if the filter is the cause, the collapse rate falls to zero across a
10-repeat set. If collapses continue at ~5%, the cause is elsewhere and the next
suspect is the advertising schedule -- both ends currently advertise with
`interval_min == interval_max`, which gives the controller no range to spread
events over.

Until this is settled, **no multi-run result should be quoted without stating the
collapse rate**, and any set of 10 should be checked for a run whose convergence
sits well outside the others' spread.

### 5.10 WLAN load at 5 Mbit/s: the control moved, so the PTA mechanism is not what was measured

*(2026-08-26. `n9load5` vs `n950f`: same manifest, same firmware, 10 repeats each;
5 Mbit/s UDP from each of three Pis concurrently. Stall run excluded per side.)*

§8.2 set out a directional prediction: `nRF→Pi` falls under WLAN load,
`Pi→nRF` holds, and `nRF→nRF` -- no CYW43455 at either end -- is immune. The last
of those is the discriminator.

| class | receiver has CYW43455? | unloaded | 5 Mbit/s | delta | t |
|---|---|---|---|---|---|
| nRF→Pi | yes | 0.593 ± 0.027 | 0.549 ± 0.087 | −0.044 | 2.04 |
| Pi→nRF | no | 0.781 ± 0.173 | 0.806 ± 0.073 | +0.025 | 0.57 |
| **nRF→nRF** | **no** | 0.790 ± 0.028 | 0.765 ± 0.037 | **−0.025** | **3.25** |
| Pi→Pi | yes | 0.910 ± 0.079 | 0.854 ± 0.075 | −0.056 | 2.16 |
| UDP | -- | 0.975 ± 0.007 | 0.974 ± 0.008 | −0.001 | 0.86 |

**The control moved, at higher significance than the link predicted to fall.**
`nRF→nRF` involves no Pi radio at either end, so a mechanism internal to the
CYW43455's front-end cannot touch it. It fell anyway. What this experiment
measured is therefore **shared-medium contention** -- WLAN transmissions colliding
with BLE advertisements on the air -- and not the PTA/receive-path mechanism of
§6.4.

**§6.4 is neither confirmed nor refuted by this.** The effect at 5 Mbit/s is small
(2-6 points) and `Pi→nRF`'s spread is too wide to resolve a 0.025 change, so the
test lacks the power to separate the two mechanisms even in principle at this load.
Note also `Pi→nRF` moved *up* by 0.025, which is within its own noise and should
not be read as anything.

**What the design got right and wrong.** Right: including a control link with no
CYW433455 at either end, which is the only reason this reads as a null result
rather than a confirmation. Wrong: choosing a single load point, and one small
enough that every effect sits within 2-3 sigma of noise.

**To make it decisive:** 10 and 20 Mbit/s points, where 3 x 20 Mbit/s approaches
saturation and any directional term should separate from the common one. The
quantity to test is not each class in isolation but the **difference** between
`nRF→Pi` and `nRF→nRF`: contention moves both, PTA moves only the first, so their
gap isolates the mechanism. At 5 Mbit/s that gap is 0.019 ± 0.03 -- consistent with
zero.

**UDP was unaffected** (0.975 -> 0.974) despite 15 Mbit/s aggregate offered. The
state datagrams are small and DTIM-buffered, so they were never competing for the
capacity the load consumed. Wi-Fi delivery is not a useful load indicator here;
the iperf3 per-host loss is.

### 5.11 Spectral geometry: the lab runs on channel 11, which explains the null

*(2026-08-26, from the recorded `wlan_channel`. `scripts/rf_survey.sh` prints this
per host.)*

BLE advertising uses three fixed channels -- 37, 38, 39 at **2402, 2426 and
2480 MHz** -- placed by design to fall between the non-overlapping Wi-Fi channels
1/6/11. This lab's AP is on **channel 11**:

| | centre | 20 MHz span | overlaps BLE adv |
|---|---|---|---|
| wi-fi ch 1 | 2412 | 2402-2422 | **37** |
| wi-fi ch 6 | 2437 | 2427-2447 | none |
| **wi-fi ch 11** | **2462** | **2452-2472** | **none** |
| wi-fi ch 13 | 2472 | 2462-2482 | **39** |

Clearances from channel 11: 50 MHz to adv 37, 26 MHz to 38, 8 MHz to 39. So the AP
and every Pi transmit where **no WLAN energy lands on an advertising channel**.

**This is why §5.10's load experiment found nothing.** Two mechanisms were being
conflated:

* **Spectral contention** -- WLAN energy colliding with BLE packets on air. Depends
  entirely on channel overlap, and here there is none. Adding WLAN traffic on
  channel 11 cannot collide with advertising on 2402/2426/2480.
* **Front-end contention** -- the Pi's own WLAN activity occupying the shared
  antenna, the 100%-vs-1.2% duty argument of §5.4. **Independent of frequency**: a
  busy antenna is busy whatever channel it is busy on.

The load sweep varied a quantity that could only act through the first mechanism,
in a configuration where that mechanism is absent. A weak, non-monotonic result is
exactly what channel 11 predicts, and the 5 Mbit/s "control moved" reading is
better explained as between-set noise -- the 10 Mbit/s point reversed it.

**The decisive experiment is therefore a channel change, not more load.** Moving
the AP to **channel 1** puts WLAN energy directly on advertising channel 37, one of
the three the platform depends on. Repeating the load sweep there separates the two
mechanisms cleanly:

* if delivery falls on channel 1 and not on 11, the effect is spectral;
* if it falls equally on both, it is front-end;
* the `nRF→nRF` control distinguishes local from ambient in either case.

That was available at zero cost from a number already in every run's metadata, and
was not checked before designing the experiment. Recording a parameter is not the
same as reasoning about it.

### 5.12 What the access point controls, and what has not been measured

Three of the platform's numbers are set by the AP, not by anything in this
repository. Two are measured; the rest are not, and two of those could change what
the experiment is.

| AP parameter | status | why it matters here |
|---|---|---|
| channel | **11 (2462 MHz)** | sets spectral overlap with BLE adv 37/38/39 -- §6.12 |
| beacon interval | **100 TU = 102.4 ms** | one factor of the broadcast buffering cycle |
| DTIM period | **3** | x beacon = 307.2 ms -> the 153.6 ms mean broadcast wait of §6.2 |
| channel width | not measured | 40 MHz on 2.4 GHz spans 2452-2492 and **would reach BLE adv 39 at 2480** |
| basic / multicast rate | not measured | broadcast goes at the lowest basic rate. **Every duty-cycle figure in this document assumes an 802.11n rate rather than measuring it** |
| multicast-to-unicast | not measured | some APs convert broadcast to per-client unicast, which would **silently change the transport under test** from broadcast to unicast |
| WMM / QoS | not measured | queueing for the UDP state datagrams |
| AP transmit power | not measured | downlink only; this platform's traffic is uplink and broadcast |

The two marked in bold are the ones that could invalidate a stated result rather
than merely add uncertainty. `channel width` because it changes §6.12's conclusion
that nothing overlaps the advertising channels; `multicast-to-unicast` because
§4 C2.1 and C2.2 reason at length about broadcast being O(1) in degree, which stops
being true if the AP is expanding each broadcast into N unicasts.

Both are readable from a beacon scan: `bash scripts/ap_info.sh <ssid>` shows the HT
operation element (channel width) and the supported/basic rate sets.

#### The largest untried lever: the 5 GHz band

The CYW43455 is dual-band, and the experiment has only ever run on 2.4 GHz. Moving
the Wi-Fi transport to 5 GHz separates the two coexistence mechanisms **completely
and by construction**:

| configuration | spectral overlap with BLE | front-end contention |
|---|---|---|
| 2.4 GHz ch 1 | **high** (WLAN energy on adv 37) | unchanged |
| 2.4 GHz ch 11 | ~none (current lab) | unchanged |
| **5 GHz** | **none, different band** | **unchanged** |

Front-end contention is unaffected by band because BLE and WLAN still share the
antenna path and the arbiter. So the three-point comparison is decisive: if BLE
delivery is the same on ch 11 and 5 GHz but worse on ch 1, the mechanism is
spectral; if 5 GHz matches ch 11 and both differ from a no-WLAN baseline, what
remains is front-end.

That is a cleaner experiment than the load sweep of §6.11, needs no new code, and
uses hardware already present.

### 5.13 Open anomalies

**A single link collapses, about once in fifteen runs.** Three occurrences:
`n6-fast-0` r6 (0.848) and r9 (0.733), and `sweep-p200` r1, where one link fell to
0.107 and convergence took 103.80 s against 13.79 ± 0.17 for its nine peers. The
signature is one link failing while its neighbours stay healthy, not a gradual
degradation — which points at state rather than radio. Undiagnosed, and it is the
whole of the 0.066 sd on `Pi→nRF` in §6.1. Every error bar quoted from this platform
is contaminated by it until it is understood.

---

## 6. Reference

### 6.1 Architecture

Three processes and one microcontroller. The hub never touches the experiment
medium — it distributes assignments over a separate TCP control plane and then gets
out of the way, so control-plane traffic cannot contaminate the measurement.

```mermaid
flowchart TB
    subgraph HUB["hub — laptop"]
        direction LR
        YAML["experiments/*.yaml"] --> TOPO["topology/<br>validate · generators"] --> RUNNER["hub/runner.py<br>ExperimentRunner"]
    end

    subgraph PI["raspberry pi 4 — one process per agent"]
        direction TB
        SVC["agent/service.py<br>AgentService · ControlServer"]
        AG["agent/agent.py — Agent<br>controller · neighbors · runlog"]
        TX["transports/<br>multi · udp · ble"]
        HCI["radio/hci.py · radio/ad.py<br>HCI user channel"]
        REL["agent/relay.py · serial/link.py"]
        SVC --> AG --> TX --> HCI
        SVC -.->|"ble agents only"| REL
    end

    subgraph NRF["nRF52-DK — ble agents only"]
        direction TB
        FW["control.c · agent.c<br>coordination_task.c"]
        RADIO["broadcaster.c · observer.c"]
        FW <--> RADIO
    end

    subgraph OUT["after the run"]
        direction LR
        RUNS[("runs/*.csv + meta")] --> TOOLS["tools/plot_run.py<br>tools/compare_runs.py"]
    end

    AP(["access point<br>DTIM 3"])
    AIR(["2.4 GHz — shared medium"])

    RUNNER ==>|"TCP control plane<br>control/protocol.py"| SVC
    REL <==>|"framed serial<br>SOF·TYPE·LEN·CRC16"| FW
    TX -->|"UDP broadcast"| AP --> AIR
    HCI <-->|"BLE advertising"| AIR
    RADIO <--> AIR
    AG -.->|"written locally,<br>collected afterwards"| RUNS
```

The two planes are deliberately separate. **Control plane**: hub → `AgentService`
over TCP, carrying the assignment, the radio parameters and the run trigger. **Data
plane**: agent → agent over the medium under test, never through the hub. Run data
is written locally on each host and collected afterwards.

Note what shares the 2.4 GHz box: the Pi's BLE, the Pi's Wi-Fi and the nRF's BLE all
occupy one medium, and on the Pi the first two also share one front-end. That is the
subject of §3 and the cause of §6.4.

#### nRF52 firmware

```mermaid
flowchart LR
    UART["uart_link.c<br>proto.c"] --> CTL["control.c"] --> AGENT["agent.c<br>coordination_task.c"]
    AGENT --> REPORT["report.c<br>STATE frame"] --> UART
    AGENT --> BC["broadcaster.c"] --> AIR(["BLE advertising"])
    AIR --> OB["observer.c"] --> AGENT
    AW["air_wire.c<br>16-byte v1 codec"]
    BC -.->|"encode"| AW
    OB -.->|"decode"| AW
```

`control.c` applies inbound frames — NETWORK, ALGORITHM, DISTURBANCE, RADIO, CONTROL
— to the agent. `coordination_task.c` holds the control law, the second of its two
implementations. `air_wire.c` is the on-air format, called by both the broadcaster
and the observer rather than sitting between them; it is deliberately separate from
`proto.c`, which is the serial framing.

### 6.2 Where the control law runs

The agent type decides this, and it is the one asymmetry that is intentional.

```mermaid
flowchart LR
    W0(["wifi agent"]) --> W1["Python controller<br>on the Pi"] --> W2["UdpTransport"] --> W3{{"Wi-Fi"}}
    B0(["bridge agent"]) --> B1["Python controller<br>on the Pi"] --> B2["MultiTransport"]
    B2 --> B3["UdpTransport"] --> B4{{"Wi-Fi"}}
    B2 --> B5["BleTransport<br>raw HCI"] --> B6{{"BLE"}}
    L0(["ble agent"]) --> L1["Pi RELAYS only<br>agent/relay.py"]
    L1 <==>|"serial"| L2["controller runs<br>on the nRF52"] --> L3{{"BLE"}}
```

* `wifi` — controller in Python, UDP only.
* `bridge` — controller in Python, **both** media. It is the only path between the
  BLE and Wi-Fi subnets, so a manifest giving it neighbours on both is unrunnable
  without it. Not airtime-comparable with either, because it transmits every packet
  twice.
* `ble` — the Pi computes nothing. The control law runs on the nRF52 and the Pi
  relays frames over serial. This is why the law exists twice, in
  `controllers/finite_time_adaptive.py` and `coordination_task.c`, and why
  `FIRMWARE_DIVERGENCE.md` exists.

`bridge` and `wifi` run the *same* controller in the same process, so a difference
between them is the medium and not the implementation. That is the comparison the
platform is built to support.


### 6.3 Radio parameters: what each controller exposes

The state of the platform as of 2026-08-26. Everything in the "manifest" column is
driven from `RadioSpec` and recorded in every run's environment block.

#### CYW43455 — the Pi, over the raw HCI user channel

| parameter | HCI command | manifest field | notes |
|---|---|---|---|
| advertising interval min/max | `0x2006` | `adv_interval_ms`, `adv_interval_max_ms` | **floor 100 ms** for `ADV_NONCONN_IND`: the controller enforces the Bluetooth 4.x rule, not 5.0's 20 ms (§6.3) |
| advertising type | `0x2006` | -- | fixed `ADV_NONCONN_IND`: non-connectable and non-scannable, so no `SCAN_REQ`/`SCAN_RSP` airtime (§5) |
| advertising channel map | `0x2006` | `channel_map` | default `0x07`, all three channels |
| own address type / filter policy | `0x2006` | -- | public address, no white list |
| advertising data | `0x2008` | -- | the 16-byte v1 air format, one manufacturer element |
| advertising enable | `0x200A` | -- | lifecycle |
| scan interval / window | `0x200B` | `scan_interval_ms`, `scan_window_ms` | equal by default = 100% receive duty, which is the exposure term in §6.4 |
| scan type | `0x200B` | `passive_scan` | passive by default; active would add `SCAN_REQ` airtime |
| duplicate filtering | `0x200C` | `filter_duplicates` | **must stay False**: enabling it took BLE-only delivery from 0.768 to 0.331 (§6.8) |
| advertising TX power | `0x2007` | -- | **read only**, and nominal: reports +12 dBm while radiating ~20 dB below the nRF (§6.4) |
| transmit power | -- | -- | **not settable**: vendor command only for legacy advertising, and Broadcom's is not public |

#### nRF52840 — Zephyr, configured over the RADIO serial frame

| parameter | applied by | manifest field | notes |
|---|---|---|---|
| advertising interval min/max | `bt_le_adv_start` | `adv_interval_ms`, `adv_interval_max_ms` | same values as the Pi, from one `RadioSpec` |
| advertising type | `bt_le_adv_start` | -- | `BT_LE_ADV_NCONN`, matching the Pi |
| advertising channel map | -- | `channel_map` | **not exposed by Zephyr's API**: applied on `bridge`, recorded as unapplied for `ble` |
| scan interval / window | `bt_le_scan_start` | `scan_interval_ms`, `scan_window_ms` | same values as the Pi |
| scan type | `bt_le_scan_start` | `passive_scan` | same |
| duplicate filtering | `bt_le_scan_start` | -- | **hardcoded off** since 2026-08-25, matching the Pi. Needs a reflash to change |
| transmit power | Nordic VS command | -- | +8 dBm, granted (needs `CONFIG_BT_CTLR_TX_PWR_DYNAMIC_CONTROL=y` — without it the command is absent and the board runs at 0 dBm). Range −40..+8, finely settable. Logged to RTT only |

#### Wi-Fi — the Pi

| parameter | how | notes |
|---|---|---|
| transmit power | `iw dev wlan0 set txpower fixed <mBm>` | **settable, verified**: 800 mBm reads back +8.0 dBm. The default reads 31.0 dBm, which is a `brcmfmac` placeholder, not a measurement |
| channel / frequency | AP-determined | recorded, not chosen |
| power save | `iw` | recorded; ruled out as the cause of the DTIM delay (§6.2) |

#### What is not controllable, and will not become so

* **BLE transmit power on the Pi.** No public interface. The Pi radiates ~20 dB
  below the nRF despite reporting higher, and the asymmetry runs opposite to the
  delivery asymmetry, so it explains nothing (§6.4).
* **Advertising below 100 ms.** Controller-enforced.
* **The nRF's granted TX power, at the host.** Needs the `STATS_REQ` handler.
* **`channel_map` on the nRF.** Zephyr does not expose it.

### 6.4 Configuring the radio parameters

`ExperimentManifest.radio` is the single source. It fans out by agent type:

| | `ble` (nRF52) | `bridge` (Pi) | `wifi` |
|---|---|---|---|
| path | RADIO serial frame | raw HCI user channel | -- |
| built | `service._radio_frame` -> `relay.radio_frame` | `service.py` -> `BleTransport` | -- |
| applied | `broadcaster_set_adv_params`, `observer_set_scan_params` | `cmd_le_set_adv_parameters`, `cmd_le_set_scan_parameters` | recorded, not applied |

Both sides convert ms to 0.625 ms units through the same `ms_to_units`, and both
the requested and the programmed value reach the run metadata, so the parameter is
reported rather than inferred. `wifi` agents carry the block unapplied so all three
agent types in one experiment share an environment record and stay comparable.

`channel_map` is the one field that does not cross: applied for `bridge`, recorded
as unapplied for `ble`, because Zephyr's advertising API does not expose it.

#### The rule

**Never set `publish_period_s` below `radio.adv_interval_ms` without saying so.**
Delivery is bounded by `min(1, T_pub / T_adv)`. Use the helper, which keeps
the pair in step:

```python
from tools.make_manifests import radio_for
"radio": radio_for(0.04)      # -> adv/scan at the 100 ms floor: ceiling 0.40,
                              #    and k as high as the controller permits
```

**Do not match `T_adv` to `T_pub`.** The ceiling is 1.0 for any `T_adv <= T_pub`,
so matching buys nothing and sets redundancy `k = T_pub/T_adv` to its minimum of 1.
§6.5 measures the cost: +0.07 to +0.15 delivery for advertising twice as often at
the same publish rate. `radio_for()` defaults to the floor for this reason.

`radio_for` raises outside the controller floor rather than silently clipping: below the
20 ms spec floor for non-connectable undirected advertising the ceiling *cannot* be
held at 1.0, and that is a fact about the experiment, not a parameter to round.

`validate.check()` warns per node when a manifest violates this, naming the ceiling
and the value that would fix it. It fires on every manifest that produced the first
sweep and on none of the corrected ones. A capped run is still legitimate -- it is
warned, not rejected -- provided the ceiling is reported next to the delivery ratio.


### 6.5 Repository layout

```
vertex/
├── vertex/
│   ├── controllers/     # Controller ABC + implementations        (C1)
│   ├── transports/      # Transport ABC: ble_hci, udp, http, mqtt, sim  (C2)
│   ├── agent/           # asyncio agent (was edge.js + back.js)
│   ├── hub/             # FastAPI + socket.io (was hub.js); public/ unchanged
│   ├── radio/           # raw HCI socket, nl80211, coex tuning    (A4, A5)
│   ├── topology/        # pydantic models, networkx validation, generators
│   ├── sim/             # fast-forward simulator                 (C5)
│   └── analysis/        # loaders, metrics, plots
├── experiments/         # YAML manifests, versioned per experiment
├── firmware/nordic/     # unchanged
└── deploy/              # pyinfra/ansible + systemd templates
```

---

## 7. Open work

### 7.1 Outstanding

#### The list

**A3. The two paths advertise different AD, so their airtime differs.** The nRF
sends a name element the Pi does not, and the Pi sends a flags element the nRF does
not:

```
nRF (broadcaster.c)                       bridge (transports/ble.py)
  Complete Local Name   9                   Flags                 3
  Manufacturer Data    20                   Manufacturer Data    20
                       --                                        --
                       29 / 31                                   23 / 31
```

The manufacturer element is identical — 1 length + 1 type + 2 company id + 16 v1
payload — so the difference is entirely name-versus-flags. Six bytes of AD is six
bytes of PDU:

| | AdvData | PDU | per channel | per advertising event (3 ch) |
|---|---|---|---|---|
| nRF | 29 B | 45 B | 360 µs | **1080 µs** |
| bridge | 23 B | 39 B | 312 µs | **936 µs** |

144 µs more TX airtime per event for a `ble` agent, ~0.14 % duty at a 100 ms
interval. Small in absolute terms, and *not* the thing being compared.

**The name is dead weight.** Nothing reads it. Every receiver filters on the
company id — `air_wire_decode_any()` in the firmware, `find_manufacturer()` in
`BleTransport` and in the loopback scanner. Grepping the tree, every occurrence of
`LABCTRL` / `AD_NAME_COMPLETE` is a writer or a constant definition; there is no
reader. 

Dropping it takes the nRF to **20 of 31 bytes with 11 spare**, up from 2, and
leaves the two ADs differing only by the flags element. Dropping that too — Flags
is optional for non-connectable undirected advertising — makes them 20 and 20,
byte-for-byte identical in size. One line in `broadcaster.c`'s `build_ad()` plus
deleting the `DEVICE_NAME` macros.

Worth noting what the 2 spare bytes mean while the name stays: the AD is full. Any
future element, or a v1.1 payload one byte longer, does not fit.

**A2. No STATS frame from the coordination firmware.** `observer_counters()`
answers the first question when a delivery ratio comes out at zero — did the
receiver hear nothing, or hear plenty of somebody else's traffic? Currently only
logged, once per run, by `observer_stop()`. The frame types exist
(`PROTO_T_STATS_REQ`/`PROTO_T_STATS`) and both loopback peers implement them, but
the host's `STATS_FIELDS` is a fixed 12-field peer-specific layout that the
coordination counters do not map onto. Either widen it per firmware or give the
coordination board its own payload — a decision, not an omission.

**A1. The nRF advertises ADV_SCAN_IND, not ADV_NONCONN_IND.** `broadcaster_init()`
passes the same elements as advertising data *and* as scan-response data. Zephyr
promotes a non-connectable advertiser to `ADV_SCAN_IND` when scan-response data is
supplied, so the board is **scannable**: an active scanner exchanges
`SCAN_REQ`/`SCAN_RSP` with it, which is TX airtime on both sides — the exact
mechanism §3 A2/A3 is about. The scan response also carries nothing the
advertisement does not. Preserved deliberately: dropping it changes what every
`ble` agent has ever put on the air, and the Pi-side scanner defaults to *passive*
so nothing is soliciting those responses today. Worth removing before the next
collection, together with A0.

**A0. The nRF scans with duplicate filtering ON.** `observer_init()` sets
`BT_LE_SCAN_OPT_FILTER_DUPLICATE`. The Pi-side scanner deliberately does the
opposite, because a suppressed duplicate is indistinguishable from a lost packet —
which is the number being measured. Whatever the controller keys its filter on,
the two receive paths are then measuring loss under different rules, across
exactly the BLE-vs-bridge axis the platform compares. Left alone rather than
changed quietly: flipping it changes what every `ble` agent has ever recorded, and
the RADIO frame has spare flag bits to make it an experimental parameter instead
of a constant. Decide before the next data-collecting run.



What pinned it was representation, not effort: the old format *was* a C struct's
compiler-chosen layout, and v1's `tx_time_us` is a **uint48** — no C type lays that
out, so no struct could express v1 at all.

Two things follow, and only the first is closed:

* `seq` now exists on the BLE path, so **sequence-based loss is derivable from a
  `ble` agent's advertisements**. Previously only the STATE frames' `fresh` bits
  gave per-link delivery, at the reporting period rather than per packet.
* `tx_time_us` exists but **is not yet trustworthy across the BLE-vs-Wi-Fi axis.**
  An nRF52 has no synchronised wall clock, so the host now sends its own epoch
  reading in the CONTROL frame (`[run:1][seed:4][epoch_us:6]`, 11 bytes) and the
  nRF adds its elapsed uptime. That is a one-way transfer, not a synchronisation:
  it inherits one UART transit as *bias* (~1.5 ms, uncorrected), and the nRF then
  free-runs on its own crystal for the rest of the run — ±20 ppm over 1600 s is
  ±32 ms, larger than the delays being measured. A delay comparison between a
  `ble` and a `wifi` agent currently measures crystal drift as much as radio
  latency. Periodic re-anchoring would fix it; written up in
  `docs/CLOCK_MODEL.md`.

The AD is now 29 of the 31 available bytes (name 9 + manufacturer 20), up from 19.
Those two spare bytes are the whole remaining margin, so a third element does not
fit.

**Runs collected before this change are not comparable** with runs after it: the
on-air format changed and so did the CONTROL frame length. Worth a firmware
version stamp in `RunMeta` before the next collection.

**C. Which PRNG the host uses.** `Disturbance` defaults to numpy's PCG64;
`vertex/pcg32.py` mirrors the firmware. A run that needs `ble` and `wifi` agents
on the *same* noise stream must inject `Pcg32(seed, node).uniform`, and which one
was used belongs in `RunMeta`. PCG64 is the better generator; PCG32 is the one a
Cortex-M4 without a 128-bit multiply can reproduce. Not decided.

**D. Direction A's UART saturation.** `tx_dropped=269` voided direction A's
delivery ratio: the peer relayed all ~21k foreign advertisements over a link that
could not carry them. A peer-side company-ID filter fixes it. Direction A is not
on the critical path any more — B measured the parameter that was in doubt — so
this is only worth doing if direction A is needed again.

**E. `pyproject.toml` `testpaths`** still points at `tests` and `validation`, both
deliberately removed. `pytest` therefore finds nothing and says so quietly.
`test/check_all.sh` is the entry point now; the stale setting should either follow
it or go.

**What to look at, and what would falsify the mechanism.** Not aggregate delivery
-- the two directions separately:

| | prediction if PTA-limited | prediction if simple congestion |
|---|---|---|
| nRF→Pi | falls with load | falls with load |
| Pi→nRF | **holds** | falls with load |
| asymmetry | **grows** | flat |
| nRF→nRF | holds (no CYW43455 either end) | holds |

`nRF→nRF` is the built-in control: neither end shares a front-end, so if it falls
too, the effect is on-air contention rather than anything about the Pi.


### 7.3 Varying airtime when the rate is floored

**The publish-rate sweep cannot vary BLE airtime past 10 Hz.** Airtime is set by
the *advertising* rate, not the publish rate, and the advertising rate is floored
at 100 ms (§6.3). Above 10 Hz publish, extra publishes are absorbed by the
transmitter and radiate nothing:

| point | publish | adv | advertisers | BLE duty | ceiling |
|---|---|---|---|---|---|
| p400 | 2.5 Hz | 400 ms | 4 | 0.86% | 1.00 |
| p200 | 5 Hz | 200 ms | 4 | 1.73% | 1.00 |
| p080 | 12.5 Hz | 100 ms | 4 | **3.46%** | 0.80 |
| p040 | 25 Hz | 100 ms | 4 | **3.46%** | 0.40 |

So the sweep spans **three** airtime levels, not four, over a 4× range — and p040
adds no airtime information whatsoever.

It is still worth running, for a different reason. p080 and p040 have *identical
airtime and different ceilings*, which makes them a controlled test of the ceiling
model itself: if `obs/ceiling` agrees between them, the model holds with airtime
held fixed. That is a stronger check than the earlier 0.818/0.826 agreement, which
had airtime varying underneath it.

#### Routes to a wider airtime range

**1. More advertisers.** Duty is `n_adv x adv_rate x 864 µs`. With the rate pinned
at its floor, the only free variable is `n_adv` — and it scales with hosts, since
each Pi contributes one `ble` and one `bridge` advertiser:

| hosts | advertisers | BLE duty |
|---|---|---|
| 2 | 4 | 3.46% |
| 3 | 6 | 5.18% |
| 4 | 8 | 6.91% |
| 6 | 12 | 10.37% |
| 10 | 20 | 17.28% |

This is the clean route: it varies airtime without touching any per-node
parameter, so every node's ceiling stays at 1.0 and delivery stays directly
comparable. It needs hardware — the third Pi takes the range to 5.18%.

Note the confound to avoid: adding hosts also adds *agents*, changing the graph.
Hold degree fixed and grow the ring, or the comparison mixes airtime with topology.

**2. A different advertising type.** The 100 ms floor applies to `ADV_NONCONN_IND`
and `ADV_SCAN_IND`. `ADV_IND` may permit 20 ms — `scripts/adv_floor.py` reports all
three. The cost is real: `ADV_IND` is connectable, so scanners may send
`CONNECT_IND`, and the platform deliberately chose non-connectable to keep
`SCAN_REQ`/`SCAN_RSP` off the air (§5, A1/A3). Buying a 5× airtime range by
changing what the medium carries would confound the thing being measured. Only
worth it if the floor turns out to be the binding constraint on a result.

**3. External load.** `iperf3` varies *Wi-Fi* airtime, not BLE, which is §8.2's
experiment. Useful for the coexistence question and not a substitute here.

### 7.4 Decision needed: the staleness window ignores the medium

`AgentConfig.resolved_max_age_s()` returns `3 x publish_period`. §6.5 shows that is
wrong whenever the radio cannot carry the publish rate: at 25 Hz publish the window
is 120 ms while the achievable arrival gap is 115 ms, so the controller discards a
large share of what it received and converges 2.5 s *slower* than the 12.5 Hz point
that delivers the same information rate.

The fix is a floor: `3 x max(publish_period, adv_interval)`, since delivered rate
is bounded by the advertising rate however fast the agent publishes.

**Not applied yet, deliberately.** It changes effective coupling, so every number
in §6 becomes non-comparable with anything collected after it. That is a
re-baseline, and it should be a decision rather than a side effect. Two options:

* **Apply it and re-run the baseline.** Cleanest, costs one `n6-fast` 10-repeat set
  (~22 min). Everything after is comparable and correct.
* **Leave it and always report the window.** The current numbers stay valid; the
  25 Hz point stays understood-but-degraded, and every future fast-publish
  configuration inherits the same trap.

Recommendation: apply it. The current default silently penalises exactly the
configurations the platform exists to explore, and the sweep has already shown the
penalty is 2.5 s of convergence — larger than most effects being measured.


---

## 9. Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-08-18 | Migrate to Python | Radio access (A5) + one language for controller/sim/analysis |
| 2026-08-18 | Rename the project; VERTEX leading, not final | Current name is the experiment, not the platform |
| 2026-08-18 | Try UDP transport **before** coex register tuning | Local TX airtime drives self-blanking (A2/A3), and UDP's airtime doesn't grow under contention the way TCP's does; cheaper and portable |
| 2026-08-18 | Move the LAN to WLAN channel 11 | Clears all three BLE adv channels (A3.1 mech. 2); free, do it immediately |
| 2026-08-18 | **Drop the TP-Link dongle — onboard CYW43455 serves both BLE and WLAN** | Integrated coex gives lossless simultaneous *reception* (A2); the dongle is unarbitrated near-field interference (A3.2). Remove `dtoverlay=disable-wifi`; README §3 becomes a documented fallback appendix |
| 2026-08-18 | **Wi-Fi transport → UDP** | TCP retransmission grows TX airtime under loss, which blanks our own BLE RX, which causes more loss (A3). UDP's airtime is flat. Also fixes the C2 pull-vs-broadcast confound. Details + open questions in C2.1 |
| 2026-08-18 | **Python, for raw HCI User Channel access** | A5. Reaching adv interval / scan window / channel map is the central constraint, and it's stdlib in Python vs. a native binding in Node |
| — | How to reuse the existing C++ min-BLE stack | **Open — blocked on reading it.** Prefer porting its knowledge to pure Python; sidecar daemon over a unix socket if it's substantial; in-process binding only with a measured reason. See A5.1 |
| — | UDP: subnet broadcast vs. IP multicast | Open — test both (C2.1). Broadcast avoids IGMP snooping
| 2026-08-21 | **Broadcast's latency cost on infrastructure WLAN is measured, not assumed** | UDP median one-way delay 171 ms with a 16 ms minimum and zero duplication, on stations whose own `power_save` is **off**. Broadcast and multicast through an AP are buffered to the next DTIM beacon whenever *any* associated station dozes — including stations that are not ours, which is why turning ours off changed nothing. With a 200 ms publish period, Wi-Fi neighbour data is nearly a full period stale while BLE is half that: the reverse of the assumed ordering. `scripts/ap_info.sh` reads beacon interval x DTIM to confirm |
| — | UDP: broadcast vs multicast vs **unicast-per-neighbour** | **Reopened**, with a third option. Unicast is not DTIM-buffered, so it should collapse the 171 ms; its cost is airtime proportional to degree — which is also the traffic knob the G1/G2 question needs, since broadcast airtime is degree-independent. `UdpTransport` already takes `send_to`, so this is a small change and a decisive experiment |
| 2026-08-20 | **Firmware integrates in `float`, publishes rounded int32** | Storing the step result back into `int32_t` re-injected the truncation error every period, so the C and Python laws were different dynamical systems. Accumulators are the truth, the int32 fields a mirror. Residual is now the float32 floor, 4.5e-5 over 400 steps |
| 2026-08-20 | **PCG32 in the firmware, replacing `rand()`** | `rand()` was unseeded (unreproducible) *and* implementation-defined (picolibc ≠ glibc), so no seeding would have made the C and Python noise comparable. PCG32 is ~10 lines with a fully specified sequence |
| 2026-08-20 | **The PRNG seed travels in the CONTROL frame** | It is a per-run quantity — the same gains replayed with a new seed is a new run — so it belongs with the trigger, not in ALGORITHM. Node id is the stream selector, so two nodes on one seed still differ |
| 2026-08-20 | **`BleTransport` owns a single reader; `HciSocket.command()` is setup-only** | One user-channel socket carries commands and advertising reports together, and `command()` discards what it reads while waiting. Called once per control period, it would eat reports indistinguishably from radio loss |
| 2026-08-20 | **Radio parameters recorded for every agent type, `wifi` included** | Three types appear in one experiment; a missing environment block makes their logs non-comparable. `applied_on` says where they actually took effect |
| — | Whether the host uses PCG64 or PCG32 by default | **Open.** PCG64 is the better generator; PCG32 is the one the nRF can mirror. Only matters when `ble` and `wifi` agents must share a noise stream. See §8b.C |
| — | Whether `broadcaster.c` moves to the v1 payload | **Open.** v1 carries `seq` and `tx_time_us`, so one-way delay and sequence-based loss become derivable from `ble` advertisements. Invalidates comparison with every run collected so far. See §8b.B | |
