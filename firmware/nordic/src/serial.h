#ifndef SERIAL_H 
#define SERIAL_H

#include <stdlib.h>
#include <string.h> 
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include "common.h"
#include "coordination_task.h"

// Definitions
#define TX_BUFF_SIZE    64
#define RX_BUFF_SIZE    64
#define RX_TIMEOUT      25000   // 25ms inactivity timeout; must be < SERIAL_DELAY on the Pi side (currently 50ms)

// Declare public functions
void serial_init(void);
void serial_log_coordination_task(coordination_params* coordination);

#endif // SERIAL_H