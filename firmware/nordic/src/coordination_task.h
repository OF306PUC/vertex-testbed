/**
 * @file coordination_task.h
 * @brief The finite-time adaptive coordination law (multiple).
 *
 * The counterpart is vertex/controllers/finite_time_adaptive.py.
 */

#ifndef COORDINATION_TASK_H
#define COORDINATION_TASK_H

#include <stdint.h>

#include "agent.h"

#define M_PI 3.14159265358979323846f

/* The fixed-point scale, shared with the wire format, the logs and
 * vertex/numeric.py. */
#define SCALE_FACTOR        1e6f
#define INV_SCALE_FACTOR    1e-6f

float sign(float x);

/** @brief Disturbance value for the current step, in engineering units. */
float disturbance(struct agent *a);

/** @brief Coordination coupling term, summed over enabled neighbours. */
float v_i(const struct agent *a);

/**
 * @brief Advance one control period.
 *
 * Integrates in `vars.*_f` and publishes the rounded int32 mirror in
 * `vars.state`/`vstate`/`vartheta`. Initial conditions and the PRNG seed are
 * latched by the CONTROL frame -- see agent.c.
 */
void discrete_step(struct agent *a);

#endif // COORDINATION_TASK_H
