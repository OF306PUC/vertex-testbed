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

/* The host supplies COMPLETE AdvData -- already element-structured. Zephyr's
 * advertising API takes an array of elements, not raw bytes, so the incoming
 * buffer is split back into elements and handed over unchanged.
 *
 * Wrapping the whole buffer as one BT_DATA_MANUFACTURER_DATA element instead
 * fails silently: the air carries `1E FF <the host's entire AD>`, which fits 31
 * bytes, so nothing errors -- but the receiver reads the first two bytes of the
 * host's FIRST element as the company id and calls every advertisement somebody
 * else's. Zero deliveries, zero mismatches, no error anywhere.
 *
 * Splitting is not parsing: values are copied byte for byte, never interpreted. */
#define ADV_MAX_ELEMENTS   8
static struct bt_data adv_ad[ADV_MAX_ELEMENTS];
static uint8_t adv_ad_count;

/* Split adv_payload's first @len bytes into bt_data elements.
 * Element layout is len(1) | type(1) | value(len-1); the length byte counts
 * type + value, not the element's total size. bt_data.data points into
 * adv_payload, which is static -- the stack keeps these pointers. */
static int split_adv_payload(uint8_t len)
{
    uint8_t n = 0;
    uint8_t i = 0;

    while (i < len) {
        uint8_t l = adv_payload[i];
        if (l == 0u) {
            break;                      /* zero length terminates: padding */
        }
        if ((uint16_t)i + 1u + l > len) {
            LOG_ERR("AD element at %u declares %u bytes, %u remain",
                    i, l, len - i - 1u);
            return -EINVAL;
        }
        if (n >= ADV_MAX_ELEMENTS) {
            LOG_ERR("more than %u AD elements", ADV_MAX_ELEMENTS);
            return -E2BIG;
        }
        adv_ad[n].type = adv_payload[i + 1u];
        adv_ad[n].data = &adv_payload[i + 2u];
        adv_ad[n].data_len = (uint8_t)(l - 1u);
        n++;
        i = (uint8_t)(i + 1u + l);
    }

    if (n == 0u) {
        return -EINVAL;
    }
    adv_ad_count = n;
    return 0;
}

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

    int err = split_adv_payload(len);
    if (err) {
        return err;
    }
    if (!adv_running) {
        return 0;                           /* takes effect on the next start */
    }
    /* Update without stopping: a stop/start would reset the advertising cadence
     * on every payload change and make an interval sweep meaningless. */
    return bt_le_adv_update_data(adv_ad, adv_ad_count, NULL, 0);
}

int ble_adv_start(uint16_t interval_min, uint16_t interval_max, uint8_t chan_map)
{
    struct bt_le_adv_param p = *BT_LE_ADV_NCONN;
    p.interval_min = interval_min;
    p.interval_max = interval_max;
    ARG_UNUSED(chan_map);   /* Zephyr exposes the channel map only via HCI or
                             * CONFIG_BT_CTLR_ADV_EXT; see the note in README. */

    if (adv_ad_count == 0u) {
        LOG_ERR("no advertising payload set; send a T frame first");
        return -EINVAL;
    }

    if (adv_running) {
        (void)bt_le_adv_stop();
        adv_running = false;
    }
    int err = bt_le_adv_start(&p, adv_ad, adv_ad_count, NULL, 0);
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

bool ble_adv_running(void) { return adv_running; }

const struct ble_scan_stats *ble_scan_stats(void) { return &stats; }
