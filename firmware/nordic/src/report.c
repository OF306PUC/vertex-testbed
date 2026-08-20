#include "report.h"

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

    uint8_t p[8 + 4 + 4 + 4 + 4 + 1 + (AGENT_MAX_NEIGHBORS * 5)];
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
        uint8_t flags = 0;
        if (a->params.neighbors_enabled[i]) {
            flags |= STATE_FLAG_ENABLED;
        }
        if (fresh & (1u << i)) {
            flags |= STATE_FLAG_FRESH;
        }
        p[n++] = flags;
    }

    int err = uart_link_send(PROTO_T_STATE, p, (uint16_t)n);
    if (err) {
        LOG_WRN("STATE dropped: %d", err);
    }
    return err;
}
