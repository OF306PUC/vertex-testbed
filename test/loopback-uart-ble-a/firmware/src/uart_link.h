/**
 * @file uart_link.h
 * @brief Framed UART transport: async RX into a ring buffer, queued TX.
 *
 * Splits cleanly from proto.c: this file owns the hardware and the concurrency,
 * proto.c owns the bytes. Neither needs to know the other's difficulties.
 */

#ifndef UART_LINK_H_
#define UART_LINK_H_

#include <stdint.h>
#include <stddef.h>

#include "proto.h"

struct uart_link_stats {
    uint32_t rx_overrun_bytes;  /* ring buffer full: bytes dropped before parsing */
    uint32_t tx_dropped;        /* TX queue full: frames never sent */
    uint32_t tx_frames;
    uint32_t rx_stopped;        /* bus errors (framing/parity/overrun) */

    /* Diagnostic for the one silent misconfiguration this driver can suffer.
     *
     * CONFIG_UART_0_NRF_HW_ASYNC=y 
     * CONFIG_UART_0_NRF_HW_ASYNC_TIMER. 
     *
     */
    uint32_t rx_partial_flushes;
    uint32_t rx_full_flushes;
};

int  uart_link_init(proto_frame_cb cb, void *ctx);

/** @brief Queue one frame. Non-blocking; drops and counts when the queue is full. */
int  uart_link_send(uint8_t type, const uint8_t *payload, uint16_t len);

const struct uart_link_stats *uart_link_stats(void);
const struct proto_stats     *uart_link_proto_stats(void);

#endif /* UART_LINK_H_ */
