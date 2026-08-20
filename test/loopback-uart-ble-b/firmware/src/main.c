/**
 * @file main.c
 * @brief Test peer for direction B: a commanded advertiser.
 *
 *     UART  T <AD bytes>  ──►  advertise those bytes verbatim
 *     UART  k / TXAT      ◄──  acknowledgement plus the board's TX timestamp
 *
 * The mirror of direction A, and deliberately smaller. Here the Pi does the
 * scanning, so this board never reports advertisements -- which removes the
 * bottleneck that voided direction A's measurement. In a busy room A had to relay
 * ~217 reports/s over a 115200 link, 96% of them from other devices, and dropped
 * its own data doing it. B moves that traffic to the Pi's HCI socket, where 21k
 * foreign adverts cost nothing.
 *
 * Scanning is therefore left OFF. Less running on the board means fewer
 * explanations for a result.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <string.h>

#include "agent.h"
#include "ble_scan.h"
#include "proto.h"
#include "uart_link.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

#define FW_VERSION          1u
#define DEFAULT_ADV_INT     0x00A0u     /* 100 ms in 0.625 ms units */

static struct agent agent;
static uint16_t tx_seq;

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
 * @brief Report when a payload actually reached the controller.
 *
 * The Pi brackets its own send and receive instants around this, which bounds
 * the clock offset to a UART round trip. Not a one-way delay -- the two clocks
 * share no origin -- but enough to separate advertising-interval effects.
 */
static void send_txat(uint16_t seq)
{
    uint8_t p[10];
    proto_st_u16(&p[0], seq);
    proto_st_u64(&p[2], (uint64_t)k_ticks_to_us_floor64(k_uptime_ticks()));
    int err = uart_link_send(PROTO_T_TXAT, p, sizeof(p));
    if (err) {
        /* From the host this is indistinguishable from a hang: it waits for a
         * reply that was built and then dropped. */
        LOG_ERR("TXAT seq %u dropped: %d", seq, err);
    }
}

static void on_frame(uint8_t type, const uint8_t *payload, uint16_t len, void *ctx)
{
    ARG_UNUSED(ctx);
    int rc = 0;

    /* Logged before dispatch: if the board hangs or faults inside a handler, this
     * is the last line and it names the culprit. */
    LOG_INF("frame 0x%02X, %u byte(s)", type, len);

    switch (type) {

    case PROTO_T_ADV_TX:
        if (len == 0u) {
            rc = ble_adv_stop();
            break;
        }
        /* Verbatim. The Pi supplies complete AD including element structure;
         * interpreting it here would defeat the instrument. */
        rc = ble_adv_set(payload, (uint8_t)len);
        if (rc == 0 && !ble_adv_running()) {
            rc = ble_adv_start(DEFAULT_ADV_INT, DEFAULT_ADV_INT, 0x07u);
        }
        if (rc == 0) {
            send_txat(++tx_seq);
            return;                     /* TXAT is the acknowledgement */
        }
        break;

    case PROTO_T_RADIO: {
        /* [adv_min:2][adv_max:2][scan_int:2][scan_win:2][flags:1]
         * Only the advertising half is honoured here; this board does not scan. */
        if (len != PROTO_RADIO_LEN) { rc = -EINVAL; break; }
        uint16_t adv_min = proto_ld_u16(&payload[0]);
        uint16_t adv_max = proto_ld_u16(&payload[2]);
        uint8_t  flags   = payload[8];

        if (adv_min == 0u || adv_min > adv_max) { rc = -EINVAL; break; }
        rc = (flags & 0x02u) ? ble_adv_start(adv_min, adv_max, 0x07u)
                             : ble_adv_stop();
        break;
    }

    case PROTO_T_PING: {
        uint8_t p[8];
        proto_st_u64(p, (uint64_t)k_ticks_to_us_floor64(k_uptime_ticks()));
        (void)uart_link_send(PROTO_T_PONG, p, sizeof(p));
        return;
    }

    case PROTO_T_STATS_REQ: {
        /* Without these the Pi cannot trust a delivery ratio. Direction A's run
         * was voided by tx_dropped, and only reading it made that visible. */
        const struct uart_link_stats *u = uart_link_stats();
        const struct proto_stats     *p = uart_link_proto_stats();
        const struct ble_scan_stats  *b = ble_scan_stats();
        uint8_t o[48]; size_t n = 0;

        proto_st_u32(&o[n], b->reports);              n += 4;
        proto_st_u32(&o[n], b->queue_dropped);        n += 4;
        proto_st_u32(&o[n], b->oversize);             n += 4;
        proto_st_u32(&o[n], u->tx_frames);            n += 4;
        proto_st_u32(&o[n], u->tx_dropped);           n += 4;
        proto_st_u32(&o[n], u->rx_overrun_bytes);     n += 4;
        proto_st_u32(&o[n], u->rx_stopped);           n += 4;
        proto_st_u32(&o[n], u->rx_partial_flushes);   n += 4;
        proto_st_u32(&o[n], u->rx_full_flushes);      n += 4;
        proto_st_u32(&o[n], p->frames_ok);            n += 4;
        proto_st_u32(&o[n], p->crc_errors);           n += 4;
        proto_st_u32(&o[n], p->timeouts);             n += 4;

        (void)uart_link_send(PROTO_T_STATS, o, (uint16_t)n);
        return;
    }

    case PROTO_T_NETWORK:
    case PROTO_T_ALGORITHM:
    case PROTO_T_DISTURBANCE:
    case PROTO_T_CONTROL:
        /* Accepted and stored; nothing on this board runs a control law. Kept so
         * the config path stays exercised against real hardware. */
        rc = agent_apply_frame(&agent, type, payload, len,
                               k_ticks_to_us_floor64(k_uptime_ticks()));
        break;

    default:
        rc = -ENOTSUP;
        break;
    }

    if (rc == 0) {
        send_ack(type, 0);
    } else {
        send_err(type, rc);
        LOG_WRN("frame 0x%02X rejected: %d", type, rc);
    }
}

int main(void)
{
    agent_init(&agent);

    int err = uart_link_init(on_frame, NULL);
    if (err) {
        LOG_ERR("UART init failed: %d", err);
        return err;
    }

    err = ble_scan_init();              /* bt_enable only; no scanning started */
    if (err) {
        LOG_ERR("BLE init failed: %d", err);
        return err;
    }

    LOG_INF("test peer B v%u ready -- commanded advertiser, not scanning", FW_VERSION);
    return 0;
}
