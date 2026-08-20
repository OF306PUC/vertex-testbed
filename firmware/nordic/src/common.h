/**
 * @file common.h
 * @brief What the radio and the agent both have to agree on.
 *
 * Only the shared vocabulary. The on-air packet format lives in air_wire.h, serial
 * framing in proto.h, frame payloads in agent.h, and radio parameters with the
 * modules that own those radios.
 *
 * This header used to carry `custom_data_type`, a C struct memcpy'd onto the air.
 * Its compiler-chosen layout *was* the wire format, which is what pinned the
 * firmware to the host's v0 payload: there is no C type for the uint48 timestamp
 * v1 carries, so no struct could express v1 at all. air_wire.h serialises field by
 * field instead, and the two sides now speak the same version.
 */

#ifndef COMMON_H_
#define COMMON_H_

#include <stdint.h>

/** Nordic Semiconductor's assigned company id. Mirrored as COMPANY_ID in
 *  `vertex/wire/codec.py` -- the host filters on it before decoding anything. */
#define MANUFACTURER_ID     0x0059

/** Neighbours this board tracks. Bounds the observer's queue, the STATE payload
 *  and the agent's arrays, so it is the one number that must not drift. */
#define N_MAX_NEIGHBORS     4

#endif // COMMON_H_
