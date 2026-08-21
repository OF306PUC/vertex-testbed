#include "broadcaster.h"
#include "air_wire.h"

#include <errno.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/hci_vs.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>

LOG_MODULE_REGISTER(Module_Broadcaster, LOG_LEVEL_INF);

/* The advertised name. */
#define DEVICE_NAME         CONFIG_BT_DEVICE_NAME
#define DEVICE_NAME_LEN     (sizeof(DEVICE_NAME) - 1)

/* Host-supplied advertising interval, 0.625 ms units. Zero means "not set", in
 * which case BT_LE_ADV_NCONN's default applies. */
static uint16_t adv_interval_min;
static uint16_t adv_interval_max;

static bool advertising;

/**
 * Set TX power through the Nordic vendor command.
 */
static void set_tx_power(uint8_t handle_type, uint16_t handle, int8_t tx_pwr_lvl)
{
	struct bt_hci_cp_vs_write_tx_power_level *cp;
	struct bt_hci_rp_vs_write_tx_power_level *rp;
	struct net_buf *buf, *rsp = NULL;

	buf = bt_hci_cmd_create(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL, sizeof(*cp));
	if (!buf) {
		LOG_ERR("Unable to allocate command buffer");
		return;
	}

	cp = net_buf_add(buf, sizeof(*cp));
	cp->handle = sys_cpu_to_le16(handle);
	cp->handle_type = handle_type;
	cp->tx_power_level = tx_pwr_lvl;

	int err = bt_hci_cmd_send_sync(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL, buf, &rsp);
	if (err) {
		uint8_t reason = rsp ?
			((struct bt_hci_rp_vs_write_tx_power_level *)rsp->data)->status : 0;
		LOG_ERR("Set Tx power err: %d reason 0x%02X", err, reason);
		if (rsp) {
			net_buf_unref(rsp);
		}
		return;
	}
	if (!rsp) {
		LOG_WRN("Set Tx power returned no response");
		return;
	}

	rp = (void *)rsp->data;
	/* Logged, not stored: the controller need not grant what was asked for -- the
	 * nRF52832 on the DK caps at +4 dBm -- */
	if (rp->selected_tx_power != tx_pwr_lvl) {
		LOG_WRN("Tx power %d dBm requested, %d dBm selected",
			tx_pwr_lvl, rp->selected_tx_power);
	} else {
		LOG_INF("Tx power: %d dBm", rp->selected_tx_power);
	}
	net_buf_unref(rsp);
}

int broadcaster_set_adv_params(uint16_t interval_min, uint16_t interval_max)
{
	if (interval_min == 0u || interval_min > interval_max) {
		return -EINVAL;
	}
	adv_interval_min = interval_min;
	adv_interval_max = interval_max;
	LOG_INF("adv interval: %u..%u units (%u..%u ms)",
		interval_min, interval_max,
		(unsigned)(interval_min * 625u / 1000u),
		(unsigned)(interval_max * 625u / 1000u));
	return 0;
}

/**
 * Serialise @p pkt and fill in the two AD elements.
 *
 * A function, not a macro: v1 has to be *encoded* rather than memcpy'd, because
 * its uint48 timestamp has no C type to lay out.
 *
 * Name(2+7) + manufacturer(2+18) = 29 of the 31 available. v0 used 19, so those
 * two spare bytes are now the entire margin -- no further element fits.
 *
 * @p value must outlive the bt_le_adv_* call: BT_DATA stores the pointer, it does
 * not copy the bytes.
 */
static int build_ad(const state_packet_type *pkt, uint8_t *value, size_t cap,
		    struct bt_data *ad)
{
	const int n = air_wire_encode_v1(pkt, value, cap);
	if (n < 0) {
		return -EINVAL;
	}
	/* Manufacturer element only. The Complete Local Name used to sit in front of
	 * it and nothing read it -- every receiver filters on the company id
	 * (air_wire_decode_any here, find_manufacturer on the Pi). It cost 9 of 31 AD
	 * bytes and 144 us of TX airtime per advertising event relative to a `bridge`
	 * agent, which sends no name. See PLATFORM.md 8b.A3. */
	ad[0] = (struct bt_data)BT_DATA(BT_DATA_MANUFACTURER_DATA, value, (uint8_t)n);
	return 0;
}

int broadcaster_init(const state_packet_type *pkt)
{
	if (advertising) {
		return 0;
	}

	uint8_t value[AIR_WIRE_AD_VALUE_SIZE];
	struct bt_data ad[1];

	if (build_ad(pkt, value, sizeof(value), ad)) {
		LOG_ERR("Could not encode the advertising payload");
		return -EINVAL;
	}

	set_tx_power(BT_HCI_VS_LL_HANDLE_TYPE_ADV, 0, TX_POWER_LEVEL_BLE);

	/* BT_LE_ADV_NCONN unless the host set an interval, in which case the same
	 * options with that interval. */
	struct bt_le_adv_param param = *BT_LE_ADV_NCONN;
	if (adv_interval_min != 0u) {
		param.interval_min = adv_interval_min;
		param.interval_max = adv_interval_max;
	}

	/* NULL scan-response data. Passing it made Zephyr advertise ADV_SCAN_IND
	 * instead of ADV_NONCONN_IND, so the board was scannable and an active
	 * scanner would exchange SCAN_REQ/SCAN_RSP with it -- TX airtime on both
	 * sides, and the exact mechanism this platform measures. It also carried
	 * nothing the advertisement did not. See PLATFORM.md 8b.A1. */
	int err = bt_le_adv_start(&param, ad, 1, NULL, 0);
	if (err) {
		LOG_ERR("Advertising failed to start (err %d)", err);
		return err;
	}
	advertising = true;
	LOG_INF("Advertising started");
	return 0;
}

int broadcaster_update(const state_packet_type *pkt)
{
	if (!advertising) {
		return -EAGAIN;
	}
	uint8_t value[AIR_WIRE_AD_VALUE_SIZE];
	struct bt_data ad[1];

	if (build_ad(pkt, value, sizeof(value), ad)) {
		return -EINVAL;
	}

	int err = bt_le_adv_update_data(ad, 1, NULL, 0);
	if (err) {
		LOG_WRN("Advertising failed to update (err %d)", err);
	}
	return err;
}

int broadcaster_stop(void)
{
	if (!advertising) {
		return 0;
	}
	int err = bt_le_adv_stop();
	if (err) {
		LOG_ERR("Advertising failed to stop (err %d)", err);
		return err;
	}
	advertising = false;
	LOG_INF("Advertising stopped");
	return 0;
}
