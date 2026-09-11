/**
 * @file common.h
 * @brief What the radio and the agent both have to agree on.
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
