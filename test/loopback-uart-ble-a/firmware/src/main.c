/**
 * @file main.c
 * @brief Test peer: a transparent bridge between the UART and the BLE
 *        advertising channel.
 *
 *     UART  T frame (AD bytes)   ──►  advertise those bytes verbatim
 *     UART  r frame (report)     ◄──  every advertising report, raw
 *
 * Producer/consumer, as sketched in the prototype:
 *   - the BLE RX thread captures reports into a message queue and returns;
 *   - this thread drains the queue and frames them onto the UART.
 *
 * The split exists so a slow UART can never stall the radio. If the queue fills,
 * reports are dropped and *counted* -- an uncounted drop would appear in the
 * host's analysis as packet loss over the air, which is precisely the measurement
 * this board exists to make trustworthy.
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
#define DEFAULT_SCAN_INT    0x00A0u     /* 100 ms in 0.625 ms units */
#define DEFAULT_SCAN_WIN    0x00A0u     /* window == interval: 100% duty */

static struct agent agent;

/* ---- inbound frames ---------------------------------------------------- */

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

static void on_frame(uint8_t type, const uint8_t *payload, uint16_t len, void *ctx)
{
    ARG_UNUSED(ctx);
    int rc = 0;

    /* Logged before dispatch: if the board hangs or faults inside a handler, this
     * is the last line and it names the culprit. */
    LOG_INF("frame 0x%02X, %u byte(s)", type, len);

    switch (type) {

    case PROTO_T_ADV_TX:
        /* Verbatim: the host supplies complete AD, including element structure.
         * Interpreting it here would defeat the purpose of the instrument. */
        rc = ble_adv_set(payload, (uint8_t)len);
        if (rc == 0 && len > 0u) {
            rc = ble_adv_start(DEFAULT_SCAN_INT, DEFAULT_SCAN_INT, 0x07u);
        }
        break;

    case PROTO_T_RADIO: {
        /* [adv_min:2][adv_max:2][scan_int:2][scan_win:2][flags:1]
         * flags bit0 = active scan, bit1 = advertising enabled */
        if (len != PROTO_RADIO_LEN) { rc = -EINVAL; break; }
        uint16_t adv_min  = proto_ld_u16(&payload[0]);
        uint16_t adv_max  = proto_ld_u16(&payload[2]);
        uint16_t scan_int = proto_ld_u16(&payload[4]);
        uint16_t scan_win = proto_ld_u16(&payload[6]);
        uint8_t  flags    = payload[8];

        if (scan_win > scan_int || scan_int == 0u) { rc = -EINVAL; break; }

        rc = ble_scan_start(scan_int, scan_win, (flags & 0x01u) != 0u);
        if (rc == 0) {
            rc = (flags & 0x02u) ? ble_adv_start(adv_min, adv_max, 0x07u)
                                 : ble_adv_stop();
        }
        break;
    }

    case PROTO_T_PING: {
        uint8_t p[8];
        proto_st_u64(p, (uint64_t)k_ticks_to_us_floor64(k_uptime_ticks()));
        (void)uart_link_send(PROTO_T_PONG, p, sizeof(p));
        return;                             /* PONG is the acknowledgement */
    }

    case PROTO_T_STATS_REQ: {
        /* Without these the host cannot trust a delivery ratio: a report dropped
         * inside the peer is indistinguishable from one lost over the air. */
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
        /* Report the failure rather than logging it: the host is the only place
         * that can correlate a rejected frame with what it sent. */
        send_err(type, rc);
        LOG_WRN("frame 0x%02X rejected: %d", type, rc);
    }
}

/* ---- report thread ----------------------------------------------------- */

#define REPORT_STACK_SIZE   2048
#define REPORT_PRIORITY     5

static void report_thread(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    struct ble_report r;
    uint8_t payload[8 + 1 + 6 + 1 + 1 + BLE_MAX_AD_LEN];

    while (1) {
        /* Blocks until a report arrives; no polling, no busy wait. */
        if (k_msgq_get(&ble_report_queue, &r, K_FOREVER) != 0) {
            continue;
        }

        /* [timestamp_us:8][rssi:1][addr_type:1][addr:6][adv_type:1][len:1][data] */
        size_t n = 0;
        proto_st_u64(&payload[n], r.timestamp_us);      n += 8;
        payload[n++] = (uint8_t)r.rssi;
        payload[n++] = r.addr_type;
        memcpy(&payload[n], r.addr, 6);                 n += 6;
        payload[n++] = r.adv_type;
        payload[n++] = r.len;
        memcpy(&payload[n], r.data, r.len);             n += r.len;

        (void)uart_link_send(PROTO_T_ADV_REPORT, payload, (uint16_t)n);
    }
}

K_THREAD_DEFINE(report_tid, REPORT_STACK_SIZE, report_thread, NULL, NULL, NULL,
                REPORT_PRIORITY, 0, 0);

/* ---- entry ------------------------------------------------------------- */

int main(void)
{
    agent_init(&agent);

    int err = uart_link_init(on_frame, NULL);
    if (err) {
        LOG_ERR("UART init failed: %d", err);
        return err;
    }

    err = ble_scan_init();
    if (err) {
        LOG_ERR("BLE init failed: %d", err);
        return err;
    }

    /* Scan immediately at 100% duty. The host narrows the window deliberately
     * when it wants to measure the effect; the default should be the
     * configuration that loses nothing. */
    err = ble_scan_start(DEFAULT_SCAN_INT, DEFAULT_SCAN_WIN, false);
    if (err) {
        LOG_ERR("scan start failed: %d", err);
        return err;
    }

    LOG_INF("test peer v%u ready", FW_VERSION);
    return 0;
}
