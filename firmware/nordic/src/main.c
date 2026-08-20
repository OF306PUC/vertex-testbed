#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <dk_buttons_and_leds.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <string.h> 

#include "coordination_task.h"
#include "common.h"
#include "observer.h"
#include "broadcaster.h"
#include "serial.h"

// Register the logger for this module
LOG_MODULE_REGISTER(Module_Main, LOG_LEVEL_INF);

/**
 * Led status
 */
#define LED_STATUS                     DK_LED1
/**
 * Thread stack size and priority
 */
#define APP_STACK_SIZE                 3072
#define THREAD_SLOW_NETWORK_PRIORITY   7
#define THREAD_FAST_DYNAMICS_PRIORITY  5

// --- TIMER, SEMAPHORE AND MUTEX structs ---
static struct k_timer dynamics_timer;
static struct k_timer network_timer;
static struct k_mutex coordination_mutex;
static struct k_sem dynamics_sem;
static struct k_sem network_sem;

/**
 * Function declarations: --------------------------------------------------------------
 */
static void leds_init(void);                          // Auxiliary function for starting LEDs
static void bt_init(void);                            // Auxiliary function for starting Bluetooth
static void dynamics_thread(void);                    // Dedicated thread for 1ms dynamics (if not discrete time)
static void network_fetching_thread(void);            // Slow network/logging thread (body of original thread_coordination_app)
static void dynamics_timer_cb(struct k_timer *dummy); // High-frequency dynamics timer callback
static void network_timer_cb(struct k_timer *dummy);  // Slow network timer callback
/*
 * -------------------------------------------------------------------------------------
 */

/**
 * --- THREAD DEFINITIONS ---
 * The fast thread (P=5) handles the 1ms dynamics via a semaphore clock.
 * The slow thread (P=7) handles network I/O and logging.
 */
K_THREAD_DEFINE(dynamics_thread_id, APP_STACK_SIZE,
                dynamics_thread, NULL, NULL, NULL,
                THREAD_FAST_DYNAMICS_PRIORITY, 0, 0);

// Assuming thread_coordination_app should execute the thread_slow_network logic
K_THREAD_DEFINE(thread_coordination_id, APP_STACK_SIZE, network_fetching_thread,
                NULL, NULL, NULL, THREAD_SLOW_NETWORK_PRIORITY, 0, 0);

/**
 * Main thread (entry point of the program):
 */
int main(void) {
    int blink_status = 0;

    coordination_params_init(); // Initialize coordination parameters
    leds_init();
    bt_init();
    serial_init();

    // Original k_work_init and k_timer_init are replaced/modified:
    k_mutex_init(&coordination_mutex);
    k_sem_init(&dynamics_sem, 0, 1);
    k_sem_init(&network_sem,  0, 1);
    k_timer_init(&dynamics_timer, dynamics_timer_cb, NULL);
    k_timer_init(&network_timer,  network_timer_cb,  NULL);

    while (1) {
        dk_set_led(LED_STATUS, (++blink_status) % 2);
        k_sleep(K_MSEC(1000)); // Simple blink, non-critical
    }
}

/**
 * Function definitions:
 */
static void leds_init(void) {
    int err = dk_leds_init();
    if (err) {
        LOG_ERR("Status LED failed to start (err %d)\n", err);
        return;
    }
    LOG_INF("Status LED successfully started\n");
}

static void bt_init(void) {
    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("Bluetooth failed to start (err %d)\n", err);
        return;
    }
    LOG_INF("Bluetooth successfully started\n");
}

/**
 * --- FAST SIMULATION LOOP (Timer Handler) ---
 * Runs every 'coordination.dt' (e.g., 1ms) in an ISR context.
 */
static void dynamics_timer_cb(struct k_timer *dummy) {
    ARG_UNUSED(dummy);
    k_sem_give(&dynamics_sem);
}

static void network_timer_cb(struct k_timer *dummy) {
    ARG_UNUSED(dummy);
    k_sem_give(&network_sem);
}

/**
 * --- DEDICATED FAST AGENT DYNAMICS THREAD ---
 * Runs when signaled by the timer semaphore (P=5).
 */
static void dynamics_thread(void) {
    while (1) {
        k_sem_take(&dynamics_sem, K_FOREVER);

        k_mutex_lock(&coordination_mutex, K_FOREVER);
        if (coordination.running && coordination.enabled) {
            discrete_step(&coordination);
            
            custom_data_type custom_data = {
                MANUFACTURER_ID,
                coordination.enabled ? NETID_ENABLED : NETID_DISABLED,
                coordination.node,
                coordination.vstate
            };
            broadcaster_update_scan_response_custom_data(&custom_data);
        }
        k_mutex_unlock(&coordination_mutex);
    }
}

/**
 * --- SLOW NETWORK/LOGGING LOOP (Thread) ---
 * Driven by network_timer (period = coordination.clock), mirroring the fast
 * dynamics_thread pattern. Supports N consecutive runs without reboot.
 *
 * When running:  network_timer fires every clock ms → k_sem_give → thread wakes.
 * When idle:     network_timer is stopped → k_sem_take times out after 1000 ms
 *                so the thread can still detect a new 't1' trigger promptly.
 */
static void network_fetching_thread(void) {
    static coordination_params log_data_copy;
    static neighbor_info_type  neighbor_info;
    static bool was_running = false;

    while (1) {
        // Idle fallback of 1000 ms: wakes the thread if no timer tick arrives
        // (i.e. while not running) so it can detect a new trigger promptly.
        k_sem_take(&network_sem, K_MSEC(1000));

        // Snapshot shared flags under mutex.
        k_mutex_lock(&coordination_mutex, K_FOREVER);
        bool running    = coordination.running;
        bool first_time = coordination.first_time_running;
        bool observed   = coordination.all_neighbors_observed;
        k_mutex_unlock(&coordination_mutex);

        if (running) {
            if (first_time) {
                // 'n'/'a'/'p' arrive before 't1', so these fields are stable by now.
                custom_data_type initial_data = {
                    MANUFACTURER_ID,
                    coordination.enabled ? NETID_ENABLED : NETID_DISABLED,
                    coordination.node,
                    coordination.vstate
                };
                broadcaster_init(&initial_data);
                observer_init();
                k_timer_start(&dynamics_timer, K_MSEC(0), K_MSEC(coordination.dt));
                k_timer_start(&network_timer,  K_MSEC(coordination.clock), K_MSEC(coordination.clock));

                k_mutex_lock(&coordination_mutex, K_FOREVER);
                coordination.first_time_running = false;
                k_mutex_unlock(&coordination_mutex);

                was_running = true;
            }

            if (observed) {
                if (!k_msgq_get(&custom_observer_msg_queue, &neighbor_info, K_NO_WAIT)) {
                    k_mutex_lock(&coordination_mutex, K_FOREVER);
                    if (coordination.enabled) {
                        memcpy(coordination.neighbor_vstates, neighbor_info.vstates, sizeof(neighbor_info.vstates));
                        memcpy(coordination.neighbor_enabled, neighbor_info.enabled, sizeof(neighbor_info.enabled));
                    }
                    memcpy(&log_data_copy, &coordination, sizeof(coordination_params)); 
                    k_mutex_unlock(&coordination_mutex);
                    /**
                     * Log only when fresh neighbor data was received (event-based)
                     */
                    serial_log_coordination_task(&log_data_copy);
                }
            }

        } else {
            if (was_running) {
                k_timer_stop(&dynamics_timer);
                k_timer_stop(&network_timer);
                broadcaster_stop();
                observer_stop(); // also purges the message queue
                was_running = false;
            }
        }
    }
}
