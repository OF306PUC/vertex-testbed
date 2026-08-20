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

#endif /* CONTROL_H_ */
