#ifndef ZSTUB_ATOMIC_H
#define ZSTUB_ATOMIC_H
#include <stdint.h>
typedef long atomic_t;
typedef long atomic_val_t;
atomic_val_t atomic_or(atomic_t *target, atomic_val_t value);
atomic_val_t atomic_and(atomic_t *target, atomic_val_t value);
atomic_val_t atomic_clear(atomic_t *target);
atomic_val_t atomic_get(const atomic_t *target);
atomic_val_t atomic_set(atomic_t *target, atomic_val_t value);
#endif
