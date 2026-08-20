#ifndef ZSTUB_DK_H
#define ZSTUB_DK_H
#include <stdint.h>
#define DK_LED1 0
#define DK_LED2 1
#define DK_LED3 2
#define DK_LED4 3
#define DK_BTN1_MSK 0x01
#define DK_BTN2_MSK 0x02
typedef void (*button_handler_t)(uint32_t state, uint32_t has_changed);
int dk_leds_init(void);
int dk_buttons_init(button_handler_t handler);
int dk_set_led(uint8_t led_idx, uint32_t val);
int dk_set_led_on(uint8_t led_idx);
int dk_set_led_off(uint8_t led_idx);
#endif
