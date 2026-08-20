/**
 * @file observer.h
 * @brief Scanning: receive neighbours' vstates off the air.
 *
 * The scan callback runs in the Bluetooth RX thread. It therefore does **not**
 * touch `struct agent`: main.c's threads read and write that under
 * `coordination_mutex`, and the callback holds no lock, so writing the agent from
 * here was an unsynchronised write to fields another thread was mid-read on.
 *
 * Instead everything the agent needs to learn travels in `neighbor_info_type`
 * through the message queue, and main.c applies it under the mutex it already
 * holds. The agent is read-only here, and only for the neighbour-id table -- which
 * is written once by a NETWORK frame before the run starts.
 */

#ifndef OBSERVER_H_
#define OBSERVER_H_

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/kernel.h>

#include "agent.h"
#include "common.h"

/**
 * One snapshot of what the neighbours are saying.
 *
 * Indexed by position in the agent's neighbour list, not by node id: the mapping
 * happens once, in the callback, so nothing downstream has to search.
 */
typedef struct {
    int32_t  vstates[AGENT_MAX_NEIGHBORS];  /* virtual state, scaled by 1e6 */
    bool     enabled[AGENT_MAX_NEIGHBORS];  /* neighbour advertised as participating */
    int8_t   rssi[AGENT_MAX_NEIGHBORS];     /* last received signal strength, dBm */
    uint16_t seq[AGENT_MAX_NEIGHBORS];      /* sender's v1 sequence number; 0 from v0 */
    /** Bit i set once neighbour i has been heard at all this run. Cumulative and
     *  monotonic, so a consumer that misses a message cannot lose the fact. */
    uint32_t heard;
} neighbor_info_type;

extern struct k_msgq custom_observer_msg_queue;

/**
 * @brief Bind the agent whose neighbour table the callback reads.
 *
 * Explicit rather than a global with external linkage: Zephyr's scan callback
 * carries no user pointer, so the module must stash it, and passing it once at
 * startup keeps the dependency visible in main.c.
 */
void observer_bind(struct agent *a);

/** @brief Start scanning with the current parameters. Returns 0 if already on. */
int observer_init(void);

/** @brief Stop scanning, purge the queue, and forget which neighbours were heard. */
int observer_stop(void);

/**
 * @brief Set scan interval/window in 0.625 ms units.
 *
 * Was hardcoded to BT_GAP_SCAN_FAST_*. Loopback test B moved delivery ratio 75.8
 * points across the window alone, so this is an experimental parameter, not a
 * deployment default.
 *
 * Restarts the scanner only if it was already running. Configuration must not
 * switch the receiver on as a side effect: a RADIO frame arrives before CONTROL,
 * and scanning early changes the airtime baseline of the run being measured.
 */
int observer_set_scan_params(uint16_t interval, uint16_t window, bool active);

#endif // OBSERVER_H_
