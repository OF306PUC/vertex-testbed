/**
 * @file agent.h
 * @brief Agent state and configuration decoding.
 *
 * All scaled quantities are int32 in units of 1e-6, matching the platform's
 * fixed-point convention (`vertex/numeric.py`). Fields arrive little-endian.
 */

#ifndef AGENT_H_
#define AGENT_H_

#include <stdbool.h>
#include <stdint.h>

#include "proto.h"

#define AGENT_MAX_NEIGHBORS     PROTO_MAX_NEIGHBORS

/* Errors returned by agent_apply_frame */
#define AGENT_OK                0
#define AGENT_ERR_LEN          (-1)     /* payload length wrong for the type */
#define AGENT_ERR_RANGE        (-2)     /* a field is out of range */
#define AGENT_ERR_TYPE         (-3)     /* not a configuration frame */

struct disturbance_params {
    bool    active;
    int32_t sine_amplitude;     /* M        */
    int32_t frequency;
    int32_t phase;
    int32_t noise_amplitude;    /* O        */
    int32_t noise_offset;       /* O_offset */
    int32_t beta;
    int32_t samples;
};

struct agent_params {
    bool    enabled;
    bool    running;
    bool    first_time_running;
    bool    all_neighbors_observed;

    uint8_t node_id;
    uint8_t n_neighbors;
    uint8_t neighbors_id[AGENT_MAX_NEIGHBORS];
    
    bool    available_neighbors[AGENT_MAX_NEIGHBORS];
    bool    neighbors_enabled[AGENT_MAX_NEIGHBORS];

    int32_t dt;                 /* ms */
    int32_t clock;              /* ms */
    int32_t state_0;
    int32_t vstate_0;
    int32_t vartheta_0;
    int32_t counter_0;
    int32_t alpha;
    int32_t delta;
    int32_t eta;

    struct disturbance_params disturbance;
};

struct agent_vars {
    int32_t state;
    int32_t vstate;
    int32_t vartheta;
    int32_t counter;
    int64_t time_us;
    int32_t neighbor_vstates[AGENT_MAX_NEIGHBORS];
};

struct agent {
    struct agent_params params;
    struct agent_vars   vars;
};

void agent_init(struct agent *a);

/**
 * @brief Apply one configuration frame.
 *
 * @param now_us Current time, injected so this unit stays host-testable. Only
 *               read when a control frame starts a run.
 * @return AGENT_OK, or a negative AGENT_ERR_*. The caller reports the failure to
 *         the host; a rejected frame must never be partially applied, which is
 *         why every length check precedes the first assignment.
 */
int agent_apply_frame(struct agent *a, uint8_t type, const uint8_t *payload,
                      uint16_t len, int64_t now_us);

/** @brief Index of @p node in the neighbour list, or -1. */
int8_t agent_neighbor_index(const struct agent *a, uint8_t node);

#endif /* AGENT_H_ */
