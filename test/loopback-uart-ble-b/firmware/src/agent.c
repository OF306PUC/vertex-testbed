#include "agent.h"

#include <string.h>

void agent_init(struct agent *a)
{
    memset(a, 0, sizeof(*a));
    a->params.dt    = 200;      /* ms */
    a->params.clock = 1000;     /* ms */
}

int8_t agent_neighbor_index(const struct agent *a, uint8_t node)
{
    for (uint8_t i = 0; i < a->params.n_neighbors; i++) {
        if (a->params.neighbors_id[i] == node) {
            return (int8_t)i;
        }
    }
    return -1;
}

static int apply_network(struct agent *a, const uint8_t *d, uint16_t len)
{
    /* Two mandatory bytes: enabled, node_id.*/
    if (len < PROTO_NETWORK_MIN_LEN) {
        return AGENT_ERR_LEN;
    }
    const uint16_t n = (uint16_t)(len - PROTO_NETWORK_MIN_LEN);
    if (n > AGENT_MAX_NEIGHBORS) {
        return AGENT_ERR_RANGE;
    }
    if (d[1] == 0u) {
        return AGENT_ERR_RANGE;         /* node id 0 is reserved */
    }

    a->params.enabled     = (d[0] == 1u);
    a->params.node_id     = d[1];
    a->params.n_neighbors = (uint8_t)n;

    for (uint16_t i = 0; i < n; i++) {
        a->params.neighbors_id[i] = d[i + PROTO_NETWORK_MIN_LEN];
    }
    /* Clear the tail so a shorter list cannot leave stale ids behind -- those
     * would still match in agent_neighbor_index() and admit a stranger. */
    for (uint16_t i = n; i < AGENT_MAX_NEIGHBORS; i++) {
        a->params.neighbors_id[i] = 0u;
    }
    return AGENT_OK;
}

static int apply_algorithm(struct agent *a, const uint8_t *d, uint16_t len)
{
    if (len != PROTO_ALGORITHM_LEN) {
        return AGENT_ERR_LEN;
    }
    const int32_t dt    = proto_ld_i32(&d[0]);
    const int32_t clock = proto_ld_i32(&d[4]);
    if (dt <= 0 || clock <= 0) {
        return AGENT_ERR_RANGE;         /* a zero period would spin the loop */
    }

    a->params.dt         = dt;
    a->params.clock      = clock;
    a->params.state_0    = proto_ld_i32(&d[8]);
    a->params.vstate_0   = proto_ld_i32(&d[12]);
    a->params.vartheta_0 = proto_ld_i32(&d[16]);
    a->params.counter_0  = proto_ld_i32(&d[20]);
    a->params.alpha      = proto_ld_i32(&d[24]);
    a->params.delta      = proto_ld_i32(&d[28]);
    a->params.eta        = proto_ld_i32(&d[32]);
    return AGENT_OK;
}

static int apply_disturbance(struct agent *a, const uint8_t *d, uint16_t len)
{
    if (len != PROTO_DISTURBANCE_LEN) {
        return AGENT_ERR_LEN;
    }
    const int32_t samples = proto_ld_i32(&d[25]);
    if (samples <= 0) {
        return AGENT_ERR_RANGE;         /* the counter wraps modulo this */
    }

    a->params.disturbance.active          = (d[0] == 1u);
    a->params.disturbance.sine_amplitude  = proto_ld_i32(&d[1]);
    a->params.disturbance.frequency       = proto_ld_i32(&d[5]);
    a->params.disturbance.phase           = proto_ld_i32(&d[9]);
    a->params.disturbance.noise_amplitude = proto_ld_i32(&d[13]);
    a->params.disturbance.noise_offset    = proto_ld_i32(&d[17]);
    a->params.disturbance.beta            = proto_ld_i32(&d[21]);
    a->params.disturbance.samples         = samples;
    return AGENT_OK;
}

static int apply_control(struct agent *a, const uint8_t *d, uint16_t len,
                         int64_t now_us)
{
    if (len != PROTO_CONTROL_LEN) {
        return AGENT_ERR_LEN;
    }

    if (d[0] == 1u) {
        a->params.seed                  = proto_ld_u32(&d[1]);
        a->params.epoch_us = 0u;
        for (unsigned i = 0; i < 6u; i++) {
            a->params.epoch_us |= (uint64_t)d[5 + i] << (8u * i);
        }
        a->params.running               = true;
        a->params.first_time_running    = true;
        a->params.all_neighbors_observed = false;

        for (uint8_t i = 0; i < AGENT_MAX_NEIGHBORS; i++) {
            a->params.available_neighbors[i] = false;
            a->params.neighbors_enabled[i]   = false;
            a->vars.neighbor_vstates[i]      = 0;
        }

        a->vars.state    = a->params.state_0;
        a->vars.vstate   = a->params.vstate_0;
        a->vars.vartheta = a->params.vartheta_0;
        a->vars.counter  = a->params.counter_0;
        a->vars.time_us  = now_us;
    } else {
        a->params.running            = false;
        a->params.first_time_running = false;
    }
    return AGENT_OK;
}

int agent_parse_radio(const uint8_t *payload, uint16_t len,
                      struct radio_params *out)
{
    if (len != PROTO_RADIO_LEN) {
        return AGENT_ERR_LEN;
    }
    const uint16_t adv_min   = proto_ld_u16(&payload[0]);
    const uint16_t adv_max   = proto_ld_u16(&payload[2]);
    const uint16_t scan_int  = proto_ld_u16(&payload[4]);
    const uint16_t scan_win  = proto_ld_u16(&payload[6]);
    const uint8_t  flags     = payload[8];

    if (scan_int == 0u || scan_win > scan_int) {
        return AGENT_ERR_RANGE;
    }
    if (adv_min == 0u || adv_min > adv_max) {
        return AGENT_ERR_RANGE;
    }

    /* Written only after every check, so a rejected frame leaves the caller's
     * previous configuration intact rather than half-updated. */
    out->adv_min       = adv_min;
    out->adv_max       = adv_max;
    out->scan_interval = scan_int;
    out->scan_window   = scan_win;
    out->active_scan   = (flags & AGENT_RADIO_ACTIVE_SCAN) != 0u;
    out->advertising   = (flags & AGENT_RADIO_ADVERTISING) != 0u;
    return AGENT_OK;
}

int agent_apply_frame(struct agent *a, uint8_t type, const uint8_t *payload,
                      uint16_t len, int64_t now_us)
{
    switch (type) {
    case PROTO_T_NETWORK:     return apply_network(a, payload, len);
    case PROTO_T_ALGORITHM:   return apply_algorithm(a, payload, len);
    case PROTO_T_DISTURBANCE: return apply_disturbance(a, payload, len);
    case PROTO_T_CONTROL:     return apply_control(a, payload, len, now_us);
    default:                  return AGENT_ERR_TYPE;
    }
}
