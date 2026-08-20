/**
 * @file agent.h
 * @brief Agent state and configuration decoding.
 *
 * All scaled quantities are int32 in units of 1e-6, matching the platform's
 * fixed-point convention (`vertex/numeric.py`). Fields arrive little-endian.
 *
 * ## Payload formats
 *
 * `proto.h` owns the *envelope* -- SOF, type, length, CRC -- which is identical
 * for every message on the link.
 *
 *     NETWORK  'N'=0x4E  >= 2 bytes, variable
 *       [enabled:1][node_id:1][neighbor_id:1 x n]
 *       enabled is 1 or 0; node_id 0 is reserved. n may be 0.
 *
 *     ALGORITHM  'A'=0x41  36 bytes
 *       [dt_ms:4][clock_ms:4][state_0:4][vstate_0:4][vartheta_0:4]
 *       [counter_0:4][alpha:4][delta:4][eta:4]
 *       dt is the control period, clock the communication update period. 
 *
 *     DISTURBANCE  'D'=0x44  29 bytes
 *       [active:1][sine_amplitude:4][frequency:4][phase:4]
 *       [noise_amplitude:4][noise_offset:4][beta:4][samples:4]
 *       nu(t) = noise_amplitude*(U - noise_offset) + beta
 *               + sine_amplitude*sin(2*pi*frequency*(t - phase))
 *       noise_offset shifts the uniform draw, so 0.5 centres the noise on zero;
 *       it is not added to the output. samples must be > 0 -- the step counter
 *       wraps modulo it, so it sets the disturbance's repeat period.
 *
 *     CONTROL  'S'=0x53  11 bytes
 *       [run:1][seed:4][epoch_us:6]
 *       run=1 latches initial conditions, seeds the PRNG and starts the run;
 *       run=0 stops it. `seed` is a uint32.
 *
 *       `epoch_us` is the host's own clock reading, in microseconds since the
 *       experiment epoch, taken as it built this frame. 
 *
 *     RADIO  'R'=0x52  9 bytes
 *       [adv_min:2][adv_max:2][scan_int:2][scan_win:2][flags:1], 0.625 ms units
 *       flags bit0 = active scan (else passive), bit1 = advertising enabled.
 *       Requires scan_int != 0 and scan_win <= scan_int.
 *
 * The reply direction is documented where it is built: see `report.h` for STATE.
 */

#ifndef AGENT_H_
#define AGENT_H_

#include <stdbool.h>
#include <stdint.h>

#include "common.h"
#include "proto.h"


#define AGENT_MAX_NEIGHBORS     N_MAX_NEIGHBORS

#if AGENT_MAX_NEIGHBORS > PROTO_MAX_NEIGHBORS
#error "AGENT_MAX_NEIGHBORS exceeds what a NETWORK frame can carry"
#endif

/* Errors returned by agent_apply_frame */
#define AGENT_OK                0
#define AGENT_ERR_LEN          (-1)     /* payload length wrong for the type */
#define AGENT_ERR_RANGE        (-2)     /* a field is out of range */
#define AGENT_ERR_TYPE         (-3)     /* not a configuration frame */

/* RADIO flag bits, as documented above. */
#define AGENT_RADIO_ACTIVE_SCAN 0x01u
#define AGENT_RADIO_ADVERTISING 0x02u

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

    int32_t  dt;                /* ms */
    int32_t  clock;             /* ms */
    int32_t  state_0;
    int32_t  vstate_0;
    int32_t  vartheta_0;
    int32_t  counter_0;
    int32_t  alpha;
    int32_t  delta;
    int32_t  eta;
    uint32_t seed;              /* per-node, per-run PRNG seed, from CONTROL */
    uint64_t epoch_us;          /* host's epoch reading at trigger, from CONTROL */

    struct disturbance_params disturbance;
};

struct agent_vars {
    /* Published state, scaled int32. Derived from the accumulators below on every
     * step; read by report.c and the broadcaster. Never integrated. */
    int32_t state;
    int32_t vstate;
    int32_t vartheta;
    int32_t counter;
    int64_t time_us;            /* run start, local uptime, MICROSECONDS */
    uint16_t tx_seq;
    int32_t neighbor_vstates[AGENT_MAX_NEIGHBORS];

    /* Full-precision integrator. */
    float state_f;
    float vstate_f;
    float vartheta_f;
};

struct agent {
    struct agent_params params;
    struct agent_vars   vars;
};

/** @brief Radio configuration decoded from a RADIO frame, in 0.625 ms units. */
struct radio_params {
    uint16_t adv_min;
    uint16_t adv_max;
    uint16_t scan_interval;
    uint16_t scan_window;
    bool     active_scan;
    bool     advertising;
};

void agent_init(struct agent *a);

/**
 * @brief Apply one configuration frame.
 *
 * Handles NETWORK, ALGORITHM, DISTURBANCE and CONTROL -- the frames that change
 * agent state. RADIO is deliberately not one of them: it configures the radio,
 * not the agent, so it decodes through agent_parse_radio() and is applied by
 * whoever owns the radio.
 *
 * @param now_us Current time, injected so this unit stays host-testable. Only
 *               read when a control frame starts a run.
 * @return AGENT_OK, or a negative AGENT_ERR_*. The caller reports the failure to
 *         the host; a rejected frame must never be partially applied, which is
 *         why every length check precedes the first assignment.
 */
int agent_apply_frame(struct agent *a, uint8_t type, const uint8_t *payload,
                      uint16_t len, int64_t now_us);

/**
 * @brief Decode and validate a RADIO payload.
 *
 * Split from applying it so the bounds check sits beside the other frame
 * decoders and stays host-testable, while the effect -- restarting the scanner --
 * stays in the module that owns the radio.
 *
 * @return AGENT_OK, or AGENT_ERR_LEN / AGENT_ERR_RANGE. @p out is untouched on
 *         failure, so a rejected frame cannot half-apply.
 */
int agent_parse_radio(const uint8_t *payload, uint16_t len,
                      struct radio_params *out);

/** @brief Index of @p node in the neighbour list, or -1. */
int8_t agent_neighbor_index(const struct agent *a, uint8_t node);

#endif /* AGENT_H_ */
