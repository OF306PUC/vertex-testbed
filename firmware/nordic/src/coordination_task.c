#include <math.h>
#include "coordination_task.h"

#include <zephyr/logging/log.h>
// Register the logger for this module
LOG_MODULE_REGISTER(Module_Consensus, LOG_LEVEL_INF);


static bool available_neighbors[N_MAX_NEIGHBORS] = {false};
static bool neighbor_enabled[N_MAX_NEIGHBORS] = {false};
static uint8_t neighbors[N_MAX_NEIGHBORS] = {0};
static int32_t neighbor_vstates[N_MAX_NEIGHBORS] = {0};

/**
 *  Global cooridination parameters instance
 */
coordination_params coordination; 

void coordination_params_init(void) {
    coordination.consensual_avg_law     = false;
    coordination.running                = false;                  
    coordination.enabled                = false;                 
    coordination.first_time_running     = true;                    // always true at the begging of the execution              
    coordination.all_neighbors_observed = false;                 
    coordination.available_neighbors    = available_neighbors;    
    coordination.node                   = 0;                     
    coordination.neighbors              = neighbors;              
    coordination.scale_factor           = 1e6f;                    // must be the same as in raspberry/algo.js      
    coordination.inv_scale_factor       = 1e-6f;                   // must be the same as in raspberry/algo.js      
    coordination.N                      = 0;                  
    coordination.time0                  = 0;                
    coordination.clock                  = 0;
    coordination.dt                     = 0;                   
    coordination.state0                 = 0;               
    coordination.vstate0                = 0;              
    coordination.vartheta0              = 0;            
    coordination.alpha                  = 0;                
    coordination.eta                    = 0;                  
    coordination.delta                  = 0;                
    coordination.state                  = 0;                
    coordination.vstate                 = 0;               
    coordination.vartheta               = 0;             
    coordination.active                 = 0;               
    coordination.epsilonON              = 0.01f;                   // must be the same as in raspberry/algo.js         
    coordination.epsilonOFF             = 0.05f;                   // must be the same as in raspberry/algo.js
    coordination.neighbor_enabled       = neighbor_enabled;       
    coordination.neighbor_vstates       = neighbor_vstates;      
    coordination.disturbance            = (disturbance_params){false, 0, 0, 0, 0, 0, 0, 0, 0};  
}


float disturbance(coordination_params* cp) {
    float nu = 0.0f;
    if (!cp->disturbance.disturbance_on) {
        nu = 0.0f; 
    } else {
        // Scaling factors
        float amp = (float)cp->disturbance.amplitude * cp->inv_scale_factor;
        float off = (float)cp->disturbance.offset * cp->inv_scale_factor;
        float beta = (float)cp->disturbance.beta * cp->inv_scale_factor;
        float A = (float)cp->disturbance.A * cp->inv_scale_factor; 
        float f = (float)cp->disturbance.frequency;                                    
        float phi_shift_s = (float)cp->disturbance.phase * cp->inv_scale_factor;       
        float t = (float)cp->disturbance.counter * (float)cp->dt * cp->inv_scale_factor; // dt must be scaled to seconds
        float m = amp * ((float)rand() / (float)RAND_MAX - off); 
        float sinusoidal = A * sinf(2.0f * M_PI * f * (t - phi_shift_s));
        nu = m + beta + sinusoidal;
    } 
    return nu; 
}

float sign(float x) {
    if (x > 0.0f) {
        return 1.0f;
    } else if (x < 0.0f) {
        return -1.0f;
    } else {
        return 0.0f;
    }
}

float max_of_two_non_negative_f(float a, float b) {
    float max_val = fmaxf(a, b);
    return fmaxf(0.0f, max_val); 
}

/**
 * Javier's coordination control law: g_i(z_i, v_i)
 */
float v_i(coordination_params* cp) {
    float vstate_f = (float)(cp->vstate * cp->inv_scale_factor);
    float vi = 0.0f;
    for (int j = 0; j < cp->N; j++) {
        if (cp->neighbor_enabled[j]) {
            float diff = vstate_f - (float)(cp->neighbor_vstates[j] * cp->inv_scale_factor);
            vi += -1.0f * sign(diff) * sqrtf(fabsf(diff));
        }
    }
    return vi;
}

/**
 * Consensus average control law: g_i(z_i, v_i)
 * - Assumes strongly connected and balanced graph to reach average consensus
 */
float g_i(coordination_params* cp){
    float z_f = (float)(cp->vstate * cp->inv_scale_factor);
    float vi = 0.0f; 
    for (int j = 0; j < cp->N; j++) {
        if (cp->neighbor_enabled[j]) {
            float diff = z_f - (float)(cp->neighbor_vstates[j] * cp->inv_scale_factor);
            vi += -1.0f * diff;
        }
    }
    return vi;
}

static inline float sanitize_f(float v) { return isfinite(v) ? v : 0.0f; }

void discrete_step(coordination_params* cp) {
    float dt      = (float)(cp->dt) * 1e-3f;
    float x       = sanitize_f((float)(cp->state    * cp->inv_scale_factor));
    float z       = sanitize_f((float)(cp->vstate   * cp->inv_scale_factor));
    float vartheta = sanitize_f((float)(cp->vartheta * cp->inv_scale_factor));

    float eta   = (float)(cp->eta   * cp->inv_scale_factor);
    float alpha = (float)(cp->alpha * cp->inv_scale_factor);
    float delta = (float)(cp->delta * cp->inv_scale_factor);

    float nu = disturbance(cp) * dt;
    float sigma = x - z;
    float grad  = sign(sigma);

    float gi = 0.0f;
    if (cp->consensual_avg_law) {
        gi = g_i(cp);
    } else {
        gi = v_i(cp);
    }
    gi = alpha * gi;

    float u = gi - vartheta * grad;
    float dvtheta = (fabsf(sigma) > delta) ? 1.0f : 0.0f;

    cp->state    = (int32_t)(sanitize_f(x + u + nu) * cp->scale_factor);
    cp->vstate   = (int32_t)(sanitize_f(z + gi)     * cp->scale_factor);
    int32_t eta_dvtheta = (int32_t)(eta * dvtheta * cp->scale_factor);
    cp->vartheta += eta_dvtheta;
    cp->disturbance.counter = (cp->disturbance.counter + 1) % cp->disturbance.samples;
}

void update_coordination(coordination_params* cp) {
    float dt = (float)(cp->dt) * 1e-3f; // Convert ms to seconds
    float x = (float)(cp->state * cp->inv_scale_factor);
    float z = (float)(cp->vstate * cp->inv_scale_factor);
    float vartheta = (float)(cp->vartheta * cp->inv_scale_factor);
    float eta = (float)(cp->eta * cp->inv_scale_factor);
    float alpha = (float)(cp->alpha * cp->inv_scale_factor);

    float nu = disturbance(cp);
    float sigma = x - z; 
    float grad = sign(sigma); 

    float gi = 0.0; 
    if (cp->consensual_avg_law) {
        gi = g_i(cp);
    } else { // Javier's law
        gi = v_i(cp);
    }

    gi = alpha * gi; // if (average consensus law) else v_i(cp) if (Javier's law)
    float ui = gi - vartheta * grad; 

    float dvtheta = 0.0f; 
    if (cp->active == 0){ 
        if ((float)fabs(sigma) > cp->epsilonON){
            cp->active = 1;
            dvtheta = eta * 1.0f; 
        } else {
            dvtheta = 0.0f; 
        }
    } else {
        if ((float)fabs(sigma) <= cp->epsilonOFF){
            cp->active = 0;
            dvtheta = 0.0f; 
        } else {
            dvtheta = eta * 1.0f; 
        }
    }
    cp->state = (int32_t)((x + dt * (ui + nu)) * cp->scale_factor);
    cp->vstate = (int32_t)((z + dt * gi) * cp->scale_factor);
    cp->vartheta = (int32_t)((vartheta + dt * dvtheta) * cp->scale_factor);
    cp->disturbance.counter = (cp->disturbance.counter + 1) % cp->disturbance.samples;
}
