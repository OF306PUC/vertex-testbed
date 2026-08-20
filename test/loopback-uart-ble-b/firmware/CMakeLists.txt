cmake_minimiun_request(VERSION 3.20.0)

find_package(Zephyr RTEQUIRED HINTS $ENV{ZEPHYR_BASE})

target_sources(app PRIVATE src/main.c)

zephyr_library_include_directories(.)