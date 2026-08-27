/*
 * TX power probe.
 *
 * One question: can this board be set to +8 dBm for advertising, and what does
 * the controller say it actually selected?
 *
 * The production firmware asks for TX_POWER_LEVEL_BLE (+8) in broadcaster.c and
 * logs the answer -- but to RTT, and only at the run trigger, so the value never
 * reaches the host and is awkward to see. This app does the same call standalone
 * and logs over UART, so a serial monitor at 115200 shows it on reset.
 *
 * It sweeps a range rather than asking once, because the interesting fact is not
 * "did +8 work" but "what does this controller do with each request": the nRF52840
 * grants +8, the nRF52832 caps at +4, and both silently return the value they
 * chose rather than failing.
 *
 *   west build -b nrf52840dk/nrf52840 test/nrf/txpower
 *   west flash
 *   # then: screen /dev/ttyACM0 115200      (or any serial monitor)
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/hci_vs.h>
#include <zephyr/sys/byteorder.h>

LOG_MODULE_REGISTER(txpower, LOG_LEVEL_INF);

/* Same value the production firmware requests -- broadcaster.h. */
#define REQUESTED_DBM 8

/* Advertising data is irrelevant to the measurement, but a handle must exist
 * before advertising TX power can be set, so the advertiser is started first. */
static const uint8_t ad_payload[] = { 0x59, 0x00, 0x00 };   /* company id + pad */
static const struct bt_data ad[] = {
	BT_DATA(BT_DATA_MANUFACTURER_DATA, ad_payload, sizeof(ad_payload)),
};

/* Returns the selected level, or INT8_MIN if the command failed. */
static int8_t write_tx_power(uint8_t handle_type, uint16_t handle, int8_t dbm)
{
	struct bt_hci_cp_vs_write_tx_power_level *cp;
	struct bt_hci_rp_vs_write_tx_power_level *rp;
	struct net_buf *buf, *rsp = NULL;
	int8_t selected = INT8_MIN;

	buf = bt_hci_cmd_create(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL, sizeof(*cp));
	if (!buf) {
		LOG_ERR("no command buffer");
		return selected;
	}
	cp = net_buf_add(buf, sizeof(*cp));
	cp->handle = sys_cpu_to_le16(handle);
	cp->handle_type = handle_type;
	cp->tx_power_level = dbm;

	const int err = bt_hci_cmd_send_sync(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL,
					     buf, &rsp);
	if (err) {
		LOG_ERR("request %+d dBm: command failed, err %d", dbm, err);
		if (rsp) {
			net_buf_unref(rsp);
		}
		return selected;
	}
	if (!rsp) {
		LOG_ERR("request %+d dBm: no response", dbm);
		return selected;
	}
	rp = (void *)rsp->data;
	selected = rp->selected_tx_power;
	net_buf_unref(rsp);
	return selected;
}

/* Read back what the controller reports for the advertising channel. This is the
 * same figure the host reads over HCI on the Pi side (LE Read Advertising
 * Physical Channel Tx Power, 0x2007), so the two are directly comparable. */
static void read_back(void)
{
	struct bt_hci_rp_vs_read_tx_power_level *rp;
	struct bt_hci_cp_vs_read_tx_power_level *cp;
	struct net_buf *buf, *rsp = NULL;

	buf = bt_hci_cmd_create(BT_HCI_OP_VS_READ_TX_POWER_LEVEL, sizeof(*cp));
	if (!buf) {
		return;
	}
	cp = net_buf_add(buf, sizeof(*cp));
	cp->handle = sys_cpu_to_le16(0);
	cp->handle_type = BT_HCI_VS_LL_HANDLE_TYPE_ADV;

	if (bt_hci_cmd_send_sync(BT_HCI_OP_VS_READ_TX_POWER_LEVEL, buf, &rsp)) {
		LOG_WRN("read back: not supported on this controller");
		return;
	}
	if (rsp) {
		rp = (void *)rsp->data;
		LOG_INF("read back:  advertising channel reports %+d dBm",
			rp->tx_power_level);
		net_buf_unref(rsp);
	}
}

int main(void)
{
	LOG_INF("=== nRF BLE TX power probe ===");

	int err = bt_enable(NULL);
	if (err) {
		LOG_ERR("bt_enable failed (%d) -- nothing further is possible", err);
		return 0;
	}
	LOG_INF("bluetooth up");

	/* Non-connectable, like the production advertiser, so the handle exists in
	 * the same state the real firmware sets power in. */
	err = bt_le_adv_start(BT_LE_ADV_NCONN, ad, ARRAY_SIZE(ad), NULL, 0);
	if (err) {
		LOG_ERR("bt_le_adv_start failed (%d)", err);
		return 0;
	}
	LOG_INF("advertising started (ADV_NONCONN_IND)");

	/* The question that was actually asked. */
	const int8_t got = write_tx_power(BT_HCI_VS_LL_HANDLE_TYPE_ADV, 0,
					  REQUESTED_DBM);
	if (got == INT8_MIN) {
		LOG_ERR("requested %+d dBm: FAILED", REQUESTED_DBM);
	} else if (got == REQUESTED_DBM) {
		LOG_INF("requested %+d dBm: GRANTED %+d dBm", REQUESTED_DBM, got);
	} else {
		LOG_WRN("requested %+d dBm: CAPPED to %+d dBm", REQUESTED_DBM, got);
	}
	read_back();

	/* What the whole ladder does, so the cap is visible rather than inferred
	 * from one data point. nRF52 supports -40..+8 in steps; anything else is
	 * rounded or clamped by the controller, and it tells you which. */
	LOG_INF("--- full ladder: requested -> selected ---");
	static const int8_t ladder[] = { -40, -20, -12, -8, -4, 0, 3, 4, 5, 8, 10, 20 };
	for (size_t i = 0; i < ARRAY_SIZE(ladder); i++) {
		const int8_t sel = write_tx_power(BT_HCI_VS_LL_HANDLE_TYPE_ADV, 0,
						  ladder[i]);
		if (sel == INT8_MIN) {
			LOG_INF("  %+4d dBm -> command failed", ladder[i]);
		} else {
			LOG_INF("  %+4d dBm -> %+4d dBm%s", ladder[i], sel,
				sel == ladder[i] ? "" : "   (adjusted)");
		}
		k_msleep(20);
	}

	/* Leave it where the production firmware leaves it, so a board flashed with
	 * this and then measured with a spectrum analyser matches the real thing. */
	const int8_t final = write_tx_power(BT_HCI_VS_LL_HANDLE_TYPE_ADV, 0,
					    REQUESTED_DBM);
	LOG_INF("--- restored to the production request: %+d dBm -> %+d dBm ---",
		REQUESTED_DBM, final);
	LOG_INF("advertising continues; reset the board to run this again");
	return 0;
}
