#include "serial.h" 

// Register the logger for this module
LOG_MODULE_REGISTER(Module_Serial, LOG_LEVEL_INF);

/**
 * Device pointer to the UART hardware
 */
const struct device *uart = DEVICE_DT_GET(DT_NODELABEL(uart0)); 

// Buffers for TX and RX data
static uint8_t tx_buf[TX_BUFF_SIZE];
static uint8_t rx_buf[RX_BUFF_SIZE];

/**
 * Function to send the logged data over serial:
 */
void serial_log_coordination_task(coordination_params* coordination) {
    // Only log when the algorithm is active
    if (!coordination->running) {
        return;
    }
    
    // Prepare the string format: "dtime,state,vstate,vartheta,neighbor_vstate1,neighbor_vstate2,...neighbor_vstateN\n\r"
    int64_t timestamp = k_uptime_get() - coordination->time0; 
    int len = snprintf(
        (char *)tx_buf, sizeof(tx_buf), 
        "d%lld,%d,%d,%d", 
        timestamp, 
        coordination->state, 
        coordination->vstate, 
        coordination->vartheta
    );

    // Append neighbor states to the buffer making sure we do not exceed de Tx buffer size
    for (int i = 0; i < coordination->N; i++) {
        len += snprintf((char *)tx_buf + len, sizeof(tx_buf) - len, ",%d", coordination->neighbor_vstates[i]); 
    }

    len += snprintf((char *)tx_buf + len, sizeof(tx_buf) - len, "\n\r"); 

    int err = uart_tx(uart, tx_buf, len, SYS_FOREVER_US);
    if (err) {
        LOG_WRN("uart_tx busy (err %d), log frame dropped", err);
    }
}

/**
 * Callback function to handle UART events
 */
static void uart_cb(const struct device *dev, struct uart_event *evt, void *user_data) {
    switch (evt->type) {

        case UART_RX_RDY: {
            
            // Temporary buffer to safely copy the received message for strtok parsing.
            char msg_buffer[RX_BUFF_SIZE];
            size_t rx_len = evt->data.rx.offset + evt->data.rx.len;
            
            // Check for line ending within the new received data
            for (int i=evt->data.rx.offset; i < rx_len; i++) {
                uint8_t c = evt->data.rx.buf[i];

                if (c == '\r' || c == '\n') {
                    
                    // --- SAFETY COPY AND NULL TERMINATION ---
                    // Copy data from rx_buf (which is re-used by the driver) into local msg_buffer.
                    size_t full_msg_len = (i < rx_len) ? i + 1 : rx_len;
                    full_msg_len = (full_msg_len < RX_BUFF_SIZE) ? full_msg_len : RX_BUFF_SIZE - 1;
                    strncpy(msg_buffer, (char *)evt->data.rx.buf, full_msg_len);
                    msg_buffer[full_msg_len] = '\0';
                    // ---------------------------------------

                    // The actual start of the message is the first byte of the copied buffer
                    uint8_t type = msg_buffer[0]; 
                    char *pt;
                    uint8_t cnt; 

                    switch (type) {

                        // When receiving network configuration type: 'n'
                        case 'n':
                            // Start parsing after the message type (at index 1)
                            pt = strtok(msg_buffer + 1, ",");
                            cnt = 0;

                            while (pt != NULL) {
                                uint8_t id = atoi(pt);

                                if (cnt == 0) {
                                    coordination.enabled = (id == 1);
                                } else if (cnt == 1) {
                                    coordination.node = id;
                                } else if (cnt - 2 < N_MAX_NEIGHBORS) {
                                    coordination.neighbors[cnt - 2] = id;
                                }

                                cnt++;
                                pt = strtok(NULL, ",");
                            }

                            coordination.N = (cnt >= 2) ? (uint8_t)(cnt - 2) : 0;
                            if (coordination.N > N_MAX_NEIGHBORS) {
                                coordination.N = N_MAX_NEIGHBORS;
                            }
                            LOG_INF("Network parameters updated over serial");
                            LOG_INF("Node ID: %d, Enabled: %d, Neighbors: ", coordination.node, coordination.enabled);
                            for (int k = 0; k < coordination.N; k++) {
                                LOG_INF("%d ", coordination.neighbors[k]);
                            }
                            break; 

                        // When receiving coordination trigger type: 't'
                        // Explicitly start/stop the coordination algorithm (reset of initial conditions)
                        case 't': 
                            if (msg_buffer[1] != '0') {
                                coordination.running = true; 
                                coordination.first_time_running = true; 
                                coordination.all_neighbors_observed = false;
                                coordination.disturbance.counter = 0;
                                coordination.time0 = k_uptime_get();
                                coordination.state = coordination.state0;
                                coordination.vstate = coordination.vstate0;
                                coordination.vartheta = coordination.vartheta0;
                                
                                for (int k = 0; k < N_MAX_NEIGHBORS; k++) {
                                    coordination.available_neighbors[k] = false;
                                    coordination.neighbor_vstates[k] = coordination.vstate0;
                                    coordination.neighbor_enabled[k] = false;
                                }
                                LOG_INF("Received 't1' (trigger). Coordination running"); 
                            } else {
                                coordination.running = false;
                                coordination.first_time_running = false;
                                LOG_INF("Received 't0' (stop). Coordination stopped");
                            }
                            break;

                        // When receiving core algorithm parameters: 'a' (6 parameters)
                        case 'a': 
                            pt = strtok(msg_buffer + 1, ","); 
                            cnt = 0; 
                            
                            while (pt != NULL) {
                                int32_t value = atoi(pt); 
                                switch (cnt) {
                                    case 0:  coordination.clock              = value;        break;
                                    case 1:  coordination.dt                 = value;        break; 
                                    case 2:  coordination.state0             = value;        break; 
                                    case 3:  coordination.vstate0            = value;        break; 
                                    case 4:  coordination.vartheta0          = value;        break; 
                                    case 5:  coordination.eta                = value;        break; 
                                    case 6:  coordination.alpha              = value;        break;
                                    case 7:  coordination.delta              = value;        break;
                                    case 8:  coordination.consensual_avg_law = (value == 1); break;
                                    default: break;
                                }
                                cnt++; 
                                pt = strtok(NULL, ",");
                            }
                            LOG_INF("Algorithm parameters updated over serial.");
                            LOG_INF("clock: %d, dt: %d, state0: %d, vstate0: %d, vartheta0: %d, eta: %d, alpha: %d, delta: %d, consensual_avg_law: %d",
                                coordination.clock, coordination.dt, coordination.state0, coordination.vstate0, coordination.vartheta0, coordination.eta,
                                coordination.alpha, coordination.delta, coordination.consensual_avg_law);
                            break;
                        
                        // When receiving disturbance related type: 'p' (8 parameters)
                        case 'p': 
                            pt = strtok(msg_buffer + 1, ","); 
                            cnt = 0; 
                            
                            while (pt != NULL) {
                                int32_t value = atoi(pt); 
                                switch (cnt) {
                                    case 0: coordination.disturbance.disturbance_on = (value == 1); break;
                                    case 1: coordination.disturbance.amplitude      = value;        break; 
                                    case 2: coordination.disturbance.offset         = value;        break; 
                                    case 3: coordination.disturbance.beta           = value;        break;
                                    case 4: coordination.disturbance.A              = value;        break;
                                    case 5: coordination.disturbance.frequency      = value;        break;
                                    case 6: coordination.disturbance.phase          = value;        break;
                                    case 7: coordination.disturbance.samples        = value;        break; 
                                    default: break;
                                }
                                cnt++; 
                                pt = strtok(NULL, ",");
                            }
                            LOG_INF("Disturbance parameters updated over serial.");
                            LOG_INF("Disturbance - on: %d, amplitude: %d, offset: %d, beta: %d, A: %d, frequency: %d, phase: %d, samples: %d", 
                                coordination.disturbance.disturbance_on, coordination.disturbance.amplitude, coordination.disturbance.offset, coordination.disturbance.beta,    
                                coordination.disturbance.A, coordination.disturbance.frequency, coordination.disturbance.phase, coordination.disturbance.samples);
                            break;
     
                        default: 
                            LOG_ERR("The received message has unknown type: %c.", type); 
                            break; 
                    }
                    
                    // Disable Rx to reset the offset of the Rx Buffer
                    uart_rx_disable(dev); 
                    break; 
                }
            }
            break; 
        } 
        
        case UART_RX_DISABLED: 
            // Re-enable RX after processing the message
            uart_rx_enable(dev, rx_buf, sizeof(rx_buf), RX_TIMEOUT); 
            break; 
        
        default: 
            break; 
    }
}

/**
 * Function to start serial:
 */
void serial_init() {

    if (!device_is_ready(uart)) {
        LOG_ERR("Serial not ready!"); 
        return; 
    }

    int err = uart_callback_set(uart, uart_cb, NULL); 
    if (err) {
        LOG_ERR("Serial failed to set callback (err %d)\n", err);
        return;
    }

    err = uart_rx_enable(uart, rx_buf, sizeof rx_buf, RX_TIMEOUT);
    if (err) {
        LOG_ERR("Serial failed to enable read (err %d)\n", err);
        return;
    }
    
    // DEBUG: Log initialization success
    LOG_INF("Serial successfully initialized and RX is enabled.");
    return;
}
