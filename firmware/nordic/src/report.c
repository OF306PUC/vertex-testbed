#include <math.h>

#include "report.h"
#include "coordination_task.h"   /* SCALE_FACTOR */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

#include "proto.h"
#include "uart_link.h"

LOG_MODULE_REGISTER(report, LOG_LEVEL_INF);

/* Set by the observer when a neighbour's advertisement is parsed, cleared on each
 * report. So `fresh` means "heard since the last report", which is the window the
 * host reconstructs delivery ratio over. */
static atomic_t fresh_mask;

void report_mark_fresh(uint8_t index)
{
    if (index < AGENT_MAX_NEIGHBORS) {
        (void)atomic_or(&fresh_mask, (atomic_val_t)(1u << index));
    }
}

int report_state(const struct agent *a)
{
    if (!a->params.running) {
        return 0;
    }

    uint8_t p[8 + 4 + 4 + 4 + 4 + 1
              + (AGENT_MAX_NEIGHBORS * STATE_NEIGHBOUR_BYTES)
              + STATE_TAIL_LC_BYTES];
    size_t n = 0;

    /* vars.time_us is the run start, in MICROSECONDS.*/
    int64_t t_us = k_ticks_to_us_floor64(k_uptime_ticks()) - a->vars.time_us;
    if (t_us < 0) {
        t_us = 0;
    }
    proto_st_u64(&p[n], (uint64_t)t_us);                     n += 8;
    proto_st_u32(&p[n], (uint32_t)a->vars.state);            n += 4;
    proto_st_u32(&p[n], (uint32_t)a->vars.vstate);           n += 4;
    proto_st_u32(&p[n], (uint32_t)a->vars.vartheta);         n += 4;
    proto_st_u32(&p[n], (uint32_t)a->vars.counter);          n += 4;
    p[n++] = a->params.n_neighbors;

    /* Read and clear in one operation: anything the observer marks after this
     * point belongs to the next reporting window, not to a window already sent. */
    const uint32_t fresh = (uint32_t)atomic_clear(&fresh_mask);

    for (uint8_t i = 0; i < a->params.n_neighbors; i++) {
        proto_st_u32(&p[n], (uint32_t)a->vars.neighbor_vstates[i]);   n += 4;
        proto_st_u16(&p[n], a->vars.neighbor_seq[i]);                 n += 2;
        p[n++] = (uint8_t)a->vars.neighbor_rssi[i];      /* int8 on the wire */
        uint8_t flags = 0;
        if (a->params.neighbors_enabled[i]) {
            flags |= STATE_FLAG_ENABLED;
        }
        if (fresh & (1u << i)) {
            flags |= STATE_FLAG_FRESH;
        }
        p[n++] = flags;
    }

    /* The second coordinate rides at the END, after the neighbour records,
     * so the offsets of everything before it are untouched and the two
     * layouts are told apart by length -- the same rule decode_stats and the
     * air codec already use. Appended only under a microgrid law, so a
     * scalar run's frame is byte for byte what it always was. */
    if (a->params.law != LAW_FINITE_TIME_ADAPTIVE) {
        proto_st_u32(&p[n], (uint32_t)a->vars.state_q);      n += 4;
        proto_st_u32(&p[n], (uint32_t)a->vars.vstate_q);     n += 4;
    }
    /* The LC interface's estimate. Under the adaptive law `vartheta` already
     * rides in the header, so only this family needs more. */
    if (a->params.law == LAW_MICROGRID_LC) {
        proto_st_u32(&p[n], (uint32_t)lrintf(a->vars.mg.S1_hat * SCALE_FACTOR));
        n += 4;
        proto_st_u32(&p[n], (uint32_t)lrintf(a->vars.mg.S2_hat * SCALE_FACTOR));
        n += 4;
    }

    int err = uart_link_send(PROTO_T_STATE, p, (uint16_t)n);
    if (err) {
        LOG_WRN("STATE dropped: %d", err);
    }
    return err;
}
