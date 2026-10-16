import sys, asyncio, struct, os, csv
from collections import deque
from datetime import datetime

import numpy as np
from PyQt5 import QtCore, QtWidgets, QtGui
import pyqtgraph as pg
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError
from qasync import QEventLoop, asyncSlot

# Device 1
APP_DEVICE_NAME_DEFAULT = "CGM_Solar_System_1"
SVC_UUID_DEFAULT   = "4F191C01-5A23-4A82-8F3D-6C9E4E9B0110".lower()
TX_CHAR_UUID_DEFAULT = "4F191C01-5A23-4A82-8F3D-6C9E4E9B0111".lower()
RX_CHAR_UUID_DEFAULT = "4F191C01-5A23-4A82-8F3D-6C9E4E9B0112".lower()

DEVICE_INDEX_MIN = 1
DEVICE_INDEX_MAX = 2
DEVICE_UUID_PREFIX = "4F191C01-5A23-4A82-8F3D-6C9E4E9B"


def device_name_for_index(idx: int) -> str:
    return f"CGM_Solar_System_{int(idx)}"


def svc_uuid_for_index(idx: int) -> str:
    return f"{DEVICE_UUID_PREFIX}{int(idx):02d}10".lower()


def tx_uuid_for_index(idx: int) -> str:
    return f"{DEVICE_UUID_PREFIX}{int(idx):02d}11".lower()


def rx_uuid_for_index(idx: int) -> str:
    return f"{DEVICE_UUID_PREFIX}{int(idx):02d}12".lower()

PACKET_MAGIC = 0xA5
PACKET_VERSION = 2

# magic, version, flags, sample_rate_sps, seq, tick_ms, raw_avg
PACK_FMT = "<BBBBHIH"
PACK_SIZE = struct.calcsize(PACK_FMT)

PLOT_UPDATE_MS = 50
DEFAULT_MAXIMUM_TIME_SEC = 2592000 # 30 days

CMD_SAMPLE_RATE_SPS = 0x10


class StatusDot(QtWidgets.QLabel):
    def __init__(self, diameter=15, parent=None):
        super().__init__(parent)
        self.diameter = diameter
        self.setStatus("disconnected")

    def setStatus(self, status: str):
        color = {
            "disconnected": "#777",
            "scanning": "#f0ad4e",
            "connecting": "#f0ad4e",
            "connected": "#5cb85c",
            "error": "#d9534f",
        }.get(status, "#777")
        pm = QtGui.QPixmap(self.diameter, self.diameter)
        pm.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pm)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(QtGui.QColor(color))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawEllipse(0, 0, self.diameter, self.diameter)
        painter.end()
        self.setPixmap(pm)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CGM Control by Ji-Hyoung Cha")
        self.resize(1800, 1100)
        self.setMinimumSize(900, 600)

        self.client = None
        self.connected = False
        self.rx_char_uuid = None
        self.rx_buf = bytearray()
        self.rate_bytes_acc = 0
        self.rate_pkts_acc = 0
        self.rate_t0 = QtCore.QElapsedTimer()
        self.rate_t0.start()

        self.cgm_t = deque(maxlen=200000)
        self.cgm_y = deque(maxlen=200000)

        # GUI-local x-axis time.
        # Starts at 0 on program start and Clear, but is not reset by disconnect/reconnect
        # or sampling-rate changes.
        self.plot_time_next_sec = 0.0

        # Very light GUI-side digital filter state: median-of-3 + EMA.
        self.filter_hist = deque(maxlen=5)
        self.filter_ema = None
        self.filter_ema_alpha = 0.20
        self.filter_enabled = True

        # CSV save state. Data Save ON always starts a new session/file set.
        self.save_enabled = False
        self.save_path = os.getcwd()
        self.save_duration_sec = 86400.0
        self.save_pre_enabled = False
        self.save_post_enabled = True
        self.save_session_tag = None
        self.save_file_index = 0
        self.save_pre_file = None
        self.save_post_file = None
        self.save_pre_writer = None
        self.save_post_writer = None
        self.save_pre_count = 0
        self.save_post_count = 0

        # Auto reconnect state.
        # After the user presses Connect once, unexpected disconnects trigger
        # repeated reconnect attempts until the user presses Disconnect or closes the GUI.
        self.auto_reconnect_enabled = False
        self.user_requested_disconnect = False
        self.reconnect_timer = QtCore.QTimer(self)
        self.reconnect_timer.setInterval(3000)
        self.reconnect_timer.timeout.connect(self.try_auto_reconnect)

        # Last received packet values for ADC/BLE debugging
        self.last_seq = None
        self.last_sps = None
        self.last_tick_ms = None
        self.last_raw_avg = None
        self.last_cgm_mv = None
        self.last_plot_y = None

        # SAADC settings used only in GUI conversion.
        # Current MCU SAADC config: internal reference 0.9 V, gain 2/7 => ideal FS ~= 3150 mV.
        self.adc_full_scale_mV = 3150.0
        self.adc_max_code = 4095.0

        self.cgm_offset_mV = 1200.00
        self.cgm_Rf_Mohm = 20.0
        self.cgm_show_voltage = True
        self.cgm_show_current = True
        self.cgm_use_offset_remove = True

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        top = QtWidgets.QHBoxLayout()
        root.addLayout(top)

        self.status_dot = StatusDot()
        top.addWidget(self.status_dot)

        top.addWidget(QtWidgets.QLabel("Device No:"))
        self.device_index_spin = QtWidgets.QSpinBox()
        self.device_index_spin.setRange(DEVICE_INDEX_MIN, DEVICE_INDEX_MAX)
        self.device_index_spin.setValue(1)
        self.device_index_spin.setFixedWidth(60)
        self.device_index_spin.valueChanged.connect(self.apply_device_index)
        top.addWidget(self.device_index_spin)
        
        # Hidden/automatic UUID fields. They are still used internally.
        self.device_name_edit = QtWidgets.QLineEdit(APP_DEVICE_NAME_DEFAULT)
        self.svc_uuid_edit = QtWidgets.QLineEdit(SVC_UUID_DEFAULT)
        self.tx_uuid_edit = QtWidgets.QLineEdit(TX_CHAR_UUID_DEFAULT)
        self.rx_uuid_edit = QtWidgets.QLineEdit(RX_CHAR_UUID_DEFAULT)

        self.rate_label = QtWidgets.QLabel("Rate: --")
        self.rate_label.setFixedWidth(360)
        top.addWidget(self.rate_label)

        self.mtu_label = QtWidgets.QLabel("MTU: --")
        self.mtu_label.setFixedWidth(100)
        top.addWidget(self.mtu_label)

        top.addWidget(QtWidgets.QLabel("Max time (s):"))
        self.shared_xspan_spin = QtWidgets.QDoubleSpinBox()
        self.shared_xspan_spin.setRange(1.0, 2592000.0)
        self.shared_xspan_spin.setDecimals(1)
        self.shared_xspan_spin.setValue(DEFAULT_MAXIMUM_TIME_SEC)
        self.shared_xspan_spin.valueChanged.connect(self.apply_cgm_x_range)
        self.shared_xspan_spin.setFixedWidth(120)
        top.addWidget(self.shared_xspan_spin)

        top.addStretch(1)

        # Data save controls: placed immediately left of Send.
        self.data_save_btn = QtWidgets.QPushButton("Data Save")
        self.data_save_btn.setCheckable(True)
        self.data_save_btn.toggled.connect(self.on_data_save_toggled)
        top.addWidget(self.data_save_btn)

        self.save_path_btn = QtWidgets.QPushButton("Path")
        self.save_path_btn.clicked.connect(self.on_save_path_clicked)
        top.addWidget(self.save_path_btn)

        self.save_path_edit = QtWidgets.QLineEdit(self.save_path)
        self.save_path_edit.setFixedWidth(320)
        self.save_path_edit.setToolTip("Save folder path. You can paste or edit directly.")
        self.save_path_edit.editingFinished.connect(self.on_save_path_edited)
        top.addWidget(self.save_path_edit)

        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self.on_send_clicked)
        top.addWidget(self.send_btn)

        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self.on_connect_clicked)
        top.addWidget(self.connect_btn)

        self.disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.on_disconnect_clicked)
        self.disconnect_btn.setEnabled(False)
        top.addWidget(self.disconnect_btn)

        self.cgm_group = QtWidgets.QGroupBox("CGM Panel")
        root.addWidget(self.cgm_group)

        cgm_outer = QtWidgets.QHBoxLayout(self.cgm_group)
        cgm_outer.setContentsMargins(10, 8, 10, 8)
        cgm_outer.setSpacing(24)

        cgm_left_widget = QtWidgets.QWidget()
        cgm_right_widget = QtWidgets.QWidget()
        cgm_outer.addWidget(cgm_left_widget, stretch=1)
        cgm_outer.addWidget(cgm_right_widget, stretch=1)

        cgm_left = QtWidgets.QGridLayout(cgm_left_widget)
        cgm_left.setHorizontalSpacing(10)
        cgm_left.setVerticalSpacing(6)

        cgm_right = QtWidgets.QGridLayout(cgm_right_widget)
        cgm_right.setHorizontalSpacing(10)
        cgm_right.setVerticalSpacing(6)

        # Left half, row 1: Voltage, Current, Offset(mV)
        self.cgm_voltage_btn = QtWidgets.QPushButton("Voltage")
        self.cgm_voltage_btn.setCheckable(True)
        self.cgm_voltage_btn.setChecked(True)
        self.cgm_voltage_btn.clicked.connect(self.on_cgm_voltage_toggled)
        cgm_left.addWidget(self.cgm_voltage_btn, 0, 0)

        self.cgm_current_btn = QtWidgets.QPushButton("Current")
        self.cgm_current_btn.setCheckable(True)
        self.cgm_current_btn.setChecked(True)
        self.cgm_current_btn.clicked.connect(self.on_cgm_current_toggled)
        cgm_left.addWidget(self.cgm_current_btn, 0, 1)

        self.cgm_offset_btn = QtWidgets.QPushButton("Offset(mV)")
        self.cgm_offset_btn.setCheckable(True)
        self.cgm_offset_btn.setChecked(True)
        self.cgm_offset_btn.clicked.connect(self.on_cgm_offset_toggled)
        cgm_left.addWidget(self.cgm_offset_btn, 0, 2)

        self.filter_btn = QtWidgets.QPushButton("Filter")
        self.filter_btn.setCheckable(True)
        self.filter_btn.setChecked(True)
        self.filter_btn.clicked.connect(self.on_filter_toggled)
        cgm_left.addWidget(self.filter_btn, 0, 3)

        cgm_left.addWidget(QtWidgets.QLabel("Hist"), 0, 4)
        self.filter_hist_spin = QtWidgets.QSpinBox()
        self.filter_hist_spin.setRange(1, 21)
        self.filter_hist_spin.setSingleStep(2)
        self.filter_hist_spin.setValue(3)
        self.filter_hist_spin.valueChanged.connect(self.on_filter_hist_changed)
        cgm_left.addWidget(self.filter_hist_spin, 0, 5)

        cgm_left.addWidget(QtWidgets.QLabel("Alpha"), 0, 6)
        self.filter_alpha_spin = QtWidgets.QDoubleSpinBox()
        self.filter_alpha_spin.setRange(0.01, 1.00)
        self.filter_alpha_spin.setDecimals(2)
        self.filter_alpha_spin.setSingleStep(0.05)
        self.filter_alpha_spin.setValue(self.filter_ema_alpha)
        self.filter_alpha_spin.valueChanged.connect(self.on_filter_alpha_changed)
        cgm_left.addWidget(self.filter_alpha_spin, 0, 7)

        # Left half, row 2: Offset, Rf, save filter options, file duration
        cgm_left.addWidget(QtWidgets.QLabel("Offset"), 1, 0)
        self.cgm_offset_spin = QtWidgets.QDoubleSpinBox()
        self.cgm_offset_spin.setRange(-10000.0, 10000.0)
        self.cgm_offset_spin.setDecimals(2)
        self.cgm_offset_spin.setValue(self.cgm_offset_mV)
        self.cgm_offset_spin.valueChanged.connect(self.on_cgm_offset_value_changed)
        cgm_left.addWidget(self.cgm_offset_spin, 1, 1)

        cgm_left.addWidget(QtWidgets.QLabel("Rf (MΩ)"), 1, 2)
        self.cgm_rf_spin = QtWidgets.QDoubleSpinBox()
        self.cgm_rf_spin.setRange(0.1, 1000.0)
        self.cgm_rf_spin.setDecimals(3)
        self.cgm_rf_spin.setValue(self.cgm_Rf_Mohm)
        self.cgm_rf_spin.valueChanged.connect(self.on_cgm_rf_changed)
        cgm_left.addWidget(self.cgm_rf_spin, 1, 3)

        self.save_pre_chk = QtWidgets.QCheckBox("Pre filter")
        self.save_pre_chk.setChecked(False)
        self.save_pre_chk.toggled.connect(self.on_save_check_changed)
        cgm_left.addWidget(self.save_pre_chk, 1, 4)

        self.save_post_chk = QtWidgets.QCheckBox("Post filter")
        self.save_post_chk.setChecked(True)
        self.save_post_chk.toggled.connect(self.on_save_check_changed)
        cgm_left.addWidget(self.save_post_chk, 1, 5)

        cgm_left.addWidget(QtWidgets.QLabel("File sec"), 1, 6)
        self.save_duration_spin = QtWidgets.QDoubleSpinBox()
        self.save_duration_spin.setRange(1.0, 31_536_000.0)
        self.save_duration_spin.setDecimals(0)
        self.save_duration_spin.setValue(self.save_duration_sec)
        self.save_duration_spin.valueChanged.connect(self.on_save_duration_changed)
        self.save_duration_spin.setFixedWidth(110)
        cgm_left.addWidget(self.save_duration_spin, 1, 7)

        cgm_left.setColumnStretch(8, 1)

        # Right half, row 1: Auto X/Y, Y-min, Y-max
        self.cgm_auto_x_btn = QtWidgets.QPushButton("Auto X-scale")
        self.cgm_auto_x_btn.setCheckable(True)
        self.cgm_auto_x_btn.setChecked(True)
        self.cgm_auto_x_btn.toggled.connect(self.on_cgm_auto_x_toggled)
        cgm_right.addWidget(self.cgm_auto_x_btn, 0, 0)

        self.cgm_auto_y_btn = QtWidgets.QPushButton("Auto Y-scale")
        self.cgm_auto_y_btn.setCheckable(True)
        self.cgm_auto_y_btn.setChecked(True)
        self.cgm_auto_y_btn.toggled.connect(self.on_cgm_auto_y_toggled)
        cgm_right.addWidget(self.cgm_auto_y_btn, 0, 1)

        cgm_right.addWidget(QtWidgets.QLabel("Y-min"), 0, 2)
        self.cgm_ymin_spin = QtWidgets.QDoubleSpinBox()
        self.cgm_ymin_spin.setRange(-1e6, 1e6)
        self.cgm_ymin_spin.setDecimals(2)
        self.cgm_ymin_spin.setValue(0.0)
        cgm_right.addWidget(self.cgm_ymin_spin, 0, 3)

        cgm_right.addWidget(QtWidgets.QLabel("Y-max"), 0, 4)
        self.cgm_ymax_spin = QtWidgets.QDoubleSpinBox()
        self.cgm_ymax_spin.setRange(-1e6, 1e6)
        self.cgm_ymax_spin.setDecimals(1)
        self.cgm_ymax_spin.setValue(100.0)
        cgm_right.addWidget(self.cgm_ymax_spin, 0, 5)

        # Right half, row 2: Sampling rate, ADC status
        cgm_right.addWidget(QtWidgets.QLabel("Sampling rate"), 1, 0)
        self.sample_rate_combo = QtWidgets.QComboBox()
        self.sample_rate_combo.addItem("1 S/s", 1)
        self.sample_rate_combo.addItem("2 S/s", 2)
        self.sample_rate_combo.setCurrentIndex(1)
        cgm_right.addWidget(self.sample_rate_combo, 1, 1)

        self.adc_debug_label = QtWidgets.QLabel("ADC: --")
        self.adc_debug_label.setMinimumWidth(620)
        cgm_right.addWidget(self.adc_debug_label, 1, 2, 1, 4)

        cgm_right.setColumnStretch(6, 1)

        self.cgm_plot = pg.PlotWidget(background="w")
        self.cgm_plot.setLabel("left", "CGM (mV)")
        self.cgm_plot.setLabel("bottom", "Time (s)")
        self.cgm_plot.showGrid(x=True, y=True, alpha=0.25)
        self.cgm_plot.setMouseEnabled(x=True, y=True)
        self.cgm_plot.setRange(yRange=(self.cgm_ymin_spin.value(), self.cgm_ymax_spin.value()), padding=0.02)
        self.cgm_curve = self.cgm_plot.plot([], [], pen=pg.mkPen('k', width=2))
        root.addWidget(self.cgm_plot, stretch=1)

        bottom = QtWidgets.QHBoxLayout()
        root.addLayout(bottom)
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_all)
        bottom.addWidget(self.clear_btn)
        self.status_label = QtWidgets.QLabel("Idle")
        bottom.addStretch(1)
        bottom.addWidget(self.status_label)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_plots)
        self.timer.start(PLOT_UPDATE_MS)

        self.rate_timer = QtCore.QTimer(self)
        self.rate_timer.timeout.connect(self.update_rate)
        self.rate_timer.start(500)

        self.update_cgm_y_label()
        self.update_adc_debug_label()
        self.apply_cgm_x_range()
        self.update_cgm_mouse_mode()

        # Start data saving automatically when the GUI opens.
        # This uses the current save path and default Post-filter save option.
        QtCore.QTimer.singleShot(0, lambda: self.data_save_btn.setChecked(True))

    def _build_lut(self):
        n = 256
        lut = np.empty((n, 3), dtype=np.ubyte)
        for i in range(n):
            x = i / 255.0
            lut[i] = [int(255 * x), int(255 * np.sqrt(x)), int(255 * (1.0 - x))]
        self.lfp_img.setLookupTable(lut)

    def apply_device_index(self, idx: int):
        idx = int(idx)
        self.device_name_edit.setText(device_name_for_index(idx))
        self.svc_uuid_edit.setText(svc_uuid_for_index(idx))
        self.tx_uuid_edit.setText(tx_uuid_for_index(idx))
        self.rx_uuid_edit.setText(rx_uuid_for_index(idx))
        if not self.connected:
            self.rx_char_uuid = None
            
    def tx_uuid(self) -> str:
        return self.tx_uuid_edit.text().strip().lower()

    def rx_uuid(self) -> str:
        return self.rx_uuid_edit.text().strip().lower()

    def svc_uuid(self) -> str:
        return self.svc_uuid_edit.text().strip().lower()

    async def find_device_by_name(self, name: str, preferred_svc_uuid: str, timeout: float = 8.0):
        try:
            finder = getattr(BleakScanner, "find_device_by_filter", None)
            if callable(finder):
                def _flt(d, ad):
                    if d.name != name:
                        return False
                    su = (ad.service_uuids or []) if ad else []
                    return any(u.lower() == preferred_svc_uuid for u in su) if preferred_svc_uuid else True
                dev = await finder(_flt, timeout=timeout)
                if dev:
                    return dev
        except Exception:
            pass
        devs = await BleakScanner.discover(timeout=timeout)
        cands = [d for d in devs if d.name == name]
        if not cands:
            return None
        cands.sort(key=lambda d: getattr(d, "rssi", -999), reverse=True)
        return cands[0]

    def on_disconnected(self, client: BleakClient):
        QtCore.QTimer.singleShot(0, self.after_disconnected)

    def after_disconnected(self):
        unexpected = self.auto_reconnect_enabled and not self.user_requested_disconnect

        # For a user-requested disconnect, close the save session.
        # For an unexpected disconnect, keep Data Save state/files alive so resumed data
        # continues in the same session after automatic reconnect.
        if not unexpected:
            self.stop_data_save()

        self.connected = False
        self.client = None
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)

        if unexpected:
            self.status_dot.setStatus("scanning")
            self.status_label.setText("Disconnected unexpectedly. Auto reconnecting…")
            if not self.reconnect_timer.isActive():
                self.reconnect_timer.start()
        else:
            self.status_dot.setStatus("disconnected")
            self.status_label.setText("Disconnected.")

    async def cleanup_connection(self):
        try:
            self.auto_reconnect_enabled = False
            self.user_requested_disconnect = True
            if self.reconnect_timer.isActive():
                self.reconnect_timer.stop()
            self.stop_data_save()
            if self.client and self.client.is_connected:
                try:
                    await self.client.stop_notify(self.tx_uuid())
                except Exception:
                    pass
                await self.client.disconnect()
        except Exception as e:
            self.status_label.setText(f"Disconnect error: {e}")
        finally:
            self.client = None
            self.connected = False
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)

    @asyncSlot()
    async def on_connect_clicked(self):
        self.auto_reconnect_enabled = True
        self.user_requested_disconnect = False
        if self.reconnect_timer.isActive():
            self.reconnect_timer.stop()

        self.connect_btn.setEnabled(False)
        self.status_dot.setStatus("scanning")
        self.status_label.setText("Scanning…")
        try:
            dev = await self.find_device_by_name(self.device_name_edit.text().strip(), self.svc_uuid(), timeout=10.0)
            if not dev:
                raise BleakError("Device not found")
            self.client = BleakClient(dev, disconnected_callback=self.on_disconnected)
            await self.client.connect(timeout=12.0)
            self.connected = True
            try:
                await self.client.request_mtu(247)
            except Exception:
                pass
            mtu_now = getattr(self.client, "mtu_size", None)
            if mtu_now:
                self.mtu_label.setText(f"MTU: {mtu_now}")
            await self.client.start_notify(self.tx_uuid(), self.on_notify)
            self.rx_char_uuid = self.rx_uuid()
            self.disconnect_btn.setEnabled(True)
            self.status_dot.setStatus("connected")
            self.status_label.setText("Connected.")
            self.rate_bytes_acc = 0
            self.rate_pkts_acc = 0
            self.rate_t0.restart()
        except Exception as e:
            self.connected = False
            self.client = None
            self.status_dot.setStatus("error")
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            self.status_label.setText(f"Connect error: {e}")
            if self.auto_reconnect_enabled and not self.user_requested_disconnect:
                if not self.reconnect_timer.isActive():
                    self.reconnect_timer.start()

    @asyncSlot()
    async def on_disconnect_clicked(self):
        self.user_requested_disconnect = True
        self.auto_reconnect_enabled = False
        if self.reconnect_timer.isActive():
            self.reconnect_timer.stop()
        await self.cleanup_connection()

    @asyncSlot()
    async def try_auto_reconnect(self):
        if self.user_requested_disconnect or not self.auto_reconnect_enabled:
            self.reconnect_timer.stop()
            return
        if self.connected or (self.client and self.client.is_connected):
            self.reconnect_timer.stop()
            return
        if not self.connect_btn.isEnabled():
            return

        await self.on_connect_clicked()

    def _tlv_append_u8(self, ba, cmd, val):
        ba += bytes([cmd, 1, int(val) & 0xFF])
        return ba

    @asyncSlot()
    async def on_send_clicked(self):
        if not (self.client and self.client.is_connected and self.rx_char_uuid):
            self.status_label.setText("Not connected (cannot send).")
            return
        try:
            tlv = bytearray()
            sps = int(self.sample_rate_combo.currentData())
            tlv = self._tlv_append_u8(tlv, CMD_SAMPLE_RATE_SPS, sps)
            await self.client.write_gatt_char(self.rx_char_uuid, tlv, response=False)
            self.status_label.setText(f"Sampling rate sent: {sps} S/s")
        except Exception as e:
            self.status_label.setText(f"Send error: {e}")

    def on_notify(self, sender, data: bytearray):
        self.rx_buf.extend(data)
        while len(self.rx_buf) >= PACK_SIZE:
            if self.rx_buf[0] != PACKET_MAGIC:
                del self.rx_buf[0]
                continue
            blob = bytes(self.rx_buf[:PACK_SIZE])
            del self.rx_buf[:PACK_SIZE]
            try:
                magic, version, flags, sample_rate_sps, seq, tick_ms, raw_avg = struct.unpack(PACK_FMT, blob)
                if magic != PACKET_MAGIC or version != PACKET_VERSION:
                    continue

                self.last_seq = int(seq)
                self.last_sps = int(sample_rate_sps)
                self.last_tick_ms = int(tick_ms)
                self.last_raw_avg = int(raw_avg)

                if flags & 0x80:
                    # Error/status packet: raw_avg carries the SAADC/nrfx status code.
                    self.last_cgm_mv = None
                    self.last_plot_y = None
                    self.status_label.setText(
                        f"SAADC error/status packet: code={self.last_raw_avg}, seq={self.last_seq}"
                    )
                    self.rate_bytes_acc += len(blob)
                    self.rate_pkts_acc += 1
                    self.update_adc_debug_label()
                    continue

                tick_sec = self.plot_time_next_sec
                cgm_mv = self.raw_to_mv(self.last_raw_avg)
                pre_value = self.convert_cgm_value(self.last_raw_avg, cgm_mv)
                post_value = self.apply_digital_filter(pre_value)
                y = post_value

                self.last_cgm_mv = float(cgm_mv)
                self.last_plot_y = float(y)

                self.cgm_t.append(tick_sec)
                self.cgm_y.append(y)

                self.save_sample(pre_value, post_value)

                sps_for_dt = int(sample_rate_sps) if int(sample_rate_sps) > 0 else int(self.sample_rate_combo.currentData())
                if sps_for_dt <= 0:
                    sps_for_dt = 1
                self.plot_time_next_sec = tick_sec + (1.0 / float(sps_for_dt))
                self.rate_bytes_acc += len(blob)
                self.rate_pkts_acc += 1
                self.update_adc_debug_label()
            except Exception:
                pass

    def raw_to_mv(self, raw: int) -> float:
        raw = max(0.0, min(float(raw), self.adc_max_code))
        return raw * float(self.adc_full_scale_mV) / self.adc_max_code

    def convert_cgm_value(self, raw: int, mv: float) -> float:
        if not self.cgm_show_voltage:
            return float(raw)

        v = float(mv)
        if self.cgm_use_offset_remove:
            v -= self.cgm_offset_mV

        if self.cgm_show_current:
            return v / max(1e-12, self.cgm_Rf_Mohm)

        return v

    def update_adc_debug_label(self):
        if self.last_seq is None:
            self.adc_debug_label.setText("ADC: --")
            return

        if self.last_cgm_mv is None or self.last_plot_y is None:
            self.adc_debug_label.setText(
                f"ADC status/error code={self.last_raw_avg} | "
                f"sps={self.last_sps} | seq={self.last_seq} | tick={self.last_tick_ms} ms"
            )
            return

        unit = "raw" if not self.cgm_show_voltage else ("nA" if self.cgm_show_current else "mV")
        self.adc_debug_label.setText(
            f"ADC raw={self.last_raw_avg} | cgm_mv={self.last_cgm_mv:.3f} mV | "
            f"plot={self.last_plot_y:.3f} {unit} | "
            f"sps={self.last_sps} | seq={self.last_seq} | tick={self.last_tick_ms} ms | "
            f"filter={'ON' if self.filter_enabled else 'OFF'}"
        )

    def apply_digital_filter(self, value: float) -> float:
        """Light post-filter: median-of-3 spike rejection + EMA smoothing."""
        v = float(value)
        if not self.filter_enabled:
            return v
        self.filter_hist.append(v)
        if len(self.filter_hist) >= 3:
            med = float(np.median(np.asarray(self.filter_hist, dtype=np.float64)))
        else:
            med = v

        if self.filter_ema is None:
            self.filter_ema = med
        else:
            a = float(self.filter_ema_alpha)
            self.filter_ema = (a * med) + ((1.0 - a) * float(self.filter_ema))
        return float(self.filter_ema)

    def on_save_check_changed(self, checked):
        self.save_pre_enabled = self.save_pre_chk.isChecked()
        self.save_post_enabled = self.save_post_chk.isChecked()
        if self.save_enabled and not (self.save_pre_enabled or self.save_post_enabled):
            self.status_label.setText("Select Pre filter and/or Post filter to save data.")

    def on_filter_toggled(self, checked):
        self.filter_enabled = bool(checked)
        self.filter_hist.clear()
        self.filter_ema = None
        self.update_adc_debug_label()

    def on_filter_hist_changed(self, v):
        hist_len = int(v)
        if hist_len < 1:
            hist_len = 1
        # Median filter is most useful with odd window lengths.
        if hist_len > 1 and (hist_len % 2) == 0:
            hist_len += 1
            self.filter_hist_spin.blockSignals(True)
            self.filter_hist_spin.setValue(hist_len)
            self.filter_hist_spin.blockSignals(False)
        old_vals = list(self.filter_hist)[-(hist_len):]
        self.filter_hist = deque(old_vals, maxlen=hist_len)
        self.filter_ema = None
        self.update_adc_debug_label()

    def on_filter_alpha_changed(self, v):
        self.filter_ema_alpha = float(v)
        self.filter_ema = None
        self.update_adc_debug_label()

    def on_save_duration_changed(self, v):
        self.save_duration_sec = max(1.0, float(v))

    def on_save_path_edited(self):
        path = self.save_path_edit.text().strip()
        if path:
            self.save_path = path
            self.save_path_edit.setToolTip(path)

    def on_save_path_clicked(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select data save folder", self.save_path)
        if path:
            self.save_path = path
            self.save_path_edit.setText(path)
            self.save_path_edit.setToolTip(path)

    def on_data_save_toggled(self, checked):
        if checked:
            self.start_data_save()
        else:
            self.stop_data_save()

    def start_data_save(self):
        self.save_pre_enabled = self.save_pre_chk.isChecked()
        self.save_post_enabled = self.save_post_chk.isChecked()
        if not (self.save_pre_enabled or self.save_post_enabled):
            self.data_save_btn.blockSignals(True)
            self.data_save_btn.setChecked(False)
            self.data_save_btn.blockSignals(False)
            self.status_label.setText("Select Pre filter and/or Post filter before Data Save.")
            return

        self.on_save_path_edited()
        self.save_duration_sec = max(1.0, float(self.save_duration_spin.value()))
        os.makedirs(self.save_path, exist_ok=True)
        self.close_save_files()
        self.save_enabled = True
        self.save_session_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.save_file_index = 0
        self.open_save_files(force_new=True)
        self.status_label.setText(f"Data save started: {self.save_path}")

    def stop_data_save(self):
        was_enabled = self.save_enabled
        self.save_enabled = False
        self.close_save_files()
        if hasattr(self, "data_save_btn") and self.data_save_btn.isChecked():
            self.data_save_btn.blockSignals(True)
            self.data_save_btn.setChecked(False)
            self.data_save_btn.blockSignals(False)
        if was_enabled:
            self.status_label.setText("Data save stopped.")

    def close_save_files(self):
        for f in (self.save_pre_file, self.save_post_file):
            try:
                if f:
                    f.flush()
                    f.close()
            except Exception:
                pass
        self.save_pre_file = None
        self.save_post_file = None
        self.save_pre_writer = None
        self.save_post_writer = None
        self.save_pre_count = 0
        self.save_post_count = 0

    def samples_per_file_limit(self) -> int:
        sps = self.last_sps if self.last_sps else int(self.sample_rate_combo.currentData())
        if sps <= 0:
            sps = 1
        duration_sec = max(1.0, float(self.save_duration_sec))
        return max(1, int(round(duration_sec * float(sps))))

    def _new_csv_file(self, kind: str):
        session = self.save_session_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"CGM_{kind}_{session}_part{self.save_file_index:04d}.csv"
        filepath = os.path.join(self.save_path, filename)
        f = open(filepath, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(f)
        writer.writerow(["datetime", "sample_value"])
        return f, writer

    def open_save_files(self, force_new: bool = False):
        if force_new:
            self.close_save_files()

        if self.save_pre_enabled and self.save_pre_file is None:
            self.save_pre_file, self.save_pre_writer = self._new_csv_file("pre_filter")
            self.save_pre_count = 0

        if self.save_post_enabled and self.save_post_file is None:
            self.save_post_file, self.save_post_writer = self._new_csv_file("post_filter")
            self.save_post_count = 0

    def rotate_save_files_if_needed(self):
        limit = self.samples_per_file_limit()
        need_rotate = False
        if self.save_pre_enabled and self.save_pre_file is not None and self.save_pre_count >= limit:
            need_rotate = True
        if self.save_post_enabled and self.save_post_file is not None and self.save_post_count >= limit:
            need_rotate = True
        if need_rotate:
            self.save_file_index += 1
            self.open_save_files(force_new=True)

    def save_sample(self, pre_value: float, post_value: float):
        if not self.save_enabled:
            return
        if not (self.save_pre_enabled or self.save_post_enabled):
            return

        self.open_save_files(force_new=False)
        self.rotate_save_files_if_needed()

        now_str = datetime.now().isoformat(timespec="milliseconds")

        try:
            if self.save_pre_enabled and self.save_pre_writer is not None:
                self.save_pre_writer.writerow([now_str, f"{float(pre_value):.9g}"])
                self.save_pre_count += 1
                if self.save_pre_file is not None:
                    self.save_pre_file.flush()

            if self.save_post_enabled and self.save_post_writer is not None:
                self.save_post_writer.writerow([now_str, f"{float(post_value):.9g}"])
                self.save_post_count += 1
                if self.save_post_file is not None:
                    self.save_post_file.flush()
        except Exception as e:
            self.status_label.setText(f"Save error: {e}")
            self.stop_data_save()

    def current_x_window(self):
        return float(self.shared_xspan_spin.value())

    def apply_cgm_x_range(self):
        last_t = float(self.cgm_t[-1]) if self.cgm_t else 0.0
        win = self.current_x_window()
        if last_t <= win:
            xmin, xmax = 0.0, max(1.0, last_t)
        else:
            xmin, xmax = last_t - win, last_t
        self.cgm_plot.setXRange(xmin, xmax, padding=0.0)

    def update_plots(self):
        if self.cgm_t:
            x = np.asarray(self.cgm_t, dtype=np.float64)
            y = np.asarray(self.cgm_y, dtype=np.float32)
            self.cgm_curve.setData(x, y)

        if self.cgm_auto_x_btn.isChecked():
            self.apply_cgm_x_range()

        if self.cgm_auto_y_btn.isChecked():
            self.fit_cgm_y_range()

    def update_rate(self):
        ms = self.rate_t0.elapsed()
        if ms < 250:
            return
        bps_bytes = (self.rate_bytes_acc * 1000.0) / max(1.0, float(ms))
        bps_bits = bps_bytes * 8.0
        pps = (self.rate_pkts_acc * 1000.0) / max(1.0, float(ms))
        if bps_bits >= 1_000_000.0:
            rate_str = f"{bps_bits/1_000_000.0:.2f} Mbps"
        elif bps_bits >= 1_000.0:
            rate_str = f"{bps_bits/1_000.0:.1f} kbps"
        else:
            rate_str = f"{bps_bits:.0f} bps"
        self.rate_label.setText(f"Rate: {rate_str} ({bps_bytes:.0f} B/s, {pps:.1f} pkts/s)")
        self.rate_bytes_acc = 0
        self.rate_pkts_acc = 0
        self.rate_t0.restart()

    def clear_all(self):
        self.cgm_t.clear()
        self.cgm_y.clear()
        self.rx_buf.clear()
        self.plot_time_next_sec = 0.0
        self.filter_hist.clear()
        self.filter_ema = None
        self.cgm_curve.setData([], [])
        self.apply_cgm_x_range()

    def on_cgm_voltage_toggled(self, checked):
        self.cgm_show_voltage = bool(checked)
        if not self.cgm_show_voltage:
            self.cgm_current_btn.setChecked(False)
            self.cgm_show_current = False
            self.cgm_offset_btn.setChecked(False)
            self.cgm_use_offset_remove = False
        self.cgm_current_btn.setEnabled(self.cgm_show_voltage)
        self.cgm_offset_btn.setEnabled(self.cgm_show_voltage)
        self.update_cgm_y_label()

    def on_cgm_current_toggled(self, checked):
        self.cgm_show_current = bool(checked) and self.cgm_show_voltage
        self.update_cgm_y_label()
        self.update_adc_debug_label()

    def on_cgm_offset_toggled(self, checked):
        self.cgm_use_offset_remove = bool(checked) and self.cgm_show_voltage
        self.update_adc_debug_label()

    def on_cgm_offset_value_changed(self, v):
        self.cgm_offset_mV = float(v)
        self.update_adc_debug_label()

    def on_cgm_rf_changed(self, v):
        self.cgm_Rf_Mohm = float(v)
        self.update_cgm_y_label()
        self.update_adc_debug_label()

    def update_cgm_mouse_mode(self):
        x_free = not self.cgm_auto_x_btn.isChecked()
        y_free = not self.cgm_auto_y_btn.isChecked()
        self.cgm_plot.setMouseEnabled(x=x_free, y=y_free)

    def on_cgm_auto_x_toggled(self, checked):
        self.update_cgm_mouse_mode()
        if checked:
            self.apply_cgm_x_range()

    def on_cgm_auto_y_toggled(self, checked):
        self.update_cgm_mouse_mode()
        if checked:
            self.fit_cgm_y_range()

    def fit_cgm_y_range(self):
        ymin = float(self.cgm_ymin_spin.value())
        ymax = float(self.cgm_ymax_spin.value())
        if ymax <= ymin:
            ymax = ymin + 1.0
        self.cgm_plot.setYRange(ymin, ymax, padding=0.02)

    def update_cgm_y_label(self):
        if not self.cgm_show_voltage:
            self.cgm_plot.setLabel("left", "CGM (raw ADC code)")
        elif self.cgm_show_current:
            self.cgm_plot.setLabel("left", "CGM (nA)")
        else:
            self.cgm_plot.setLabel("left", "CGM (mV)")


async def main_async():
    app = QtWidgets.QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    win = MainWindow()
    win.show()
    app.aboutToQuit.connect(lambda: asyncio.create_task(win.cleanup_connection()))
    with loop:
        try:
            loop.run_forever()
        finally:
            try:
                await win.cleanup_connection()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main_async())
