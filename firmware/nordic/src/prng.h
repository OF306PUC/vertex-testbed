#ifndef PRNG_H
#define PRNG_H

#include <stdint.h>

/**
 * PCG32 (O'Neill's minimal xsh-rr 64/32), replacing rand().
 *
 * PCG32 is ~10 lines with a fully specified sequence, so `vertex/numeric/pcg32.py`
 * reproduces it exactly. 
 */
void prng_seed(uint64_t state, uint64_t sequence);
uint32_t prng_u32(void);

/**
 * Uniform on [0, 1), 24-bit mantissa.
 *
 * 24 bits, not 32: the result is then exactly representable in float32, so this
 * firmware and a float64 host agree on the value bit for bit rather than to
 * within a rounding.
 */
float prng_uniform(void);

#endif // PRNG_H
