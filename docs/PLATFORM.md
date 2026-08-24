# VERTEX platform

An experimental testbed for finite-time adaptive coordination controllers over a
heterogeneous radio network. Raspberry Pi 4 nodes each host up to three logical
agents (`ble`, `wifi`, `bridge`); each Pi is paired over USB with an nRF52-DK. A
hub on a laptop orchestrates runs.

The point of the platform is to make the *transport* a controlled variable: the
same control law, the same initial conditions, the same disturbance stream, run
over BLE advertising and over Wi-Fi, so that differences in convergence are
attributable to the network rather than to the algorithm.

**This document is current state: design decisions and what has been measured.**
Two companions:

* `RUNBOOK.md` — how to bring up and run an experiment, and the traps.
* `JOURNAL.md` — the dated record of every run, verbatim, in order.
* `CLOCK_MODEL.md` — six different things here are called "clock"; read before
  touching anything time-related.
* `FIRMWARE_DIVERGENCE.md` — where the C and Python implementations disagree.

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

---

## 2. Settled decisions

Three decisions are premises, not open questions.

1. **The Pi code is Python, not JavaScript.** The driving reason is raw HCI User
   Channel access: stdlib in Python, a native binding in Node. §3 is the whole
   argument.
2. **The onboard CYW43455 serves both BLE and WLAN.** The TP-Link USB dongle is
   retired and `dtoverlay=disable-wifi` is gone. §3 A2 is why this is defensible,
   and §6.4 is the measurement that shows what it costs.
3. **The Wi-Fi transport is UDP broadcast, not HTTP pull.** TCP retransmission
   inflates TX airtime, which blanks this node's own BLE receiver, which causes
   more loss — a positive feedback loop UDP breaks. §4 C2.1.


### Naming — still open

Current name describes the experiment, not the platform. Candidates:

| Name | Expansion | Character |
|---|---|---|
| **VERTEX** *(lead)* | Virtual Edge Radio Testbed for EXperimentation | Graph pun (vertices/edges) matches the object of study; reuses existing "edge-device" vocabulary |
| **MANTIS** | Multi-Agent Networked Testbed for IoT Systems | Most memorable, unique in search |
| **HERMES** | Heterogeneous Edge Radio Mesh Experiment System | Emphasizes transport layer |
| **TRIAD** | Testbed for Radio-heterogeneous Interacting Agents and Distributed control | Fits today's 3-agents-per-node, but that ceiling is meant to go |

Tagline: *an open testbed for distributed and multi-agent control over real
heterogeneous radio links, on ~$100 of hardware per node.*

Rename touches: repo, `package.json` name (currently `consensus`), the `LABCTRL`
BLE device name (`nordic/prj.conf`, `raspberry/ble.js`, `raspberry/bleadv.sh`),
tmux session name in `start.sh`, RNG seed strings in `net.js` (`FTRAC`,
`FTRAC_run_N` — **changing these changes all initial conditions**, so either keep
them or bump a schema version deliberately).

---

---

## 3. Radio access and coexistence

The central technical constraint of the platform, and the reason for the Python
migration. Everything in this section is implemented unless marked otherwise.

### A1. Root cause: we are two abstraction layers above the knobs

- `raspberry/bleadv.sh` drives `bluetoothctl` via an `expect` script. The
  `advertise` menu exposes manufacturer data and name — **not** advertising
  interval, channel map, TX power, or PHY.
- `raspberry/ble.js` uses BlueZ D-Bus `SetDiscoveryFilter`, whose entire
  vocabulary is `Transport`, `RSSI`, `Pathloss`, `UUIDs`, `DuplicateData`.
  **Scan interval and scan window are not in that API at all.**

So the two parameters that actually determine how fast a bridge sees its BLE
neighbors — advertiser interval and scanner window/interval duty cycle — are
exactly the two that are unreachable. Everything downstream is compensation:
the exponential-backoff respawn in `edge.js`, RSSI-as-liveness, the 2 s stale
cache, and the `_uuidClassification` map that exists only to dodge
`max_match_rules_per_connection=2048` (see `docs/platform_running_info/historical_errors.txt`).

Second-order problem: `bleGetState()` polls BlueZ's *cached* `ManufacturerData`
over D-Bus, at a rate unrelated to the advertising rate, with no reception
timestamp. "Neighbor sent the same value twice" is indistinguishable from
"neighbor is dead and I'm reading a stale cache."

### A2. CYW43455 coexistence — what the datasheet claims, and what it means here

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
the whole dongle question — see A3.

**Antenna wiring.** The "dual-antenna applications" clause needs a board that
routes two antenna ports. No Raspberry Pi does: Pi 4 has a single PCB trace
antenna shared by WLAN and BT, and CM4's external antenna connector is likewise
shared. We are unavoidably in the shared-antenna case — which is precisely the
case the quote covers, so this is fine.

**Host interfaces are already separate** and are not a bottleneck: on Pi 4, WLAN
is on SDIO, BT is on a PL011 UART at 3 Mbaud (~6600 HCI advertising reports/sec
ceiling — far above our scale, but worth remembering if we ever scan dense
environments).

**Coexistence survives HCI User Channel.** Coex arbitration lives inside the
combo chip between the WLAN and BT cores; it does not depend on the Linux
Bluetooth host stack. So taking exclusive control of `hci0` (A5) does **not**
disable coexistence. These two workstreams compose.

### A3. The airtime argument — this is probably why the dongle was needed

Given A2, the honest hypothesis is that the dongle is a workaround for a
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

**Hypothesis to test: BLE + onboard Wi-Fi coexist acceptably once the Wi-Fi
agent stops using TCP/HTTP, because the RX/RX case is already lossless by
design and the TX/RX case becomes rare.** If that holds, the dongle goes away
*and* the Wi-Fi transport gets better semantics at the same time (see C2 — HTTP
request/response vs. BLE broadcast is currently a confound in any BLE-vs-Wi-Fi
comparison).

Do the UDP work **before** the register work. It is cheaper, portable, and may
make the register work unnecessary.

#### A3.1 Three independent mechanisms — do not conflate them

Reducing airtime only addresses the first. Ranking them is what A7 is for.

1. **Self-blanking (coexistence).** *My* WLAN TX deafens *my* BLE RX, and vice
   versa. Scales with **local transmit airtime**. → fixed by UDP (A3).
   Note the useful corollary: a *neighbor's* WLAN frame arriving while we scan is
   the lossless RX/RX case, so each node governs its own BLE reception quality by
   governing its own Wi-Fi TX. Tractable and local.
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
   with adv 37 or 38. Then, once A5 lands, set the **advertising channel map** to
   drop 37 as well. (One of the original motivations for wanting register access.)
3. **Coex policy throttling.** The firmware arbiter deprioritizes BLE scan and adv
   *regardless* of how little airtime we use. Independent of both above.
   → `btc_params` (A4).

#### A3.2 The dongle trades arbitrated interference for unarbitrated interference

Worth stating because it cuts *in favor* of going native. A TP-Link stick
transmitting at ~20 dBm a few centimetres from the Pi's PCB antenna is a strong
in-band near-field interferer, and the two chips have **no coexistence wiring
between them** — neither knows the other exists. The integrated CYW43455 at least
*knows* when both radios want the antenna and schedules around it.

So the real trade is: dongle = unarbitrated, unpredictable interference but a
full-duty BLE scanner; integrated = arbitrated, predictable interference but a
throttled BLE scanner. Which wins is an empirical question (A7), and the
integrated side improves further once we control the scan parameters ourselves (A5).

#### A3.3 Ordering caveat

BlueZ's default scan window/interval duty cycle is unknown and unreachable today
(A1) and **may be losing more packets than blanking ever did.** If so, A5 (HCI
scan-window control) outranks A3 (UDP) for BLE reception specifically. We do not
know which dominates — that is exactly what A7's matrix resolves. UDP still goes
first: cheap, portable, and it fixes the C2 transport confound regardless of how
the ranking comes out.

### A4. Coexistence knobs, if A3 is not sufficient

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

### A5. Reaching the BLE parameters: raw HCI User Channel

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
no cache, no freshness guesswork), and there is **no D-Bus**, so the match-rule
exhaustion bug cannot occur.

~200 lines of `socket` + `struct` in Python (stdlib). In Node it needs a native
binding — this is the single strongest argument for the language switch (§2).
Alternative if we don't want to hand-roll: [Bumble](https://github.com/google/bumble),
Google's Python Bluetooth stack, which does user-channel HCI and exposes the
full command surface.

**Honest limit:** the CYW43455 LE controller firmware is closed. HCI is the
deepest legitimate interface; actual silicon registers are not exposed. If
"register-level" means HCI parameter control, this delivers all of it. If it
means PHY-level, escalate to A6.

#### A5.1 The existing C++ min-BLE stack — port, sidecar, or bind? *(OPEN)*

One of JI's developers has already written a **minimal BLE stack in C++ over
`socket()`**. That is exactly this layer, already built. Three ways to use it,
and the choice matters for the whole deployment story:

| Option | Cost | When it's right |
|---|---|---|
| **a) Port the knowledge, not the binary** — read it as the reference spec, reimplement in pure Python | one-time reading effort; no build system | **default choice.** The stack is ~200 lines of `socket`+`struct` territory; the *valuable* part is the HCI command sequencing, controller quirks, and event-parsing edge cases it already encodes. Extract those, drop the C++ |
| **b) Sidecar daemon** — keep the C++ as a separate process, speak msgpack/lines over a unix socket | small protocol to define | the stack is large and battle-tested, or it does something genuinely timing-critical. Gains: crash isolation (a segfault doesn't kill the agent), no GIL interaction, no build coupling, and it drops straight into the `Transport` ABC (C2) as `BleHciTransport` → local daemon |
| **c) In-process binding** — pybind11 / nanobind / cffi | ARM build or wheel on every Pi, ABI + lifetime management, two-language debugging | **last resort.** Only with a *measured* latency reason |

Reasoning against (c) as the default: it re-imports the two-language problem the
Python migration is meant to remove (§2, decision 1), and it puts a cross-compile or
per-Pi build into the deployment path (D6) for a hot path that isn't hot — the
latency-critical operation is a `recv()` loop over HCI advertising reports, and
the 3 Mbaud BT UART caps at ~6600 reports/s, which pure Python clears comfortably.

**Prefer (a); fall back to (b) if the stack is substantial.** Note that (b) beats
(c) even when we want to keep the C++, and is the better shape for a testbed:
the BLE radio becomes a replaceable service rather than a linked dependency.

**Blocked on reading the code.** Decide once we've seen its size and what quirks
it handles — those quirks are the asset either way, so the first task is a read-through
and a written list of what it knows that a naive implementation wouldn't.

Note: advertising and scanning simultaneously from one controller (what the
bridge needs) requires the controller to support concurrent adv + scan roles —
verify against the 43455's LE feature bits before committing.

### A6. Escalation: own the controller (nRF52840 as HCI radio)

Flash an nRF52840 dongle with Zephyr's `hci_uart` sample; plug into each Pi as
its BLE radio over USB CDC. The Pi then has a controller whose firmware **we**
compile. Strictly better than the TP-Link workaround: it frees the onboard chip
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

Switching Wi-Fi from HTTP/TCP to UDP is decided (§10). It is not a drop-in
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

*Verify:* subnet broadcast vs. IP multicast. Multicast risks IGMP snooping and
AP-side buffering quirks; subnet broadcast sidesteps IGMP entirely and is simpler.
Test both. (AP power-save buffering shouldn't apply once `power_save off` is set,
but confirm rather than assume.)

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

- **C3. Single source of truth for the control law.** JS and C will drift. Minimum:
  a cross-validation test running both against one fixture. Better: generate both
  from one spec.
- **C4. Versioned binary payload with sequence number + TX timestamp.** Current
  BLE payload is 6 bytes `[flag | node | int32 vstate]`; the adv packet allows 31.
  Adding `uint16 seq` + `uint32 tx_ts` (+6 bytes) yields **per-link delivery ratio
  and true one-way delay for free**, on both transports, with no extra
  instrumentation. For a platform whose selling point is real links, those two
  numbers are the headline product. **Prerequisite for both A7 and C2.1** —
  under UDP there are no ACKs, so this is the only loss signal we have.
  One layout, versioned, shared by BLE adv and UDP alike.
- **C5. Simulation mode.** N agents, mock transport, one process, 100× real time.
  Validate an algorithm in seconds before touching hardware.
- **C6. Topology validation** via `networkx`: connectivity, strong connectivity,
  balance, spectral gap λ₂ — reported *before* the run, next to the convergence
  rate it predicts.
- **C7. Zeroconf/mDNS discovery** instead of hardcoded IPs.
- **C8. Unicast-per-neighbour as a selectable UDP mode.** *Proposed, not decided.*
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

**It is structural, not congestion.** It is present at 2.6% duty cycle with no
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

*Cost.* Airtime rises with degree, exactly as C2.1 said — at degree 4 that is 4x
the frames. On a shared medium serving BLE as well, that is the self-blanking
mechanism of §3 A3 acting on purpose rather than by accident. Which is the point:
it becomes a variable.

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

## 5. What the JS platform got wrong

Found by reading the legacy code. These are the defects the rewrite exists to fix;
none of them were visible in the collected data at the time.

### The six bugs

These are defects in `raspberry/*.js`, kept as a record of what the port has to
avoid rather than as a task list. 1-3 and 6 are closed by design in `vertex`:
`Disturbance` refuses to construct without a seed or an injected stream,
`resolve_local_ip()` names the interface and raises rather than falling back, `decode_manufacturer_data` rejects a
foreign company id instead of guessing, and `StatePacket` raises on int32
overflow rather than wrapping. 4 and 5 are open questions for the new design, not
inherited bugs — the Python agent re-resolves neighbours continuously and copies
before logging, but neither is measured yet.

1. **Disturbance is not reproducible.** `algo.js` `computeDisturbance()` calls
   `Math.random()` directly, while `net.js` carefully seeds `seedrandom('FTRAC')`
   for initial conditions. ICs replay exactly; disturbances never do. **Every
   multi-run comparison inherits this.**
2. **Non-deterministic IP selection.** `net.js` `getIpAddress()` returns the first
   non-internal IPv4 in OS enumeration order — a coin flip when `wlan0` and
   `wlan1` both exist. The deterministic version is commented out right above it.
3. **Manufacturer-data fallback can parse a stranger's packet.** `ble.js`
   `_extractPayload()` falls back to `Object.values(dataRaw)[0]`; any nearby
   advertiser with ≥6 bytes of manufacturer data can be read as a neighbor state.
4. **No dynamic membership.** `bleGetDevices()` runs once at trigger
   (30 × 200 ms ≈ 6 s). A neighbor that reboots or arrives late stays invisible
   for the rest of the run — silently changing the graph the algorithm runs on.
5. **`state.neighborVStates` written by both loops** (`edge.js`, network loop and
   dynamics loop) and assigned by reference → logged snapshots can be internally
   inconsistent.
6. **`int32` at scale 1e6 caps state at ±2147.** Fine today; document or move to
   float32 in the payload (pairs with C4).

---

### The seventh, found later: the advertising interval was never set

`bleadv.sh` drives `bluetoothctl advertise on`, which has no way to specify an
advertising interval, so the bridge agents inherited the controller default while
the nRF agents advertised at `BT_LE_ADV_NCONN`'s 100–150 ms. At the 500 ms publish
period of the original experiments that put the two agent classes on different
delivery ceilings — see §6.3, which quantifies the mechanism, and note that the
ceiling difference follows exactly the axis those experiments compared.

---

## 6. Results on the new platform

All figures below are from `n6-ring`/`n6-fast`: 6 agents on 2 hosts, 25 Hz
dynamics, 120 s runs, 10 repeats with the run index held fixed so initial
conditions and disturbance streams are bit-identical across the set and the
network realisation is the only variable. `JOURNAL.md` has the full record.

### 6.1 The transport trade-off, with error bars

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

### 6.2 The 171 ms UDP delay is the access point's DTIM cycle

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

### 6.3 The advertising interval is a delivery ceiling

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

### 6.4 nRF→Pi is the weak direction, and the receiver is why

`nRF→Pi` is the worst link class in every measurement, at every rate. The
explanation that fits is JI's: the nRF52 is a single-protocol radio with no Packet
Traffic Arbitration, and needs none. The CYW43455 has PTA because WLAN and BLE
share one front-end there — **but PTA can only arbitrate transmissions the chip
itself originates.** The Pi can schedule its own BLE TX around its own WLAN. It
cannot schedule the nRF's. The direction in which the Pi *receives* is the one with
no coordination available to it.

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

### 6.5 Open anomalies

**A single link collapses, about once in fifteen runs.** Three occurrences:
`n6-fast-0` r6 (0.848) and r9 (0.733), and `sweep-p200` r1, where one link fell to
0.107 and convergence took 103.80 s against 13.79 ± 0.17 for its nine peers. The
signature is one link failing while its neighbours stay healthy, not a gradual
degradation — which points at state rather than radio. Undiagnosed, and it is the
whole of the 0.066 sd on `Pi→nRF` in §6.1. Every error bar quoted from this platform
is contaminated by it until it is understood.

---

## 7. Reference

### 7.1 Architecture

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

### 7.2 Where the control law runs

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


### 7.3 Configuring the radio parameters

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
Delivery is bounded by `min(1, T_pub / T_adv)` (A3.4). Use the helper, which keeps
the pair in step:

```python
from tools.make_manifests import radio_for
"radio": radio_for(0.04)      # -> adv/scan 40 ms, ceiling 1.0 at 25 Hz
```

`radio_for` raises outside 20..10240 ms rather than silently clipping: below the
20 ms spec floor for non-connectable undirected advertising the ceiling *cannot* be
held at 1.0, and that is a fact about the experiment, not a parameter to round.

`validate.check()` warns per node when a manifest violates this, naming the ceiling
and the value that would fix it. It fires on every manifest that produced the first
sweep and on none of the corrected ones. A capped run is still legitimate -- it is
warned, not rejected -- provided the ceiling is reported next to the delivery ratio.

#### Two generator traps found while wiring this up

**The host list is not persisted.** `make_manifests.py` takes `--hosts` (or
`VERTEX_HOSTS`); run without it, it silently rewrites every manifest back to the
built-in `10.6.5.1..10`. The committed set was already inconsistent because of this
-- `n6-*` on `.2/.4`, `n9-ring` on `.1/.2/.3` -- i.e. generated at different times
with different flags. Always pass `--hosts`, or export `VERTEX_HOSTS`.

**A skip used to drop everything downstream.** `if len(FIRST_RUN_HOSTS) < 3: ...
return out` returned from `manifests()` rather than skipping `n9-ring`, so a 2-host
lab produced 3 manifests instead of 11 while printing only `skip n9-ring`. Both
skips are now guards; only the last block in the function returns early.

### 7.4 Repository layout

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

### 7.5 Experiment infrastructure

- **D1. Fault injection as a scenario DSL.** `midRunEvents` in `hub.js` is the
  seed. Generalize: link drop, added delay, packet-loss rate, node kill, and
  **Byzantine agents** broadcasting wrong/adversarial states. Byzantine
  resilience is heavily studied and we are one config file away from testing it.
- **D2. Per-link QoS metrics as headline output.** `neighborReceived` (fresh vs.
  cache) already exists in `edge.js`. Aggregate into delivery ratio + latency per
  link; plot beside the trajectories.
- **D3. Clock-sync quality in run metadata.** `chronyc tracking` offset/jitter per
  node per run → error bars on time axes.
- **D4. Data format.** JSON-per-node → Parquet or SQLite with a schema version.
  `pd.read_parquet(run_dir)` and you're analyzing.
- **D5. Timing instrumentation.** The hand-rolled drift compensation in `edge.js`
  is reasonable but nothing measures the residual. Log actual vs. nominal step
  time; report a jitter histogram. Then `SCHED_FIFO` + CPU pinning + `isolcpus`
  if warranted.
- **D6. Deployment automation.** `pyinfra`/Ansible + templated systemd units
  replaces "copy files to each Pi, open three tmux panes". Folds in the chrony
  setup and `power_save off` currently documented as manual README steps.
- **D7. Tests.** `npm test` is currently `exit 1`. Golden-fixture controller tests
  + a simulation-mode integration test.
- **D8. Structured logging + metrics endpoint.** `console.log` → structured JSON;
  Prometheus-style counters so the UI can show live link health.

---

---

## 8. Open work

### 8.1 Outstanding

#### The list

> Item letters here are local to §8.1. Bare `A2`/`A3` elsewhere in this document
> refer to §3's coexistence analysis; those are written `§3 A2` below.

**A0–A3 are one decision, not four.** Each is a systematic difference between what
a `ble` agent puts on the air and what a `bridge` agent does, sitting directly
across the axis §3 A2/A3 compares — the same shape of confound as the firmware
divergences, in the radio layer instead of the arithmetic. Each is also cheap to
fix and each invalidates comparison with previously collected runs. So they are
worth fixing **together**, spending one "runs before this are not comparable"
boundary rather than four. None has been changed unilaterally for that reason.

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
reader. It is a holdover from `raspberry/ble.js`, which matched on
`name !== 'LABCTRL'` because BlueZ's D-Bus API surfaced the name conveniently and
manufacturer data awkwardly — and that code path is gone.

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

**A. `channel_map` reaches `bridge` but not `ble`.** The manifest can request an
advertising channel map; `BleTransport` applies it through HCI, and the RADIO
frame has no field for it because Zephyr's advertising API does not expose the
advertising channel map. Recorded honestly as
`environment.radio.channel_map_applied`, so nobody reads a restricted map into a
`ble` run that never had one. Closing it means reaching the controller through
Zephyr's HCI driver directly on the nRF. Worth doing: §3 A3.1 says channel 11 is what
makes the map matter, and steering the map is the other half of that argument.

**B. ~~The nRF still advertises v0.~~ Closed — both sides speak v1.**
`firmware/nordic/src/air_wire.h` replaces the memcpy'd `custom_data_type` with a
field-by-field serialiser, and `test/crossval/check_air_wire.py` links the real
`air_wire.c` to check that the two encoders are **byte-identical in both directions**,
that v0 still decodes on receive, and that both sides reject the same six
malformed inputs.

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

---

### 8.2 The next experiment: does WLAN load break one direction only?

§6.4 predicts an asymmetry that §6.3 shows cannot be read off a delivery curve.
Load the Pi's WLAN with `iperf3` at stepped rates and watch the two directions
separately:

* **nRF→Pi** delivery degrades, and its RSSI spread widens further.
* **Pi→nRF** stays flat, because PTA schedules that direction.
* the **asymmetry between them grows with load**.

If both degrade equally, §6.4 is wrong and the cause is shared airtime rather than
receiver arbitration. If neither degrades, the effect is not coexistence at all.

That is a *directional* prediction, and it is worth more than "performance degrades
under heavier traffic": a monotonic decline in aggregate delivery cannot separate
congestion from coexistence, whereas a decline in one direction only, on the link
whose receiver shares a front-end, with a widening RSSI spread, distinguishes them
by construction. The platform's own Wi-Fi transport is already a load source — at
0.52% duty the asymmetry is 12 points — so `iperf3` is only the controlled version
of something already happening.

It also reframes the degree comparison. Degree does not change broadcast airtime
(§4 C2.2), so G1 vs G2 cannot vary traffic on the medium — but it does change how many
*accepted* packets each receiver processes, and under §6.4's mechanism the receiver
is where the damage happens. An effect there would be receive-side load, not
airtime. Reporting it as airtime would repeat the §6.2 error.

### 8.3 Also queued

* Re-run the publish-rate sweep with the ceiling pinned (`scripts/sweep.sh`).
* Diagnose the single-link collapse (§6.5) — it gates the credibility of every
  error bar.
* Nine agents on three hosts. Needs a third Pi and `n9-ring` regenerated: it
  currently declares hosts that are not the current lab.
* Hatch the "not measured" delay bars rather than drawing them at zero (§6.1),
  which needs a STATS frame from the nRF.


## 9. Idea inbox

Unsorted ideas go here; promote into a workstream once shaped.

- *(2026-08-18, JI)* Drop the TP-Link USB dongle and use CYW43455 native
  BT/WLAN coexistence instead — datasheet documents lossless simultaneous
  reception via shared LNA + joint AGC. → shaped into §3 A2/A3/A7.
- **Check the router's current WLAN channel.** If 1 or 6 it is colliding with BLE
  adv channel 37 or 38 on every frame — see A3.1. Free fix, unblocked, do first.
- *(2026-08-21, measured)* **Broadcast costs 153.6 ms of DTIM latency on
  infrastructure WLAN.** Confirmed to 0.8% against the AP's beacon interval and
  DTIM period. Unicast-per-neighbour proposed as a selectable mode -> shaped into
  C2.2 / C8. Also supplies the O(degree) traffic knob that broadcast cannot.
- *(add yours here)*

---

## 10. Decision log

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
