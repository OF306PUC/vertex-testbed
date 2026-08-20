#ifndef ZSTUB_UART_H
#define ZSTUB_UART_H
#include <stdint.h>
#include <zephyr/device.h>
enum uart_event_type {
    UART_TX_DONE, UART_TX_ABORTED, UART_RX_RDY, UART_RX_BUF_REQUEST,
    UART_RX_BUF_RELEASED, UART_RX_DISABLED, UART_RX_STOPPED,
};
struct uart_event_rx { uint8_t *buf; size_t offset; size_t len; };
struct uart_event_rx_buf { uint8_t *buf; };
struct uart_event_rx_stop { int reason; };
struct uart_event_tx { const uint8_t *buf; size_t len; };
struct uart_event {
    enum uart_event_type type;
    union {
        struct uart_event_rx rx;
        struct uart_event_rx_buf rx_buf;
        struct uart_event_rx_stop rx_stop;
        struct uart_event_tx tx;
    } data;
};
typedef void (*uart_callback_t)(const struct device *dev,
                               struct uart_event *evt, void *user_data);
int uart_callback_set(const struct device *dev, uart_callback_t cb, void *ud);
int uart_rx_enable(const struct device *dev, uint8_t *buf, size_t len, int32_t timeout);
int uart_rx_buf_rsp(const struct device *dev, uint8_t *buf, size_t len);
int uart_tx(const struct device *dev, const uint8_t *buf, size_t len, int32_t timeout);
int uart_rx_disable(const struct device *dev);
#endif
