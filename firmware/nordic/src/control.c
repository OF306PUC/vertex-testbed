/**
 * @file control.c
 * @brief Serial frame dispatcher: link -> agent, radio, or a reply.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "agent.h"
#include "broadcaster.h"
#include "control.h"
#include "observer.h"
#include "proto.h"
#include "uart_link.h"

LOG_MODULE_REGISTER(control, LOG_LEVEL_INF);

/* Called on every accepted CONTROL frame, so the run loops react to a trigger
 * immediately instead of at the next idle poll. See control.h. */
static void (*trigger_hook)(void);

void control_set_trigger_hook(void (*hook)(void))
{
    trigger_hook = hook;
}

static void send_ack(uint8_t type, int status)
{
    uint8_t p[2] = { type, (uint8_t)(int8_t)status };
    (void)uart_link_send(PROTO_T_ACK, p, sizeof(p));
}

static void send_err(uint8_t type, int code)
{
    uint8_t p[2] = { type, (uint8_t)(int8_t)code };
    (void)uart_link_send(PROTO_T_ERR, p, sizeof(p));
}

/**
 * Decode a RADIO frame and hand it to the observer.
 */
static int apply_radio(const uint8_t *payload, uint16_t len)
{
    struct radio_params r;
    const int rc = agent_parse_radio(payload, len, &r);
    if (rc != AGENT_OK) {
        return rc;
    }
    /* Advertising interval: stored, not applied. Zephyr takes it through
     * bt_le_adv_start(), so it lands when main.c starts the broadcaster on the
     * next run. Applying it mid-run means tearing the advertiser down in the
     * middle of the thing being measured. */
    if (broadcaster_set_adv_params(r.adv_min, r.adv_max)) {
        return AGENT_ERR_RANGE;
    }
    if (observer_set_scan_params(r.scan_interval, r.scan_window, r.active_scan)) {
        return AGENT_ERR_RANGE;
    }
    return AGENT_OK;
}

void control_on_frame(uint8_t type, const uint8_t *payload, uint16_t len, void *ctx)
{
    struct agent *a = ctx;
    int rc;

    if (a == NULL) {
        /* uart_link_init() was called without the agent. Report rather than
         * dereference: a fault here would look like a dead serial link. */
        send_err(type, AGENT_ERR_TYPE);
        LOG_ERR("no agent bound to the control plane");
        return;
    }

    switch (type) {
    case PROTO_T_NETWORK:
    case PROTO_T_ALGORITHM:
    case PROTO_T_DISTURBANCE:
    case PROTO_T_CONTROL:
        rc = agent_apply_frame(a, type, payload, len,
                               k_ticks_to_us_floor64(k_uptime_ticks()));
        break;

    case PROTO_T_RADIO:
        rc = apply_radio(payload, len);
        break;

    case PROTO_T_PING: {
        uint8_t p[8];
        proto_st_u64(p, (uint64_t)k_ticks_to_us_floor64(k_uptime_ticks()));
        (void)uart_link_send(PROTO_T_PONG, p, sizeof(p));
        return;                         /* PONG is the reply; no ACK */
    }

    default:
        rc = AGENT_ERR_TYPE;
        break;
    }

    if (rc == AGENT_OK) {
        send_ack(type, 0);
        /* After the ACK: the host's next frame can then arrive while the run
         * loops are already spinning up, rather than behind them. Fires on stop
         * as well as start, so teardown is just as prompt. */
        if (type == PROTO_T_CONTROL && trigger_hook != NULL) {
            trigger_hook();
        }
    } else {
        /* Reported, not just logged: the host is the only place that can
         * correlate a rejection with what it sent. */
        send_err(type, rc);
        LOG_WRN("frame 0x%02X rejected: %d", type, rc);
    }
}
