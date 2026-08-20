#ifndef OBSERVER_H_
#define OBSERVER_H_

// Include modules
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/logging/log.h>
#include "common.h"
#include "coordination_task.h"
#include "serial.h"

// Define a type for the message queue
typedef struct {
    int32_t vstates[N_MAX_NEIGHBORS];   // to store the virtual state of the neighbors 
    bool enabled[N_MAX_NEIGHBORS];      // to store whether the neighbor is enabled or not
} neighbor_info_type;

// Declare the message queue as extern to be used outside the observer module
extern struct k_msgq custom_observer_msg_queue;

// Declare public functions
int observer_init(void);
int observer_stop(void);

#endif // OBSERVER_H_

