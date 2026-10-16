#include "parameter.h"
#include "adc.h"
#include "ble.h"

#define GPIO_LED NRF_GPIO_PIN_MAP(1, 10)

K_MUTEX_DEFINE(adc_cfg_mutex);

// Device 1
#define APP_DEVICE_NAME "CGM_Solar_System_1"
static struct bt_uuid_128 SVC_UUID  = BT_UUID_INIT_128(
    BT_UUID_128_ENCODE(0x4F191C01,0x5A23,0x4A82,0x8F3D,0x6C9E4E9B0110ULL));
static struct bt_uuid_128 TX_UUID= BT_UUID_INIT_128(
    BT_UUID_128_ENCODE(0x4F191C01,0x5A23,0x4A82,0x8F3D,0x6C9E4E9B0111ULL));
static struct bt_uuid_128 RX_UUID= BT_UUID_INIT_128(
    BT_UUID_128_ENCODE(0x4F191C01,0x5A23,0x4A82,0x8F3D,0x6C9E4E9B0112ULL));
// // Device 2
// #define APP_DEVICE_NAME "CGM_Solar_System_2"
// static struct bt_uuid_128 SVC_UUID  = BT_UUID_INIT_128(
//     BT_UUID_128_ENCODE(0x4F191C01,0x5A23,0x4A82,0x8F3D,0x6C9E4E9B0210ULL));
// static struct bt_uuid_128 TX_UUID= BT_UUID_INIT_128(
//     BT_UUID_128_ENCODE(0x4F191C01,0x5A23,0x4A82,0x8F3D,0x6C9E4E9B0211ULL));
// static struct bt_uuid_128 RX_UUID= BT_UUID_INIT_128(
//     BT_UUID_128_ENCODE(0x4F191C01,0x5A23,0x4A82,0x8F3D,0x6C9E4E9B0212ULL));

static struct bt_conn *current_conn;
static atomic_t g_ccc_on = ATOMIC_INIT(0);
static struct k_work_delayable phy_update_work;
static struct bt_conn *phy_update_conn;
static struct k_work_delayable adv_restart_work;
static atomic_t adv_restart_armed = ATOMIC_INIT(0);

static ssize_t tx_read(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf, uint16_t len, uint16_t offset)
{
    uint32_t zero = 0;
    return bt_gatt_attr_read(conn, attr, buf, len, offset, &zero, sizeof(zero));
}

static ssize_t rx_write(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
    const uint8_t *p = (const uint8_t *)buf;
    uint16_t i = 0;
	bool changed = false;

    struct adc_config_struct new_cfg;
    {
        unsigned int key = irq_lock();
        new_cfg = g_cfg;
        irq_unlock(key);
    }

    while (i + 2 <= len) {
        uint8_t cmd = p[i++], L = p[i++];
        if (i + L > len) break;
        switch (cmd) {
        case CMD_SAMPLE_RATE_SPS:
            if (L >= 1) {
                uint8_t sps = p[i];
                if (sps < CGM_SAMPLE_RATE_SPS_MIN) {
                    sps = CGM_SAMPLE_RATE_SPS_MIN;
                }
                if (sps > CGM_SAMPLE_RATE_SPS_MAX) {
                    sps = CGM_SAMPLE_RATE_SPS_MAX;
                }
                new_cfg.sample_rate_sps = sps;
                new_cfg.sample_period_ms = (sps >= 2u) ? CGM_PERIOD_MS_2SPS : CGM_PERIOD_MS_1SPS;
                changed = true;
            }
            break;


        default: break;
        }
        i += L;
    }

    if (changed) {
        unsigned int key = irq_lock();
        g_cfg = new_cfg;
        irq_unlock(key);
        k_mutex_lock(&adc_cfg_mutex, K_FOREVER);
        g_cfg = new_cfg;
        k_mutex_unlock(&adc_cfg_mutex);
        adc_apply_config(&g_cfg);
    }

    return len;	
}

static void tx_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    bool on = (value & (STREAM_USE_INDICATE ? BT_GATT_CCC_INDICATE : BT_GATT_CCC_NOTIFY)) != 0;
    atomic_set(&g_ccc_on, on);
}

#if defined(CONFIG_BT)
static uint8_t dummy_readback;
BT_GATT_SERVICE_DEFINE(cgm_svc,
        BT_GATT_PRIMARY_SERVICE(&SVC_UUID),
        BT_GATT_CHARACTERISTIC(&TX_UUID.uuid,
#if             STREAM_USE_INDICATE
                BT_GATT_CHRC_READ | BT_GATT_CHRC_INDICATE,
#else
                BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
#endif
                BT_GATT_PERM_READ, tx_read, NULL, &dummy_readback),
        BT_GATT_CCC(tx_ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
        BT_GATT_CHARACTERISTIC(&RX_UUID.uuid,
                BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
                BT_GATT_PERM_WRITE, NULL, rx_write, NULL),
);
#endif

#if defined(CONFIG_BT_PHY_UPDATE)
static void phy_update_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    if (!phy_update_conn) return;
    const struct bt_conn_le_phy_param pref = {
        .options     = BT_CONN_LE_PHY_OPT_NONE,
        .pref_rx_phy = BT_GAP_LE_PHY_2M,
        .pref_tx_phy = BT_GAP_LE_PHY_2M,
    };
    bt_conn_le_phy_update(phy_update_conn, &pref);
    bt_conn_unref(phy_update_conn);
    phy_update_conn = NULL;
}
#endif

static int adv_start(void);

static void adv_restart_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    atomic_set(&adv_restart_armed, 0);

    (void)bt_le_adv_stop();

    int rc = adv_start();
    if (rc) {
        if (atomic_cas(&adv_restart_armed, 0, 1)) {
            k_work_schedule(&adv_restart_work, K_MSEC(300));
        }
    }
}

static inline void adv_restart_request(k_timeout_t delay)
{
    if (atomic_cas(&adv_restart_armed, 0, 1)) {
        k_work_schedule(&adv_restart_work, delay);
    }
}

static void connected(struct bt_conn *conn, uint8_t err)
{
    if (err) {
        adv_restart_request(K_NO_WAIT);
        return;
    }
    current_conn = bt_conn_ref(conn);

#if defined(CONFIG_BT_PHY_UPDATE)
    if (phy_update_conn) {
        bt_conn_unref(phy_update_conn);
        phy_update_conn = NULL;
    }
    phy_update_conn = bt_conn_ref(conn);
    k_work_schedule(&phy_update_work, K_MSEC(200));
#endif

    struct bt_le_conn_param param = {
        .interval_min	= REQ_CONN_INTERVAL_MIN,
        .interval_max	= REQ_CONN_INTERVAL_MAX,
        .latency        = REQ_CONN_LATENCY,
        .timeout        = REQ_CONN_TIMEOUT,
    };
    bt_conn_le_param_update(conn, &param);

    int i = 0;
    while(i < 5) {
        nrf_gpio_cfg_output(GPIO_LED);
        nrf_gpio_pin_set(GPIO_LED);
        k_sleep(K_MSEC(200));
        nrf_gpio_pin_clear(GPIO_LED);
        k_sleep(K_MSEC(200));
        nrf_gpio_cfg_default(GPIO_LED);
        i++;
    }
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
    ARG_UNUSED(reason);
    if (current_conn) { bt_conn_unref(current_conn); current_conn = NULL; }
    atomic_set(&g_ccc_on, 0);
    adv_restart_request(K_NO_WAIT);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
    .connected = connected,
    .disconnected = disconnected,
};

static const struct bt_gatt_attr *tx_attr(void) { return &cgm_svc.attrs[2]; }

#define WORK_STACK_SZ 4096
K_THREAD_STACK_DEFINE(work_stack_area, WORK_STACK_SZ);
static struct k_thread work_thread_data;
static k_tid_t work_tid;

static void work_thread(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

    static uint8_t buf[64];

    for (;;) {
        if (!current_conn || !atomic_get(&g_ccc_on)) {
            k_sleep(K_MSEC(1000));
            continue;
        }

        uint16_t mtu = bt_gatt_get_mtu(current_conn);
        if (mtu < 23) {
            mtu = 23;
        }

        uint16_t payload = (mtu - 3);
        if (payload > sizeof(buf)) {
            payload = sizeof(buf);
        }
        if (payload > MAX_NOTIFY_PAYLOAD_BYTES) {
            payload = MAX_NOTIFY_PAYLOAD_BYTES;
        }

        bool sent_any = false;

#if STREAM_USE_INDICATE
        static struct bt_gatt_indicate_params ip = {0};
        ip.attr = tx_attr();

        for (;;) {
            size_t n = adc_fill_chunk(buf, payload);
            if (!n) {
                break;
            }

            ip.data = buf;
            ip.len  = (uint16_t)n;
            int rc = bt_gatt_indicate(current_conn, &ip);
            if (rc == 0) {
                sent_any = true;
                continue;
            }
            if (rc == -ENOMEM) {
                break;
            }
            break;
        }
#else
        for (;;) {
            size_t n = adc_fill_chunk(buf, payload);
            if (!n) {
                break;
            }

            struct bt_gatt_notify_params np = {
                .attr = tx_attr(),
                .data = buf,
                .len  = (uint16_t)n,
            };
            int rc = bt_gatt_notify_cb(current_conn, &np);
            if (rc == 0) {
                sent_any = true;
                continue;
            }
            if (rc == -ENOMEM) {
                break;
            }
            break;
        }
#endif

        k_sleep(sent_any ? K_MSEC(500) : K_MSEC(10));
    }
}

static int adv_start(void)
{
    struct bt_data ad[] = {
        BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
        BT_DATA(BT_DATA_UUID128_ALL, SVC_UUID.val, sizeof(SVC_UUID.val))
    };
    struct bt_data sd[] = {
        BT_DATA(BT_DATA_NAME_COMPLETE, APP_DEVICE_NAME, strlen(APP_DEVICE_NAME)),
    };
    return bt_le_adv_start(BT_LE_ADV_CONN_FAST_1, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
}

int main(void)
{
    adc_init();

    int i = 0;
    while(i < 3) {
        nrf_gpio_cfg_output(GPIO_LED);
        nrf_gpio_pin_set(GPIO_LED);
        k_sleep(K_MSEC(200));
        nrf_gpio_pin_clear(GPIO_LED);
        k_sleep(K_MSEC(200));
        nrf_gpio_cfg_default(GPIO_LED);
        i++;
    }
    
    (void)bt_set_name(APP_DEVICE_NAME);
    bt_enable(NULL);
    k_work_init_delayable(&adv_restart_work, adv_restart_work_handler);
#if defined(CONFIG_BT_PHY_UPDATE)
    k_work_init_delayable(&phy_update_work, phy_update_work_handler);
    phy_update_conn = NULL;
#endif

    if (adv_start()) {
        adv_restart_request(K_MSEC(1000));
    }

    if (work_tid) return 0;
    work_tid = k_thread_create(&work_thread_data, work_stack_area, K_THREAD_STACK_SIZEOF(work_stack_area),
                               work_thread, NULL, NULL, NULL, K_PRIO_PREEMPT(4), 0, K_NO_WAIT);
    k_thread_name_set(work_tid, "work");

    while (1) {
        k_sleep(K_FOREVER);
    }
}