#ifndef ZSTUB_BT_HCI_VS_H
#define ZSTUB_BT_HCI_VS_H
#include <stdint.h>
#include <zephyr/bluetooth/hci.h>
#define BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL 0xFC0E
#define BT_HCI_VS_LL_HANDLE_TYPE_ADV      0x00
#define BT_HCI_VS_LL_HANDLE_TYPE_SCAN     0x01
#define BT_HCI_VS_LL_HANDLE_TYPE_CONN     0x02
struct bt_hci_cp_vs_write_tx_power_level {
    uint16_t handle; uint8_t handle_type; int8_t tx_power_level;
};
struct bt_hci_rp_vs_write_tx_power_level {
    uint8_t status; uint16_t handle; uint8_t handle_type; int8_t selected_tx_power;
};
#endif
