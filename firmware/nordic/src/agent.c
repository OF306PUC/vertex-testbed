/**
 * @file agent.c
 * @brief Configuration frames -> struct agent. Payload formats live in agent.h.
 */

#include "agent.h"
#include "microgrid_task.h"

#include <string.h>

#include "prng.h"

void agent_init(struct agent *a)
{
    memset(a, 0, sizeof(*a));
    a->params.dt    = 200;      /* ms */
    a->params.clock = 1000;     /* ms */
    a->params.disturbance.samples = 1000;
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
    /* Two mandatory bytes: enabled, node_id. */
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

    /* Every configuration sequence re-declares the law. A MICROGRID frame
     * later in the same sequence upgrades it; nothing else can. */
    a->params.law = LAW_FINITE_TIME_ADAPTIVE;
    /* And the blocks that would let it be re-selected. Clearing the law but
     * keeping the bits would let a lone MG_IFACE frame commit a law over a
     * plant and an observer belonging to the previous run. */
    a->params.mg_blocks = 0u;
    a->params.enabled     = (d[0] == 1u);
    a->params.node_id     = d[1];
    a->params.n_neighbors = (uint8_t)n;

    for (uint16_t i = 0; i < n; i++) {
        a->params.neighbors_id[i] = d[i + PROTO_NETWORK_MIN_LEN];
    }
    /* Clear the tail so a shorter list cannot leave stale ids behind */
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
        return AGENT_ERR_RANGE;         /* a zero period would spin the timer */
    }

    a->params.dt         = dt;
    a->params.clock      = clock;
    a->params.state_0    = proto_ld_i32(&d[8]);
    a->params.vstate_0   = proto_ld_i32(&d[12]);
    a->params.vartheta_0 = proto_ld_i32(&d[16]);
    a->params.counter_0  = proto_ld_i32(&d[20]);
    a->params.gain_ij    = proto_ld_i32(&d[24]);
    a->params.delta      = proto_ld_i32(&d[28]);
    a->params.eta        = proto_ld_i32(&d[32]);
    a->params.alpha      = proto_ld_i32(&d[36]);
    return AGENT_OK;
}

/**
 * @brief The microgrid parameters, and the law that uses them.
 *
 * Receiving this frame is what selects the second control family. There is
 * no other path to it: `apply_network` resets the law, so every
 * configuration sequence declares it afresh and a stale selection cannot
 * survive a reconfigure. A host that does not know about this type leaves
 * every board on the scalar law, which is the behaviour that predates it.
 */
/*
 * The microgrid configuration arrives as three frames, one per block, and is
 * committed by the last of them.
 *
 * Splitting it was forced by the transport: one frame was 121 bytes and the
 * board's USB CDC link carries 64. Keeping it committed-as-a-set is not
 * forced, it is chosen -- `law` is the field that takes the board out of the
 * scalar family, and a board running a law whose plant or observer never
 * arrived would integrate garbage while reporting normally.
 */
static int apply_mg_plant(struct agent *a, const uint8_t *d, uint16_t len)
{
    if (len != PROTO_MG_PLANT_LEN) {
        return AGENT_ERR_LEN;
    }
    struct microgrid_params *m = &a->params.microgrid;
    m->profile = d[0];
    m->kappa = proto_ld_i32(&d[1]);
    m->S1    = proto_ld_i32(&d[5]);
    m->S2    = proto_ld_i32(&d[9]);
    m->p_max = proto_ld_i32(&d[13]);
    m->q_max = proto_ld_i32(&d[17]);
    m->X0_P  = proto_ld_i32(&d[21]);
    m->X0_Q  = proto_ld_i32(&d[25]);

    if (m->kappa <= 0 || m->p_max <= 0 || m->q_max <= 0) {
        a->params.mg_blocks &= (uint8_t)~MG_BLOCK_PLANT;
        return AGENT_ERR_RANGE;         /* x = X/kappa, and a rating limiter */
    }
    a->params.mg_blocks |= MG_BLOCK_PLANT;
    return AGENT_OK;
}

static int apply_mg_virtual(struct agent *a, const uint8_t *d, uint16_t len)
{
    if (len != PROTO_MG_VIRTUAL_LEN) {
        return AGENT_ERR_LEN;
    }
    struct microgrid_params *m = &a->params.microgrid;
    m->T_o     = proto_ld_i32(&d[0]);
    m->alpha_0 = proto_ld_i32(&d[4]);
    m->delta_o = proto_ld_i32(&d[8]);
    m->kappa_o = proto_ld_i32(&d[12]);
    m->xi      = proto_ld_i32(&d[16]);
    m->c_0     = proto_ld_i32(&d[20]);
    m->b       = proto_ld_i32(&d[24]);
    m->rP      = proto_ld_i32(&d[28]);
    m->rQ      = proto_ld_i32(&d[32]);
    m->degree  = proto_ld_i32(&d[36]);
    m->z0_P    = proto_ld_i32(&d[40]);
    m->z0_Q    = proto_ld_i32(&d[44]);

    if (m->b == 0 && (m->rP != 0 || m->rQ != 0)) {
        /* An unpinned node was sent the reference. Not knowing it is the
         * hypothesis, so this is a configuration error and not a value to
         * quietly accept. */
        a->params.mg_blocks &= (uint8_t)~MG_BLOCK_VIRTUAL;
        return AGENT_ERR_RANGE;
    }
    a->params.mg_blocks |= MG_BLOCK_VIRTUAL;
    return AGENT_OK;
}

static int apply_mg_iface(struct agent *a, const uint8_t *d, uint16_t len)
{
    if (len != PROTO_MG_IFACE_LEN) {
        return AGENT_ERR_LEN;
    }
    if (d[0] != (uint8_t)LAW_MICROGRID_ADAPTIVE
        && d[0] != (uint8_t)LAW_MICROGRID_LC) {
        /* The scalar law is not selectable here: it is what you get by not
         * sending these frames, and accepting it would give two ways to mean
         * the same thing. */
        return AGENT_ERR_RANGE;
    }
    const uint8_t need = MG_BLOCK_PLANT | MG_BLOCK_VIRTUAL;
    if ((a->params.mg_blocks & need) != need) {
        /* Refused rather than queued: the host sends the three in order, so
         * arriving here without the others means one was lost or rejected,
         * and the honest outcome is that the board stays on the scalar law
         * and says so. */
        return AGENT_ERR_RANGE;
    }

    struct microgrid_params *m = &a->params.microgrid;
    m->terminal   = d[1];
    m->mu_max     = proto_ld_i32(&d[2]);
    m->gamma      = proto_ld_i32(&d[6]);
    m->eps        = proto_ld_i32(&d[10]);
    m->vartheta_0 = proto_ld_i32(&d[14]);
    m->T_c        = proto_ld_i32(&d[18]);
    m->beta_c     = proto_ld_i32(&d[22]);
    m->Phi        = proto_ld_i32(&d[26]);
    m->delta_c    = proto_ld_i32(&d[30]);
    m->adapt_band = proto_ld_i32(&d[34]);

    /* The commit. Nothing above this line changed which law runs. */
    a->params.law = (enum control_law)d[0];
    microgrid_reset(a);
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
        a->params.seed                   = proto_ld_u32(&d[1]);
        /* uint48, little-endian, assembled byte by byte: there is no 6-byte load
         * in proto.h, and adding one there would put a radio-format width into
         * the serial header. */
        a->params.epoch_us = 0u;
        for (unsigned i = 0; i < 6u; i++) {
            a->params.epoch_us |= (uint64_t)d[5 + i] << (8u * i);
        }
        a->params.running                = true;
        a->params.first_time_running     = true;
        a->params.all_neighbors_observed = false;

        for (uint8_t i = 0; i < AGENT_MAX_NEIGHBORS; i++) {
            a->params.available_neighbors[i] = false;
            a->params.neighbors_enabled[i]   = false;
            a->vars.neighbor_vstates[i]      = 0;
            a->vars.neighbor_vstates_q[i]    = 0;
            a->vars.neighbor_seq[i]          = 0;
            a->vars.neighbor_rssi[i]         = 0;
        }

        a->vars.state    = a->params.state_0;
        a->vars.vstate   = a->params.vstate_0;
        a->vars.vartheta = a->params.vartheta_0;
        a->vars.counter  = a->params.counter_0;
        a->vars.time_us  = now_us;
        a->vars.tx_seq   = 0u;

        a->vars.state_f    = (float)a->params.state_0    * 1e-6f;
        a->vars.vstate_f   = (float)a->params.vstate_0   * 1e-6f;
        a->vars.vartheta_f = (float)a->params.vartheta_0 * 1e-6f;

        /* The trigger is what starts a run, so it is where initial conditions
         * are latched -- for whichever law is selected. Without this the
         * lines above put the SCALAR law's state_0 into the mirrors and the
         * microgrid integrators kept whatever the previous run left them,
         * so configuring once and triggering twice gave two different runs
         * and the first reported sample was the wrong law's value. */
        if (a->params.law != LAW_FINITE_TIME_ADAPTIVE) {
            microgrid_reset(a);
        }

        prng_seed((uint64_t)a->params.seed, (uint64_t)a->params.node_id);
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
    case PROTO_T_MG_PLANT:    return apply_mg_plant(a, payload, len);
    case PROTO_T_MG_VIRTUAL:  return apply_mg_virtual(a, payload, len);
    case PROTO_T_MG_IFACE:    return apply_mg_iface(a, payload, len);
    case PROTO_T_DISTURBANCE: return apply_disturbance(a, payload, len);
    case PROTO_T_CONTROL:     return apply_control(a, payload, len, now_us);
    default:                  return AGENT_ERR_TYPE;
    }
}
