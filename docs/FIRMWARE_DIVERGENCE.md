# Firmware / host divergence

**Status:** unverified on hardware. Found by reading `nordic/src/` while porting
the control law to Python. Both items below are code-reading findings; confirm by
measurement before acting on them.

## Why this matters more than it looks

The coordination law has always existed **twice**:

| Agent type | Where the law runs | Implementation |
|---|---|---|
| `ble` | nRF52-DK microcontroller | `nordic/src/coordination_task.c` (C, `float`) |
| `wifi`, `bridge` | Raspberry Pi | previously `raspberry/algo.js` (JS, `double`) |

Those two are **not** equivalent. And the platform's headline comparison is
*BLE agents versus Wi-Fi agents versus bridge agents* — so any systematic
difference between the two implementations is a confound sitting directly across
the axis being measured. It is not a rounding curiosity; it is an alternative
explanation for a between-transport difference.

This is a second confound on the same axis as the transport asymmetry (broadcast
BLE vs. request/response Wi-Fi) already recorded in `PLATFORM.md` C2.

---

## 1. The disturbance time base is scaled by 1e-6 instead of 1e-3

`coordination_task.c`, in `disturbance()`:

```c
float t = (float)cp->disturbance.counter * (float)cp->dt * cp->inv_scale_factor;
                                        // ^ comment says "dt must be scaled to seconds"
```

`cp->dt` is in **milliseconds** and `inv_scale_factor` is **1e-6**. Converting
milliseconds to seconds needs `1e-3`. The same function 60 lines below gets it
right:

```c
float dt = (float)(cp->dt) * 1e-3f;     // discrete_step(), correct
```

**Effect.** The sinusoidal component's time argument advances 1000x too slowly, so
its effective frequency is divided by 1000. At `dt = 200 ms`,
`period_samples = 1000` and `sine_frequency_hz = 2`:

| | time swept over one counter cycle | sine cycles completed |
|---|---|---|
| Host implementation | 1000 x 0.2 s = 200 s | ~400 |
| Firmware | 1000 x 0.0002 s = 0.2 s | ~0.4 |

So `ble` agents receive an almost-constant offset where `wifi` and `bridge` agents
receive a 2 Hz oscillation. **The disturbance is qualitatively different, not
slightly different.** The constant (`beta`) and uniform-noise components are
unaffected; only the sinusoid is.

The uniform component also differs in kind: the firmware draws from `rand()`,
which is unseeded here, so the firmware's noise is not reproducible at all.

## 2. The firmware truncates where the host rounds, and integrates in fixed point

Two separate issues, in the same lines:

```c
cp->state  = (int32_t)(sanitize_f(x + u + nu) * cp->scale_factor);
cp->vstate = (int32_t)(sanitize_f(z + gi)     * cp->scale_factor);
```

**(a) Truncation vs. rounding.** A C cast from float to integer truncates toward
zero. The host implementation rounds half away from zero. Truncation is a *biased*
quantizer — it always moves a value toward the origin — where rounding is
symmetric. Up to 1 LSB (1e-6) per step, always in the same direction.

**(b) Fixed-point vs. floating-point integration — the bigger one.** The firmware
stores each step's result back into `int32_t`, so its integrator carries
**quantized** state from step to step and re-injects the truncation error on every
iteration. The host integrator keeps full precision internally and quantizes only
on output. Over a 1600 s run at `dt = 200 ms` that is 8000 accumulations of a
one-directional error, so the two are not the same dynamical system.

**(c) Single precision.** The firmware works in `float`. At a state of ~25 the
float32 quantum is ~2e-6 — the same order as the 1e-6 scale factor itself. The
representation is therefore at the edge of its resolution for exactly the state
magnitudes in use. `vartheta` avoids this by accumulating in integers
(`cp->vartheta += eta_dvtheta`), which is fine; `state` and `vstate` do not.

---

## Recommended order of work

1. **Measure before fixing.** Run one topology with the disturbance disabled
   entirely (`enabled: false`). That removes finding 1 and the unseeded `rand()`
   from the picture, so any remaining BLE-vs-Wi-Fi difference is attributable to
   finding 2 and to the transports themselves. This is cheap and it is the
   cleanest available separation of the two confounds.
2. **Fix finding 1** (`1e-6` -> `1e-3`). One character, but it changes the
   disturbance every BLE agent has ever experienced, so previously collected runs
   are not comparable with runs after the fix. Bump the manifest `seed` or record
   a firmware version alongside the data.
3. **Decide finding 2 deliberately.** Options, in increasing order of effort:
   round instead of truncate; keep a separate full-precision accumulator and
   quantize only for transmission; or move to `int64`/Q-format fixed point
   throughout. The middle option matches the host implementation most closely and
   is the smallest change that removes the per-step error re-injection.
4. **Seed the firmware's `rand()`** per node per run, so its noise is reproducible
   the way the host's now is.
5. **Then** treat `vertex` as the specification and cross-validate the firmware
   against it, rather than treating either previous implementation as ground truth.

Until step 5 is done, `validation/` in the Python port proves agreement with the
*host* implementation only. It says nothing about the BLE path — see that
directory's README.
