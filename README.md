# CGM Solar System

A low-power wireless CGM acquisition system built around the Nordic nRF54L15. The firmware targets the **nRF54L15 DK** and was developed with **nRF Connect SDK v3.2.1** and **nRF Connect SDK Toolchain v3.2.1**. A Windows desktop GUI receives and displays the measurements over Bluetooth Low Energy (BLE).

## System overview

```text
Analog sensor output
        ↓
nRF54L15 SAADC → averaged sample packet → BLE GATT notifications
                                               ↓
                                    Python/PyQt desktop GUI
                                    live plots and CSV logging
```

### Firmware and data acquisition

- The SAADC is configured for **12-bit, single-ended conversion** on external analog input **AIN6**, with the internal 0.9 V reference, gain 2/7, and a 20 µs acquisition time.
- Acquisition is duty-cycled: the firmware initializes and configures the SAADC for each measurement burst, performs **10 single conversions**, averages the raw readings, and uninitializes the peripheral.
- The default rate is **1 sample/s**; the GUI can request **1 or 2 samples/s** (1,000 or 500 ms period). The main application thread sleeps while idle, and BLE transmission is paused unless a connection and notification subscription are active.
- Samples are placed in a small fixed packet pool before transmission. Each packed 12-byte packet contains a magic byte, protocol version, flags, sample rate, sequence number, MCU uptime tick, and averaged raw ADC code.
- The nRF54L15 advertises a custom 128-bit GATT service. The TX characteristic sends sample packets with notifications; the RX characteristic accepts GUI commands. The current command sets the sample rate.

### Desktop GUI

The Python GUI scans for the selected device number, connects with BLE, subscribes to the TX characteristic, and sends sample-rate settings through the RX characteristic. It checks packet framing and sequence information, converts raw ADC codes to an estimated input voltage, and can display voltage or an offset-corrected current using the configured feedback resistance. A light median/EMA filter can be applied for display, and the GUI supports live plots and CSV data saving.

The displayed voltage/current conversion is a GUI-side calculation from the raw ADC code; interpreting it as glucose concentration requires the appropriate sensor calibration.

## Repository contents

- `src/main.c` — BLE service, advertising, connection handling, command parsing, and notification worker.
- `src/adc.c`, `src/adc.h` — duty-cycled SAADC acquisition, averaging, packet queue, and packet format.
- `src/CGM_Solar_System_GUI.py` — Python desktop application.
- `src/CGM_Solar_System_GUI.exe` — packaged Windows executable for launching the GUI without running the Python script directly.
- `boards/`, `common/`, `prj.conf`, `CMakeLists.txt` — board configuration, pin mapping, Zephyr/NCS configuration, and build files.

## Running the GUI

On Windows, double-click **`src/CGM_Solar_System_GUI.exe`**. Select the device number, connect to the nRF54L15, and use **Send** to apply the selected sample rate. Use **Data Save** to start or stop CSV logging.

The Python source is provided at `src/CGM_Solar_System_GUI.py` for inspection or development.

## Building the firmware

Open this project in an nRF Connect SDK v3.2.1 environment with the matching Toolchain v3.2.1, select the nRF54L15 DK application core target, and build the Zephyr application using the provided `CMakeLists.txt`, `prj.conf`, and board overlay.
