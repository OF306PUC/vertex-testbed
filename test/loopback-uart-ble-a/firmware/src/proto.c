#include "proto.h"

#include <string.h>

/*
 * ONE parser, dispatching on the type byte.
 */

/* CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor. */
uint16_t proto_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFFu;

    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (unsigned b = 0; b < 8u; b++) {
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u)
                                  : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

void proto_parser_init(struct proto_parser *p, proto_frame_cb cb, void *ctx)
{
    memset(p, 0, sizeof(*p));
    p->cb = cb;
    p->ctx = ctx;
    p->state = PROTO_WAIT_SOF;
}

void proto_parser_reset(struct proto_parser *p)
{
    p->state = PROTO_WAIT_SOF;
    p->received_len = 0;
    p->expected_len = 0;
}

void proto_parser_timeout(struct proto_parser *p)
{
    if (p->state != PROTO_WAIT_SOF) {
        p->stats.timeouts++;
        proto_parser_reset(p);
    }
}

/** Expected payload length for a type, or 0xFFFF if the type is unknown. */
static uint16_t max_len_for(uint8_t type)
{
    switch (type) {
    case PROTO_T_NETWORK:     return PROTO_NETWORK_MAX_LEN;
    case PROTO_T_ALGORITHM:   return PROTO_ALGORITHM_LEN;
    case PROTO_T_DISTURBANCE: return PROTO_DISTURBANCE_LEN;
    case PROTO_T_CONTROL:     return PROTO_CONTROL_LEN;
    case PROTO_T_RADIO:       return PROTO_RADIO_LEN;
    case PROTO_T_PING:        return PROTO_PING_LEN;
    case PROTO_T_STATS_REQ:   return PROTO_STATS_REQ_LEN;
    case PROTO_T_ADV_TX:      return PROTO_MAX_PAYLOAD;   /* AD bytes, variable */
    default:                  return 0xFFFFu;
    }
}

static void deliver(struct proto_parser *p)
{
    p->stats.frames_ok++;
    if (p->cb != NULL) {
        p->cb(p->type, p->payload, p->received_len, p->ctx);
    }
}

void proto_feed(struct proto_parser *p, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        const uint8_t byte = data[i];
        p->stats.bytes_in++;

        switch (p->state) {

        case PROTO_WAIT_SOF:
            if (byte == PROTO_SOF) {
                p->state = PROTO_WAIT_TYPE;
            }
            /* Anything else is inter-frame noise: drop it silently. Counting it
             * would make normal start-up look like an error, since the Pi may
             * open the port mid-frame. */
            break;

        case PROTO_WAIT_TYPE: {
            uint16_t cap = max_len_for(byte);
            if (cap == 0xFFFFu) {
                /* Unknown type. Do NOT assume this byte was a stray SOF and
                 * restart here -- resync from the next SOF instead. */
                p->stats.resyncs++;
                p->state = PROTO_WAIT_SOF;
            } else {
                p->type = byte;
                p->state = PROTO_WAIT_LEN_LO;
            }
            break;
        }

        case PROTO_WAIT_LEN_LO:
            p->expected_len = byte;
            p->state = PROTO_WAIT_LEN_HI;
            break;

        case PROTO_WAIT_LEN_HI: {
            p->expected_len |= (uint16_t)((uint16_t)byte << 8);
            uint16_t cap = max_len_for(p->type);

            if (p->expected_len > cap || p->expected_len > PROTO_MAX_PAYLOAD) {
                p->stats.len_errors++;
                p->stats.resyncs++;
                p->state = PROTO_WAIT_SOF;
            } else if (p->expected_len == 0u) {
                /* Zero-length frames are legal (PING). Straight to the CRC. */
                p->received_len = 0;
                p->state = PROTO_WAIT_CRC_LO;
            } else {
                p->received_len = 0;
                p->state = PROTO_WAIT_PAYLOAD;
            }
            break;
        }

        case PROTO_WAIT_PAYLOAD:
            p->payload[p->received_len++] = byte;
            /* The prototype reset the state after EVERY payload byte, not only
             * when the payload completed, so nothing longer than one byte could
             * ever be received. The guard belongs here. */
            if (p->received_len >= p->expected_len) {
                p->state = PROTO_WAIT_CRC_LO;
            }
            break;

        case PROTO_WAIT_CRC_LO:
            p->crc_rx = byte;
            p->state = PROTO_WAIT_CRC_HI;
            break;

        case PROTO_WAIT_CRC_HI: {
            p->crc_rx |= (uint16_t)((uint16_t)byte << 8);

            /* CRC covers TYPE, LEN and PAYLOAD -- rebuild that span. */
            uint8_t head[3];
            head[0] = p->type;
            proto_st_u16(&head[1], p->expected_len);

            uint16_t crc = proto_crc16(head, sizeof(head));
            /* Continue the CRC over the payload without a scratch copy. */
            for (uint16_t k = 0; k < p->received_len; k++) {
                crc ^= (uint16_t)((uint16_t)p->payload[k] << 8);
                for (unsigned b = 0; b < 8u; b++) {
                    crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u)
                                          : (uint16_t)(crc << 1);
                }
            }

            if (crc == p->crc_rx) {
                deliver(p);
            } else {
                p->stats.crc_errors++;
                p->stats.resyncs++;
            }
            p->state = PROTO_WAIT_SOF;
            break;
        }

        default:
            p->state = PROTO_WAIT_SOF;
            break;
        }
    }
}

size_t proto_build(uint8_t *out, size_t out_size, uint8_t type,
                   const uint8_t *payload, uint16_t len)
{
    if (len > PROTO_MAX_PAYLOAD) {
        return 0;
    }
    const size_t total = (size_t)len + PROTO_OVERHEAD;
    if (out_size < total) {
        return 0;
    }

    out[0] = PROTO_SOF;
    out[1] = type;
    proto_st_u16(&out[2], len);
    if (len > 0u && payload != NULL) {
        memcpy(&out[4], payload, len);
    }

    const uint16_t crc = proto_crc16(&out[1], (size_t)len + 3u);
    proto_st_u16(&out[4 + len], crc);
    return total;
}
