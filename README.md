# VERTEX — Virtual Edge Radio Testbed for EXperimentation

An open testbed for distributed and multi-agent control over real heterogeneous
radio links.

## The question it exists to answer

The same distributed control law runs on every agent. What changes between agents
is the **transport** — BLE advertising, Wi-Fi UDP broadcast, or both at once — so
a difference in what the fleet achieves is attributable to the medium rather than
to the implementation.

Making that attribution honest is most of the work, and most of what this
repository contains.

| Agent type | Where the control law runs | Medium | Privileges |
|---|---|---|---|
| `ble` | on the nRF52840, in C | BLE advertising | serial port (`dialout`) |
| `wifi` | on the Raspberry Pi, in Python | UDP broadcast over the LAN | none |
| `bridge` | on the Raspberry Pi, in Python | both, so the two subnetworks join | `CAP_NET_ADMIN` (HCI) |

## ⚙️ System Overview

<!-- <p align="center">
  <img src="docs/hardware.svg" width="40%">
</p> -->

```
  hub (laptop)  ──control plane, TCP──►  agents on each node
        │                                      │
        │  configure → trigger → collect       │
        ▼                                      ▼
   runs/<name>/                        node = Raspberry Pi + nRF52840-DK
                                       up to 3 agents per node
```

**Python version:** 3.11.2 (≥ 3.11 required)

**Nordic nRF Connect SDK version:** 2.7.0

**Board:** `nrf52840dk/nrf52840`

**Raspberry Pi OS:** Bookworm 

The project includes several coordinated components:

- **Node:**
  A physical hardware unit composed of a Raspberry Pi connected via USB to an
  nRF52-DK board.

- **Edge-device:**
  A logical process running inside a node.
  Each node can host up to **three edge-devices**, one per communication type:
  - `ble` → BLE process (advertises and listens)
  - `wifi` → Wi-Fi process (UDP broadcaster)
  - `bridge` → bridge process (links BLE and Wi-Fi subnetworks)

  All three at once is the interesting case: it is where the CYW43455's BLE and
  Wi-Fi coexistence behaviour actually appears. Each type listens on its own
  control port — `ble` 3001, `wifi` 3002, `bridge` 3003 — so they can be
  addressed and restarted independently. State broadcasts share UDP port 3010.

- **Router:**
  Provides the local Wi-Fi LAN for inter-node communication.

- **Hub server:**
  Runs on a laptop and is responsible for configuring network parameters,
  broadcasting experiment triggers, and coordinating all edge-devices. It also
  decides the single epoch the whole fleet timestamps against, and collects each
  node's log when the run ends.

- **User Interface (UI):**
  Not built yet. Runs are driven from the command line and read back with
  `tools/plot_run.py` and `tools/compare_runs.py`.

## What the platform guarantees

These are the properties that make a between-transport comparison meaningful.
Each is enforced by a check, not by intention:

- **One law, two implementations, proven equivalent.** The C law on the nRF and
  the Python law on the Pi are cross-validated against each other on every
  `test/check_all.sh` run, and agree to float32 precision.
- **One clock.** The hub sends a single epoch with the trigger and every clock
  holder is rebound to it, so samples from different nodes share an origin.
- **Matched airtime.** Both radios emit a byte-identical advertisement — same
  elements, same length — so neither arm of the comparison pays for airtime the
  other does not.
- **Per-link delivery, measured.** Sequence numbers travel in every packet and
  reach the log, so delivery, loss, duplicates and reordering are derivable per
  link, including links terminating at an nRF.

---

## 🚀 Implementation Steps

### 0. Check it without hardware first

```bash
pip install -e .
bash test/check_all.sh          # every check that needs no board and no radio
python3 tools/simulate.py       # every manifest, simulated, ~1 s each
```

`check_all.sh` covers firmware syntax and symbols, host/firmware frame layouts
for all three firmwares, the on-air codec in both directions, the C law against
the Python law, the BLE transport against a fake controller, HCI socket reuse,
the manifest generator, and the whole hub → agents → logs → collect path on
loopback. It ends in `all checks passed` or names what failed.

Worth doing after **any** edit to a frame layout or the control law: the two
checks that catch a one-sided change — `serial layout` and `control law C vs
Python` — cost seconds here and cost a bench session otherwise.

---

### 1. Flash the nRF52-DK firmware

1. Build and flash the firmware located in
   `firmware/nordic`.
   Use **Nordic SDK 2.7.0**.
   ```bash
   west build -b nrf52840dk/nrf52840 firmware/nordic
   west flash
   ```
2. Refer to the [Nordic DevAcademy courses](https://academy.nordicsemi.com/) for
   instructions on installing toolchains and flashing via nRF Connect or
   `nrfutil`.

What this firmware does: it runs the control law on the board itself. One thread
steps the law and absorbs neighbour packets every `dt`; the other reports one
sample to the Pi every `dt` and advertises the new value every `clock`. Those
three rates are deliberately separate — matching what a Pi agent does, so the two
agent classes are not compared at different resolutions.

**Logs come out over RTT, not UART.** `uart0` carries the binary protocol, and
console text interleaved with frames corrupts them — the CRC then rejects frames
that were never damaged on the wire, which reads as a link fault. Use
`JLinkRTTLogger`, `nrfjprog --rtt` or `west attach`.

Verify a freshly flashed board before wiring it into a fleet:

```bash
python3 test/nrf/check_board.py
```

It pings, checks the board *rejects* a malformed frame, configures and triggers a
run, collects STATE reports, and confirms something is on the air. It also fails
if the control law starts more than 100 ms after the trigger, which is the
regression it exists to catch.

---

### 2. Prepare the Raspberry Pi devices

1. Flash each SD card with **Raspbian Bookworm OS** and enable the **SSH server**.
   Default credentials:
   ```bash
   username: ---
   password: ---
   ```
2. Install required packages:
   ```bash
   sudo apt-get update
   sudo apt-get upgrade
   sudo apt install chrony
   sudo apt-get install -y libopenblas0
   ```
   `libopenblas0` is numpy's BLAS backend; `chrony` is what makes timestamps from
   different nodes comparable at all.
3. Configure chrony NTP server:
   ```bash
   sudo nano /etc/chrony/chrony.conf
   ```
   And add your specific clocking server. Then confirm it actually reached a
   source — the reference ID must not be `00000000`:
   ```bash
   chronyc tracking
   ```
   This is worth checking on every host, every time. The hub hands out one epoch,
   but each node still stamps arrivals with its **own** clock, so an unsynchronised
   host produces one-way delays that are really the difference between two nodes'
   clocks — a number in the right units and wildly wrong. Offsets of −10 s and
   +20 s have been measured this way, and nothing in the data says the delay is
   fictitious.
4. Verify and note the `wlan0` IP address:
   ```bash
   ip -br a
   ```
5. Reboot

**Keep the hosts identical.** 

---

### 3. Configure the experimental environment

1. To avoid BlueZ taking control of the BLE host controller interface (hci), turn
   it down
   ```bash
   sudo systemctl disable --now bluetooth   # or
   sudo hciconfig hci0 down
   ```
   and run
   ```bash
   sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f $(command -v python3))
   ```
   The HCI **user channel** is exclusive: exactly one process may hold `hci0`, and
   BlueZ counts. The `readlink -f` matters too — a virtualenv's `bin/python3` is a
   symlink, and file capabilities only live on regular files, so without it the
   capability silently lands on nothing.

   Only the `bridge` agent needs this; `ble` needs the serial port and `wifi`
   needs nothing. Elevating all three would be simpler and wrong — it makes every
   run log root-owned and hands the radio to processes that never touch it.
2. Attach or connect the nordic device to one of the raspberry usb ports and
   identify it:
   ```bash
   ls -l /dev/ttyACM*
   ```
3. Give current user permissions to access to serial ports
   ```bash
   sudo usermod -aG dialout $USER #  agent's serial port; needs a re-login
   ```
4. To avoid sleep cycles (unstable network behaviour) for the wlan WiFi
   controller, set `power_save off`
   ```bash
   iw dev wlan0 get power_save      # want it off
   sudo iw dev wlan0 set power_save off
   ```
   This one is first-order, not hygiene: a sleeping station's buffered frames wait
   for the next beacon, which turns a ~1 ms link into a ~170 ms one. Both the
   setting and the interface's channel are recorded with each run, because neither
   is reconstructable from the data afterwards. It does **not** survive a reboot
   unless you make it persistent.
5. Clone the repository
   ```bash
   git clone <repo-url>
   cd vertex-testbed
   ```
6. Create virtual environment
   ```bash
   python3 -m venv myvenv
   source myvenv/bin/activate
   ```
7. Install system packages
   ```bash
   pip install -e .
   ```
   This is what brings in `pyserial` — a `ble` agent cannot reach its nRF without
   it, and the import is lazy, so a missing install shows up only when that one
   agent type starts.
8. Change in `scripts/agents.sh` the current serial port (`/dev/ttyACM*`), or set
   it per invocation without editing the script:
   ```bash
   export VERTEX_SERIAL=/dev/ttyACM0
   ```
9. run preflight
   ```bash
   bash scripts/agents.sh preflight
   ```
   Four sections, each one a thing that has silently invalidated a run: **host**
   (interface, address, broadcast address as the *kernel* reports it — a derived
   /24 on a /22 network sends every datagram to an ordinary host address, and
   nothing complains), **python** (which interpreter, which venv, and whether its
   optional extension modules are all present), **clock** (chrony's reference and
   offset — "the shared epoch is only shared if these are"), and **radio/serial**
   per agent type.

   Everything after this assumes it passed. Run it on every host before you commit
   to a long run.

---

### 4. Start the agents on each node

```bash
bash scripts/agents.sh start
bash scripts/agents.sh status
bash scripts/agents.sh logs bridge     # follow one of them
```

Three processes per node, one per type, each with a pidfile and a log. They come
up, open their control port, and **wait** — no run has started, and no epoch
exists yet. Starting the agents is not starting an experiment, which is why they
can sit idle between runs.

The script deliberately does not pass an epoch. The epoch is per-run and the hub
sends it with the trigger, so every node in a run shares one origin; an epoch
fixed at launch would give each agent its own.

---

### 5. Trigger the run from the hub

```bash
python3 -m vertex.hub status experiments/n6-ring.yaml       # are all nodes reachable
python3 -m vertex.hub run experiments/n6-ring.yaml --duration 120
```

`status` first: it is cheap and it tells you which node is not answering before a
120-second run finds out for you.

The hub configures every node **before** triggering any of them. A run where node
7 was configured and node 8 was not is not a shorter run, it is a different
experiment — so configuration is a barrier, and if any node refuses, nothing
starts. Then one trigger goes out carrying the shared epoch.

Useful flags:

| Flag | What it does |
|---|---|
| `--duration S` | run length in seconds |
| `--only 1,2,21` | bring up a subset of node ids |
| `--run-index N` | selects the initial-condition substream; a different index is a different run of the same experiment |
| `--settle S` | seconds between configuring and triggering |
| `--repeat N` | N runs of the same configuration (see step 7) |

Each node's log is collected into `runs/<manifest>-<run-index>/` as two files per
node: the rows (`.bin`, `.csv` or `.jsonl` — the suffix says how to read it back)
and a `.meta.json` carrying the manifest, the controller parameters, the
interpreter, the radio and Wi-Fi state, and the per-link delivery and delay
counters.

---

### 6. Read the results

```bash
python3 tools/plot_run.py runs/n6-ring-0
```

Writes into `results/<run>/`. Panels are coloured **by agent type**, not by node,
because the question is BLE versus Wi-Fi versus bridge: a systematic split shows
up as three bands rather than six unrelated lines. Everything is plotted against
the host clock on the shared epoch, never against the nRF's own clock, which
counts from its own trigger arrival and is not comparable between nodes.

Four things to look at before believing a run:

- **First timestamp near zero** for every node. A first sample at 85 s means a
  clock holder kept its launch-time origin instead of the run's.
- **Trigger spread**, printed by the hub. Tens of milliseconds is normal; hundreds
  means a node was slow to start stepping.
- **Median against minimum delay** per link. The minimum is the propagation floor;
  a large gap between them is queueing, not a slow link. A *negative* minimum is
  residual clock skew, and means chrony is not doing its job.
- **Delivery ratio** per link, from the `.meta.json` counters where they exist.
  The row-derived figure is a lower bound — the rows sample the last-known
  sequence number at the control rate, so two arrivals inside one interval look
  like one.

---

### 7. A measurement, rather than a single run

Run-to-run spread on this platform is not small — BLE delivery has ranged over 6
points across nominally identical runs while UDP moved 2 — so a single run cannot
resolve a small effect. Repeat a configuration and aggregate:

```bash
python3 -m vertex.hub run experiments/n6-ring.yaml --duration 120 --repeat 10
python3 tools/compare_runs.py 'runs/n6-ring-0-r*'
```

`--repeat` holds the run index fixed, so initial conditions and every node's
disturbance stream are identical across the set and the network realisation is
the only variable. Varying initial conditions is a separate dimension: several
`--run-index` values, each with its own set of repeats, gives the two-factor
design. `compare_runs.py` checks the runs really are replicates before it
aggregates, and reports mean ± sd with n — the sd being the thing that says
whether a difference between two configurations means anything.

---

### 8. Stopping

```bash
bash scripts/agents.sh stop
```

It waits for each process to actually exit before dropping its pidfile — a
survivor still holds `hci0` exclusively, and an untracked holder is what makes
`hciconfig hci0 down` fail with `EBUSY`. If that happens:

```bash
bash scripts/agents.sh hci        # says who holds the adapter
```

`EBUSY` almost never means the adapter is wedged. It means someone holds the user
channel, and the holder has to exit first.

---

## Repository map

| Path | What |
|---|---|
| `vertex/` | the platform: `agent`, `control`, `hub`, `transports`, `serial`, `wire`, `topology`, `analysis`, `sim` |
| `firmware/nordic/` | coordination firmware — the control law in C, on the nRF |
| `test/` | hardware-free checks (`check_all.sh`), plus the two UART/BLE loopback benches |
| `experiments/` | generated manifests — edit the generator, not these |
| `tools/` | manifest generation, plotting, run comparison, simulation |
| `scripts/` | agent supervision and bench diagnostics |
| `deploy/` | systemd units, for when more than one host makes `agents.sh` insufficient |

A manifest declares who the agents are, which host each runs on, who neighbours
whom, and the controller and radio parameters. They are **generated**: host
addresses live in `tools/make_manifests.py`, never in a generated file, because
regenerating would silently revert a hand edit.

```bash
python3 tools/make_manifests.py --hosts 10.6.5.2,10.6.5.4
```

Note that this writes every manifest on every invocation.

## Diagnostics

Each of these answers one question and changes nothing:

| Command | Question |
|---|---|
| `bash scripts/agents.sh preflight` | is this host fit to run? |
| `bash scripts/agents.sh hci` | who holds `hci0`, and can it be taken? |
| `bash scripts/host_report.sh` | what differs between these two Pis? (`diff` two of them) |
| `python3 scripts/udp_check.py --interface wlan0` | does UDP broadcast actually cross between hosts? |
| `python3 scripts/adv_floor.py` | what is this controller's real advertising floor? |
| `python3 scripts/check_interpreter.py` | is this interpreter complete, and does it match the others? |
| `python3 test/nrf/check_board.py` | does the attached board configure, run and report? |
| `bash scripts/radio_check.sh` | read-only radio state for one node |

`udp_check.py` is the one to reach for when a Wi-Fi link delivers nothing: run it
on both Pis at once, and a pass means the medium works and the fault is in the
platform, while a fail means the network is not carrying broadcast and no amount
of agent debugging will help.

## Constraints worth knowing before designing a run

- **The advertising interval is a delivery ceiling.** Publishing faster than the
  radio advertises overwrites values before they are radiated, which is
  indistinguishable from packet loss downstream. The manifest validator warns;
  the hub refuses without `--force`.
- **The CYW43455's advertising floor is 100 ms**, not the 20 ms Bluetooth 5.0
  allows — it enforces the 4.x rule and rejects anything faster outright. So a
  `bridge` cannot publish faster than 10 Hz without capping its own delivery, and
  above that rate the ceiling has to be reported alongside the result.
- **The publish period must be an integer multiple of `dt_s`.** The nRF derives
  its publish interval by integer division; a Pi agent sleeps the period exactly.
  A non-multiple puts the two agent classes on different rates.
- **`ble` and `wifi` agents share no medium**, so an edge between them cannot
  carry a packet however well the graph validates. Bridges are what join the two
  subnetworks; the validator rejects the rest.
- **A BLE agent's neighbour count is capped by the firmware.** Ring topologies of
  degree 4 sit exactly at that limit, which is why the denser manifests put
  bridges at every `ble`/`wifi` boundary.
