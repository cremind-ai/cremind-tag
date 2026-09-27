/*
 * VDD for CHALLENGE.battery_mv, RESULT.battery_mv and the low-battery flag
 * (0 = unknown, never reported as low).
 *
 * nRF52: the Zephyr ADC API on the SAADC's internal VDD input (gain 1/6,
 * 0.6 V reference: 3.6 V full scale, 12 bit).
 * nRF51: Zephyr's nRF51 ADC driver cannot select the supply input (it maps
 * input_positive to an analog pin, 8 bit only), so VDD is read with the ADC
 * registers directly (INPSEL = 1/3 supply, REFSEL = 1.2 V band gap, 10 bit)
 * when CONFIG_APP_BATTERY_NRF51_DIRECT is set; the ADC devicetree node stays
 * disabled so no driver owns the peripheral.
 */
#include <errno.h>

#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>

#include "app.h"

#if defined(CONFIG_APP_BATTERY_NRF51_DIRECT)

#include <soc.h>

BUILD_ASSERT(!DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(adc)), "a driver owns the nRF51 ADC");

int battery_init(void)
{
	return 0;
}

uint16_t tag_hal_battery_mv(void)
{
	uint32_t raw = 0;

	NRF_ADC->CONFIG = (ADC_CONFIG_RES_10bit << ADC_CONFIG_RES_Pos) |
			  (ADC_CONFIG_INPSEL_SupplyOneThirdPrescaling << ADC_CONFIG_INPSEL_Pos) |
			  (ADC_CONFIG_REFSEL_VBG << ADC_CONFIG_REFSEL_Pos);
	NRF_ADC->ENABLE = ADC_ENABLE_ENABLE_Enabled;
	NRF_ADC->EVENTS_END = 0;
	NRF_ADC->TASKS_START = 1;
	/* 10-bit conversion: 68 us; bounded in case the peripheral misbehaves. */
	for (int i = 0; i < 200 && NRF_ADC->EVENTS_END == 0; i++) {
		k_busy_wait(1);
	}
	if (NRF_ADC->EVENTS_END != 0) {
		raw = NRF_ADC->RESULT;
	}
	NRF_ADC->EVENTS_END = 0;
	NRF_ADC->ENABLE = ADC_ENABLE_ENABLE_Disabled;
	return (uint16_t)(raw * 3600u / 1023u);
}

#elif defined(CONFIG_ADC) && DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(adc))

#include <zephyr/drivers/adc.h>
#include <zephyr/dt-bindings/adc/nrf-saadc.h>

static const struct device *const adc = DEVICE_DT_GET(DT_NODELABEL(adc));
static bool ready;

int battery_init(void)
{
	static const struct adc_channel_cfg ch = {
		.gain = ADC_GAIN_1_6,
		.reference = ADC_REF_INTERNAL,
		.acquisition_time = ADC_ACQ_TIME(ADC_ACQ_TIME_MICROSECONDS, 10),
		.channel_id = 0,
		.input_positive = NRF_SAADC_VDD,
	};

	ready = device_is_ready(adc) && adc_channel_setup(adc, &ch) == 0;
	return ready ? 0 : -ENODEV;
}

uint16_t tag_hal_battery_mv(void)
{
	int16_t raw = 0;
	const struct adc_sequence seq = {
		.channels = BIT(0),
		.buffer = &raw,
		.buffer_size = sizeof(raw),
		.resolution = 12,
	};

	if (!ready || adc_read(adc, &seq) != 0 || raw < 0) {
		return 0;
	}
	return (uint16_t)((uint32_t)raw * 3600u / 4096u);
}

#else

int battery_init(void)
{
	return -ENOTSUP;
}

uint16_t tag_hal_battery_mv(void)
{
	return 0; /* not measured on this build (docs/tag-firmware.md) */
}

#endif
