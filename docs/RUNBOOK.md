# VERTEX runbook

Everything needed to get a run off the ground on real hardware, and the traps that
cost time the first time round. Design rationale is in `PLATFORM.md`; the dated
record of what each run showed is in `JOURNAL.md`.

---

## Bringing up a run

### 8d-0. `n4-noble`: two hosts, no nRF

`experiments/n4-noble.yaml`. Below n6-ring, and worth running first regardless of
whether the second nRF is ready.

`wifi` + `bridge` only, so **no nRF, no firmware, no serial link**. 4-cycle,
lambda_2 = 2.0:

```
11(wifi,h0) - 12(wifi,h1) - 21(bri,h0) - 22(bri,h1) - back to 11
```

Two reasons it is not merely a fallback:

* It is the **cleanest comparison the platform can make**. `bridge` and `wifi` run
  the *same* controller in the same process, so a difference between them is the
  medium and nothing else -- no firmware, no second language, no clock transfer.
* It is the **only manifest that exercises bridge-to-bridge BLE**. n6-ring's forced
  cycle leaves the 21-22 edge out, so Pi-to-Pi BLE via HCI is untested there.

It also halves the number of things that can be wrong on a first two-host run: no
`--serial`, no `dialout`, no flashed board, and the `ble` agent -- the one whose
reporting path had no caller at all until recently -- is absent.

Rehearsed host-side: 4/4 nodes, 160 samples, one epoch.

### 8d. Runbook: two hosts, six agents

`experiments/n6-ring.yaml`. The step between one board and the full nine.

**The topology is forced, not chosen.** Every edge must cross hosts (an intra-host
link never reaches the radio) and must not join `ble` to `wifi` (no shared medium).
That leaves seven legal edges among six agents and **exactly one** Hamiltonian
cycle through them:

```
1(ble,h0) - 2(ble,h1) - 21(bri,h0) - 12(wifi,h1) - 11(wifi,h0) - 22(bri,h1) - back to 1
```

lambda_2 = 1.0, degree 2 everywhere. Every path the platform compares appears once:

| edge | path exercised |
|---|---|
| 1-2 | nRF to nRF over BLE |
| 2-21, 22-1 | nRF advertises, a Pi's HCI scanner receives -- the direction loopback B validated |
| 11-12 | UDP broadcast between hosts |
| 21-22 *(unused)* | the seventh legal edge; the only densification available |

Note 21-22 is *not* in the cycle, so bridge-to-bridge over BLE is the one path this
manifest does not cover. Add that edge to get it, at the cost of degree 3 on the
bridges.

### Sequence

**On each Pi**, once:

```
bash scripts/agents.sh preflight     # do not skip this
sudo hciconfig hci0 down             # the bridge needs the HCI user channel, exclusively
bash scripts/agents.sh start
bash scripts/agents.sh status
```

`preflight` checks the four things that actually stop a run: the interface has an
address, **chrony is tracking** (the shared epoch is only shared if the clocks
are), the nRF's serial port exists and is writable, and `hci0` is DOWN with
CAP_NET_ADMIN available. It is a checklist rather than a stack trace on purpose.

`scripts/agents.sh` does **not** pass `--epoch`. The epoch is per-run and the hub
sends it with the trigger; one fixed at launch would give each agent its own origin.

**On the hub:**

```
python3 -m vertex.hub status experiments/n6-ring.yaml
python3 -m vertex.hub run    experiments/n6-ring.yaml --duration 120
```

`status` first, always. Finding a Pi that did not come up costs seconds there and a
whole run otherwise.

### What to look for in the result

* **`enabled=[True]` on some neighbour.** The first thing two boards buy that one
  cannot. With one board every `enabled` was False, so the coupling term was
  identically zero and `vstate` could not move. If it is still all False here, the
  agents are running but nothing is being heard.
* **`vstate` actually moving.** Same reason. `state` moved on one board because the
  disturbance integrates alone; `vstate` moves only under coupling.
* **`fresh` bits varying.** All-True means every link delivered in every window,
  which at this range is plausible and means loss is not yet measurable. All-False
  with `enabled=True` means stale values are being reused -- worth chasing.
* **`trigger spread`** from the hub, and **`device_timestamp - timestamp`** per
  `ble` node. Together they bound how much of any early transient is real.
* **The `bridge` agents will hear their own host's nRF** -- inches away, and not a
  declared neighbour of theirs in this manifest. Those land in the observer's
  `unknown_node` counter, which is the filtering working, not a fault.

Host-side rehearsed with `python3 test/hub/check_fleet.py experiments/n6-ring.yaml`:
6/6 nodes, 240 samples, one epoch.

---

### Deployment facts to know before trying

* The `bridge` agent binds the **HCI user channel exclusively**. BlueZ must be
  stopped and the adapter down (`hciconfig hci0 down`), and the process needs
  `CAP_NET_ADMIN`.
* On each Pi the `bridge` and `wifi` agents share the CYW43455. That is the
  coexistence effect being measured (PLATFORM.md §3 A2/A3), not a misconfiguration.
* The three agents on a host take distinct control ports (3001/3002/3003) and
  share `STATE_PORT` via `SO_REUSEPORT`. The manifest validator already refuses two
  agents of the same type on one address.

### Restarting agents, and stale pidfiles

`scripts/agents.sh start` is idempotent -- it skips what is already alive, so after
a partial failure it brings up only the dead ones. `restart` stops and starts
everything.

`status` now distinguishes three states, because "DOWN" conflated two that need
different responses:

```
wifi     up   pid 3062441  wifi agent on 127.0.0.1, control port 3002
wifi     DIED      <last log line -- the reason>
wifi     stopped   shutting down
```

`DIED` means a pidfile exists but the process does not: it started and exited, and
the last log line is the reason. `stopped` means it was never started or was
stopped cleanly.

Liveness also verifies the pid is *ours*. A dead agent leaves a stale pidfile and
Linux recycles pids, so `/proc/<pid>` existing is not enough -- a recycled pid would
make `start` skip a dead agent and `status` report it up. It now also checks
`/proc/<pid>/cmdline` contains `vertex.agent` and the agent type.

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
6. PLATFORM.md §8.1: `pyproject.toml` `testpaths`.

---

## Recovering a stuck Bluetooth adapter

`cannot take hci0 on the user channel ([Errno 16] Device or resource busy)` means
the adapter is up under BlueZ, or a previous process still holds the user channel.

**`hciconfig hci0 down` will not fix it, and cannot.** A user-channel socket owns
the device exclusively, so the kernel refuses `hciconfig` while it is held — that
*is* the EBUSY. The holder has to exit first. Ask who it is:

```bash
bash scripts/agents.sh hci        # adapter state + who is holding it
```

Then, in this order:

```bash
bash scripts/agents.sh stop       # waits for exit, escalates to KILL if needed
sudo hciconfig hci0 down          # only now, and only if still needed
bash scripts/agents.sh start
```

If `agents.sh` reports nothing running but EBUSY persists, the holder is an
untracked orphan — `pgrep -af 'vertex\.agent'`, then `sudo pkill -f 'vertex\.agent'`.
That used to be reachable through `stop` itself: it removed the pidfile without
waiting for the process to die, so anything slow to exit stopped being tracked
while still holding `hci0`. Fixed 2026-08-24; `stop` now confirms exit before
dropping the pidfile, and `restart` no longer races a flat one-second sleep.

Since 2026-08-24 each agent process opens **one** HCI socket and keeps it, so this
should no longer accumulate across a sweep. If it recurs, check for a second agent
process on the same host holding `hci0` -- only one process per host may own the
user channel.

## Privileges and capabilities

### Two hosts or ten: which privilege route

Three ways to give the bridge its capability, and they do not scale the same way.

| | grant lands on | survives reboot | logs | per-host cost |
|---|---|---|---|---|
| `sudo -E bash scripts/agents.sh` | the whole process tree | no | files in /tmp, root-owned | none |
| `setcap` on python3 | **every python3 on the host** | until the next `apt upgrade` of python3 | files, user-owned | one command |
| **systemd template** (`deploy/`) | **one unit** | yes | journald, rotated | one env file |

`setcap` is the tempting middle option and it is the one that does not scale: it
grants `CAP_NET_ADMIN` to every python3 process the machine will ever run,
including a shell one-liner. It is also silently cleared when the interpreter is
replaced by a package upgrade, and it sets `AT_SECURE`, so the dynamic loader
starts ignoring `LD_*` -- worth knowing before debugging a venv.

`deploy/vertex-agent@.service` is the extensible answer. The capability lives in
`vertex-agent@bridge.service.d/capabilities.conf` and applies to **that instance
only**, which is the thing neither shell route can express:

```
[Service]
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
Conflicts=bluetooth.service
ExecStartPre=/usr/bin/hciconfig hci0 down
```

`ble` and `wifi` keep `CapabilityBoundingSet=` empty, so they cannot acquire it at
all. `Conflicts=bluetooth.service` encodes the exclusivity of the user channel --
bluetoothd holding hci0 is the EBUSY that reads as a missing adapter -- and
`After=chrony-wait.service` encodes the thing that caught pi2: an agent started
before the clock is synchronised stamps its early samples on an origin nobody
shares. Neither dependency is expressible in a shell launcher; both are one line
here.

**Sequencing.** For the two-host run, use the script -- it sudo's the bridge and
nothing else, and it is already working. Install the units when going past two
hosts, which is where enabling agents by hand stops being reasonable:

```
sudo bash deploy/install.sh          # then edit /etc/default/vertex-agent
sudo systemctl enable --now vertex-agent@{ble,wifi,bridge}
```

One trap worth recording, caught by `systemd-analyze verify` rather than on the
bench: **only `ExecStart=` expands `${VAR}` from an `EnvironmentFile`.** `User=`,
`WorkingDirectory=` and `ReadWritePaths=` take literals, and a unit that references
a variable there fails to start with "path is not absolute". `install.sh` generates
those three into a drop-in from the env file, and runs `systemd-analyze verify` on
all three instances before it finishes.

### Privileges: only the bridge needs them

Of the three agents, `bridge` alone needs elevation -- it binds the **HCI user
channel**, which is exclusive and root-only. `ble` needs the serial port
(`dialout` group) and `wifi` needs nothing beyond the LAN.

`scripts/agents.sh` therefore elevates the bridge and nothing else. Running all
three under `sudo` would be simpler and wrong: every run log becomes root-owned,
and two processes that never touch the radio get the capability anyway.

Three ways to satisfy it, in order of preference:

```
# 1. capability on the interpreter -- one-time, then everything runs unprivileged
sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f $(command -v python3))

# 2. nothing: the script sudo's the bridge by itself, if sudo is available
bash scripts/agents.sh start

# 3. everything as root -- works, leaves root-owned logs
sudo -E bash scripts/agents.sh start
```

`preflight` reports which route will actually be used rather than failing on the
absence of any one of them.

Two details this forced, both of which look like bugs when they bite:

* `kill -0` cannot test a root-owned process from an unprivileged shell -- it
  returns EPERM, indistinguishable from "gone", so `status` reported a running
  bridge as DOWN. The liveness test reads `/proc/<pid>` instead.
* Stopping a sudo'd agent signals `sudo`, which forwards TERM to its child. The
  stop path tries a plain `kill` first and falls back to `sudo kill`.

### File capabilities and venvs: the capability is not where preflight said

pi1's preflight reported

```
privileges  cap_net_admin on /home/plant123/.../.venv/bin/python3
```

which is not where the capability is. A default venv's `bin/python3` is a
**symlink**, and file capabilities attach only to regular files -- so `setcap` on it
resolved through and landed on the **base interpreter**, shared by everything that
uses it. `getcap` follows the link, so the check reported success against a
different file than the one it printed.

```
/tmp/vc/bin/python3 -> /usr/bin/python3        (islink=True)
readlink -f          -> /usr/bin/python3.12    <- where setcap actually applied
```

Two consequences, and the first one bites on the very next step:

* **Recreating the venv loses it.** The capability stays on the old source-built
  binary; the new venv points somewhere else, and the bridge fails with EPERM that
  reads as a missing adapter.
* **It was never scoped to the venv.** Every process using that base interpreter
  had CAP_NET_ADMIN, which is the same objection as `setcap` on the system python
  -- the thing the systemd drop-in exists to avoid.

The fix, if staying with the shell launcher, is `--copies`, which makes the venv's
interpreter a real file that can carry its own capability:

```
/usr/bin/python3 -m venv --copies --clear .venv
.venv/bin/pip install -e .
sudo setcap cap_net_admin,cap_net_raw+eip .venv/bin/python3
```

Preflight now names the file the capability is on, and says explicitly when that is
not the interpreter being used.

Untested claim, flagged as such: a file capability sets `AT_SECURE`, and whether
that interacts badly with a venv's `.pth`-based editable install has not been
checked here -- no root available. If `--copies` plus `setcap` misbehaves, use
`sudo -E bash scripts/agents.sh start` or the systemd units, where
`AmbientCapabilities` grants the capability to the process rather than to a file
and the question does not arise.

### Three layers, and only one of them is per-run

Easy to collapse these into one checklist and then repeat setup steps that did not
need repeating -- or worse, skip a per-boot step because it looks like setup.

**Layer 1 -- once per Pi, survives reboots.** Image setup.

```
# Bookworm or newer: vertex needs python >= 3.11 (enum.StrEnum). Bullseye has 3.9.
sudo apt install chrony
#   ... point /etc/chrony/chrony.conf at a REACHABLE source, then verify the
#   reference id is not 00000000 -- see "The chrony trap" above.
sudo usermod -aG dialout $USER        # the ble agent's serial port; needs a re-login
sudo systemctl disable --now bluetooth  # else bluetoothd re-claims hci0 every boot

# ONE of these, or neither if you let the script sudo the bridge:
sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f $(command -v python3))
sudo bash deploy/install.sh            # the scaling answer; see below
```

Disabling `bluetooth.service` is the one people miss. `hciconfig hci0 down` is
undone by the next reboot, so without it the bridge fails on every cold start with
an EBUSY that reads as a missing adapter.

**Layer 2 -- once per boot, on each Pi.** Start the agents; they are long-lived.

```
bash scripts/agents.sh preflight       # a check; it starts nothing
bash scripts/agents.sh start
```

**Layer 3 -- per run, on the hub only.** Nothing touches the Pis.

```
python3 -m vertex.hub status experiments/n6-ring.yaml
python3 -m vertex.hub run    experiments/n6-ring.yaml --duration 120 --run-index 0
```

The agents survive consecutive runs: the hub re-configures, triggers, stops and
collects each time, and each run gets its own epoch. Verified for three runs back
to back against one set of agents, 6/6 nodes each time. If an agent had to be
restarted between runs, "per run" would mean six process launches across two hosts;
it does not.

**`--run-index` is the knob that makes a run different**, not `--run-name`:

| | |
|---|---|
| same `--run-index` | identical initial conditions -- a *repeat* of the same run |
| new `--run-index` | a new draw from the manifest seed -- another *sample* |
| `--run-name` | the label and output directory, nothing more |

So `--run-index 0..9` is ten samples of one experiment, each exactly reproducible.
Re-running index 0 after a firmware change is the comparison to make.

## Python environments on the Pis

### Virtual environments: three separate traps

Hit in that order on the first venv launch.

**1. The deps were simply not installed.** All three agents died with
`ModuleNotFoundError: No module named 'numpy'`. `numpy` is a declared dependency,
reached through `vertex.controllers.disturbance`, and a fresh venv has none of the
four. `pip install -e .` fixes it.

Why `test/nrf/check_board.py` had worked in the *same* venv: it imports only
`vertex.serial`, `vertex.numeric`, `vertex.clock`, `vertex.radio` and `vertex.wire`,
none of which touch numpy. So the board test is not evidence the environment can
run an agent.

Preflight checked the Python *version* but not that the imports resolve. It now
runs `import vertex.agent.__main__`, which is what `start` will do, and prints the
interpreter path plus a warning when `$VIRTUAL_ENV` is set but the interpreter is
outside it.

**2. `sudo -E` does not preserve the PATH lookup.** Debian's sudoers sets
`secure_path`, which overrides PATH when resolving the command -- so a bare
`python3` under sudo finds `/usr/bin/python3` while the two unprivileged agents
find the venv's. The bridge would have run a *different interpreter with different
packages* from its siblings, silently. `scripts/agents.sh` now resolves the
interpreter to an absolute path once, up front.

**3. A systemd unit inherits no shell, so a venv is invisible to it** -- and
neither obvious workaround works:

* `ExecStart=${VERTEX_PYTHON} ...` fails to start. systemd expands `${VAR}` in
  ExecStart's *arguments* but not in the executable position:
  `Command ${VERTEX_PYTHON} is not executable`.
* A stable symlink to the venv's interpreter **loses the venv**. Invoking
  `/usr/local/bin/vertex-python -> .venv/bin/python3` reports `sys.prefix=/usr` and
  imports a different numpy entirely. Measured:

```
direct : /tmp/vtest        <- venv detected
symlink: /usr              <- venv lost
```

  Venv detection reads `pyvenv.cfg` relative to the interpreter's own location, and
  the symlink is not in the venv.

So `install.sh` generates the whole `ExecStart` into the host drop-in from
`VERTEX_PYTHON`, alongside the other three directives that cannot reference
variables. The template deliberately has no `ExecStart` -- one definition, one
place -- which makes it a fragment, so verify it through `install.sh` rather than
alone. `install.sh` also refuses to install if the chosen interpreter cannot
`import vertex.agent`, rather than leaving a unit that fails at start with a
traceback in the journal.

All three of these were caught by tooling rather than on the bench:
`systemd-analyze verify` for the first ExecStart mistake, and an actual venv for the
symlink claim I was about to ship.

### `pip install -e .` is not needed, and on an old pip it cannot work

```
ERROR: File "setup.py" not found. Directory cannot be installed in editable mode
(A "pyproject.toml" file was found, but editable mode currently requires a setup.py based build.)
```

That is the pre-21.3 pip message: an editable install from `pyproject.toml` alone
needs **PEP 660**, which arrived in pip 21.3. Bullseye bundles 20.3.4; Bookworm
bundles 23.0.1.

But the editable install was never required. **`vertex` is imported from the repo,
not installed:** `scripts/agents.sh` cds to the repo root, the systemd unit sets
`WorkingDirectory`, and `python -m` puts the working directory on `sys.path`.
Verified in a venv containing the four dependencies and nothing else -- `pip list`
shows no `vertex`, and `python3 -m vertex.agent` runs.

So install only the dependencies:

```
.venv/bin/pip install numpy pydantic networkx pyyaml
```

Preflight now says this rather than `pip install -e .`, and derives the list from
`pyproject.toml` so it cannot drift.

**The pip version is also a diagnostic.** pip 20.3.4 is bundled with python **3.9**,
not 3.11 -- so seeing that error from a venv built with `/usr/bin/python3` says
`/usr/bin/python3` is 3.9, i.e. the host is on Bullseye. In that case:

* the venv just created cannot run vertex at all (`requires-python >= 3.11`), and
* the source-built 3.11.8 was a reasonable workaround rather than a mistake.

The choice is then between upgrading that host to Bookworm -- which also makes it
match the other Pi, the point of the whole exercise -- and keeping the source build
with the full dev-library set. Check `/usr/bin/python3 --version` before deciding;
everything else follows from it.

### pyserial was never declared, and preflight could not see it

`hub status` on the first two-host attempt:

```
FAIL   1 10.6.5.4:3001  cannot connect ([Errno 111] Connect call failed)
FAIL   2 10.6.5.2:3001  cannot connect ([Errno 111] Connect call failed)
ok    11 10.6.5.4:3002  type=wifi    ...
ok    21 10.6.5.4:3003  type=bridge  ...
```

Both `ble` agents dead, both `wifi` and `bridge` up, on both hosts. ECONNREFUSED
means nothing is listening -- the process exited at startup.

**`pyserial` was missing from `pyproject.toml`.** A `ble` agent relays to an nRF
over a serial port and cannot start without it; the other two never touch it. So
the dependency list -- and the install instructions derived from it -- were quietly
incomplete in exactly the way that lets two thirds of a fleet come up.

Three defects, one cause:

* **Undeclared.** Now in `dependencies`, with a comment on why "not imported on
  every path" is not the same as "not needed".
* **`import serial` sat outside the `try`** in `SerialLink.open()`, so a missing
  module escaped as a bare `ModuleNotFoundError` traceback -- and
  `vertex/agent/__main__.py` only catches `LinkError`. It now raises `LinkError`
  with the install command.
* **Preflight could not catch it.** Its dependency check imports
  `vertex.agent.__main__`, which reaches `SerialLink` but not the lazy
  `import serial` inside `open()`. It now checks `import serial` under the `ble`
  section, where it is actually needed, and prints the version.

The general shape, worth remembering: a lazily-imported dependency is invisible to
an import-the-entrypoint check. Anything imported inside a function needs its own
probe at the point of use.

### Updating an editable install after a dependency is added

`pip install -e .` does **not** pick up a new dependency retroactively. pip resolves
dependencies at install time, so an editable install performed before `pyserial`
was declared did not install it and will not notice.

Re-running `pip install -e .` does re-resolve -- but only against the
`pyproject.toml` present on that machine. So the order matters:

```
1. sync the repo to the Pi        # pyproject.toml must already contain pyserial
2. .venv/bin/pip install -e .
```

Reversed, step 2 is a no-op and the `ble` agent still dies with the same error.
`pip install pyserial` works too and is independent of how `vertex` was installed;
re-running `-e .` is preferable only because it keeps the declared set
authoritative and picks up any later additions.

### Rebuilding the interpreter, if that is the route

CPython's configure step skips any optional extension whose dev library is absent,
prints the list once, and **succeeds**. `_bz2` was only the first module that
happened to be reached; `_lzma` is needed by the same import and `_ctypes` by
`vertex/radio/hci.py` for `sockaddr_hci`, so a partial rebuild trades one late
failure for another.

Install the full set before configuring -- this is CPython's own devguide list:

```
sudo apt install build-essential pkg-config \
    libbz2-dev libffi-dev libgdbm-dev libgdbm-compat-dev liblzma-dev \
    libncurses-dev libreadline-dev libsqlite3-dev libssl-dev \
    tk-dev uuid-dev zlib1g-dev
```

Then verify positively rather than reading the build log:

```
/path/to/new/python3 scripts/check_interpreter.py
```

It imports every optional extension, names the Debian package behind each missing
one, and prints a digest of the module set. **Installing a dev package after the
fact does nothing** -- extensions are selected at configure time, so the build has
to be redone.

Rebuilding also invalidates two things downstream:

* **The venv** points at the replaced binary. Recreate it (`--copies --clear`).
* **The file capability** is cleared when the file is replaced. Re-apply `setcap`,
  and see the section above for why `--copies` matters when doing so.

**Compare the digest across hosts.** That was the substantive concern behind
recommending the distro interpreter, and it survives the choice to rebuild -- it is
just now something to check rather than something guaranteed:

```
pi1$ .venv/bin/python3 scripts/check_interpreter.py --fingerprint
pi2$ .venv/bin/python3 scripts/check_interpreter.py --fingerprint
```

Identical output means the interpreters match. A difference is a difference between
two machines whose comparison is the experiment, and it does not appear anywhere in
the collected data -- which is why `RunMeta.environment` now carries the version and
build, and why preflight prints the digest on a clean run.

### A source-built interpreter, and why the hosts should match

pi1 failed preflight with `ModuleNotFoundError: No module named '_bz2'` while pi2
passed. Not a missing package -- `_bz2` is a **CPython stdlib C extension**, and no
`pip install` can supply it. The interpreter was built from source without
`libbz2-dev` present, and CPython skips optional modules whose dev library is
missing **silently**: the build prints them in a list and succeeds.

`networkx` is what needs it, for its compressed-graph readers:

```
numpy     pulls in: -
pydantic  pulls in: -
networkx  pulls in: ['_bz2', '_lzma', 'bz2', 'lzma']
yaml      pulls in: -
```

So `_lzma` is almost certainly missing on that host too, and quite possibly
`_sqlite3` and `readline`. `_ssl` evidently is not, since pip reached PyPI.

Preflight's advice was wrong here -- it said `pip install -e .` for any
ImportError. It now classifies the two cases, because they point in opposite
directions: a missing package is a pip problem, a missing `_`-prefixed stdlib
module means the interpreter itself is incomplete and names the dev library that
was absent.

**The recommendation is to use the distro interpreter on both Pis**, not to rebuild
pi1's with libbz2-dev. Bookworm ships 3.11.2, which satisfies `requires-python`:

```
/usr/bin/python3 -m venv --clear .venv
.venv/bin/pip install -e .
```

The reason is not convenience. pi1 was on 3.11.8 and pi2 on 3.11.2, from different
builds with different module sets -- a difference between hosts in an experiment
whose entire purpose is comparing hosts. It would not change the control law
(float arithmetic is identical), but it is needless variance in the one place the
platform is trying to isolate.

Which is also why `RunMeta.environment` now records the interpreter with every
run -- version, build, implementation, executable path and platform. This
difference existed for some time and nothing in the collected data would have shown
it.

### The hosts are on different OS releases, and that is the bigger problem

`/usr/bin/python3 --version` returning 3.9 on pi1 settles it: that host is on
**Bullseye**, while pi2's 3.11.2 is **Bookworm**. Confirm with:

```
cat /etc/os-release          # VERSION_CODENAME=bullseye | bookworm
cat /etc/debian_version      # 11.x = bullseye, 12.x = bookworm
uname -r
```

The python version is the symptom that surfaced first, not the difference that
matters most. Two OS releases apart means a different **kernel**, a different
**BlueZ**, and -- the one that goes straight to the experiment -- possibly
different **CYW43455 firmware**. PLATFORM.md §3 A2/A3 is entirely about BLE/Wi-Fi contention on
that chip. A firmware difference between the two Pis is a difference in the thing
being measured, and it appears in no log.

`scripts/host_report.sh` prints everything that can differ, one `key: value`
per line, for diffing:

```
diff <(ssh pi1 'bash -s' < scripts/host_report.sh) \
     <(ssh pi2 'bash -s' < scripts/host_report.sh)
```

It covers the OS release and kernel, every interpreter on the box plus the module
digest, the **md5 of each radio firmware blob** (`brcmfmac43455-sdio.bin`, its
`clm_blob`, and `BCM4345C0.hcd` for the Bluetooth side), the Pi firmware version,
the BlueZ package version, whether bluetoothd is running, and the chrony reference
and WLAN channel/power-save. Firmware blobs are hashed from disk rather than read
out of `dmesg`, which is often root-only and says nothing after a reboot.

**Recommended: bring pi1 to Bookworm.** It fixes the python question as a side
effect, retires the source build, and -- the actual reason -- makes the two hosts
comparable, which is the premise of every result the testbed produces. A source-built
3.11 on Bullseye leaves the kernel, BlueZ and radio firmware still mismatched.

Note that some of this is already captured with the data: `RunMeta.environment`
records `platform.platform()`, whose glibc component distinguishes the releases
(Bullseye 2.31, Bookworm 2.36). Radio firmware is not, and should be -- see A2.

## Host and clock configuration

### The chrony trap, found on the first two-host preflight

pi2 reported:

```
chrony offset          0.000000005 seconds fast of NTP time (ref 00000000 ())
```

Five nanoseconds. It looks like the best-synchronised machine in the building. It
is synchronised to **nothing**: `Reference ID 00000000` means chrony has no source,
so the offset it reports is against its own local clock and is necessarily ~0.
pi1, on the same LAN, was 1.157 ms off a real source (`ref 2DAA6404`).

So the two hosts were not aligned with each other, the shared epoch would have been
shared in name only, and every cross-node delay would have been measuring their
clock offset. **The original preflight passed this**, because it only checked that
`chronyc tracking` produced output. It now fails on `ref 00000000` explicitly, with
the reason, because a number that reads perfect for the wrong reason is worse than
no number.

Worth stating the general shape: an unsynchronised chrony does not report an error,
it reports agreement with itself. Anything that checks "is the offset small" will
pass it.

Fix on the affected host, then re-run preflight:

```
chronyc sources -v          # is there a reachable source at all?
sudo chronyc makestep       # step now rather than slewing over minutes
```

If the Pis are on an isolated LAN with no route to an NTP server, one of them has
to serve time to the other (`local stratum 10` plus an `allow` line in
`chrony.conf`) -- and then the *served* host is the reference, so its own accuracy
is what bounds every delay measurement in the experiment.

### Host addresses belong in the generator, not in the generated file

The two boards were `10.6.5.4` and `10.6.5.2`, and `n6-ring.yaml` had been edited
by hand to match -- a file whose own header says "edit the generator, not this",
and which the next `make_manifests.py` run would have silently reverted to
`HOSTS[:2]`.

`tools/make_manifests.py` now takes `--hosts` (or `VERTEX_HOSTS`):

```
python3 tools/make_manifests.py --hosts 10.6.5.4,10.6.5.2
```

which reproduces that hand-edited file byte for byte, and skips the manifests that
need more hosts than were declared rather than aborting:

```
hosts: 10.6.5.4, 10.6.5.2
skip     n9-ring                  needs 3 hosts, 2 declared
ok       n4-noble.yaml            nodes=4 edges=8 lambda2=2.0
ok       n6-ring.yaml             nodes=6 edges=12 lambda2=1.0
```

Addresses are the one part of a manifest that is not derivable, and the part that
changes when a Pi is re-imaged or swapped. That makes them an argument, not an
edit.
