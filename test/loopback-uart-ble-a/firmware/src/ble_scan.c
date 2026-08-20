#include "ble_scan.h"

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <string.h>

LOG_MODULE_REGISTER(ble_scan, LOG_LEVEL_INF);

#define REPORT_QUEUE_DEPTH  16u

K_MSGQ_DEFINE(ble_report_queue, sizeof(struct ble_report), REPORT_QUEUE_DEPTH, 4);

static struct ble_scan_stats stats;
static bool adv_running;
static uint8_t adv_payload[BLE_MAX_AD_LEN];
static uint8_t adv_payload_len;

/* Raw AD passthrough: payload is handed to the stack as-is via a manufacturer-agnostic raw set. */
static struct bt_data adv_ad[1];

static void on_device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t adv_type,
                            struct net_buf_simple *ad)
{
    struct ble_report r;

    stats.reports++;

    if (ad->len > BLE_MAX_AD_LEN) {
        stats.oversize++;
        return;
    }

    r.timestamp_us = (uint64_t)k_ticks_to_us_floor64(k_uptime_ticks());
    r.rssi = rssi;
    r.adv_type = adv_type;
    r.addr_type = addr->type;
    memcpy(r.addr, addr->a.val, sizeof(r.addr));
    r.len = (uint8_t)ad->len;
    memcpy(r.data, ad->data, ad->len);

    /* Never block the BLE RX thread. */
    if (k_msgq_put(&ble_report_queue, &r, K_NO_WAIT) != 0) {
        stats.queue_dropped++;
    }
}

int ble_scan_init(void)
{
    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("bt_enable: %d", err);
        return err;
    }
    LOG_INF("Bluetooth initialised");
    return 0;
}

int ble_scan_start(uint16_t interval, uint16_t window, bool active)
{
    struct bt_le_scan_param p = {
        .type     = active ? BT_LE_SCAN_TYPE_ACTIVE : BT_LE_SCAN_TYPE_PASSIVE,
        .options  = BT_LE_SCAN_OPT_NONE,    /* duplicates REPORTED -- see the header */
        .interval = interval,
        .window   = window,
    };
    (void)bt_le_scan_stop();                /* idempotent restart */

    int err = bt_le_scan_start(&p, on_device_found);
    if (err) {
        LOG_ERR("bt_le_scan_start: %d", err);
        return err;
    }
    LOG_INF("scanning: interval=%u window=%u (%u%% duty) %s",
            interval, window, interval ? (100u * window / interval) : 0u,
            active ? "active" : "passive");
    return 0;
}

int ble_scan_stop(void)
{
    return bt_le_scan_stop();
}

int ble_adv_set(const uint8_t *data, uint8_t len)
{
    if (len > BLE_MAX_AD_LEN) {
        return -EMSGSIZE;
    }
    memcpy(adv_payload, data, len);
    adv_payload_len = len;

    if (!adv_running) {
        return 0;                           /* takes effect on the next start */
    }
    /* Update without stopping: a stop/start would reset the advertising cadence
     * on every payload change and make the interval sweep meaningless. */
    adv_ad[0].type = BT_DATA_MANUFACTURER_DATA;
    adv_ad[0].data = adv_payload;
    adv_ad[0].data_len = adv_payload_len;
    return bt_le_adv_update_data(adv_ad, 1, NULL, 0);
}

int ble_adv_start(uint16_t interval_min, uint16_t interval_max, uint8_t chan_map)
{
    struct bt_le_adv_param p = *BT_LE_ADV_NCONN;
    p.interval_min = interval_min;
    p.interval_max = interval_max;
    ARG_UNUSED(chan_map);   /* Zephyr exposes the channel map only via HCI or
                             * CONFIG_BT_CTLR_ADV_EXT; see the note in README. */

    adv_ad[0].type = BT_DATA_MANUFACTURER_DATA;
    adv_ad[0].data = adv_payload;
    adv_ad[0].data_len = adv_payload_len;

    if (adv_running) {
        (void)bt_le_adv_stop();
        adv_running = false;
    }
    int err = bt_le_adv_start(&p, adv_ad, 1, NULL, 0);
    if (err) {
        LOG_ERR("bt_le_adv_start: %d", err);
        return err;
    }
    adv_running = true;
    LOG_INF("advertising: interval %u..%u", interval_min, interval_max);
    return 0;
}

int ble_adv_stop(void)
{
    if (!adv_running) {
        return 0;
    }
    adv_running = false;
    return bt_le_adv_stop();
}

const struct ble_scan_stats *ble_scan_stats(void) { return &stats; }
