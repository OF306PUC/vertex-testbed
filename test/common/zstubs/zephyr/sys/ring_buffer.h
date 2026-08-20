#ifndef ZSTUB_RINGBUF_H
#define ZSTUB_RINGBUF_H
#include <stdint.h>
struct ring_buf { int _; };
#define RING_BUF_DECLARE(name, size) struct ring_buf name
uint32_t ring_buf_put(struct ring_buf *rb, const uint8_t *data, uint32_t n);
uint32_t ring_buf_get(struct ring_buf *rb, uint8_t *data, uint32_t n);
#endif
