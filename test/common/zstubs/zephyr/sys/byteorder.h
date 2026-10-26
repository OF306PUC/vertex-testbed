#ifndef ZSTUB_BYTEORDER_H
#define ZSTUB_BYTEORDER_H
#include <stdint.h>
#define sys_cpu_to_le16(x) ((uint16_t)(x))
#define sys_le16_to_cpu(x) ((uint16_t)(x))
#define sys_cpu_to_le32(x) ((uint32_t)(x))
#define sys_le32_to_cpu(x) ((uint32_t)(x))
#endif
