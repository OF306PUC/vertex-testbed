#include "broadcaster.h"

// Register the logger for this module
LOG_MODULE_REGISTER(Module_Broadcaster, LOG_LEVEL_INF);

/**
 * Type variable to control many aspects of the advertising 
 * --> it could replace the default advertising parameters given by BT_LE_ADV_NCONN
 */

static const struct bt_le_adv_param *adv_param = BT_LE_ADV_NCONN; 
// static const struct bt_le_adv_param *adv_param =
// 	BT_LE_ADV_PARAM(BT_LE_ADV_OPT_NONE, /* No options specified */
// 	MIN_ADV_INTERVAL, 
// 	MAX_ADV_INTERVAL,
// 	NULL /* Set to NULL for undirected advertising */
// ); /* Set to NULL for undirected advertising */
            

void set_tx_power(uint8_t handle_type, uint16_t handle, int8_t tx_pwr_lvl)
{
	struct bt_hci_cp_vs_write_tx_power_level *cp; 
	struct bt_hci_rp_vs_write_tx_power_level *rp; 
	struct net_buf *buf, *rsp = NULL; 
	int err; 

	buf = bt_hci_cmd_create(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL,
		sizeof(*cp));
	if (!buf) {
		LOG_ERR("Unable to allocate command buffer\n");
		return;
	}

	cp = net_buf_add(buf, sizeof(*cp)); 
	cp->handle = sys_cpu_to_le16(handle); 
	cp->handle_type = handle_type; 
	cp->tx_power_level = tx_pwr_lvl; 

	err = bt_hci_cmd_send_sync(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL,
		buf, &rsp);
	if (err) {
		uint8_t reason = rsp ?
			((struct bt_hci_rp_vs_write_tx_power_level *)rsp->data)->status : 0;
		LOG_ERR("Set Tx power err: %d reason 0x%02X\n", err, reason);
		return;
	}

	rp = (void *)rsp->data;
	LOG_INF("Actual Tx Power: %d\n", rp->selected_tx_power); 
	net_buf_unref(rsp); 
}

/** 
 * Function that initialize the broadcaster with scan response
 */
int broadcaster_init(custom_data_type* custom_data)
{
	struct bt_data ad[] = {
		BT_DATA(BT_DATA_NAME_COMPLETE, DEVICE_NAME, DEVICE_NAME_LEN),
		BT_DATA(BT_DATA_MANUFACTURER_DATA, (unsigned char *)custom_data, sizeof(custom_data_type))
	};

	// Set 8 dBm Tx power
	set_tx_power(BT_HCI_VS_LL_HANDLE_TYPE_ADV, 0, TX_POWER_LEVEL_BLE); 

	// Min adv time is 100ms, max is 150ms for BT_LE_ADV_NCONN
    int err = bt_le_adv_start(adv_param, ad, ARRAY_SIZE(ad), ad, ARRAY_SIZE(ad));
	if (err) {
		LOG_ERR("Advertising failed to start (err %d)\n", err);
		return err;
	}
	LOG_INF("Advertising successfully started\n");
	return 0;
}

/**
 * Function that updates the broadcaster with scan response
 */
int broadcaster_update_scan_response_custom_data(custom_data_type* custom_data)
{
	struct bt_data ad[] = {
		BT_DATA(BT_DATA_NAME_COMPLETE, DEVICE_NAME, DEVICE_NAME_LEN),
		BT_DATA(BT_DATA_MANUFACTURER_DATA, (unsigned char *)custom_data, sizeof(custom_data_type))
	};
	int err = bt_le_adv_update_data(ad, ARRAY_SIZE(ad), ad, ARRAY_SIZE(ad));
	if (err) {
		LOG_ERR("Advertising failed to update (err %d)\n", err);
		return err;
	}

	return 0;
	
}

int broadcaster_stop(void)
{
    int err = bt_le_adv_stop();
    if (err) {
        LOG_ERR("Advertising failed to stop (err %d)", err);
        return err;
    }
    LOG_INF("Advertising stopped");
    return 0;
}
