#ifndef ADC_H__
#define ADC_H__

#define CH0_AIN 0

#define SAADC_CHANNEL_SE_ACQ(_pin_p, _pin_n, _index, _mode)            \
{                                                                      \
    .channel_config =                                                  \
    {                                                                  \
        NRFX_COND_CODE_1(NRF_SAADC_HAS_CH_CONFIG_RES,                  \
                         (.resistor_p = NRF_SAADC_RESISTOR_DISABLED,   \
                          .resistor_n = NRF_SAADC_RESISTOR_DISABLED,), \
                        ())                                            \
        .gain       = NRF_SAADC_GAIN2_7,                               \
        .reference  = NRF_SAADC_REFERENCE_INTERNAL,                    \
        NRFX_COND_CODE_1(NRF_SAADC_HAS_ACQTIME_ENUM,                   \
                         (.acq_time = NRF_SAADC_ACQTIME_20US,),        \
                         (.acq_time = 20,))                            \
        .mode       = NRF_SAADC_MODE_SINGLE_ENDED,                     \
        .burst      = NRF_SAADC_BURST_DISABLED,                        \
    },                                                                 \
    .pin_p          = (nrfx_analog_input_t)_pin_p,                     \
    .pin_n          = NRFX_ANALOG_INPUT_DISABLED,                      \
    .channel_index  = _index,                                          \
}

#define ADC_RESOLUTION                 NRF_SAADC_RESOLUTION_12BIT
#define ADC_12BIT_MAX_CODE             4095
#define SAMPLING_ITERATIONS            10
#define CGM_BURST_SAMPLES_DEFAULT      1u
#define CGM_BURST_SAMPLE_GAP_MS        2u
#define CGM_SAMPLE_RATE_SPS_DEFAULT    1u
#define CGM_SAMPLE_RATE_SPS_MIN        1u
#define CGM_SAMPLE_RATE_SPS_MAX        2u
#define CGM_PERIOD_MS_1SPS             1000u
#define CGM_PERIOD_MS_2SPS             500u
#define PACKET_POOL_SIZE               4
#define MAX_NOTIFY_PAYLOAD_BYTES       200
#define CMD_SAMPLE_RATE_SPS            0x10u

#pragma pack(push, 1)
struct adc_config_struct {
    uint8_t  sample_rate_sps;
    uint8_t  avg_samples;
    uint16_t sample_period_ms;
};

struct cgm_feature_packet {
    uint8_t  magic;
    uint8_t  version;
    uint8_t  flags;
    uint8_t  sample_rate_sps;
    uint16_t seq;
    uint32_t tick_ms;
    uint16_t raw_avg;
};
#pragma pack(pop)

void adc_init(void);
void adc_apply_config(const struct adc_config_struct *cfg);
void adc_set_sample_rate_sps(uint8_t sps);
size_t adc_fill_chunk(uint8_t *dst, size_t max_len);

extern struct adc_config_struct g_cfg;

#endif /* ADC_H__ */
