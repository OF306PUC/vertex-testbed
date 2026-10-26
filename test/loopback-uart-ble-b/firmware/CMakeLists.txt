cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(vertex_testpeer_b C)

target_sources(app PRIVATE
    src/main.c
    src/proto.c
    src/agent.c
    src/uart_link.c
    src/ble_scan.c
)
target_include_directories(app PRIVATE src)
