#ifndef ZSTUB_ATOMIC_H
#define ZSTUB_ATOMIC_H
typedef long atomic_t;
typedef long atomic_val_t;
atomic_val_t atomic_add(atomic_t *t, atomic_val_t v);
atomic_val_t atomic_set(atomic_t *t, atomic_val_t v);
#endif
