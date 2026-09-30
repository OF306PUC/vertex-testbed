/**
 * @file report.h
 * @brief STATE reports to the Raspberry Pi.
 *
 * Payload, little-endian:
 *
 *   [t_us:8][state:4][vstate:4][vartheta:4][counter:4][n:1]
 *   then per neighbour: [vstate:4][seq:2][rssi:1][flags:1]   -- 8 bytes each
 *
 * `seq` is the SENDER's v1 sequence number, so per-link delivery ratio, loss,
 * duplicates and reordering are all derivable offline for a link into an nRF.
 * Without it those four links carried no delivery statistics at all, while every
 * link into a Pi agent did. `rssi` is the last received signal strength in dBm,
 * and it is the discriminator between "the signal got worse" (interference) and
 * "packets were dropped elsewhere" (load).
 *
 * Both were already captured by observer.c and discarded at this boundary.
 *
 * flags bit0 = enabled  (the neighbour advertised itself as participating)
 *       bit1 = fresh    (a packet arrived from it since the last report)
 */

#ifndef REPORT_H_
#define REPORT_H_

#include <stdint.h>

#include "agent.h"

#define STATE_FLAG_ENABLED  0x01u
#define STATE_FLAG_FRESH    0x02u

/** Bytes per neighbour record. Mirrored by STATE_NEIGHBOUR in
 *  `vertex/serial/proto.py`; test/common/check_proto_layout.py compares them. */
#define STATE_NEIGHBOUR_BYTES  8u

/** Bytes appended when a microgrid law runs: state_q and vstate_q. */
#define STATE_TAIL_BYTES       8u

/**
 * @brief Bytes appended under the LC laws: the above plus S1_hat, S2_hat.
 *
 * The estimator is not derivable from the trajectory the way g, mu and sigma
 * are: those are functions of (x, z, t), while this is the integral of its
 * own error and re-integrating it offline only reproduces the host's answer.
 * When LC+ tracked on three Pi nodes and not on the two nRF ones, the one
 * quantity that separates the explanations was the one nothing reported.
 */
#define STATE_TAIL_LC_BYTES    16u

/** @brief Send one STATE frame. Call at the `clock` period while running. */
int report_state(const struct agent *a);

/** @brief Mark a neighbour as heard. Cleared by each report_state(). */
void report_mark_fresh(uint8_t index);

#endif /* REPORT_H_ */
