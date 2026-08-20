#ifndef ZSTUB_BT_HCI_H
#define ZSTUB_BT_HCI_H
#include <zephyr/bluetooth/bluetooth.h>
struct net_buf { uint8_t *data; uint16_t len; };
struct net_buf *bt_hci_cmd_create(uint16_t opcode, uint8_t param_len);
void *net_buf_add(struct net_buf *buf, size_t len);
void net_buf_unref(struct net_buf *buf);
int bt_hci_cmd_send_sync(uint16_t opcode, struct net_buf *buf, struct net_buf **rsp);
#define sys_cpu_to_le16(x) (x)
#endif
