/**
 * @file main.c
 * @brief Wiring and the two run loops.
 *
 * Owns the agent and hands it to the modules that need it: the control plane
 * through uart_link's `ctx`, the observer through observer_bind(). One owner makes
 * the initialisation order visible instead of hiding it behind a linker symbol.
 *
 * Two loops, both semaphore-driven off a k_timer:
 *
 *   dynamics_thread (P5, every `dt` ms)     -- one control step, then republish
 *   network_thread  (P7, every `clock` ms)  -- absorb neighbour data, send STATE
 *
 * `coordination_mutex` guards the agent between them. The Bluetooth RX thread is
 * deliberately not a third writer: the observer hands its findings over through a
 * message queue and the network thread applies them under the mutex. See
 * observer.h.
 */

#include <string.h>

#include <dk_buttons_and_leds.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "agent.h"
#include "broadcaster.h"
#include "common.h"
#include "control.h"
#include "coordination_task.h"
#include "observer.h"
#include "report.h"
#include "air_wire.h"
#include "uart_link.h"

LOG_MODULE_REGISTER(Module_Main, LOG_LEVEL_INF);

#define LED_STATUS                     DK_LED1
#define BLINK_OK_MS                    1000
#define BLINK_FAULT_MS                 100    /* fast blink: no control plane */

#define APP_STACK_SIZE                 3072
#define THREAD_SLOW_NETWORK_PRIORITY   7
#define THREAD_FAST_DYNAMICS_PRIORITY  5

/** Idle wake-up period while stopped. A safety net only: control.c wakes this
 *  thread directly when a CONTROL frame lands, so a trigger is not waited for at
 *  this granularity. */
#define IDLE_POLL_MS                   1000

/**
 * The agent. Single owner; every other module receives a pointer.
 */
static struct agent agent;

static struct k_timer dynamics_timer;
static struct k_timer network_timer;
static struct k_mutex coordination_mutex;
static struct k_sem   dynamics_sem;
static struct k_sem   network_sem;

static void leds_init(void);
static void bt_init(void);
static void dynamics_thread(void);
static void network_fetching_thread(void);
static void dynamics_timer_cb(struct k_timer *dummy);
static void network_timer_cb(struct k_timer *dummy);
static void on_control_frame(void);
static void absorb_neighbors(const neighbor_info_type *info);

K_THREAD_DEFINE(dynamics_thread_id, APP_STACK_SIZE,
                dynamics_thread, NULL, NULL, NULL,
                THREAD_FAST_DYNAMICS_PRIORITY, 0, 0);

K_THREAD_DEFINE(thread_coordination_id, APP_STACK_SIZE, network_fetching_thread,
                NULL, NULL, NULL, THREAD_SLOW_NETWORK_PRIORITY, 0, 0);

int main(void)
{
    int blink_status = 0;
    uint32_t blink_ms = BLINK_OK_MS;

    agent_init(&agent);
    observer_bind(&agent);
    leds_init();
    bt_init();

    k_mutex_init(&coordination_mutex);
    k_sem_init(&dynamics_sem, 0, 1);
    k_sem_init(&network_sem,  0, 1);
    k_timer_init(&dynamics_timer, dynamics_timer_cb, NULL);
    k_timer_init(&network_timer,  network_timer_cb,  NULL);

    control_set_trigger_hook(on_control_frame);
    if (uart_link_init(control_on_frame, &agent)) {
        LOG_ERR("UART link init failed -- no control plane");
        blink_ms = BLINK_FAULT_MS;
    }

    while (1) {
        dk_set_led(LED_STATUS, (++blink_status) % 2);
        k_sleep(K_MSEC(blink_ms));
    }
}

static void leds_init(void)
{
    int err = dk_leds_init();
    if (err) {
        LOG_ERR("Status LED failed to start (err %d)", err);
        return;
    }
    LOG_INF("Status LED started");
}

static void bt_init(void)
{
    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("Bluetooth failed to start (err %d)", err);
        return;
    }
    LOG_INF("Bluetooth started");
}

static void dynamics_timer_cb(struct k_timer *dummy)
{
    ARG_UNUSED(dummy);
    k_sem_give(&dynamics_sem);
}

static void network_timer_cb(struct k_timer *dummy)
{
    ARG_UNUSED(dummy);
    k_sem_give(&network_sem);
}

/**
 * Wake the network thread now, from control.c's frame callback.
 *
 * Without this the thread learns of a trigger only when its idle poll next fires,
 * so the first control step lands anywhere in a 0..IDLE_POLL_MS window. 
 */
static void on_control_frame(void)
{
    k_sem_give(&network_sem);
}

/**
 * Build the packet to advertise. Caller holds the mutex.
 */
static state_packet_type on_air_packet(int64_t uptime_us)
{
    uint64_t tx_time_us = 0u;
    if (agent.params.epoch_us != 0u) {
        int64_t elapsed_us = uptime_us - agent.vars.time_us;
        if (elapsed_us < 0) {
            elapsed_us = 0;
        }
        tx_time_us = agent.params.epoch_us + (uint64_t)elapsed_us;
    }
    return (state_packet_type){
        .node           = agent.params.node_id,
        .enabled        = agent.params.enabled,
        .disturbance_on = agent.params.disturbance.active,
        .seq            = agent.vars.tx_seq,
        .vstate         = agent.vars.vstate,
        .tx_time_us     = tx_time_us,
    };
}

/**
 * --- CONTROL LOOP --- every `dt`
 *
 * Absorb whatever the observer has heard, then take one step. Deliberately no
 * I/O: this is the thread whose period the experiment depends on, and an HCI
 * round trip or a UART write here shows up as jitter in the control period.
 *
 * Absorbing at `dt` rather than at `clock` is what makes this symmetric with a Pi
 * agent, which folds a packet in the moment it arrives. Absorbing at `clock` meant
 * the law took `clock/dt` steps against neighbour values up to a full second old.
 */
static void dynamics_thread(void)
{
    static neighbor_info_type neighbor_info;

    while (1) {
        k_sem_take(&dynamics_sem, K_FOREVER);

        /* Non-blocking: a period with no new neighbour data is normal. */
        const bool fresh_data =
            k_msgq_get(&custom_observer_msg_queue, &neighbor_info, K_NO_WAIT) == 0;

        k_mutex_lock(&coordination_mutex, K_FOREVER);
        if (agent.params.running && agent.params.enabled) {
            if (fresh_data) {
                absorb_neighbors(&neighbor_info);
            }
            discrete_step(&agent);
        }
        k_mutex_unlock(&coordination_mutex);
    }
}

/**
 * Fold one observer snapshot into the agent. Caller holds the mutex.
 */
static void absorb_neighbors(const neighbor_info_type *info)
{
    if (!agent.params.enabled) {
        return;
    }
    memcpy(agent.vars.neighbor_vstates, info->vstates, sizeof(info->vstates));
    memcpy(agent.params.neighbors_enabled, info->enabled, sizeof(info->enabled));

    uint8_t seen = 0;
    for (uint8_t i = 0; i < agent.params.n_neighbors; i++) {
        const bool heard = (info->heard & (1u << i)) != 0u;
        agent.params.available_neighbors[i] = heard;
        if (heard) {
            seen++;
        }
    }
    agent.params.all_neighbors_observed = (seen == agent.params.n_neighbors);
}

/**
 * --- NETWORK / REPORTING LOOP ---
 * Driven by network_timer at `clock` ms. Supports N consecutive runs without a
 * reboot: the trigger comes back through a CONTROL frame.
 */
static void network_fetching_thread(void)
{
    static struct agent log_data_copy;
    static bool was_running = false;
    /* Publish on every `publish_every`-th tick of this loop, which runs at `dt`. */
    static uint32_t publish_every = 1u;
    static uint32_t tick = 0u;

    while (1) {
        /* Idle fallback so a new trigger is noticed while the timer is stopped. */
        k_sem_take(&network_sem, K_MSEC(IDLE_POLL_MS));

        k_mutex_lock(&coordination_mutex, K_FOREVER);
        const bool running    = agent.params.running;
        const bool first_time = agent.params.first_time_running;
        k_mutex_unlock(&coordination_mutex);

        if (!running) {
            if (was_running) {
                k_timer_stop(&dynamics_timer);
                k_timer_stop(&network_timer);
                broadcaster_stop();
                observer_stop();        /* also purges the message queue */
                was_running = false;
            }
            continue;
        }

        if (first_time) {
            /* NETWORK/ALGORITHM/DISTURBANCE arrive before CONTROL, so these
             * fields are stable by now. */
            k_mutex_lock(&coordination_mutex, K_FOREVER);
            const state_packet_type initial_data =
                on_air_packet(k_ticks_to_us_floor64(k_uptime_ticks()));
            const int32_t dt_ms    = agent.params.dt;
            const int32_t clock_ms = agent.params.clock;
            agent.params.first_time_running = false;
            k_mutex_unlock(&coordination_mutex);

            broadcaster_init(&initial_data);
            observer_init();
            k_timer_start(&dynamics_timer, K_MSEC(0), K_MSEC(dt_ms));
            /* Both loops tick at `dt`. Publishing is every `publish_every`-th tick
             * of this one, so `clock` still sets the publish period -- see below. */
            k_timer_start(&network_timer, K_MSEC(dt_ms), K_MSEC(dt_ms));
            publish_every = (dt_ms > 0) ? (uint32_t)(clock_ms / dt_ms) : 1u;
            if (publish_every == 0u) {
                publish_every = 1u;     /* clock < dt: publish every step */
            }
            tick = 0u;
            was_running = true;
        }

        k_mutex_lock(&coordination_mutex, K_FOREVER);
        /* A real snapshot: struct agent holds its neighbour arrays inline, where
         * the struct this replaced held pointers. */
        memcpy(&log_data_copy, &agent, sizeof(agent));
        k_mutex_unlock(&coordination_mutex);

        /* REPORT every `dt`, matching a Pi agent, which logs a sample per control
         * step. Reporting at `clock` made the ble trajectory `clock/dt` times
         * coarser than the wifi one in the same run -- a resolution difference
         * across the axis being compared. At 25 Hz with four neighbours this is
         * ~1.3 kB/s against 11.5 kB/s of UART. */
        report_state(&log_data_copy);

        /* PUBLISH every `clock`, matching a Pi agent's publish loop. It used to
         * happen once per control step, so an nRF put `clock/dt` times more
         * traffic on the air than a Pi did -- more airtime, and more chances to
         * get past a receiver's duplicate filter, which is the leading suspect
         * for the 0.65 freshness on the one bridge-to-nRF link. */
        if (++tick >= publish_every) {
            tick = 0u;
            state_packet_type pkt;
            bool publish = false;

            k_mutex_lock(&coordination_mutex, K_FOREVER);
            if (agent.params.running && agent.params.enabled) {
                /* Incremented per PUBLISH, never per step. A sequence number that
                 * advanced per step while only every Nth packet went out would
                 * read at the receiver as (N-1)/N of the traffic lost. */
                agent.vars.tx_seq++;
                pkt = on_air_packet(k_ticks_to_us_floor64(k_uptime_ticks()));
                publish = true;
            }
            k_mutex_unlock(&coordination_mutex);

            /* Outside the mutex: an HCI round trip must not block the control
             * thread, which needs this lock every `dt`. */
            if (publish) {
                (void)broadcaster_update(&pkt);
            }
        }
    }
}
