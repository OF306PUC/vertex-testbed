#ifndef BROADCASTER_H_
#define BROADCASTER_H_

// Include modules
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/hci_vs.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/logging/log.h>
#include "common.h"

#define MIN_ADV_INTERVAL      2048   /* 1280 ms (2048 * 0.625 ms) */
#define MAX_ADV_INTERVAL      2048   /* 1280 ms (2048 * 0.625 ms) */
#define TX_POWER_LEVEL_BLE    8

// Declare public functions
void set_tx_power(uint8_t handle_type, uint16_t handle, int8_t tx_pwr_lvl);
int broadcaster_init(custom_data_type* custom_data);
int broadcaster_update_scan_response_custom_data(custom_data_type* custom_data);
int broadcaster_stop(void);

#endif // BROADCASTER_H_
