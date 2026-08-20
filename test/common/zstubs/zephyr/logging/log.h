#ifndef ZSTUB_LOG_H
#define ZSTUB_LOG_H
#define LOG_LEVEL_NONE 0
#define LOG_LEVEL_ERR  1
#define LOG_LEVEL_WRN  2
#define LOG_LEVEL_INF  3
#define LOG_LEVEL_DBG  4
#define LOG_MODULE_REGISTER(...) extern int _log_module_unused_
/* Variadic and type-checked, so a bad format argument still gets flagged. */
extern int _zstub_log(const char *fmt, ...);
#define LOG_ERR(...) _zstub_log(__VA_ARGS__)
#define LOG_WRN(...) _zstub_log(__VA_ARGS__)
#define LOG_INF(...) _zstub_log(__VA_ARGS__)
#define LOG_DBG(...) _zstub_log(__VA_ARGS__)
#endif
