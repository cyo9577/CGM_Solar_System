#ifndef BLE_H__
#define BLE_H__

#include "parameter.h"

/* 0: Notify, 1: Indicate */
#define STREAM_USE_INDICATE 0
#define MAX_NOTIFY_PAYLOAD_BYTES 200

/* ==== Advertising name ==== */
#define APP_DEVICE_NAME_LEN (sizeof(APP_DEVICE_NAME) - 1)

#define APP_BLE_CONN_CFG_TAG    1
#define APP_BLE_OBSERVER_PRIO   3
#define NUS_SERVICE_UUID_TYPE   BLE_UUID_TYPE_VENDOR_BEGIN

#define APP_ADV_INTERVAL    640
#define APP_ADV_DURATION    18000

/* CGM-only traffic is 1 or 2 short packets/s.
 * Longer BLE interval reduces radio wakeups compared with 7.5~11.25 ms.
 * Units: 1.25 ms.
 * 80  = 100 ms
 * 160 = 200 ms
 */
#define REQ_CONN_INTERVAL_MIN 80
#define REQ_CONN_INTERVAL_MAX 160
#define REQ_CONN_LATENCY      4
#define REQ_CONN_TIMEOUT      400

#define DEAD_BEEF           0xDEADBEEF

#define UART_TX_BUF_SIZE    256
#define UART_RX_BUF_SIZE    256

#endif // BLE_H__
