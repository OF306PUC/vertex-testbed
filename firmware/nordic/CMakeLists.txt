cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(NONE)

# NORDIC SDK APP START
target_sources(app PRIVATE
  src/main.c
  src/broadcaster.c
  src/observer.c
  src/serial.c
  src/coordination_task.c
)

# NORDIC SDK APP END
zephyr_library_include_directories(.)
