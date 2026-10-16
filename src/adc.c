#include "parameter.h"
#include "adc.h"

struct adc_config_struct g_cfg;
extern struct k_mutex adc_cfg_mutex;

static const nrfx_saadc_channel_t m_channels[] = {
    SAADC_CHANNEL_SE_ACQ(CH0_AIN, NULL, 0, 1),
};
static int16_t s_saadc_buf[2];

struct packet_node {
    void *fifo_reserved;
    uint16_t len;
    uint8_t bytes[sizeof(struct cgm_feature_packet)];
};

K_FIFO_DEFINE(g_free_fifo);
K_FIFO_DEFINE(g_filled_fifo);
static struct packet_node g_packet_pool[PACKET_POOL_SIZE];

static struct k_work_delayable s_cgm_sample_work;
static uint16_t s_packet_seq = 0;
static atomic_t s_saadc_irq_connected = ATOMIC_INIT(0);

static volatile int s_saadc_last_status = 0;

static inline uint16_t sample_rate_to_period_ms(uint8_t sps)
{
    return (sps >= 2u) ? CGM_PERIOD_MS_2SPS : CGM_PERIOD_MS_1SPS;
}

static struct adc_config_struct adc_cfg_snapshot(void)
{
    unsigned int key = irq_lock();
    struct adc_config_struct cfg = g_cfg;
    irq_unlock(key);
    return cfg;
}

static inline void saadc_scan_once(uint16_t *out, int n_ch)
{
    nrfx_saadc_mode_trigger();
    for (int i = 0; i < n_ch; ++i) {
        out[i] = (uint16_t)(s_saadc_buf[i] & 0x0FFF);
    }
}

static void queue_packet(uint8_t flags, uint16_t raw_avg)
{
    struct packet_node *node = k_fifo_get(&g_free_fifo, K_NO_WAIT);
    if (!node) {
        node = k_fifo_get(&g_filled_fifo, K_NO_WAIT);
        if (!node) {
            return;
        }
    }

    struct adc_config_struct cfg = adc_cfg_snapshot();

    struct cgm_feature_packet pkt;
    memset(&pkt, 0, sizeof(pkt));
    pkt.magic = 0xA5u;
    pkt.version = 2u;
    pkt.flags = flags;
    pkt.sample_rate_sps = cfg.sample_rate_sps;
    pkt.seq = s_packet_seq++;
    pkt.tick_ms = k_uptime_get_32();
    pkt.raw_avg = raw_avg;

    memcpy(node->bytes, &pkt, sizeof(pkt));
    node->len = sizeof(pkt);
    k_fifo_put(&g_filled_fifo, node);
}

static void queue_cgm_packet(uint16_t raw_avg)
{
    queue_packet(0x01u, raw_avg);
}

static void cgm_sample_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    nrfx_saadc_init(NRFX_SAADC_DEFAULT_CONFIG_IRQ_PRIORITY);
    nrfx_saadc_channel_config(m_channels);
    nrfx_saadc_simple_mode_set((1UL << 0), ADC_RESOLUTION, NRF_SAADC_OVERSAMPLE_DISABLED, NULL);
    nrfx_saadc_buffer_set(s_saadc_buf, 1);
    nrfx_saadc_offset_calibrate(NULL);

    uint16_t raw[1];
    uint32_t sum0 = 0;
    for (int i = 0; i < SAMPLING_ITERATIONS; ++i) {
        saadc_scan_once(raw, 1);
        sum0 += raw[0];
    }
    uint16_t avg_raw0 = (uint16_t)(sum0 / SAMPLING_ITERATIONS);
    nrfx_saadc_uninit();

    queue_cgm_packet(avg_raw0);

    struct adc_config_struct cfg = adc_cfg_snapshot();
    uint16_t period_ms = cfg.sample_period_ms;
    if (period_ms == 0u) {
        period_ms = sample_rate_to_period_ms(CGM_SAMPLE_RATE_SPS_DEFAULT);
    }

    k_work_schedule(&s_cgm_sample_work, K_MSEC(period_ms));
}

void adc_init(void)
{
#if defined(__ZEPHYR__)
    if (atomic_cas(&s_saadc_irq_connected, 0, 1)) {
        IRQ_CONNECT(NRFX_IRQ_NUMBER_GET(NRF_SAADC), IRQ_PRIO_LOWEST,
                    nrfx_saadc_irq_handler, 0, 0);
    }
#endif

    unsigned int key = irq_lock();
    g_cfg.sample_rate_sps = CGM_SAMPLE_RATE_SPS_DEFAULT;
    g_cfg.avg_samples = CGM_BURST_SAMPLES_DEFAULT;
    g_cfg.sample_period_ms = sample_rate_to_period_ms(CGM_SAMPLE_RATE_SPS_DEFAULT);
    irq_unlock(key);

    for (int i = 0; i < PACKET_POOL_SIZE; ++i) {
        g_packet_pool[i].len = 0;
        k_fifo_put(&g_free_fifo, &g_packet_pool[i]);
    }

    k_work_init_delayable(&s_cgm_sample_work, cgm_sample_work_handler);
    k_work_schedule(&s_cgm_sample_work, K_MSEC(g_cfg.sample_period_ms));
}

void adc_apply_config(const struct adc_config_struct *cfg)
{
    struct adc_config_struct new_cfg = *cfg;

    if (new_cfg.sample_rate_sps < CGM_SAMPLE_RATE_SPS_MIN) {
        new_cfg.sample_rate_sps = CGM_SAMPLE_RATE_SPS_MIN;
    }
    if (new_cfg.sample_rate_sps > CGM_SAMPLE_RATE_SPS_MAX) {
        new_cfg.sample_rate_sps = CGM_SAMPLE_RATE_SPS_MAX;
    }
    if (new_cfg.avg_samples == 0u) {
        new_cfg.avg_samples = CGM_BURST_SAMPLES_DEFAULT;
    }
    new_cfg.sample_period_ms = sample_rate_to_period_ms(new_cfg.sample_rate_sps);

    unsigned int key = irq_lock();
    g_cfg = new_cfg;
    irq_unlock(key);

    k_work_reschedule(&s_cgm_sample_work, K_MSEC(new_cfg.sample_period_ms));
}

void adc_set_sample_rate_sps(uint8_t sps)
{
    struct adc_config_struct cfg = adc_cfg_snapshot();
    cfg.sample_rate_sps = sps;
    adc_apply_config(&cfg);
}

size_t adc_fill_chunk(uint8_t *dst, size_t max_len)
{
    struct packet_node *ck = k_fifo_get(&g_filled_fifo, K_NO_WAIT);
    if (!ck) {
        return 0;
    }

    size_t n = ck->len;
    if (n > max_len) {
        n = max_len;
    }
    memcpy(dst, ck->bytes, n);

    k_fifo_put(&g_free_fifo, ck);
    return n;
}
