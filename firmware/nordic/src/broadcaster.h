/**
 * @file broadcaster.h
 * @brief Advertising: put this agent's vstate on the air.
 */

#ifndef BROADCASTER_H_
#define BROADCASTER_H_

#include <stdbool.h>
#include <stdint.h>

#include "common.h"
#include "air_wire.h"

/** Requested TX power in dBm. The nRF52832 on the DK tops out at +4, so the
 *  vendor command selects the nearest supported level rather than failing. */
#define TX_POWER_LEVEL_BLE    8

/**
 * @brief Set the advertising interval, in 0.625 ms units.
 *
 * Takes effect on the next broadcaster_init(). Advertising parameters are only
 * settable while advertising is stopped.
 *
 * @return 0, or -EINVAL if min is zero or exceeds max.
 */
int broadcaster_set_adv_params(uint16_t interval_min, uint16_t interval_max);

/** @brief Start advertising @p pkt. Idempotent-safe: returns 0 if the advertiser
 *  is already running. */
int broadcaster_init(const state_packet_type *pkt);

/** @brief Replace the advertised payload. Called every control period.
 *
 *  Returns -EAGAIN if the advertiser was never started, without logging: at one
 *  call per `dt` a log line here is a flood, and the UART it floods is the same
 *  one carrying the STATE reports. */
int broadcaster_update(const state_packet_type *pkt);

int broadcaster_stop(void);

#endif // BROADCASTER_H_
