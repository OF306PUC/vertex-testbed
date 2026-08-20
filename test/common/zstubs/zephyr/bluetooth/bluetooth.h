#ifndef ZSTUB_BT_H
#define ZSTUB_BT_H
#include <stdint.h>
#include <zephyr/kernel.h>

#define BT_DATA_FLAGS             0x01
#define BT_DATA_NAME_COMPLETE     0x09
#define BT_DATA_MANUFACTURER_DATA 0xFF
#define BT_GAP_ADV_MAX_ADV_DATA_LEN 31
#define BT_GAP_SCAN_FAST_INTERVAL 0x0060
#define BT_GAP_SCAN_FAST_WINDOW   0x0030
#define BT_ADDR_LE_PUBLIC 0
#define BDADDR_LE_RANDOM  1

struct bt_data { uint8_t type; uint8_t data_len; const uint8_t *data; };
#define BT_DATA(_t, _d, _l) { .type = (_t), .data_len = (_l), .data = (const uint8_t *)(_d) }
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

typedef struct { uint8_t val[6]; } bt_addr_t;
typedef struct { uint8_t type; bt_addr_t a; } bt_addr_le_t;

enum { BT_LE_SCAN_TYPE_PASSIVE = 0, BT_LE_SCAN_TYPE_ACTIVE = 1 };
#define BT_LE_SCAN_OPT_NONE             0
#define BT_LE_SCAN_OPT_FILTER_DUPLICATE 1
struct bt_le_scan_param { uint8_t type; uint32_t options; uint16_t interval; uint16_t window; };

#define BT_LE_ADV_OPT_NONE 0
struct bt_le_adv_param {
    uint8_t id; uint8_t sid; uint8_t secondary_max_skip; uint32_t options;
    uint32_t interval_min; uint32_t interval_max; const bt_addr_le_t *peer;
};
extern const struct bt_le_adv_param *BT_LE_ADV_NCONN;
#define BT_LE_ADV_PARAM(o, imin, imax, p) ((const struct bt_le_adv_param *)0)

typedef void bt_ready_cb_t(int err);
int bt_enable(bt_ready_cb_t cb);

typedef void bt_le_scan_cb_t(const bt_addr_le_t *addr, int8_t rssi,
                            uint8_t adv_type, struct net_buf_simple *buf);
int bt_le_scan_start(const struct bt_le_scan_param *param, bt_le_scan_cb_t cb);
int bt_le_scan_stop(void);

int bt_le_adv_start(const struct bt_le_adv_param *param,
                    const struct bt_data *ad, size_t ad_len,
                    const struct bt_data *sd, size_t sd_len);
int bt_le_adv_update_data(const struct bt_data *ad, size_t ad_len,
                          const struct bt_data *sd, size_t sd_len);
int bt_le_adv_stop(void);

typedef bool (*bt_data_parse_func_t)(struct bt_data *data, void *user_data);
void bt_data_parse(struct net_buf_simple *ad, bt_data_parse_func_t func, void *ud);
#endif
