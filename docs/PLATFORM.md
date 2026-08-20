# Platform Evolution — Working Context

**Status:** planning. Nothing here is implemented yet, but three direction-setting
decisions are now **made** (§10): Python, onboard-radio-only, and UDP for Wi-Fi.
Treat those as settled premises; everything else is still open.
**Purpose:** the shared context for turning this repo from *one finite-time adaptive
coordination experiment* into a *general testbed for distributed and multi-agent
control over real heterogeneous radio links*.

Read this first in any new session. Append ideas to §9 (Idea Inbox); record
choices in §10 (Decision Log).

---

## 1. Naming — OPEN

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

## 2. Design Principles

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

## 3. Workstream A — Radio Access & Coexistence

**The highest-priority workstream.** Two coupled goals: reach the radio
parameters that govern link behavior, and drop the TP-Link USB dongle.

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
binding — this is the single strongest argument for the language switch (§4).
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
Python migration is meant to remove (§4 reason 2), and it puts a cross-compile or
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

### A7. Measurement plan — decide the dongle question with data

Do not decide this by reading datasheets, including this document. Instrument and
measure. Prerequisite: **sequence numbers in the payload** (see C4).

Matrix — for each cell, record BLE per-link packet delivery ratio, BLE one-way
delay (chrony gives us ~30–100 µs, so this is measurable), and Wi-Fi RTT/jitter:

| | Wi-Fi idle | Wi-Fi light (UDP agent) | Wi-Fi loaded (`iperf3`) |
|---|---|---|---|
| BLE scan off | — | | |
| BLE scan 50% duty | | | |
| BLE scan 100% duty | | | |

Run each row twice: **onboard CYW43455 only** (the chosen configuration) vs.
**TP-Link + onboard-BLE** split (the retired one, as a baseline). Then sweep
`btc_params` if the onboard case underperforms.

Control for A3.1's three mechanisms separately or the result is uninterpretable:
fix the WLAN channel at 11 throughout (removes mechanism 2), log per-node Wi-Fi TX
airtime alongside BLE delivery ratio (isolates mechanism 1), and only then sweep
`btc_params` (mechanism 3). Also record BLE scan window/interval per row — once A5
lands this is a controlled variable rather than an unknown, which is what makes the
table interpretable at all (A3.3).

The output is a coexistence characterization table for the Pi 4 — itself a
publishable platform contribution.

**Note the changed purpose.** The dongle question is now *decided* (§10): onboard
radio for both. A7 is therefore no longer a decision input but **validation and
tuning** — quantify what the integrated configuration actually delivers, and give
`btc_params` sweeps a metric to optimize. The retired dongle configuration stays
in the matrix as a baseline, and stays documented as a fallback in case the
numbers come back bad.

**Migration back to onboard Wi-Fi** — this is now the forward path, not a
rollback. Per Pi:

1. Remove `dtoverlay=disable-wifi` from `/boot/firmware/config.txt`; reboot.
2. Reconnect `wlan0` to the LAN via `nmcli`; unplug the TP-Link dongle.
3. `iw dev wlan0 set power_save off` (make it persistent — a NetworkManager
   `wifi.powersave=2` connection setting, not a one-shot command).
4. Verify: `iw dev` shows `wlan0` only; `ethtool -i wlan0 | grep driver` shows
   `brcmfmac`; `hciconfig -a` still shows `hci0`.
5. Update IPs in the topology manifest — `wlan0`'s address replaces the dongle's.

README §3 (the dongle procedure) should be rewritten as an appendix: *"if
integrated coexistence proves insufficient, here is the split-radio fallback,"*
with A7's numbers as the reason it wasn't needed. Keep it — it's a real result
about the hardware, and other people on Pi 4s will hit the same question.

Side effect: with only `wlan0` present, known bug §7.2 (non-deterministic
interface selection) stops biting — but still fix it, and pin to `wlan0`
explicitly, since the intent should be in the code rather than in the absence of
a second interface.

---

## 4. Workstream B — Python Migration

**Decision: yes.** Reasons, in order of weight:

1. **Radio access** (A4, A5). Raw HCI sockets, netlink, `pyroute2`, `bumble`,
   `dbus-fast` — all stdlib or one pip install. This alone justifies it.
2. **One language for controller, simulator, and analysis.** The control law
   currently exists twice (`raspberry/algo.js` and `nordic/src/coordination_task.c`)
   and analysis lives separately in `scripts/organize_data.py`. In Python the same
   `Controller.step()` runs on hardware, in the fast-forward simulator, and in the
   notebook that produces the figures.
3. **Ecosystem:** `networkx` (topology generation *and validation* — `net.js`
   notes the average-consensus law assumes strongly connected + balanced, and
   nothing checks it), `numpy`/`scipy`, `python-control`, `cvxpy` for distributed
   optimization agents, `matplotlib` for the SVGs already in `docs/plots/`.
4. **asyncio maps 1:1** onto the current event-driven structure.

**Honest caveat:** Python GC adds jitter. At `dt = 200 ms` this is three orders
of magnitude of headroom — irrelevant. If we later want `dt ≤ 5 ms` on the Pi:
`uvloop`, preallocate arrays, and/or push the fast loop into the nRF (which is
already what the BLE agent does).

### Stack mapping

| Now | Python |
|---|---|
| `express` + `socket.io` | **FastAPI** + `python-socketio` |
| `axios` | raw UDP (preferred, see A3) / `httpx` |
| `serialport` | `pyserial-asyncio` |
| `node-ble` + `bleadv.sh` | raw HCI socket / `bumble` |
| `child_process.fork` IPC | `asyncio` + `multiprocessing`, or separate processes over a unix socket |
| `pm2` / `ecosystem.config.js` | templated `systemd` units (`vertex-agent@ble.service`) |
| `seedrandom` | `numpy.random.default_rng(seed)` |

### Phased plan — never leaves a broken testbed

1. **Config first.** Replace the `TOPOLOGY` block in `net.js` (lines ~178–365,
   plus the large commented-out topology library) with YAML manifests + `pydantic`
   models. Both codebases read the same file. Zero risk.
2. **Port the controller with golden fixtures.** `algo.js` → numpy `Controller`,
   asserted to bit-parity against existing JSON logs, step for step. This is the
   safety net for everything after.
3. **Port the agent** (`edge.js`) with asyncio + a `Transport` ABC. Python and JS
   agents can run in the *same* experiment — they only need to agree on the wire
   format — so migrate one node at a time.
4. **Port the hub** with FastAPI + `python-socketio`. That library is
   protocol-compatible with socket.io v4, so **`raspberry/public/*.html` needs
   zero changes.**
5. **Then** replace `bleadv.sh` with the HCI layer (A5).
6. Firmware stays C. Only the parameter schema is shared.

---

## 5. Workstream C — Platform Architecture

- **C1. Controller plugin interface.** `Controller` ABC:
  `step(t, self_state, neighbor_states) -> output`, discovered by entry point.
  Ship: finite-time adaptive (ours), average consensus, Kuramoto, distributed
  gradient / ADMM, formation control, distributed Kalman. *This is the change
  that turns a project into a platform.*
- **C2. Transport ABC.** `discover()`, `publish(state)`, `fetch(neighbor)`.
  Impls: BLE-adv (HCI), BLE-GATT, UDP, HTTP, MQTT, LoRa, 802.15.4/Thread. The
  `bridge` stops being an `if`-branch at the top of `edge.js` and becomes "an
  agent with two transports" — which also lifts the three-agents-per-node ceiling.
  **Also fixes a confound:** today the Wi-Fi agent does TCP request/response
  while the BLE agent does connectionless broadcast, so the two "virtual agents"
  experience structurally different communication. Unify both as *best-effort
  broadcast at rate f* and the comparison becomes meaningful.
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

---

## 6. Workstream D — Experiment Infrastructure

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

## 7. Known Bugs (found reading the legacy JS)

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

## 8. Target Layout

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

## 8a. Loopback test results (2026-08-20, hardware)

**Direction A — Pi transmit.** 493/500 delivered, **0 byte mismatches**. The Python
encoder, AD element structure, company id and little-endian field order agree with
the C capture byte for byte. Measurement voided by UART saturation, not radio loss:
the peer relayed ~217 reports/s over 115200 (92% of line rate), 96% of them other
devices' beacons, and dropped 269 of its own. Fixable with a peer-side company-id
filter; not on the critical path.

**Direction B — Pi receive, scan-window sweep.**

| duty | delivered | ratio | reports | foreign | ours |
|---|---|---|---|---|---|
| 100% | 452/500 | 90.4% | 17903 | 17203 | 700 |
| 50%  | 328/500 | 65.6% | 13175 | 12692 | 483 |
| 25%  | 182/500 | 36.4% |  6787 |  6521 | 266 |
| 10%  |  73/500 | 14.6% |  2635 |  2537 |  98 |

**The scan window is under our control** -- 75.8 points of movement, the parameter
BlueZ's D-Bus API never exposed (§3 A1). Ratio tracks duty between the one- and
two-chance models, as expected at 100 ms advertising against a 200 ms payload
period. Foreign reports scale with duty independently, confirming the window gates
reception rather than something else in the chain. 0 byte mismatches over 1035
deliveries; 1528 across both directions.

### The consequence for the platform

Delivery varies **6x** with the scan window, and the production firmware hardcodes
it (`BT_GAP_SCAN_FAST_INTERVAL`/`WINDOW` in `observer.c`, and the `BT_LE_ADV_NCONN`
defaults in `broadcaster.c`). So every BLE agent's neighbour visibility -- which
determines whether consensus converges at all -- is set by a Zephyr default nobody
chose and recorded in no run's metadata.

**Radio parameters are therefore a first-class experimental variable, not a
deployment detail.** They belong in the manifest and in `RunMeta.environment`.

---

## 8b. Outstanding for the new design

Items 1-8 of the previous revision are **done**. What follows records what closed
and what is still open. Everything claimed here is checked by
`bash test/check_all.sh`, which runs without hardware.

### Closed

**1. `ble` relay mode** — `vertex/agent/relay.py` plus `AgentService.is_relay`. No
local controller and no Transport for a `ble` agent: the nRF owns the law, the
radio and the timing. `assignment_to_frames()` is the single place engineering
units become scaled int32, and reports are logged `units="scaled_int"` rather than
converted, with `vertex.analysis.units` normalising on read.

**2. The STATE frame** — `[t_us:8][state:4][vstate:4][vartheta:4][counter:4][n:1]`
then per neighbour `[vstate:4][flags:1]`, flags bit0 = enabled, bit1 = fresh.
Scaled int32 throughout. `fresh` is cleared on every report, so it means "heard
since the last report" and per-link delivery ratio for a `ble` agent stays
derivable from its log alone — which matters because the nRF's own advertisements
are still v0 and carry no sequence number.

**3. Binary serial protocol in the firmware** — replaces `serial.c`'s ASCII
`n`/`a`/`p`/`t`: same four jobs, framed with a CRC, no 64-byte split, no 50 ms
inter-command gap. Split three ways, matching the loopback peers:

* `proto.h/.c` — the **envelope** only: SOF, type, length, CRC. Nothing about what
  a payload means, because the envelope is shared by every message on the link.
* `agent.h/.c` — the **payload formats** (documented in the header, beside the
  decoder) and `struct agent`. No Zephyr dependency, so it compiles and runs on
  the host — which is what lets the cross-check drive the real decoder.
* `control.c` — the **dispatcher**: pick a handler, turn its result into an ACK or
  an ERR, answer a PING. The agent arrives through uart_link's `ctx`, so the
  dispatcher holds no state of its own.

RADIO decodes through `agent_parse_radio()` but is applied by whoever owns the
radio — the observer on the nRF, `ble_scan`/`ble_adv` on the peers. One validator,
three effects; previously each of the three had its own partial bounds check.

`test/common/check_proto_layout.py` checks that the host encodes what each
firmware decodes, by length *and* by field offset, across **all three** firmwares.
Both failure modes are demonstrated by reintroducing them: a one-sided
`PROTO_CONTROL_LEN` edit, and an offset that drifts while the length still
matches. Checking only one firmware is how `PROTO_CONTROL_LEN` came to be 5 on
the host and on the nRF while both peers stayed at 1.

**4. The firmware divergences** — closed and now guarded by
`test/crossval/compare.py`, which links the real `agent.c` + `coordination_task.c`
+ `prng.c` against the Python controller and reports the residual. Configuration
enters as encoded frames built by the host's own encoders, so the decoders are on
the checked path too. Currently max 4.5e-5 over 400 steps,
which is the float32 floor. See `docs/FIRMWARE_DIVERGENCE.md` for the measured
size of each old divergence. `rand()` is gone in favour of PCG32, mirrored in
`vertex/pcg32.py`, seeded per node per run from the CONTROL frame.

**5. Direction B harness** — ran. Delivery moved 90.4% -> 14.6% across the scan
window, 75.8 points, which is the parameter BlueZ never exposed. See §8a.

**6. `transports/ble.py`** — `BleTransport`, the `bridge` agent's path. One HCI
user-channel socket carries commands, completions and advertising reports
together, so the transport installs a single reader (`_pump`) and routes command
completions to whoever awaits that opcode. `HciSocket.command()` blocks and
*discards* intervening reports, which is fine during setup and ruinous during a
run: `publish()` issues a command every control period and each one would eat the
reports queued behind it, indistinguishably from radio loss.
`test/transports/check_ble.py` covers AD framing, setup ordering, that pump
behaviour, self-filtering and v0 acceptance, against a datagram socketpair.

**7. Radio parameters in the manifest** — `RadioSpec` in the manifest, carried on
`AgentAssignment.radio`, into `RunMeta.environment["radio"]` with the requested
milliseconds, the programmed 0.625 ms units, the duty cycle, `radio_source`
(`manifest` or `launch-override`) and `applied_on` (`nrf52` / `pi-hci` / `none`).
Recorded for `wifi` agents too, so all three types in one experiment carry the
same environment block.

**8. Firmware, in order** — all five: binary protocol, STATE frame with the
`fresh` bit, settable scan parameters (`observer_set_scan_params`), the two
divergences plus a seeded PRNG, and `update_coordination()`/epsilon removed.

**9. The radio and wiring modules follow.** `common.h` / `broadcaster` /
`observer` / `main` refactored onto `struct agent`, with four defects fixed that
the restructure exposed rather than introduced:

* **The log snapshot was not a snapshot.** `coordination_params` held its
  neighbour arrays as **pointers**, so `memcpy(&log_data_copy, &coordination, ...)`
  copied the pointers: the snapshot aliased live memory and reported vstates could
  change under `report_state()` mid-frame. `struct agent` holds them inline.
* **STATE was reported on the wrong condition.** Only when a queue message arrived
  *and* every neighbour had been heard at least once. So a node with one silent
  neighbour reported nothing for a whole run, and the surviving timestamps were
  event-driven rather than periodic — making "nothing arrived" and "nothing was
  sent" indistinguishable in the very log the delivery ratio comes from. Now every
  `clock` tick, unconditionally; the `fresh` bits already say which links
  delivered, but only if the window closes on schedule.
* **The scan callback wrote the agent with no lock**, from the Bluetooth RX
  thread, while main's two threads used `coordination_mutex` on the same fields.
  The observer is now read-only on the agent: availability travels as a cumulative
  `heard` bitmask in the queue message and `main.c` applies it under the mutex it
  already holds. `report.c`'s `fresh_mask` is `atomic_t` for the same reason —
  marked from one thread, read-and-cleared from another.
* **The advertising interval was unreachable.** `broadcaster.h` defined
  `MIN_ADV_INTERVAL`/`MAX_ADV_INTERVAL` as 1280 ms; both were unused and the code
  ran Zephyr's 100–150 ms default. The RADIO frame's `adv_min`/`adv_max` were
  validated and then dropped on the floor. Now `broadcaster_set_adv_params()`
  stores them and `broadcaster_init()` applies them, so half of item A closes: the
  advertising interval reaches the nRF. `channel_map` still does not.

Also: `custom_data_type` is the manufacturer AD element's value, memcpy'd onto the
air, so its memory layout *is* the wire format — previously unpacked and
unasserted, working only because the field order happens to need no padding on
this target. Now packed, with a size assertion whose diagnostic names the problem,
and `test/crossval/check_v0.py` compiles the real header and hands the real
struct's bytes to the host's `decode_any()`.

### Open

> Item letters here are local to §8b. Bare `A2`/`A3` elsewhere in this document
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

## 8c. Road to a first run: 3 Pis, 9 agents

Three Raspberry Pis, each hosting `ble` + `wifi` + `bridge`. The smallest
configuration that exercises every path the platform compares, and the target that
orders the remaining work.

Everything in §8a/§8b is about the *node*. This section is about the fact that
nothing yet **starts** one.

### Status

**Items 1-5 are done.** `bash test/check_all.sh` now ends with a `fleet end to end`
stage that drives the whole host-side path -- hub, control plane, nine
`AgentService` instances, controllers and relay, logs, fetch, files on disk -- on
one machine with faked radios. Nine of nine nodes, 360 samples, one epoch.

What shipped:

| | |
|---|---|
| `vertex/agent/__main__.py` | `python3 -m vertex.agent --type {ble,wifi,bridge}` |
| `vertex/serial/link.py` | `SerialLink`: port, reader thread, request/reply, **STATE path** |
| `vertex/hub/runner.py` | `ExperimentRunner`: configure, trigger, wait, stop, collect |
| `vertex/hub/__main__.py` | `python3 -m vertex.hub {status,run} <manifest>` |
| `vertex/transports/multi.py` | `MultiTransport`: the bridge's two media |
| `experiments/n9-ring.yaml` | the bring-up manifest |
| `test/hub/check_fleet.py` | the end-to-end check |

Four defects were found doing it, three of which would have cost a bench session:

**A `bridge` had only one medium.** `Agent` holds one `Transport` and the factory
gave `bridge` a `BleTransport`, so a `wifi` and a `ble` agent -- which share no
medium -- had no path between them at all. `make_manifests.py`'s own comment says
bridges "join the BLE and Wi-Fi subnets" and `n30-clusters` gives node 21 both a
BLE and a Wi-Fi neighbour, so the intent was always both. Fixed at the Transport
seam (`MultiTransport`) rather than by teaching `Agent` about lists, so `Agent` is
unchanged. A bridge now transmits every packet twice, which is intended and is not
airtime-comparable with a single-medium agent -- that belongs in the analysis.

**Three of the four n30 manifests declared links that cannot carry a packet.** The
generators walk ids numerically, so `ring`/`line` over 1..30 puts a `ble` agent
next to a `wifi` agent at the 10/11 boundary. The validator now rejects that
outright -- it is an error, not a warning, because the run *looks* healthy: both
agents start, both publish, and the link reports 0% delivery, indistinguishable
from a radio fault. The fix is `BAND_ORDER`, a relabelling that puts five bridges
at each boundary; λ₂ is bit-identical before and after (0.0219, 0.2166), because a
relabelled ring is the same graph. `n30-clusters` was always clean -- its edges
were hand-declared with bridges at the boundaries.

The validator also now warns on **intra-host links**: a local UDP broadcast is
delivered by the kernel and never reaches the radio, and two BLE radios centimetres
apart are not a link under test, so such a link reports ~100% delivery and ~0 delay
and flatters any average it lands in. `n9-ring`'s ordering avoids them entirely,
which is why its edges are written out rather than generated.

**`AgentService.shutdown()` hung whenever a hub was connected.** From Python 3.12
`Server.wait_closed()` waits for every live handler, and the hub holds its
connections open for the whole experiment. So a SIGTERM to an agent mid-run would
have needed a SIGKILL behind it. `ControlServer` now tracks its writers and closes
them, with a bounded drain.

**The epoch had nowhere to come from.** An agent's `WallClock` was fixed at launch,
so nine agents meant nine origins and a one-way delay measuring process launch
order. The epoch is per-run and the hub owns it, so it now travels with `start` --
the same argument as the nRF's seed travelling in CONTROL rather than ALGORITHM --
and lands in `RunMeta.environment.epoch_unix_s`. The check asserts all nine agree.

### Blocking — nothing runs without these

**1. There is no agent process.** `AgentService` is complete and has no caller: no
`__main__.py`, no console script, nothing anywhere constructs it. Needs to pick a
`node_type`, resolve `host_ip` with `resolve_local_ip()`, open the serial port when
the type is `ble`, and serve until signalled.

**2. There is no production serial link.** `vertex/serial/` is codec only —
`proto.py` and nothing else. `BleRelay` needs an object with
`request(type, payload, timeout)`. `test/common/peer.py` has a good one (pyserial,
reader thread, the `_awaiting` gate that fixed the TXAT race) but it is
test-harness code, and it has no STATE path.

**3. STATE frames never reach the relay.** `BleRelay.handle_frame` has **no caller
anywhere**, and `on_state` appears only in the docstring that claims `peer.py`
satisfies the contract — which it does not: `peer.py` routes ACK/ERR/PONG/STATS/
TXAT and ADV_REPORT, and STATE is not among them. A `ble` agent would therefore
configure the nRF, start it, and log **zero samples**.

Worth recording how this survived: the end-to-end check that exercised relay mode
called `handle_frame` directly. The harness supplied the wiring it was meant to be
testing, so the gap was invisible from a passing run. Items 2 and 3 are one change,
because they are one path.

**4. There is no hub.** `ControlClient` is complete and strictly single-node;
nothing instantiates it. The fan-out is small because the pieces exist:
`assignments_for(manifest, run_index)` returns exactly the `configure` payload
keyed by node id, and `CONTROL_PORTS` resolves host and type to a port.
`HUB_PORT = 3000` is declared and unused.

**5. There is no 9-node manifest.** All four in `experiments/` are 30 nodes over
ten hardcoded addresses; `tools/make_manifests.py` pins `HOSTS` and the 1/11/21
band offsets.

### The largest unknown is not on that list

**The coordination firmware has never been through `west build`.**
`test/check_all.sh` is `gcc -fsyntax-only` against stub headers plus host-linked
cross-checks. That catches undeclared identifiers, offset drift and codec
disagreement; it cannot catch a Kconfig conflict, a devicetree problem or a link
error. Items 1-4 are all host-side, so building and flashing one board is the
cheapest de-risking available and it parallelises with all of them.

One Kconfig problem has already been found by reading rather than building:
`firmware/nordic/prj.conf` was missing `CONFIG_UART_0_NRF_HW_ASYNC` and its timer,
which both loopback peers have carried since direction A. Without hardware byte
counting the UARTE driver cannot know how many bytes sit in the DMA buffer before
it fills, so the RX idle timeout never fires and a short frame is never delivered.
Every configuration frame is short: the board would have accepted nothing and
looked like a dead cable. The same file also had `CONFIG_LOG=n`, compiling every
`LOG_INF` in the firmware to nothing, and left `CONFIG_UART_CONSOLE` at its default
of on — writing console text into the binary stream on uart0.

### Deployment facts to know before trying

* The `bridge` agent binds the **HCI user channel exclusively**. BlueZ must be
  stopped and the adapter down (`hciconfig hci0 down`), and the process needs
  `CAP_NET_ADMIN`.
* On each Pi the `bridge` and `wifi` agents share the CYW43455. That is the
  coexistence effect being measured (§3 A2/A3), not a misconfiguration.
* The three agents on a host take distinct control ports (3001/3002/3003) and
  share `STATE_PORT` via `SO_REUSEPORT`. The manifest validator already refuses two
  agents of the same type on one address.

### Wanted, not blocking

* `vertex/analysis/` is `units.py` only — no loaders, metrics or multi-node
  aggregation. The run reader is `runlog.read_run_file`.
* No systemd units and no provisioning. `scripts/radio_check.sh` is single-node
  read-only diagnosis.
* README is two lines and describes no procedure.
* §8b.E: `pyproject.toml` `testpaths` still names two deleted directories, so
  `pytest` collects nothing and says so quietly.

### What is left

Nothing host-side blocks a first run. Remaining, in order:

1. **Bring up the flashed board:** `python3 test/nrf/check_board.py --port
   /dev/ttyACM0`, then again with `--scan`. Deliberately NOT in `check_all.sh`,
   which is hardware-free. It runs in the order things fail: PING (which is the
   test for `CONFIG_UART_0_NRF_HW_ASYNC` -- an empty payload makes the smallest
   frame there is, and without byte counting it never leaves the DMA buffer), then
   a deliberate rejection, then configure/trigger, then the STATE stream, then
   `--scan` reads the board's own advertisements back with the *host* codec. That
   last stage is the only check that puts firmware-encoded v1 on a real radio;
   `check_air_wire.py` proves the two codecs agree, this proves the radio path
   does.
2. **Bring up one Pi**: `python3 -m vertex.agent --type wifi`, then
   `python3 -m vertex.hub status experiments/n9-ring.yaml --only 11`.
3. **Provisioning.** Three Pis × three agents launched by hand is nine terminals;
   templated systemd units (D6) and chrony are what make it repeatable. chrony
   matters more than convenience: without it the shared epoch is shared in name
   only.
4. **Analysis.** `vertex/analysis/` is `units.py`; a collected run currently needs
   `runlog.read_run_file` by hand. Loaders and per-link metrics next.
5. **README.** Two lines, no procedure, and the procedure now exists.
6. §8b.E: `pyproject.toml` `testpaths`.

---

## 9. Idea Inbox

Unsorted ideas go here; promote into a workstream once shaped.

- *(2026-08-18, JI)* Drop the TP-Link USB dongle and use CYW43455 native
  BT/WLAN coexistence instead — datasheet documents lossless simultaneous
  reception via shared LNA + joint AGC. → shaped into §3 A2/A3/A7.
- **Check the router's current WLAN channel.** If 1 or 6 it is colliding with BLE
  adv channel 37 or 38 on every frame — see A3.1. Free fix, unblocked, do first.
- *(add yours here)*

---

## 10. Decision Log

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
| 2026-08-20 | **Firmware integrates in `float`, publishes rounded int32** | Storing the step result back into `int32_t` re-injected the truncation error every period, so the C and Python laws were different dynamical systems. Accumulators are the truth, the int32 fields a mirror. Residual is now the float32 floor, 4.5e-5 over 400 steps |
| 2026-08-20 | **PCG32 in the firmware, replacing `rand()`** | `rand()` was unseeded (unreproducible) *and* implementation-defined (picolibc ≠ glibc), so no seeding would have made the C and Python noise comparable. PCG32 is ~10 lines with a fully specified sequence |
| 2026-08-20 | **The PRNG seed travels in the CONTROL frame** | It is a per-run quantity — the same gains replayed with a new seed is a new run — so it belongs with the trigger, not in ALGORITHM. Node id is the stream selector, so two nodes on one seed still differ |
| 2026-08-20 | **`BleTransport` owns a single reader; `HciSocket.command()` is setup-only** | One user-channel socket carries commands and advertising reports together, and `command()` discards what it reads while waiting. Called once per control period, it would eat reports indistinguishably from radio loss |
| 2026-08-20 | **Radio parameters recorded for every agent type, `wifi` included** | Three types appear in one experiment; a missing environment block makes their logs non-comparable. `applied_on` says where they actually took effect |
| — | Whether the host uses PCG64 or PCG32 by default | **Open.** PCG64 is the better generator; PCG32 is the one the nRF can mirror. Only matters when `ble` and `wifi` agents must share a noise stream. See §8b.C |
| — | Whether `broadcaster.c` moves to the v1 payload | **Open.** v1 carries `seq` and `tx_time_us`, so one-way delay and sequence-based loss become derivable from `ble` advertisements. Invalidates comparison with every run collected so far. See §8b.B | |
