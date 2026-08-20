/* Crude Zephyr stubs. Types are deliberately minimal -- only the NAMES need to
 * be right, so `gcc -fsyntax-only` can catch undeclared identifiers, missing
 * declarations and use-before-declare in our own code. Not for building. */
#ifndef ZSTUB_KERNEL_H
#define ZSTUB_KERNEL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <errno.h>

#define ARG_UNUSED(x) ((void)(x))
#define IS_ENABLED(x) 0
#define SYS_FOREVER_US (-1)

typedef struct { int _; } k_timeout_t;
#define K_NO_WAIT   ((k_timeout_t){0})
#define K_FOREVER   ((k_timeout_t){0})
#define K_MSEC(x)   ((k_timeout_t){0})
#define K_USEC(x)   ((k_timeout_t){0})

struct k_msgq { int _; };
#define K_MSGQ_DEFINE(name, sz, cnt, align) struct k_msgq name
int k_msgq_put(struct k_msgq *q, const void *data, k_timeout_t t);
int k_msgq_get(struct k_msgq *q, void *data, k_timeout_t t);
void k_msgq_purge(struct k_msgq *q);
unsigned int k_msgq_num_used_get(struct k_msgq *q);

struct k_work;
typedef void (*k_work_handler_t)(struct k_work *work);
struct k_work { int _; };
struct k_work_delayable { int _; };
#define K_WORK_DEFINE(name, fn) struct k_work name
#define K_WORK_DELAYABLE_DEFINE(name, fn) struct k_work_delayable name
int k_work_submit(struct k_work *w);
int k_work_reschedule(struct k_work_delayable *w, k_timeout_t t);
int k_work_cancel_delayable(struct k_work_delayable *w);

#define K_THREAD_DEFINE(tid, stack, fn, p1, p2, p3, prio, opt, delay) \
    extern int tid##_unused_
int64_t k_uptime_get(void);
int64_t k_uptime_ticks(void);
int64_t k_ticks_to_us_floor64(int64_t t);
int64_t k_ticks_to_us_near64(int64_t t);
void k_sleep(k_timeout_t t);
struct k_timer { int _; };
void k_timer_start(struct k_timer *t, k_timeout_t d, k_timeout_t p);
struct k_sem { int _; };
int k_sem_take(struct k_sem *s, k_timeout_t t);
void k_sem_give(struct k_sem *s);

struct net_buf_simple { uint8_t *data; uint16_t len; };
#endif
