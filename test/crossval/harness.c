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
 *
 * Prints one CSV row per step: step,state,vstate,vartheta,counter -- scaled
 * int32, exactly what the STATE frame carries.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "agent.h"
#include "coordination_task.h"

static struct agent agent;

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
    if (argc < 3) {
        fprintf(stderr, "usage: %s steps neighbor_vstate...  (frames on stdin)\n",
                argv[0]);
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

    /* Neighbours come from argv, not from a frame: they are the *inputs* to each
     * step, which on hardware arrive from the observer. */
    const int given = argc - 2;
    const uint8_t n = (uint8_t)(given > AGENT_MAX_NEIGHBORS
                                ? AGENT_MAX_NEIGHBORS : given);
    if (n > agent.params.n_neighbors) {
        fprintf(stderr, "%u neighbour values but NETWORK declared %u\n",
                n, agent.params.n_neighbors);
        return 1;
    }
    for (uint8_t j = 0; j < n; j++) {
        agent.vars.neighbor_vstates[j] = atoi(argv[2 + j]);
        agent.params.neighbors_enabled[j] = true;
    }

    for (int k = 0; k < steps; k++) {
        discrete_step(&agent);
        printf("%d,%d,%d,%d,%d\n", k, agent.vars.state, agent.vars.vstate,
               agent.vars.vartheta, agent.vars.counter);
    }
    return 0;
}
