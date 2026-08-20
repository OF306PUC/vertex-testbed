/**
 * @file report.h
 * @brief STATE reports to the Raspberry Pi.
 *
 * Payload, little-endian:
 *
 *   [t_us:8][state:4][vstate:4][vartheta:4][counter:4][n:1]
 *   then per neighbour: [vstate:4][flags:1]
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

/** @brief Send one STATE frame. Call at the `clock` period while running. */
int report_state(const struct agent *a);

/** @brief Mark a neighbour as heard. Cleared by each report_state(). */
void report_mark_fresh(uint8_t index);

#endif /* REPORT_H_ */
