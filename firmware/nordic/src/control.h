#ifndef CONTROL_H_
#define CONTROL_H_

#include <stdint.h>

/**
 * @brief Frame dispatcher. Pass to uart_link_init() with the agent as `ctx`.
 *
 *     uart_link_init(control_on_frame, &agent);
 *
 * Payload formats are documented in agent.h; the envelope in proto.h.
 */
void control_on_frame(uint8_t type, const uint8_t *payload, uint16_t len, void *ctx);

/**
 * @brief Register a hook called whenever an accepted CONTROL frame lands.
 *
 * The run loops are semaphore-driven, and before this the network thread only
 * noticed a trigger when its idle poll next fired 
 *
 * main.c registers a hook that gives the network semaphore. Called from the
 * uart_link callback context, so the hook must be ISR-safe.
 */
void control_set_trigger_hook(void (*hook)(void));

#endif /* CONTROL_H_ */
