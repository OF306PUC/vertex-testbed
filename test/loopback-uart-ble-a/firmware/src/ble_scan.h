/**
 * @file ble_scan.h
 * @brief BLE observer: every advertising report, raw and unparsed.
 *
 * The instrument's defining property. It does NOT parse advertising data, does
 * NOT filter by manufacturer, and does NOT filter duplicates. It captures the raw
 * AD bytes and hands them to the host.
 *
 * Each of those is deliberate:
 *
 * - **No parsing.** The Pi already has a decoder. A second one here could
 *   disagree with it, and a peer that agrees with the host's bug is worse than
 *   no peer at all. Raw bytes let the host assert byte equality against exactly
 *   what it transmitted.
 * - **No duplicate filtering** (`BT_LE_SCAN_OPT_NONE`). The coordination firmware
 *   uses `BT_LE_SCAN_OPT_FILTER_DUPLICATE`, which suppresses a repeat of an
 *   identical payload. A suppressed duplicate is indistinguishable from a lost
 *   packet, so filtering makes delivery ratio unmeasurable.
 * - **Passive by default.** Active scanning transmits scan requests, spending
 *   airtime and perturbing the very coexistence being measured.
 */

#ifndef BLE_SCAN_H_
#define BLE_SCAN_H_

#include <stdbool.h>
#include <stdint.h>

#define BLE_MAX_AD_LEN      31u

struct ble_report {
    uint64_t timestamp_us;      /* capture instant, board clock */
    int8_t   rssi;
    uint8_t  addr[6];           /* little-endian, as it appears on the wire */
    uint8_t  addr_type;
    uint8_t  adv_type;
    uint8_t  len;
    uint8_t  data[BLE_MAX_AD_LEN];
};

extern struct k_msgq ble_report_queue;

struct ble_scan_stats {
    uint32_t reports;
    uint32_t queue_dropped;     /* consumer too slow -- NOT radio loss */
    uint32_t oversize;
};

int  ble_scan_init(void);
int  ble_scan_start(uint16_t interval, uint16_t window, bool active);
int  ble_scan_stop(void);

/** @brief Advertise @p data verbatim. Replaces the payload without stopping. */
int  ble_adv_set(const uint8_t *data, uint8_t len);
int  ble_adv_start(uint16_t interval_min, uint16_t interval_max, uint8_t chan_map);
int  ble_adv_stop(void);

const struct ble_scan_stats *ble_scan_stats(void);

#endif /* BLE_SCAN_H_ */
