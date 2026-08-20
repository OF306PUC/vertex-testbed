/**
 * @file proto.h
 * @brief Framed binary protocol between the Raspberry Pi and this board.
 *
 * Frame layout, all multi-byte fields LITTLE-endian:
 *
 *     +------+------+--------+-----------------+--------+
 *     | SOF  | TYPE | LEN:2  | PAYLOAD[LEN]    | CRC:2  |
 *     | 0x7E |      |        |                 |        |
 *     +------+------+--------+-----------------+--------+
 *                   \___________________________/
 *                     CRC-16/CCITT-FALSE covers
 *                     TYPE, LEN and PAYLOAD
 *
 * Three properties this buys, none of which a bare type-byte-plus-length has:
 *
 * 1. A dedicated start-of-frame byte. With binary payloads, any payload byte can
 *    equal a type code, so using the type as the sync marker means a corrupted
 *    stream re-synchronises inside a payload and silently accepts garbage.
 * 2. A CRC. Resync after corruption is then *detected* rather than hoped for, and
 *    the error is counted instead of applied as configuration.
 * 3. One flat parser for every frame type. 
 *
 * Endianness is little to match the rest of the platform: the BLE payload
 * (`vertex/wire/codec.py`), `custom_data_type` in the coordination firmware, and
 * the Cortex-M itself are all little-endian. Two endiannesses in one system is a
 * standing invitation to a field that reads plausibly and is wrong.
 */

#ifndef PROTO_H_
#define PROTO_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PROTO_SOF               0x7Eu
#define PROTO_MAX_PAYLOAD       256u
#define PROTO_OVERHEAD          6u      /* SOF + TYPE + LEN(2) + CRC(2) */
#define PROTO_MAX_FRAME         (PROTO_MAX_PAYLOAD + PROTO_OVERHEAD)

/* Pi -> board */
#define PROTO_T_NETWORK         0x4Eu   /* 'N' */
#define PROTO_T_ALGORITHM       0x41u   /* 'A' -- note: 'A', not 0x61 ('a') */
#define PROTO_T_DISTURBANCE     0x44u   /* 'D' */
#define PROTO_T_CONTROL         0x53u   /* 'S' */
#define PROTO_T_ADV_TX          0x54u   /* 'T' -- advertise these AD bytes verbatim */
#define PROTO_T_RADIO           0x52u   /* 'R' -- set adv/scan parameters */
#define PROTO_T_PING            0x50u   /* 'P' */
#define PROTO_T_STATS_REQ       0x51u   /* 'Q' -- read counters */

/* board -> Pi */
#define PROTO_T_ADV_REPORT      0x72u   /* 'r' -- one advertising report */
#define PROTO_T_STATE           0x78u   /* 'x' -- one control step */
#define PROTO_T_ACK             0x6Bu   /* 'k' */
#define PROTO_T_ERR             0x65u   /* 'e' */
#define PROTO_T_PONG            0x70u   /* 'p' */
#define PROTO_T_STATS           0x71u   /* 'q' -- counter dump */

/* Payload sizes for the fixed-length types. Network is variable. */
#define PROTO_MAX_NEIGHBORS     16u
#define PROTO_NETWORK_MIN_LEN   2u      /* enabled + node_id, zero neighbours */
#define PROTO_NETWORK_MAX_LEN   (PROTO_NETWORK_MIN_LEN + PROTO_MAX_NEIGHBORS)
#define PROTO_ALGORITHM_LEN     36u     /* 9 x int32 */
#define PROTO_DISTURBANCE_LEN   29u     /* 1 x uint8 + 7 x int32 */
#define PROTO_CONTROL_LEN       1u
#define PROTO_RADIO_LEN         9u
#define PROTO_PING_LEN          0u
#define PROTO_STATS_REQ_LEN     0u

enum proto_state {
    PROTO_WAIT_SOF = 0,
    PROTO_WAIT_TYPE,
    PROTO_WAIT_LEN_LO,
    PROTO_WAIT_LEN_HI,
    PROTO_WAIT_PAYLOAD,
    PROTO_WAIT_CRC_LO,
    PROTO_WAIT_CRC_HI,
};

struct proto_stats {
    uint32_t frames_ok;
    uint32_t crc_errors;
    uint32_t len_errors;
    uint32_t resyncs;       /* bad type/length or failed CRC: frame discarded */
    uint32_t timeouts;      /* frames abandoned because the line went idle */
    uint32_t bytes_in;
};

/**
 * @brief Called once per validated frame. Payload is only valid for the call.
 */
typedef void (*proto_frame_cb)(uint8_t type, const uint8_t *payload,
                               uint16_t len, void *ctx);

struct proto_parser {
    enum proto_state state;
    uint8_t  type;
    uint16_t expected_len;
    uint16_t received_len;
    uint16_t crc_rx;
    uint8_t  payload[PROTO_MAX_PAYLOAD];
    proto_frame_cb cb;
    void *ctx;
    struct proto_stats stats;
};

void     proto_parser_init(struct proto_parser *p, proto_frame_cb cb, void *ctx);
void     proto_parser_reset(struct proto_parser *p);
void     proto_feed(struct proto_parser *p, const uint8_t *data, size_t len);
uint16_t proto_crc16(const uint8_t *data, size_t len);

/**
 * @brief True while a frame is partially received.
 *
 * The transport uses this to decide whether an idle line means "nothing to do"
 * or "a frame was cut short" -- see :c:func:`proto_parser_timeout`.
 */
static inline bool proto_parser_in_frame(const struct proto_parser *p)
{
    return p->state != PROTO_WAIT_SOF;
}

/**
 * @brief Abandon a partially received frame. Call when the line has been idle.
 *
 * Necessary because a mid-payload SOF byte is deliberately NOT treated as a new
 * frame: payloads are binary and contain every byte value, so honouring an
 * embedded 0x7E would corrupt every frame that happens to carry one.
 *
 * Recommended: 100 ms of silence while ``proto_parser_in_frame()``. Truncation on
 * USB CDC essentially only happens when the host dies or reopens the port
 * mid-write, so the timeout is a recovery path rather than a hot one.
 */
void     proto_parser_timeout(struct proto_parser *p);

/**
 * @brief Build a frame into @p out.
 * @return total bytes written, or 0 if @p out is too small or the payload too big.
 */
size_t proto_build(uint8_t *out, size_t out_size, uint8_t type,
                   const uint8_t *payload, uint16_t len);

/* Little-endian field accessors. Byte-wise, so they are alignment-safe:
 * a payload sits at an arbitrary offset in a buffer and a cast would fault
 * on a strict-alignment target. */
static inline uint16_t proto_ld_u16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline uint32_t proto_ld_u32(const uint8_t *p)
{
    return ((uint32_t)p[0]) | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline int32_t proto_ld_i32(const uint8_t *p)
{
    return (int32_t)proto_ld_u32(p);
}

static inline void proto_st_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)(v >> 8);
}

static inline void proto_st_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static inline void proto_st_u64(uint8_t *p, uint64_t v)
{
    for (unsigned i = 0; i < 8u; i++) {
        p[i] = (uint8_t)((v >> (8u * i)) & 0xFFu);
    }
}

#endif /* PROTO_H_ */
