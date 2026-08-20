# Firmware / host divergence

**Status: closed, and now checked.** Both findings below are fixed, and
`test/crossval/compare.py` runs the real firmware sources against the Python
controller on every `bash test/check_all.sh`. Reintroducing either finding makes
that check fail — verified by putting each one back and watching it fail.

## Why this mattered more than it looked

The coordination law exists **twice**:

| Agent type | Where the law runs | Implementation |
|---|---|---|
| `ble` | nRF52-DK microcontroller | `firmware/nordic/src/coordination_task.c` (C, `float`) |
| `wifi`, `bridge` | Raspberry Pi | `vertex/controllers/finite_time_adaptive.py` (Python, `double`) |

Those two were **not** equivalent. And the platform's headline comparison is
*BLE agents versus Wi-Fi agents versus bridge agents* — so any systematic
difference between the two implementations was a confound sitting directly across
the axis being measured. Not a rounding curiosity; an alternative explanation for
a between-transport difference.

---

## 1. The disturbance time base was scaled by 1e-6 instead of 1e-3 — FIXED

`cp->dt` is in **milliseconds** and `inv_scale_factor` is **1e-6**, so
`counter * dt * inv_scale_factor` advanced the sinusoid's time argument 1000x too
slowly. `ble` agents received an almost-constant offset where `wifi` and `bridge`
agents received a 2 Hz oscillation.

Now `counter * dt * 1e-3f`, with the unit written in the comment beside it.

Two related fixes went in at the same time:

* **`frequency` was read unscaled** while the host quantized it like every other
  field, so a requested 2 Hz arrived as 2 000 000 Hz. Found by the cross-check,
  not by reading. Every scaled field now goes through `inv_scale_factor` without
  exception — a struct with some fields scaled and some not is the failure mode
  this codebase keeps paying for.
* **`rand()` is gone.** It was unseeded, so the firmware's noise was the one
  quantity in the experiment that could not be reproduced; and it is
  implementation-defined, so even seeded it would differ between Zephyr's
  picolibc and the host's glibc, making an exact comparison impossible by
  construction. Replaced by PCG32 (`firmware/nordic/src/prng.c`), mirrored
  exactly in `vertex/pcg32.py`. The seed travels in the CONTROL frame — a per-run
  quantity, so it belongs with the trigger — with the node id as the stream
  selector, so two nodes on one seed still draw different streams.

**Measured effect of the old behaviour:** 18.1 units of state error at step 399,
against a state of ~23. Qualitatively different, as predicted.

## 2. The firmware truncated where the host rounds, and integrated in fixed point — FIXED

```c
/* was */ cp->state = (int32_t)(sanitize_f(x + u + nu) * cp->scale_factor);
```

**(a) Truncation vs. rounding.** A C cast truncates toward zero; the host rounds
half toward +infinity. Truncation is a *biased* quantizer — it always moves a
value toward the origin. Now `quantize_f()`, which mirrors
`vertex/numeric.py::round_half_up` including its comparison on the fractional
part rather than the tempting `floorf(v + 0.5f)`.

**(b) Fixed-point vs. floating-point integration — the bigger one.** Storing each
step's result back into `int32_t` carried quantized state from step to step and
re-injected the error every period. `coordination_params` now holds
`state_f`/`vstate_f`/`vartheta_f` as the integrator and the `int32_t` fields as a
*published mirror*, derived on each step and never integrated. `v_i()` reads the
accumulator too — reading the mirror there would have put the quantization error
straight back into the loop it was removed from.

**(c) Single precision.** The firmware still works in `float`. At a state of ~25
the float32 quantum is ~2e-6, the same order as the 1e-6 scale factor, so this is
a precision floor rather than a bug. It is what the residual in the cross-check
measures: **max 4.5e-5 over 400 steps**, growing like accumulated rounding.

**Measured effect of the old behaviour:** 2.18e-2 of state error and 118 LSB of
`vartheta` error at step 399 — the latter being ~59 adaptation increments never
applied.

## 3. Dead code removed

`update_coordination()`, `g_i()`, `consensual_avg_law`, `epsilonON`/`epsilonOFF`,
`active` and `max_of_two_non_negative_f()` are gone. The host removed the
consensus-average law and the epsilon hysteresis band deliberately; leaving the C
half in place is how the two drifted apart the first time. The inverted
hysteresis band documented in the previous version of this file lived only in
`update_coordination()` and went with it.

---

## What the cross-check does and does not prove

`test/crossval/harness.c` links `coordination_task.c` and `prng.c` **unmodified**
against the stub Zephyr headers, so the code under test is what gets flashed, not
a transcription of it. What it establishes:

* The two implementations are the same dynamical system to within float32.
* The disturbance — noise, bias and sinusoid — is identical, from the same PRNG
  stream.

What it does not establish:

* **Timing.** The host's residual says nothing about whether the nRF's `dt` timer
  actually fires at 200 ms under BLE load. That is a hardware measurement.
* **The neighbour path.** The harness feeds fixed neighbour vstates. Whether the
  observer decodes them correctly, and how many arrive, is what the loopback
  tests and the `fresh` bit in the STATE frame are for.
* **That PCG32 is the right generator.** `Disturbance` still defaults to numpy's
  PCG64 for host agents; PCG32 is what a Cortex-M4 can mirror. Runs that need
  `ble` and `wifi` agents on the *same* noise stream must pass
  `uniform=Pcg32(seed, node).uniform`, and which one a run used belongs in
  `RunMeta`. That choice is open — see PLATFORM.md.
