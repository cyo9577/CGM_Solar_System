cmake_minimum_required(VERSION 3.20.0)

if(DEFINED CUSTOM_BOILERPLATE)
    include(${CUSTOM_BOILERPLATE})
    return()
endif()

set(COMMON_PATH ${CMAKE_CURRENT_LIST_DIR}/common)
include(${COMMON_PATH}/common.cmake)

GET_DEVICE_CONFIG_FILES(${BOARD} boards)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(CGM_Solar_System)
target_sources(app PRIVATE src/main.c)
target_sources(app PRIVATE src/adc.c)
target_include_directories(app PRIVATE common)
target_include_directories(app PRIVATE ${CMAKE_CURRENT_LIST_DIR}/src)
