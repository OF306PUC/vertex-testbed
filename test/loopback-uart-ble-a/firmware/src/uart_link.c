#include "uart_link.h"

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/ring_buffer.h>

LOG_MODULE_REGISTER(uart_link, LOG_LEVEL_INF);


static const struct device *const uart = DEVICE_DT_GET(DT_NODELABEL(uart0));

#define DMA_BUF_SIZE        256u
#define RX_RING_SIZE        1024u
#define RX_IDLE_TIMEOUT_US  1000u
#define TX_QUEUE_DEPTH      8u

/* A frame left incomplete is abandoned after this much silence. */
#define FRAME_IDLE_TIMEOUT_MS 100

RING_BUF_DECLARE(rx_ring, RX_RING_SIZE);

static uint8_t dma_buf[2][DMA_BUF_SIZE];
static uint8_t active_buf;

struct tx_entry {
    uint16_t len;
    uint8_t  data[PROTO_MAX_FRAME];
};

K_MSGQ_DEFINE(tx_queue, sizeof(struct tx_entry), TX_QUEUE_DEPTH, 4);

static struct proto_parser        parser;
static struct uart_link_stats     stats;
static struct tx_entry            tx_current;
static volatile bool              tx_in_progress;

static void rx_work_handler(struct k_work *work);
static void tx_work_handler(struct k_work *work);
static void idle_work_handler(struct k_work *work);

static K_WORK_DEFINE(rx_work, rx_work_handler);
static K_WORK_DEFINE(tx_work, tx_work_handler);
static K_WORK_DELAYABLE_DEFINE(idle_work, idle_work_handler);

/* ---- RX --------------------------------------------------------------- */

static void rx_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    uint8_t chunk[64];
    uint32_t n;

    /* Drain in chunks rather than a byte at a time: same result, far fewer
     * ring-buffer calls when a full DMA buffer lands. */
    while ((n = ring_buf_get(&rx_ring, chunk, sizeof(chunk))) > 0u) {
        proto_feed(&parser, chunk, n);
    }

    /* Arm or disarm the frame-idle timeout based on whether a frame is open. */
    if (proto_parser_in_frame(&parser)) {
        k_work_reschedule(&idle_work, K_MSEC(FRAME_IDLE_TIMEOUT_MS));
    } else {
        k_work_cancel_delayable(&idle_work);
    }
}

static void idle_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    if (proto_parser_in_frame(&parser)) {
        LOG_WRN("frame timed out mid-payload; resyncing");
        proto_parser_timeout(&parser);
    }
}

/* ---- TX --------------------------------------------------------------- */

static void tx_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    if (tx_in_progress) {
        return;
    }
    if (k_msgq_get(&tx_queue, &tx_current, K_NO_WAIT) != 0) {
        return;                                     /* nothing queued */
    }
    tx_in_progress = true;
    int err = uart_tx(uart, tx_current.data, tx_current.len, SYS_FOREVER_US);
    if (err) {
        LOG_ERR("uart_tx failed: %d", err);
        tx_in_progress = false;
        stats.tx_dropped++;
    } else {
        stats.tx_frames++;
    }
}

int uart_link_send(uint8_t type, const uint8_t *payload, uint16_t len)
{
    struct tx_entry e;
    size_t n = proto_build(e.data, sizeof(e.data), type, payload, len);
    if (n == 0u) {
        return -EINVAL;
    }
    e.len = (uint16_t)n;

    /* Never block. This is called from the BLE report path and from the control
     * loop; a full queue must cost a dropped report, not a stalled radio. */
    if (k_msgq_put(&tx_queue, &e, K_NO_WAIT) != 0) {
        stats.tx_dropped++;
        return -ENOMEM;
    }
    k_work_submit(&tx_work);
    return 0;
}

/* ---- driver callback --------------------------------------------------- */

static void uart_cb(const struct device *dev, struct uart_event *evt, void *user_data)
{
    ARG_UNUSED(user_data);

    switch (evt->type) {

    case UART_RX_RDY: {
        if (evt->data.rx.len < DMA_BUF_SIZE) {
            stats.rx_partial_flushes++;     /* idle timeout fired */
        } else {
            stats.rx_full_flushes++;
        }
        uint32_t put = ring_buf_put(&rx_ring,
                                    evt->data.rx.buf + evt->data.rx.offset,
                                    evt->data.rx.len);
        if (put < evt->data.rx.len) {
            stats.rx_overrun_bytes += (evt->data.rx.len - put);
        }
        k_work_submit(&rx_work);
        break;
    }

    case UART_RX_BUF_REQUEST:
        active_buf ^= 1u;
        uart_rx_buf_rsp(dev, dma_buf[active_buf], DMA_BUF_SIZE);
        break;

    case UART_RX_BUF_RELEASED:
        break;

    case UART_RX_STOPPED:
        stats.rx_stopped++;
        LOG_WRN("UART RX stopped (reason 0x%x); will re-enable",
                (unsigned)evt->data.rx_stop.reason);
        break;

    case UART_RX_DISABLED: {
        /* Re-arm, or the link goes silently deaf after the first bus error. */
        active_buf = 0u;
        int err = uart_rx_enable(dev, dma_buf[active_buf], DMA_BUF_SIZE,
                                 RX_IDLE_TIMEOUT_US);
        if (err) {
            LOG_ERR("UART RX re-enable failed: %d", err);
        }
        proto_parser_reset(&parser);
        break;
    }

    case UART_TX_DONE:
        tx_in_progress = false;
        k_work_submit(&tx_work);            /* drain the next queued frame */
        break;

    case UART_TX_ABORTED:
        LOG_ERR("UART TX aborted");
        tx_in_progress = false;
        k_work_submit(&tx_work);
        break;

    default:
        break;
    }
}

int uart_link_init(proto_frame_cb cb, void *ctx)
{
    if (!device_is_ready(uart)) {
        LOG_ERR("UART device not ready");
        return -ENODEV;
    }
    proto_parser_init(&parser, cb, ctx);

    int err = uart_callback_set(uart, uart_cb, NULL);
    if (err) {
        LOG_ERR("uart_callback_set: %d", err);
        return err;
    }
    active_buf = 0u;
    err = uart_rx_enable(uart, dma_buf[active_buf], DMA_BUF_SIZE, RX_IDLE_TIMEOUT_US);
    if (err) {
        LOG_ERR("uart_rx_enable: %d", err);
        return err;
    }
    LOG_INF("UART link up");
    return 0;
}

const struct uart_link_stats *uart_link_stats(void)       { return &stats; }
const struct proto_stats     *uart_link_proto_stats(void) { return &parser.stats; }
