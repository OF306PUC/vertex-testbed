#include "prng.h"

static uint64_t pcg_state;
static uint64_t pcg_inc = 1u;

uint32_t prng_u32(void)
{
    uint64_t old = pcg_state;
    pcg_state = old * 6364136223846793005ULL + pcg_inc;
    uint32_t xorshifted = (uint32_t)(((old >> 18u) ^ old) >> 27u);
    uint32_t rot = (uint32_t)(old >> 59u);
    return (xorshifted >> rot) | (xorshifted << ((32u - rot) & 31u));
}

void prng_seed(uint64_t state, uint64_t sequence)
{
    pcg_state = 0u;
    pcg_inc = (sequence << 1u) | 1u;
    (void)prng_u32();
    pcg_state += state;
    (void)prng_u32();
}

float prng_uniform(void)
{
    return (float)(prng_u32() >> 8) * (1.0f / 16777216.0f);
}
