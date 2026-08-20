#include "air_wire.h"

/* Little-endian byte pokes, local rather than borrowed from proto.h: that header
 * is the *serial* envelope, and coupling the radio format to it would mean a
 * change to one silently reaching the other. */
static inline void st_u16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)(v & 0xFFu);
	p[1] = (uint8_t)(v >> 8);
}

static inline uint16_t ld_u16(const uint8_t *p)
{
	return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline void st_u32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)(v & 0xFFu);
	p[1] = (uint8_t)((v >> 8) & 0xFFu);
	p[2] = (uint8_t)((v >> 16) & 0xFFu);
	p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static inline uint32_t ld_u32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
	       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline void st_u48(uint8_t *p, uint64_t v)
{
	for (unsigned i = 0; i < 6u; i++) {
		p[i] = (uint8_t)((v >> (8u * i)) & 0xFFu);
	}
}

static inline uint64_t ld_u48(const uint8_t *p)
{
	uint64_t v = 0;
	for (unsigned i = 0; i < 6u; i++) {
		v |= (uint64_t)p[i] << (8u * i);
	}
	return v;
}

int air_wire_encode_v1(const state_packet_type *p, uint8_t *out, size_t cap)
{
	if (cap < AIR_WIRE_AD_VALUE_SIZE) {
		return AIR_WIRE_ERR_LEN;
	}
	st_u16(&out[0], (uint16_t)MANUFACTURER_ID);

	uint8_t *v = &out[2];
	v[0] = (uint8_t)AIR_WIRE_V1_VERSION;
	v[1] = (uint8_t)((p->enabled        ? AIR_WIRE_V1_FLAG_ENABLED     : 0u) |
	                 (p->disturbance_on ? AIR_WIRE_V1_FLAG_DISTURBANCE : 0u));
	v[2] = p->node;
	v[3] = 0u;                          /* reserved; the host rejects nonzero */
	st_u16(&v[4], p->seq);
	st_u32(&v[6], (uint32_t)p->vstate);
	/* uint48: the top 16 bits of tx_time_us are dropped, which is 8.9 years of
	 * microseconds. Masked rather than rejected -- a run does not fail because a
	 * timestamp wrapped. */
	st_u48(&v[10], p->tx_time_us);
	return (int)AIR_WIRE_AD_VALUE_SIZE;
}

static int decode_v1(const uint8_t *v, state_packet_type *out)
{
	if (v[0] != (uint8_t)AIR_WIRE_V1_VERSION) {
		return AIR_WIRE_ERR_FORMAT;
	}
	if (v[3] != 0u) {
		/* Reserved byte. Rejected, not ignored, so a future v1.x that uses it
		 * cannot be silently misread as this version. Same rule as the host. */
		return AIR_WIRE_ERR_FORMAT;
	}
	if (v[2] == 0u) {
		return AIR_WIRE_ERR_FORMAT;         /* node id 0 is reserved */
	}

	out->node             = v[2];
	out->enabled          = (v[1] & AIR_WIRE_V1_FLAG_ENABLED) != 0u;
	out->disturbance_on   = (v[1] & AIR_WIRE_V1_FLAG_DISTURBANCE) != 0u;
	out->seq              = ld_u16(&v[4]);
	out->vstate           = (int32_t)ld_u32(&v[6]);
	out->tx_time_us       = ld_u48(&v[10]);
	out->has_seq_and_time = true;
	return AIR_WIRE_OK;
}

static int decode_v0(const uint8_t *v, state_packet_type *out)
{
	if (v[0] != AIR_WIRE_V0_FLAG_ENABLED && v[0] != AIR_WIRE_V0_FLAG_DISABLED) {
		return AIR_WIRE_ERR_FORMAT;
	}
	if (v[1] == 0u) {
		return AIR_WIRE_ERR_FORMAT;         /* node id 0 is reserved */
	}

	out->node           = v[1];
	out->enabled        = (v[0] == AIR_WIRE_V0_FLAG_ENABLED);
	out->disturbance_on = false;
	out->vstate         = (int32_t)ld_u32(&v[2]);
	/* v0 carries neither. Zeroed *and* flagged: a consumer that reads these
	 * without checking gets 0, and 0 is a legitimate timestamp. */
	out->seq              = 0u;
	out->tx_time_us       = 0u;
	out->has_seq_and_time = false;
	return AIR_WIRE_OK;
}

int air_wire_decode_any(const uint8_t *value, size_t len, state_packet_type *out)
{
	if (len < 2u) {
		return AIR_WIRE_ERR_LEN;
	}
	if (ld_u16(value) != (uint16_t)MANUFACTURER_ID) {
		return AIR_WIRE_ERR_COMPANY;
	}

	state_packet_type tmp = {0};
	int rc;

	/* Dispatch on length, then verify the version byte. Both formats are fixed
	 * size, so a length that matches neither is not a truncated packet worth
	 * guessing at. */
	if (len == AIR_WIRE_AD_VALUE_SIZE) {
		rc = decode_v1(&value[2], &tmp);
	} else if (len == AIR_WIRE_V0_AD_VALUE_SIZE) {
		rc = decode_v0(&value[2], &tmp);
	} else {
		return AIR_WIRE_ERR_LEN;
	}

	if (rc != AIR_WIRE_OK) {
		return rc;                      /* out untouched: no half-decoded packet */
	}
	*out = tmp;
	return AIR_WIRE_OK;
}
