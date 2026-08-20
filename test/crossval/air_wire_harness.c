/**
 * Exercise the firmware's on-air codec from the host, so the real air_wire.c meets
 * the real vertex/wire/codec.py rather than a description of either.
 *
 * air_wire.c has no Zephyr dependency, so it links here unchanged.
 *
 *   enc <node> <enabled> <disturbance> <seq> <vstate> <tx_time_us>
 *       -> the AD element value this firmware would advertise, as hex
 *
 *   dec <hex>
 *       -> "rc,node,enabled,disturbance,seq,vstate,tx_time_us,has_seq_and_time"
 *          rc is the AIR_WIRE_* code; a negative rc means the rest is absent
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "air_wire.h"

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int main(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "enc") == 0 && argc == 8) {
        const state_packet_type p = {
            .node           = (uint8_t)atoi(argv[2]),
            .enabled        = atoi(argv[3]) != 0,
            .disturbance_on = atoi(argv[4]) != 0,
            .seq            = (uint16_t)strtoul(argv[5], NULL, 10),
            .vstate         = (int32_t)strtol(argv[6], NULL, 10),
            .tx_time_us     = strtoull(argv[7], NULL, 10),
        };
        uint8_t out[AIR_WIRE_AD_VALUE_SIZE];
        const int n = air_wire_encode_v1(&p, out, sizeof(out));
        if (n < 0) {
            fprintf(stderr, "encode failed: %d\n", n);
            return 1;
        }
        for (int i = 0; i < n; i++) {
            printf("%02x", out[i]);
        }
        printf("\n");
        return 0;
    }

    if (argc == 3 && strcmp(argv[1], "dec") == 0) {
        uint8_t buf[64];
        size_t n = 0;
        for (const char *q = argv[2]; q[0] && q[1]; q += 2) {
            const int hi = hexval(q[0]), lo = hexval(q[1]);
            if (hi < 0 || lo < 0 || n >= sizeof(buf)) {
                fprintf(stderr, "bad hex\n");
                return 2;
            }
            buf[n++] = (uint8_t)((hi << 4) | lo);
        }
        state_packet_type p = {0};
        const int rc = air_wire_decode_any(buf, n, &p);
        if (rc != AIR_WIRE_OK) {
            printf("%d\n", rc);
            return 0;
        }
        printf("%d,%u,%d,%d,%u,%d,%llu,%d\n", rc, p.node, (int)p.enabled,
               (int)p.disturbance_on, p.seq, p.vstate,
               (unsigned long long)p.tx_time_us, (int)p.has_seq_and_time);
        return 0;
    }

    fprintf(stderr, "usage: %s enc ... | %s dec <hex>\n", argv[0], argv[0]);
    return 2;
}
