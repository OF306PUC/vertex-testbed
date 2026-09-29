/**
 * @file microgrid_task.h
 * @brief The five-DG microgrid benchmark, on the board.
 *
 * The counterpart is vertex/controllers/{plant,virtual,interface_*,microgrid}.py,
 * and the two are kept transliterable on purpose: the Python evaluates one
 * node in plain doubles for the same reason this evaluates one node in
 * floats, so a reader can hold them side by side the way
 * coordination_task.c and finite_time_adaptive.py already are.
 *
 * Selected by the manifest, never by the board. `agent_params.law` says
 * which of the two families runs, and it defaults to the scalar one.
 *
 * ## The tick, in the order sec:testbed fixes
 *
 *   acquire the measurement and the held packet snapshot
 *   compute the virtual candidate and the effective slope g_i[k]
 *   evaluate the interface on the OLD physical, virtual and adaptive states
 *   saturate and apply
 *   update the emulator
 *   commit the virtual state
 *
 * so the command at sample k never sees a state that sample k produced. The
 * adaptive state is committed inside the interface, after its command is
 * formed, for the same reason.
 *
 * ## Float32
 *
 * Measured against the double-precision model before this was written: the
 * observer alone differs by at most 1.4e-5 relative, and the whole loop by
 * under 7e-6 in state, against a tracking band of 0.45. Single precision is
 * not a constraint on this law at these rates.
 */

#ifndef MICROGRID_TASK_H
#define MICROGRID_TASK_H

#include <stdbool.h>
#include <stdint.h>

#include "agent.h"

/** Watchdog causes, matching WATCHDOG_CAUSES in the Python. */
#define MG_WD_NONE          0u
#define MG_WD_STATE         1u
#define MG_WD_VIRTUAL       2u
#define MG_WD_VARTHETA      3u
#define MG_WD_SATURATION    4u

/** @brief Reset every integrator to the manifest's initial condition. */
void microgrid_reset(struct agent *a);

/**
 * @brief Advance one control period.
 *
 * Reads the held neighbour states from `vars.neighbor_vstates`, which for
 * these laws carries two scaled int32 per neighbour rather than one.
 * Publishes the rounded int32 mirror in `vars.state`/`vstate` so report.c
 * and the broadcaster need not know which law ran.
 */
void microgrid_step(struct agent *a);

/** @brief a_zeta(t): the known time profile of the regressor. */
float mg_a_zeta(uint8_t profile, float t_s);

/** @brief The rating limiter, P^s and Q^s from physical P and Q. */
void mg_smooth_saturation(float p, float q, float p_max, float q_max,
                          float *ps, float *qs);

#endif /* MICROGRID_TASK_H */
