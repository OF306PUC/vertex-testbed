/**
 * @file microgrid_task.c
 * @brief The microgrid benchmark on the board. See microgrid_task.h.
 */

#include <math.h>

#include "coordination_task.h"      /* SCALE_FACTOR, INV_SCALE_FACTOR */
#include "microgrid_task.h"

#define MG_X_WATCH          5.0e3f
#define MG_VARTHETA_WATCH   5.0e3f
#define MG_SAT_PERSIST      5u

static inline float u(int32_t v) { return (float)v * INV_SCALE_FACTOR; }
static inline float ms(int32_t v) { return (float)v * 1e-3f; }

static inline float sat_f(float v, float lim)
{
    return v > lim ? lim : (v < -lim ? -lim : v);
}

float mg_a_zeta(uint8_t profile, float t_s)
{
    /* eq:replication_profile / eq:persistent_profile. The replication one has
     * effectively vanished by t = 2 s, which is why it cannot answer the
     * terminal-policy question and the persistent one exists. */
    if (profile == 0u) {
        return 1e-3f * expf(-5.0f * t_s);
    }
    return 1e-3f * (0.75f + 0.25f * sinf(0.1f * t_s));
}

void mg_smooth_saturation(float p, float q, float p_max, float q_max,
                          float *ps, float *qs)
{
    /* A smooth limiter and not a clip, so the regressor stays differentiable
     * and its bound holds without assuming anything about the trajectory
     * after the fact. */
    *ps = p_max * tanhf(p / p_max);
    *qs = q_max * tanhf(q / q_max);
}

void microgrid_reset(struct agent *a)
{
    const struct microgrid_params *m = &a->params.microgrid;
    struct microgrid_vars *v = &a->vars.mg;
    const float kappa = u(m->kappa);

    /* x = X/kappa: the manifest carries the physical initial condition,
     * because that is where the nameplate constrains it. */
    v->xP = u(m->X0_P) / kappa;
    v->xQ = u(m->X0_Q) / kappa;
    v->zP = u(m->z0_P);
    v->zQ = u(m->z0_Q);
    v->c = u(m->c_0);
    /* Before the first packet a node has heard nobody, so the only term it
     * can form is its own pinned reference. */
    v->wP = u(m->b) * u(m->rP);
    v->wQ = u(m->b) * u(m->rQ);
    v->vartheta = u(m->vartheta_0);
    /* Never given the answer. `m->S1` is the emulator's true
     * parameter and is read by the plant and by nothing else;
     * the `_hat` is what the interface is trying to learn. The
     * two are one word of context apart in this file, and a slip
     * between them would make the law work perfectly and the
     * benchmark meaningless, so they do not share a name. */
    v->S1_hat = 0.0f;
    v->S2_hat = 0.0f;
    v->updates = 0u;
    v->watchdog = MG_WD_NONE;
    v->sat_run = 0u;

    /* The law's clock. `t = counter * h`, so a run that inherited a counter
     * would start mid-schedule, past T_o on a second trigger. It is reset
     * here rather than trusted to ALGORITHM's counter_0, because it belongs
     * to this law and not to the frame that configures the other one. */
    a->vars.counter = 0u;

    /* Publish the int32 mirrors now, not at the end of the first step.
     * report.c sends whatever is in them, so leaving them stale made the
     * first sample of every run carry the previous law's value -- the sample
     * at t = 0, which is the initial condition every plot starts from. */
    a->vars.state    = (int32_t)lrintf(v->xP * SCALE_FACTOR);
    a->vars.vstate   = (int32_t)lrintf(v->zP * SCALE_FACTOR);
    a->vars.vartheta = (int32_t)lrintf(v->vartheta * SCALE_FACTOR);
    a->vars.state_q  = (int32_t)lrintf(v->xQ * SCALE_FACTOR);
    a->vars.vstate_q = (int32_t)lrintf(v->zQ * SCALE_FACTOR);
}

/** alpha(t) = alpha_0 T_o / max(T_o - t, delta_o), continued past T_o. */
static float mg_alpha(const struct microgrid_params *m, float t_s)
{
    const float T_o = ms(m->T_o);
    const float d_o = ms(m->delta_o);
    float rem = T_o - t_s;
    if (rem < d_o) {
        rem = d_o;
    }
    return u(m->alpha_0) * T_o / rem;
}

/** Freeze w_i = sum_j a_ij z~_ij + b_i r for this tick. */
static void mg_hold(struct agent *a)
{
    const struct microgrid_params *m = &a->params.microgrid;
    float sP = 0.0f, sQ = 0.0f;

    for (uint8_t j = 0; j < a->params.n_neighbors; j++) {
        /* A neighbour whose packet was lost contributes its LAST value, not
         * nothing: the disagreement is defined over a fixed edge set, so
         * dropping a term changes the graph where holding a stale one only
         * ages the information. */
        sP += (float)a->vars.neighbor_vstates[j] * INV_SCALE_FACTOR;
        sQ += (float)a->vars.neighbor_vstates_q[j] * INV_SCALE_FACTOR;
    }
    a->vars.mg.wP = sP + u(m->b) * u(m->rP);
    a->vars.mg.wQ = sQ + u(m->b) * u(m->rQ);
}

void microgrid_step(struct agent *a)
{
    const struct microgrid_params *m = &a->params.microgrid;
    struct microgrid_vars *v = &a->vars.mg;
    const float h = ms(a->params.dt);
    const float t = (float)a->vars.counter * h;
    const float kappa = u(m->kappa);

    /* 1. the held snapshot, frozen for this tick */
    mg_hold(a);

    /* 2. the virtual candidate and the effective slope.
     *    D_i = sum_j a_ij + b_i, precomputed by the host. */
    const float D = (float)m->degree + u(m->b);
    const float f_o = mg_alpha(m, t) + u(m->kappa_o);
    const float rho_o = 2.0f * mg_alpha(m, t);
    const float a_o = v->c * f_o;
    const float den = 1.0f + h * a_o * D;
    const float etaP = D * v->zP - v->wP;
    const float etaQ = D * v->zQ - v->wQ;
    const float k = -a_o / den;
    const float gP = k * etaP, gQ = k * etaQ;
    const float zP_new = v->zP + h * gP;
    const float zQ_new = v->zQ + h * gQ;
    const float c_new = v->c + h * rho_o * u(m->xi) * f_o * f_o
                        * (etaP * etaP + etaQ * etaQ);

    /* 3. the interface, on the OLD physical, virtual and adaptive states */
    const float sP = v->xP - v->zP;
    const float sQ = v->xQ - v->zQ;
    const float mu_max = u(m->mu_max);
    float muP, muQ;
    bool adapt_now = false;
    const float trig = fabsf(sP) + fabsf(sQ);

    if (a->params.law == LAW_MICROGRID_LC) {
        const float T_c = ms(m->T_c);
        const bool terminal = (m->terminal != 0u) && (t >= T_c);
        float ps, qs;
        mg_smooth_saturation(kappa * v->xP, kappa * v->xQ,
                             u(m->p_max), u(m->q_max), &ps, &qs);
        const float A = mg_a_zeta(m->profile, t) / kappa;
        if (terminal) {
            /* eq:lc_source_terminal: zero input AND a frozen estimate. */
            muP = 0.0f;
            muQ = 0.0f;
        } else {
            float d_c = ms(m->delta_c);
            if (d_c <= 0.0f) {
                d_c = 2.0f * h;
            }
            float rem = T_c - t;
            if (rem < d_c) {
                rem = d_c;
            }
            const float lam = u(m->beta_c) / rem;
            muP = gP - lam * sP - A * (ps * v->S1_hat + qs * v->S2_hat);
            muQ = gQ - lam * sQ - A * (qs * v->S1_hat - ps * v->S2_hat);

            adapt_now = true;
            if (m->adapt_band >= 0 && trig > u(m->adapt_band)) {
                adapt_now = false;
            }
        }
        /* 4. saturate */
        const float aP = sat_f(muP, mu_max);
        const float aQ = sat_f(muQ, mu_max);
        const bool saturated = (aP != muP) || (aQ != muQ);

        /* The command is formed before the estimate moves. */
        if (adapt_now) {
            const float step = h * u(m->Phi) * kappa * kappa * A;
            v->S1_hat += step * (ps * sP + qs * sQ);
            v->S2_hat += step * (qs * sP - ps * sQ);
            v->updates++;
        }
        muP = aP;
        muQ = aQ;
        v->sat_run = saturated ? (uint16_t)(v->sat_run + 1u) : 0u;
    } else {
        /* eq:sampled_correction: the correction is CLIPPED, so past
         * vartheta >= |sigma|/h it equals sigma/h however much further the
         * gain grows. A drifting gain need not change the applied input. */
        const float lim = v->vartheta;
        const float wP_ = (sP == 0.0f) ? 0.0f
                        : (sP > 0.0f ? 1.0f : -1.0f)
                          * fminf(lim, fabsf(sP) / h);
        const float wQ_ = (sQ == 0.0f) ? 0.0f
                        : (sQ > 0.0f ? 1.0f : -1.0f)
                          * fminf(lim, fabsf(sQ) / h);
        muP = gP - wP_;
        muQ = gQ - wQ_;
        const float aP = sat_f(muP, mu_max);
        const float aQ = sat_f(muQ, mu_max);
        const bool saturated = (aP != muP) || (aQ != muQ);
        if (trig > u(m->eps)) {         /* eps = 0 selects C_AA */
            v->vartheta += u(m->gamma);
            v->updates++;
        }
        muP = aP;
        muQ = aQ;
        v->sat_run = saturated ? (uint16_t)(v->sat_run + 1u) : 0u;
    }

    /* 5. advance the emulator, on the APPLIED command */
    float ps, qs;
    mg_smooth_saturation(kappa * v->xP, kappa * v->xQ,
                         u(m->p_max), u(m->q_max), &ps, &qs);
    const float az = mg_a_zeta(m->profile, t) / kappa;
    const float zetaP = az * (ps * u(m->S1) + qs * u(m->S2));
    const float zetaQ = az * (qs * u(m->S1) - ps * u(m->S2));
    v->xP += h * (muP + zetaP);
    v->xQ += h * (muQ + zetaQ);

    /* 6. commit the virtual state. The adaptive state was committed above,
     *    after its command was formed. */
    v->zP = zP_new;
    v->zQ = zQ_new;
    v->c = c_new;

    /* eq:watchdog. Latching: a run does not recover. The saturation branch
     * arms only past T_c, because the transient saturates by design while
     * the plant is far from its share. */
    if (v->watchdog == MG_WD_NONE) {
        if (!isfinite(v->xP) || !isfinite(v->xQ)
            || fabsf(v->xP) > MG_X_WATCH || fabsf(v->xQ) > MG_X_WATCH) {
            v->watchdog = MG_WD_STATE;
        } else if (!isfinite(v->zP) || !isfinite(v->zQ)) {
            v->watchdog = MG_WD_VIRTUAL;
        } else if (v->vartheta > MG_VARTHETA_WATCH) {
            v->watchdog = MG_WD_VARTHETA;
        } else if (t >= ms(m->T_c) && v->sat_run > MG_SAT_PERSIST) {
            v->watchdog = MG_WD_SATURATION;
        }
    }

    /* The published mirror. report.c and the broadcaster read these and need
     * not know which law ran; the P coordinate goes where the scalar law's
     * single value went, so a v1 reader degrades rather than misreads. */
    a->vars.state = (int32_t)lrintf(v->xP * SCALE_FACTOR);
    a->vars.vstate = (int32_t)lrintf(v->zP * SCALE_FACTOR);
    a->vars.vartheta = (int32_t)lrintf(v->vartheta * SCALE_FACTOR);
    /* The second coordinate. Everything else the law computes -- g, mu,
     * sigma, theta, the estimator states -- is a function of (x, z, t) and
     * the parameters, so it is recoverable offline from these four and does
     * not need to cross the wire at 40 Hz. */
    a->vars.state_q = (int32_t)lrintf(v->xQ * SCALE_FACTOR);
    a->vars.vstate_q = (int32_t)lrintf(v->zQ * SCALE_FACTOR);
    a->vars.counter = (a->vars.counter + 1) % a->params.disturbance.samples;
}
