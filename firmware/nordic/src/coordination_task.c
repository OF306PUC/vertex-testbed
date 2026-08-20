#include <math.h>

#include "coordination_task.h"
#include "prng.h"

float disturbance(struct agent *a)
{
    const struct disturbance_params *d = &a->params.disturbance;
    if (!d->active) {
        return 0.0f;
    }
    const float amp   = (float)d->noise_amplitude * INV_SCALE_FACTOR;
    const float off   = (float)d->noise_offset    * INV_SCALE_FACTOR;
    const float beta  = (float)d->beta            * INV_SCALE_FACTOR;
    const float A     = (float)d->sine_amplitude  * INV_SCALE_FACTOR;
    const float f     = (float)d->frequency       * INV_SCALE_FACTOR;   /* Hz */
    const float phi_s = (float)d->phase           * INV_SCALE_FACTOR;   /* s  */
    const float t = (float)a->vars.counter * (float)a->params.dt * 1e-3f;

    const float noise      = amp * (prng_uniform() - off);
    const float sinusoidal = A * sinf(2.0f * M_PI * f * (t - phi_s));
    return noise + beta + sinusoidal;
}

float sign(float x)
{
    if (x > 0.0f) {
        return 1.0f;
    } else if (x < 0.0f) {
        return -1.0f;
    } else {
        return 0.0f;
    }
}

/**
 * Coordination coupling term.
 *
 */
float v_i(const struct agent *a)
{
    const float z = a->vars.vstate_f;
    float vi = 0.0f;
    for (uint8_t j = 0; j < a->params.n_neighbors; j++) {
        if (a->params.neighbors_enabled[j]) {
            const float diff =
                z - (float)a->vars.neighbor_vstates[j] * INV_SCALE_FACTOR;
            /* sign(0) == 0, so a neighbour already in agreement contributes
             * nothing -- the same convention as vertex/numeric.py::sign. */
            vi += -1.0f * sign(diff) * sqrtf(fabsf(diff));
        }
    }
    return vi;
}

static inline float sanitize_f(float v) { return isfinite(v) ? v : 0.0f; }

/**
 * Engineering units -> scaled int32, rounding half toward +infinity.
 *
 * Matches vertex/numeric.py::round_half_up exactly.
 */
static int32_t quantize_f(float v)
{
    if (!isfinite(v)) {
        return 0;
    }
    const float scaled = v * SCALE_FACTOR;
    if (scaled >= 2147483520.0f)  { return INT32_MAX; }
    if (scaled <= -2147483520.0f) { return INT32_MIN; }
    const float f = floorf(scaled);
    return (int32_t)(((scaled - f) >= 0.5f) ? f + 1.0f : f);
}

void discrete_step(struct agent *a)
{
    const float dt       = (float)a->params.dt * 1e-3f;
    const float x        = sanitize_f(a->vars.state_f);
    const float z        = sanitize_f(a->vars.vstate_f);
    const float vartheta = sanitize_f(a->vars.vartheta_f);

    const float eta   = (float)a->params.eta   * INV_SCALE_FACTOR;
    const float alpha = (float)a->params.alpha * INV_SCALE_FACTOR;
    const float delta = (float)a->params.delta * INV_SCALE_FACTOR;

    const float nu    = disturbance(a) * dt;
    const float sigma = x - z;
    const float grad  = sign(sigma);

    const float gi = alpha * v_i(a);

    const float u       = gi - vartheta * grad;
    const float dvtheta = (fabsf(sigma) > delta) ? 1.0f : 0.0f;

    a->vars.state_f    = sanitize_f(x + u + nu);
    a->vars.vstate_f   = sanitize_f(z + gi);
    a->vars.vartheta_f = sanitize_f(vartheta + eta * dvtheta);

    a->vars.state    = quantize_f(a->vars.state_f);
    a->vars.vstate   = quantize_f(a->vars.vstate_f);
    a->vars.vartheta = quantize_f(a->vars.vartheta_f);

    a->vars.counter = (a->vars.counter + 1) % a->params.disturbance.samples;
}
