/**
 * Drives the firmware's decoder and control law on the host so the Python
 * implementation can be compared against them step by step.
 *
 * agent.c, coordination_task.c and prng.c depend only on libc, so they link here
 * unchanged against test/common/zstubs. That matters: the thing under test is the
 * real firmware source, not a transcription of it.
 *
 * Configuration goes in as *encoded frames* on stdin rather than as struct
 * assignments, so agent.c's decoders are on the path too -- an offset that drifts
 * from the Python encoder fails here, not on the bench. Each line of stdin is one
 * hex-encoded payload prefixed by its type byte:
 *
 *     41 <72 hex chars>      ALGORITHM
 *     44 <58 hex chars>      DISTURBANCE
 *     4E <hex>               NETWORK
 *     53 <10 hex chars>      CONTROL
 *     4D <hex>               MICROGRID
 *
 * Prints one CSV row per step. Which row depends on the law the frames
 * selected, because the two families do not have the same state:
 *
 *     scalar     step,state,vstate,vartheta,counter
 *     microgrid  step,xP,xQ,zP,zQ,c,vartheta,S1,S2,watchdog
 *
 * Scaled int32 either way. The scalar row is exactly what the STATE frame
 * carries; the microgrid row is wider than any frame, because the check
 * compares integrator state the wire never sees.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "agent.h"
#include "coordination_task.h"

static struct agent agent;

/**
 * Engineering units -> scaled int32, rounding half toward +infinity.
 *
 * A copy of coordination_task.c's `quantize_f`, which is static there. The
 * duplication is deliberate: exporting it would widen the firmware's surface
 * for a test's convenience, and the rule is four lines whose agreement with
 * vertex/numeric.py::round_half_up is itself what the comparison checks.
 */
static int32_t q_f(float v)
{
    if (!isfinite(v)) {
        return 0;
    }
    const float scaled = v * SCALE_FACTOR;
    if (scaled >= 2147483520.0f)  { return INT32_MAX; }
    if (scaled <= -2147483520.0f) { return INT32_MIN; }
    const float f = floorf(scaled);
    return (int32_t)(((scaled - f) >= 0.5f) ? f + 1.0f : f);
}

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/** Decode a hex line into bytes. Returns the byte count, or -1. */
static int unhex(const char *line, uint8_t *out, size_t cap)
{
    size_t n = 0;
    for (const char *p = line; p[0] && p[1]; ) {
        if (*p == ' ' || *p == '\n' || *p == '\t') { p++; continue; }
        const int hi = hexval(p[0]), lo = hexval(p[1]);
        if (hi < 0 || lo < 0 || n >= cap) return -1;
        out[n++] = (uint8_t)((hi << 4) | lo);
        p += 2;
    }
    return (int)n;
}

int main(int argc, char **argv)
{
    /* Two arguments is legal: a pinned microgrid node with degree 0 has no
     * neighbours to supply, and refusing that would make the one
     * configuration a single board can run the one this cannot check. */
    if (argc < 2) {
        fprintf(stderr,
                "usage: %s steps [neighbor_vstate...]  (frames on stdin)\n"
                "  a neighbour is \"z\" for the scalar law, \"P,Q\" for the "
                "microgrid laws\n", argv[0]);
        return 2;
    }
    const int steps = atoi(argv[1]);

    agent_init(&agent);

    char line[1024];
    while (fgets(line, sizeof(line), stdin)) {
        uint8_t buf[512];
        const int n = unhex(line, buf, sizeof(buf));
        if (n < 1) continue;
        /* now_us is fixed, not read from a clock: the harness must be
         * deterministic, and vars.time_us only offsets the STATE timestamp. */
        const int rc = agent_apply_frame(&agent, buf[0], &buf[1],
                                         (uint16_t)(n - 1), 0);
        if (rc != AGENT_OK) {
            fprintf(stderr, "frame 0x%02X rejected: %d\n", buf[0], rc);
            return 1;
        }
    }

    if (!agent.params.running) {
        fprintf(stderr, "no CONTROL frame started the run\n");
        return 1;
    }

    /* A neighbour argument of the form @path means the values CHANGE, one
     * line per step, "P,Q P,Q". Fixed neighbours make `g` constant, and `g`
     * is what the LC interface feeds forward -- so a law can agree with the
     * host perfectly on a constant input and diverge on a realistic one.
     * That gap is not hypothetical: it is the only regime left unexplained
     * when a fleet run diverged and both fixed-input checks passed. */
    FILE *nbr_file = NULL;
    if (argc >= 3 && argv[2][0] == '@') {
        nbr_file = fopen(&argv[2][1], "r");
        if (nbr_file == NULL) {
            fprintf(stderr, "cannot open %s\n", &argv[2][1]);
            return 1;
        }
    }

    /* Neighbours come from argv, not from a frame: they are the *inputs* to each
     * step, which on hardware arrive from the observer. */
    const int given = nbr_file ? agent.params.n_neighbors : argc - 2;
    const uint8_t n = (uint8_t)(given > AGENT_MAX_NEIGHBORS
                                ? AGENT_MAX_NEIGHBORS : given);
    if (n > agent.params.n_neighbors) {
        fprintf(stderr, "%u neighbour values but NETWORK declared %u\n",
                n, agent.params.n_neighbors);
        return 1;
    }
    for (uint8_t j = 0; j < n; j++) {
        if (nbr_file) { agent.params.neighbors_enabled[j] = true; continue; }
        const char *s = argv[2 + j];
        agent.vars.neighbor_vstates[j] = atoi(s);
        /* "P,Q" carries the second coordinate. The scalar law never reads it,
         * so a bare "z" leaves it zero rather than being an error: one harness
         * serves both families and the frames decide which. */
        const char *comma = strchr(s, ',');
        agent.vars.neighbor_vstates_q[j] = comma ? atoi(comma + 1) : 0;
        agent.params.neighbors_enabled[j] = true;
    }

    /* Read once, after the frames: `law` is set by MICROGRID and reset by
     * NETWORK, so asking mid-loop would let the row format change shape. */
    const bool microgrid = (agent.params.law != LAW_FINITE_TIME_ADAPTIVE);

    char nline[512];
    for (int k = 0; k < steps; k++) {
        if (nbr_file) {
            if (fgets(nline, sizeof(nline), nbr_file) == NULL) {
                fprintf(stderr, "neighbour file ran out at step %d\n", k);
                return 1;
            }
            char *tok = strtok(nline, " \t\n");
            for (uint8_t j = 0; j < n && tok != NULL; j++) {
                agent.vars.neighbor_vstates[j] = atoi(tok);
                const char *c = strchr(tok, ',');
                agent.vars.neighbor_vstates_q[j] = c ? atoi(c + 1) : 0;
                tok = strtok(NULL, " \t\n");
            }
        }
        discrete_step(&agent);
        if (microgrid) {
            const struct microgrid_vars *v = &agent.vars.mg;
            printf("%d,%d,%d,%d,%d,%d,%d,%d,%d,%u\n", k,
                   q_f(v->xP), q_f(v->xQ), q_f(v->zP), q_f(v->zQ), q_f(v->c),
                   q_f(v->vartheta), q_f(v->S1_hat), q_f(v->S2_hat),
                   (unsigned)v->watchdog);
        } else {
            printf("%d,%d,%d,%d,%d\n", k, agent.vars.state, agent.vars.vstate,
                   agent.vars.vartheta, agent.vars.counter);
        }
    }
    return 0;
}
