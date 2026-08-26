#!/usr/bin/env python3
"""Report this host's transmit power on both radios.

Neither TX power is currently controlled, and until now neither was recorded:

* BLE on the Pi -- never set and never read. The Bluetooth specification has no
  way to SET transmit power for legacy advertising; that is a vendor command and
  Broadcom's is not public. So it can be measured, not chosen.
* BLE on the nRF -- broadcaster.c asks for +8 dBm through the Nordic vendor
  command. The controller may grant less; the granted value is logged to RTT and
  never reaches the host, because the production firmware does not answer
  STATS_REQ (PLATFORM.md 8.1, item A2).
* Wi-Fi -- readable and, unlike BLE, settable (`iw dev wlan0 set txpower`).

Run with the agents stopped; the BLE read needs the user channel.

    bash scripts/agents.sh stop
    sudo .venv/bin/python scripts/tx_power.py

NOTE: taking the user channel and releasing it hands `hci0` back to bluetoothd,
which powers the adapter UP -- and the user channel requires it DOWN. So the next
agent start fails with EBUSY. This script therefore puts the adapter back DOWN on
exit, and says so. If it is killed before that, run:

    sudo hciconfig hci0 down
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vertex.net import wlan_state                                   # noqa: E402
from vertex.radio.hci import (HciSocket, cmd_le_read_adv_tx_power,  # noqa: E402
                              cmd_reset)


def ble_tx_power() -> tuple[int | None, str]:
    try:
        sock = HciSocket(0).open()
    except Exception as exc:
        return None, f"cannot open hci0: {exc}"
    try:
        sock.command(cmd_reset())
        rp = sock.command(cmd_le_read_adv_tx_power())
        if not rp.params:
            return None, "controller returned no parameters"
        v = rp.params[0]
        return (v - 256 if v > 127 else v), "LE Read Advertising Channel Tx Power"
    except Exception as exc:
        return None, f"read refused: {exc}"
    finally:
        try:
            sock.close()
        except Exception:
            pass
        # bluetoothd reclaims and powers the adapter up the moment the user
        # channel is released, which blocks the next agent start. Put it back.
        subprocess.run(["hciconfig", "hci0", "down"],
                       capture_output=True, check=False)


def main() -> int:
    print("== BLE (this Pi's CYW43455)")
    dbm, how = ble_tx_power()
    if dbm is None:
        print(f"  advertising tx power   unavailable -- {how}")
    else:
        print(f"  advertising tx power   {dbm:+d} dBm   ({how})")
        print(f"  settable               no: vendor command only for legacy adv")

    print("\n== Wi-Fi")
    st = wlan_state() or {}
    tp = st.get("wlan_txpower_dbm")
    print(f"  txpower                {tp:+.1f} dBm" if tp is not None
          else "  txpower                unknown")
    print( "  settable               yes: iw dev <ifc> set txpower fixed <mBm>")

    if dbm is not None:
        st_after = subprocess.run(["hciconfig", "hci0"], capture_output=True,
                                  text=True, check=False).stdout
        state = "DOWN" if "DOWN" in st_after else ("UP" if "UP" in st_after else "?")
        print(f"  adapter left            {state}"
              f"{'  (correct for the user channel)' if state == 'DOWN' else ''}")
        if state != "DOWN":
            print("  -> run: sudo hciconfig hci0 down   before starting agents")

    print("\n== nRF52840 (peer board, for reference)")
    print("  requested              +8 dBm (broadcaster.h TX_POWER_LEVEL_BLE)")
    print("  granted                nRF52840 supports +8; nRF52832 caps at +4")
    print("  reported to host       NO -- RTT log only, no STATS handler")
    print("  read it with:          JLinkRTTClient, or west attach, at boot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
