#ifndef COORDINATION_TASK_H
#define COORDINATION_TASK_H

#include <stdlib.h>
#include <stdint.h>
#include <zephyr/kernel.h>
#include "common.h"

#define M_PI 3.14159265358979323846f

/**
 * Custom type to store disturbance parameters sent by user through UART
 */
typedef struct {
    bool disturbance_on; 
    // Uniform disturbance parameters:
    int32_t offset;
    int32_t amplitude;
    // Constant disturbance parameter:
    int32_t beta;
    // Sinusoidal disturbance parameters:
    int32_t A;
    int32_t frequency;
    int32_t phase; 
    uint32_t counter; 
    uint32_t samples; 
} disturbance_params;

/**
 * Custom type to store coordination parameters sent by user through UART 
 */
typedef struct {
    bool consensual_avg_law;           // whether to use the consensual asymptotic average law or Javier's law 
    bool running;                      // whether the coordination algorithm is running or not
    bool enabled;                      // whether the coordination algorithm is enabled or not
    bool first_time_running;           // to initialize: first broadcasting, observing, timer start
    bool all_neighbors_observed;       // to check if all neighbors have been observed at least once
    bool* available_neighbors;         // array to track which neighbors have been observed
    uint8_t node;                      // node ID
    uint8_t* neighbors;                // array of neighbor IDs
    float scale_factor;                // scaling factor for fixed-point representation: 1.0f --> (uint32_t) 1e6
    float inv_scale_factor;            // inverse of the scaling factor: (uint32_t) 1e6 --> 1.0f
    uint8_t N;                         // number of neighbors
    int64_t time0;                     // internal clock time0   
    int32_t clock;                     // network fetching period (ms)
    int32_t dt;                        // dynamics timer and integration step (if Euler Solver is used) (ms)
    int32_t state0;                    // initial state
    int32_t vstate0;                   // initial vstate
    int32_t vartheta0;                 // initial vartheta
    int32_t alpha;                     // coordination task control gain (discrete time) --> vstates: z **(NEW)
    int32_t eta;                       // adaptation gain for vartheta 
    int32_t delta;                     // threshold for vartheta update (discrete time) **(NEW)
    int32_t state;                     // current state
    int32_t vstate;                    // current vstate
    int32_t vartheta;                  // current vartheta
    uint8_t active;                    // (hysteresis rule) whether the adaptation is active or not
    float epsilonON;                   // threshold to turn ON the adaptation
    float epsilonOFF;                  // threshold to turn OFF the adaptation
    bool* neighbor_enabled;            // array to track which neighbors are enabled
    int32_t* neighbor_vstates;         // array to store neighbor vstates
    disturbance_params disturbance;    // disturbance parameters
} coordination_params;

/**
 * Global coordination parameters instance
 */
extern coordination_params coordination;

void coordination_params_init(void); 
float sign(float x);
float max_of_two_non_negative_f(float a, float b);
float disturbance(coordination_params* cp);
float v_i(coordination_params* cp);
float g_i(coordination_params* cp); 
void discrete_step(coordination_params* cp);
void update_coordination(coordination_params* cp);

#endif // COORDINATION_TASK_H