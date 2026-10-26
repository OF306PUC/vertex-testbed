# Test peer firmware

A transparent bridge between the UART and the BLE advertising channel. It parses
no advertising data, filters no manufacturers, and suppresses no duplicates — it
captures raw bytes and hands them to the host, and advertises whatever bytes the
host gives it.

That is the whole design. Every layer of interpretation on a test peer is a place
for the test to agree with your bug.

```
   Pi  ──T frame (AD bytes)──►  peer  ──►  advertise verbatim
   Pi  ◄──r frame (report)───   peer  ◄──  every advertising report, raw
```

## Layout

| File | Zephyr? | What |
|---|---|---|
| `src/proto.h/.c` | no | frame format, CRC, parser, builder |
| `src/agent.h/.c` | no | agent state, configuration decoding |
| `src/uart_link.h/.c` | yes | async UART, ring buffer, TX queue, idle timeout |
| `src/ble_scan.h/.c` | yes | observer and commanded advertising |
| `src/main.c` | yes | wiring, frame dispatch, report thread |
| `tests/` | no | host tests for the two Zephyr-free units |

`proto` and `agent` are deliberately Zephyr-free. Framing and field decoding are
where protocol bugs live, and finding them on a laptop costs seconds:

```
$ make -C tests
proto framing tests
43 checks, 0 failures
agent configuration tests
70 checks, 0 failures
```

## Frame format

```
+------+------+--------+--------------+--------+
| SOF  | TYPE | LEN:2  | PAYLOAD[LEN] | CRC:2  |
| 0x7E |      |        |              |        |
+------+------+--------+--------------+--------+
              \______________________/
                CRC-16/CCITT-FALSE over TYPE, LEN, PAYLOAD
```

Little-endian throughout, matching the BLE payload, `custom_data_type`, and the
Cortex-M itself. Two endiannesses in one system is a standing invitation to a field
that reads plausibly and is wrong.

| Type | Dir | Payload |
|---|---|---|
| `N` 0x4E | → | `enabled:1, node_id:1, neighbours:0..16` |
| `A` 0x41 | → | 9 × int32: dt, clock, state₀, vstate₀, vartheta₀, counter₀, alpha, delta, eta |
| `D` 0x44 | → | `active:1` + 7 × int32: M, frequency, phase, O, O_offset, beta, samples |
| `S` 0x53 | → | `trigger:1` |
| `T` 0x54 | → | AD bytes, advertised verbatim |
| `R` 0x52 | → | `adv_min:2, adv_max:2, scan_int:2, scan_win:2, flags:1` |
| `P` 0x50 | → | — |
| `r` 0x72 | ← | `timestamp_us:8, rssi:1, addr_type:1, addr:6, adv_type:1, len:1, data` |
| `k` 0x6B | ← | `type:1, status:1` |
| `e` 0x65 | ← | `type:1, error:1` |
| `p` 0x70 | ← | `uptime_us:8` |

`A` carries **nine** int32s (36 bytes), including `counter₀`. `D` carries
**eight** fields (29 bytes), including `samples`. The prototype's header comment
listed eight and seven respectively; the code was right and the comment was not.

## Design notes worth knowing before changing anything

**One parser, dispatching on the type byte.** The prototype nested the frame state
machine inside a second machine cycling `N → A → D → S`, each state accepting only
its own magic byte. That made the four frames a fixed sequence: a lone `S` — which
is how you *stop a run* — could never be received unless the other three had
arrived first, in order, since reset. The type byte already identifies the frame;
type is data, not parser state.

**A mid-payload `0x7E` is not a frame start.** Payloads are binary and contain
every byte value. Honouring an embedded SOF would corrupt any frame carrying one.
The cost is that a truncated frame waits for bytes that never arrive — and
consumes the *following* frames as payload while it waits. `uart_link` therefore
arms a 100 ms idle timeout whenever `proto_parser_in_frame()`. That is not
optional; there is a host test for both halves of the behaviour.

**Queue overflows are counted, never silent.** `ble_scan.queue_dropped` and
`uart_link.tx_dropped` exist because an uncounted drop reaches the host's analysis
as *packet loss over the air* — the one measurement this board exists to make
trustworthy.

**Duplicate filtering is off** (`BT_LE_SCAN_OPT_NONE`), the opposite of the
coordination firmware. A suppressed duplicate is indistinguishable from a lost
packet, so filtering makes delivery ratio unmeasurable.

**Timestamps are captured first**, before any copying, so the value is the capture
instant rather than a measure of the handler.

## Will a short frame get stuck in the DMA buffer?

No — but the reason is a config option, so it is worth knowing where it lives.

`uart_rx_enable(dev, buf, size, timeout)` raises `UART_RX_RDY` on **either** the
buffer filling **or** the line going idle for `timeout`. The idle path is what
delivers a 42-byte config frame out of a 256-byte buffer. On nRF the driver can
only detect idle if it counts bytes mid-transfer, which requires:

```
CONFIG_UART_0_NRF_HW_ASYNC=y
CONFIG_UART_0_NRF_HW_ASYNC_TIMER=2
```

Without those the timeout never fires, `UART_RX_RDY` arrives only on a full
buffer, and a short frame waits until 214 more bytes happen to turn up. The
symptom is config commands being ignored, seemingly at random — one of the more
unpleasant things to debug from the far side of a UART.

**How to check it is working:** query `Q` and read `rx_partial_flushes`. A partial
flush means the idle timeout fired. If it is `0` after the host has sent frames,
byte counting is not active — check the two options above, and check that TIMER2
is not claimed by something else (the BLE controller uses TIMER0 and RTC0, so
TIMER1/2 are normally free, but verify rather than assume).

Two related points:

**The timeout is 1 ms, not 10.** A flush landing mid-frame is harmless: `proto_feed`
is a byte-stream parser that reassembles across chunk boundaries, and a host test
asserts byte-at-a-time input gives the same result as one bulk call. So there is
nothing to wait for, and a short timeout just lowers latency — ~4.6 ms end-to-end
for a 42-byte frame instead of ~13.6 ms.

**The buffer-swap gap is covered** by double buffering. `UART_RX_BUF_REQUEST` is
answered from the callback with the other buffer, so reception continues while the
filled one is being drained. With a single buffer, bytes arriving between "full"
and "re-armed" would be lost.

## Known limitation

`ble_adv_start()` accepts a channel map and ignores it: Zephyr's `bt_le_adv_param`
does not expose one. Restricting the peer to channel 39 needs either
`CONFIG_BT_CTLR_ADV_EXT` with extended advertising parameters, or a vendor HCI
command. The Pi side has full channel-map control through `HCI_CHANNEL_USER`, so
direction A can be swept without this; direction B cannot, yet.
