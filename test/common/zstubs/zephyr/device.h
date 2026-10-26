#ifndef ZSTUB_DEVICE_H
#define ZSTUB_DEVICE_H
#include <stdbool.h>
struct device { int _; };
bool device_is_ready(const struct device *dev);
#endif
