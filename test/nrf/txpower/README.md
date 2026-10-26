# TX power probe

One question: **can this board be set to +8 dBm for advertising, and what does the
controller say it actually selected?**

The production firmware asks for the same value in `broadcaster.c` and logs the
answer — but to RTT, and only at the run trigger, so it never reaches the host and
is awkward to see. This app makes the same call standalone and **logs over UART**,
so any serial monitor shows it.

```
west build -b nrf52840dk/nrf52840 test/nrf/txpower
west flash
screen /dev/ttyACM0 115200        # or minicom, picocom, the Arduino monitor...
```

Press reset to run it again.

## Why UART here and RTT in the production firmware

The production app keeps `uart0` exclusively for the binary frame protocol — log
text interleaved with framed bytes corrupts them, and the CRC then rejects frames
that were never damaged on the wire. So it sets `CONFIG_UART_CONSOLE=n` and logs to
RTT. This probe runs no protocol on `uart0`, so it can use the console, which is
the entire point of it.

## What it prints

```
=== nRF BLE TX power probe ===
bluetooth up
advertising started (ADV_NONCONN_IND)
requested +8 dBm: GRANTED +8 dBm
read back:  advertising channel reports +8 dBm
--- full ladder: requested -> selected ---
   -40 dBm ->  -40 dBm
   ...
    +8 dBm ->   +8 dBm
   +10 dBm ->   +8 dBm   (adjusted)
```

The ladder matters more than the single answer: the controller does not reject an
out-of-range request, it silently returns the value it chose. `+10 -> +8` is the
cap becoming visible. On an nRF52832 the same sweep tops out at `+4`.

It advertises `ADV_NONCONN_IND` — the production type — because TX power is set
against an advertising handle and the handle must be in the same state as the real
firmware's for the answer to transfer.

It finishes by restoring the production request, so a board flashed with this and
then measured on a spectrum analyser matches what the real firmware radiates.

## Note

This exists because the value has no route to the host. With the `STATS_REQ`
handler implemented (PLATFORM.md §8.1, item A2) the granted power would land in
every run's environment block like every other radio parameter, and this probe
would be a convenience rather than the only way to find out.
