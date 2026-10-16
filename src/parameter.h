#ifndef PARAMETER_H__
#define PARAMETER_H__
#include "boards/nrf54l15dk_nrf54l15_config.h"

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdbool.h>

#include <zephyr/types.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/settings/settings.h>
#include <zephyr/kernel.h>

#include <hal/nrf_gpio.h>
#include <hal/nrf_saadc.h>
#include <nrfx_saadc.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>

#endif /* PARAMETER_H__ */
