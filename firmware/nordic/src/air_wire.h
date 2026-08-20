/**
 * @file air_wire.h
 * @brief The on-air state packet. Mirrors `vertex/wire/codec.py`.
 *
 * ## v1 -- 16 bytes, little-endian
 *
 *     off  size  type    field
 *     0    1     uint8   version = 1
 *     1    1     uint8   flags: bit0 enabled, bit1 disturbance_on
 *     2    1     uint8   node_id, 1..255
 *     3    1     uint8   reserved, must be 0 (the host rejects nonzero, so v1.x
 *                        can claim it)
 *     4    2     uint16  seq, wraps mod 2^16
 *     6    4     int32   vstate, scaled by 1e6
 *     10   6     uint48  tx_time_us, microseconds since the experiment epoch
 *
 * The manufacturer-specific AD element's *value* is the two-byte company id then
 * this payload: 18 bytes. With the name element that is 9 + (2 + 18) = 29 of the
 * 31 available.
 *
 * ## tx_time_us is on the *host's* epoch, not this board's uptime
 *
 * ## v0 on receive
 *
 * Transmit is v1 only. Receive accepts v0 as well, mirroring the host's
 * `decode_any()`: a bench where half the boards are reflashed and half are not
 * should degrade to missing timestamps, not to a silent blackout.
 */

#ifndef AIR_WIRE_H_
#define AIR_WIRE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "common.h"

#define AIR_WIRE_V1_VERSION             1u
#define AIR_WIRE_V1_PAYLOAD_SIZE        16u
#define AIR_WIRE_V1_FLAG_ENABLED        0x01u
#define AIR_WIRE_V1_FLAG_DISTURBANCE    0x02u

/** Company id (2) + v1 payload (16). The AD element's value, what `bt_data_parse`
 *  hands back as `data->data` / `data->data_len`. */
#define AIR_WIRE_AD_VALUE_SIZE          (2u + AIR_WIRE_V1_PAYLOAD_SIZE)

/* v0: the 6-byte payload this firmware used to transmit. Decode only. */
#define AIR_WIRE_V0_PAYLOAD_SIZE        6u
#define AIR_WIRE_V0_FLAG_ENABLED        0x7Fu
#define AIR_WIRE_V0_FLAG_DISABLED       0x70u
#define AIR_WIRE_V0_AD_VALUE_SIZE       (2u + AIR_WIRE_V0_PAYLOAD_SIZE)

#define AIR_WIRE_OK              0
#define AIR_WIRE_ERR_LEN        (-1)    /* not a length either version uses */
#define AIR_WIRE_ERR_COMPANY    (-2)    /* somebody else's manufacturer data */
#define AIR_WIRE_ERR_FORMAT     (-3)    /* right length, unreadable content */

/** One agent's broadcast state, in the units the wire uses. */
typedef struct {
	uint8_t  node;
	bool     enabled;
	bool     disturbance_on;
	uint16_t seq;
	int32_t  vstate;            /* scaled by 1e6 */
	uint64_t tx_time_us;        /* experiment epoch; uint48 on the wire */
	/** Set by the decoder: false when the packet was v0, which carries no
	 *  sequence number and no timestamp. Those fields then read 0, and a consumer
	 *  must not treat that as "sent at time zero". */
	bool     has_seq_and_time;
} state_packet_type;

/**
 * @brief Encode a v1 AD element value: company id, then the payload.
 *
 * @return AIR_WIRE_AD_VALUE_SIZE, or AIR_WIRE_ERR_LEN if @p cap is too small.
 */
int air_wire_encode_v1(const state_packet_type *p, uint8_t *out, size_t cap);

/**
 * @brief Decode an AD element value, v1 or v0.
 *
 * @param value `data->data` from bt_data_parse -- company id included.
 * @return AIR_WIRE_OK, or a negative AIR_WIRE_ERR_*. @p out is untouched on failure.
 */
int air_wire_decode_any(const uint8_t *value, size_t len, state_packet_type *out);

#endif /* AIR_WIRE_H_ */
