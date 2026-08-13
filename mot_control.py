"""
OpenQuantum Controls UI.

Single-laser rubidium MOT control interface. The one Koheron-controlled laser
is locked with one Linien/Red Pitaya spectroscopy signal. The repump VCO tune
voltage (V_tune) is set by the DAC Click (MCP4921) on the Red Pitaya Click
Shield through the patched linien-server's ``dac_click_voltage`` parameter,
and the repump RF gate is a Linien GPIO bit (DIO1_N, active-low). The MOT
coils run from a DPS5005 supply; the dispenser runs from a DPS5020 with a
timed 10 s fire and a 15 min lockout cooldown.
"""

import os
import sys
import time

import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
EIT_CONTROLS = os.path.join(REPO_ROOT, "eit-controls")
if EIT_CONTROLS not in sys.path:
    sys.path.insert(0, EIT_CONTROLS)

from linien_widget import LinienWidget
from styles import (
    APP_STYLESHEET,
    NEON_BLUE,
    add_readout_row,
    add_slider_row as _eit_add_slider_row,
    make_card,
    make_section_title as _eit_make_section_title,
)
from workers import LaserSetpointWorker

from mot_logging import EventLog
from mot_workers import (
    DacClickWorker,
    DispenserWorker,
    LinienGpioWorker,
    RidenDPSWorker,
    SerialProbeWorker,
    _is_likely_usb_serial,
)

# Riden DPS protection register (reg 7) values.
DPS_PROTECTION = {1: "OVP", 2: "OCP", 3: "OPP"}

# App typography: IBM Plex Sans for body text, Space Grotesk for titles.
FONTS_DIR = os.path.join(HERE, "fonts")
BODY_FONT_FAMILY = "IBM Plex Sans"
TITLE_FONT_FAMILY = "Space Grotesk"


def load_app_fonts():
    """Register the font files bundled in fonts/.

    Both families ship with the repo under the SIL OFL (IBM Plex Sans for
    body text, Space Grotesk for titles); additional font files dropped
    into fonts/ are also registered but stay local (licensed files are
    gitignored). Fonts are registered from bytes because the path-based
    QFontDatabase.addApplicationFont silently fails on this Qt build
    (6.11/macOS). Missing families fall back to Helvetica Neue, with a
    console warning.
    """
    if os.path.isdir(FONTS_DIR):
        for name in sorted(os.listdir(FONTS_DIR)):
            if not name.lower().endswith((".ttf", ".otf")):
                continue
            path = os.path.join(FONTS_DIR, name)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                print(f"! could not read font {name}: {e}")
                continue
            if QtGui.QFontDatabase.addApplicationFontFromData(data) < 0:
                print(f"! could not register font: {name}")
    families = set(QtGui.QFontDatabase.families())
    for family in (BODY_FONT_FAMILY, TITLE_FONT_FAMILY):
        if family not in families:
            print(f"! font family not available, using fallback: {family}")


def build_app_stylesheet():
    return APP_STYLESHEET + (
        "\n* {"
        f' font-family: "{BODY_FONT_FAMILY}", "Helvetica Neue",'
        ' "Segoe UI", sans-serif; }\n'
    )


def make_section_title(text):
    label = _eit_make_section_title(text)
    label.setStyleSheet(
        label.styleSheet() + f' font-family: "{TITLE_FONT_FAMILY}";'
    )
    return label


def add_slider_row(*args, **kwargs):
    slider, edit = _eit_add_slider_row(*args, **kwargs)
    # The shared helper sizes its value edit for the old monospace font;
    # the proportional body font is wider, so give values like "1000 mV"
    # room (the smoke test checks every edit fits its extreme values).
    edit.setFixedWidth(112)

    # Make the value box actually editable: type a number (suffix
    # optional), press Enter or click away, and the slider follows —
    # clamped to the slider's range.
    def commit():
        import re

        match = re.search(r"[-+]?\d*\.?\d+", edit.text())
        if match is None:
            slider.valueChanged.emit(slider.value())  # restore display
            return
        raw = int(round(float(match.group(0)) * edit.scale))
        raw = max(edit.min_val, min(edit.max_val, raw))
        if raw == slider.value():
            slider.valueChanged.emit(raw)  # normalize/clamp the text
        else:
            slider.setValue(raw)

    edit.editingFinished.connect(commit)
    return slider, edit


class WheelGuard(QtCore.QObject):
    """Stop the mouse wheel from changing unfocused controls.

    The control column scrolls; without this, scrolling with the cursor
    over a slider silently changes a hardware setpoint. Wheel events on
    an unfocused control are redirected to the enclosing scroll area
    (click a control first if you do want wheel adjustment).
    """

    def eventFilter(self, obj, event):
        if isinstance(obj, QtWidgets.QScrollBar):
            # Scrollbars must keep native wheel behavior — and forwarding
            # them would recurse (viewport wheel is delivered via the bar).
            return False
        if (
            event.type() == QtCore.QEvent.Wheel
            and isinstance(obj, QtWidgets.QWidget)
            and not obj.hasFocus()
        ):
            parent = obj.parentWidget()
            while parent is not None and not isinstance(
                parent, QtWidgets.QScrollArea
            ):
                parent = parent.parentWidget()
            if parent is not None:
                QtWidgets.QApplication.sendEvent(parent.viewport(), event)
            return True
        return False


# Voltage to frequency conversion for Koheron drivers + 780 nm laser diodes.
_C = 299792458.0
_LAMBDA_0 = 780.2e-9
_MOD_GAIN = 2.5e-3
_CURRENT_COEFF = 2.0e-9
V_TO_GHZ = -_C / _LAMBDA_0**2 * _MOD_GAIN * _CURRENT_COEFF / 1e9

# Repump VCO tuning curve from the datasheet @25 C: V_tune (V) -> output
# frequency (MHz), linear interpolation between points, clamped at the ends.
# The DAC Click reaches 0-5 V of this curve (~1790-2382 MHz).
_VCO_VTUNE_V = [
    0.00, 0.50, 1.00, 1.50, 2.00, 3.00, 4.00, 5.00, 6.00, 6.50,
    7.00, 8.00, 9.00, 10.00, 11.00, 12.00, 12.50, 13.00, 13.50, 14.00,
]
_VCO_FREQ_MHZ = [
    1790.1, 1888.8, 1967.3, 2034.2, 2094.4, 2199.9, 2294.2, 2381.7, 2466.0,
    2505.7, 2546.8, 2625.8, 2703.8, 2782.1, 2861.2, 2940.8, 2979.7, 3019.9,
    3058.0, 3096.2,
]


def vtune_to_vco_mhz(vtune_v: float) -> float:
    """Convert a repump V_tune voltage to the VCO output frequency in MHz."""
    from bisect import bisect_left

    if vtune_v <= _VCO_VTUNE_V[0]:
        return _VCO_FREQ_MHZ[0]
    if vtune_v >= _VCO_VTUNE_V[-1]:
        return _VCO_FREQ_MHZ[-1]
    i = bisect_left(_VCO_VTUNE_V, vtune_v)
    v0, v1 = _VCO_VTUNE_V[i - 1], _VCO_VTUNE_V[i]
    f0, f1 = _VCO_FREQ_MHZ[i - 1], _VCO_FREQ_MHZ[i]
    w = (vtune_v - v0) / (v1 - v0)
    return f0 + w * (f1 - f0)


def thermistor_ohms_to_c(ohms: float) -> float:
    """Steinhart-Hart for the laser head's 10 k NTC thermistor.

    Coefficients carried over from the old qMOT-os rig (same laser head).
    """
    import math

    r = max(1e-3, float(ohms))
    ln_r = math.log(r)
    return 1.0 / (0.00113 + 0.000234 * ln_r + 8.78e-8 * ln_r**3) - 273.15

COMBO_STYLE = """
    QComboBox {
        background: #12121a;
        color: #c0c0d0;
        border: 1px solid #1e1e2e;
        border-radius: 4px;
        padding: 2px 6px;
        font-size: 12px;
    }
    QComboBox:focus {
        color: white;
        border-color: #4070b0;
    }
    QComboBox::drop-down {
        border: none;
        width: 18px;
    }
    QComboBox QAbstractItemView {
        background: #12121a;
        color: #c0c0d0;
        selection-background-color: #2a4a80;
        selection-color: white;
        outline: none;
    }
"""


def _style_combo(combo):
    combo.setStyleSheet(COMBO_STYLE)
    try:
        combo.view().setStyleSheet(
            "background: #12121a; color: #c0c0d0;"
            "selection-background-color: #2a4a80; selection-color: white;"
        )
    except Exception:
        pass


def _serial_port_label(port):
    bits = [port.device]
    if port.description and port.description != "n/a":
        bits.append(port.description)
    if port.manufacturer:
        bits.append(port.manufacturer)
    if port.serial_number:
        bits.append(f"SN {port.serial_number}")
    return " | ".join(str(bit) for bit in bits if bit)


def _score_port(port, purpose):
    haystack = " ".join(
        str(x or "")
        for x in (
            port.device,
            port.description,
            port.manufacturer,
            port.product,
            port.interface,
        )
    ).lower()
    score = 0
    if _is_likely_usb_serial(port):
        score += 10
    if purpose == "laser":
        for token in ("koheron", "ctl", "laser"):
            if token in haystack:
                score += 30
    else:
        for token in ("dps", "riden", "rd60", "ch340", "wch", "cp210", "ftdi"):
            if token in haystack:
                score += 20
    return score


def discover_serial_ports():
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    return list(list_ports.comports())


class CollapsibleCard(QtWidgets.QFrame):
    """Card with a clickable header that shows/hides its content."""

    def __init__(self, title, collapsed=True, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: #14141c;
                border: 1px solid #1e1e2a;
                border-radius: 10px;
            }
        """)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        self.toggle_btn = QtWidgets.QToolButton()
        self.toggle_btn.setText(title.upper())
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(not collapsed)
        self.toggle_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.toggle_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle_btn.setArrowType(
            QtCore.Qt.RightArrow if collapsed else QtCore.Qt.DownArrow
        )
        self.toggle_btn.setStyleSheet(
            f"QToolButton {{ color: {NEON_BLUE}; font-size: 11px;"
            "font-weight: 600; letter-spacing: 1px; border: none;"
            "background: transparent; padding: 2px 4px;"
            f' font-family: "{TITLE_FONT_FAMILY}"; }}'
        )
        self.toggle_btn.toggled.connect(self._on_toggled)
        header.addWidget(self.toggle_btn)
        header.addStretch()
        self.header_status = QtWidgets.QLabel("")
        self.header_status.setStyleSheet(
            "color: #8888a0; font-size: 11px; font-weight: 600; border: none;"
        )
        header.addWidget(self.header_status)
        outer.addLayout(header)

        self.content = QtWidgets.QWidget()
        self.content.setStyleSheet("background: transparent; border: none;")
        self.content_layout = QtWidgets.QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(6)
        outer.addWidget(self.content)
        self.content.setVisible(not collapsed)

    def _on_toggled(self, expanded):
        self.content.setVisible(expanded)
        self.toggle_btn.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow
        )

    def set_expanded(self, expanded):
        self.toggle_btn.setChecked(bool(expanded))

    def set_status(self, text, color="#8888a0"):
        self.header_status.setText(text)
        self.header_status.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600; border: none;"
        )


def infer_serial_assignments(ports, current):
    assignments = {}
    used = set()
    candidates = sorted(
        [port for port in ports if _is_likely_usb_serial(port)],
        key=lambda port: port.device,
    )

    for key in ("laser", "mot_coil", "dispenser"):
        if current.get(key):
            used.add(current[key])

    for key, purpose in (
        ("laser", "laser"),
        ("mot_coil", "dps"),
        ("dispenser", "dps"),
    ):
        if current.get(key):
            assignments[key] = current[key]
            continue
        available = [port for port in candidates if port.device not in used]
        if not available:
            assignments[key] = ""
            continue
        best = max(available, key=lambda port: (_score_port(port, purpose), port.device))
        if _score_port(best, purpose) <= 0:
            assignments[key] = ""
            continue
        assignments[key] = best.device
        used.add(best.device)
    return assignments


class CameraViewWidget(QtWidgets.QWidget):
    """USB camera panel with graceful no-camera fallback."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera = None
        self._capture_session = None
        self._camera_devices = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        title = QtWidgets.QLabel("USB CAMERA")
        title.setStyleSheet(
            f"color: {NEON_BLUE}; font-size: 12px; font-weight: 700;"
            "letter-spacing: 1px; border: none; padding: 4px 6px;"
            f'font-family: "{TITLE_FONT_FAMILY}";'
        )
        header.addWidget(title)
        header.addStretch()

        self.camera_combo = QtWidgets.QComboBox()
        _style_combo(self.camera_combo)
        self.camera_combo.currentIndexChanged.connect(self._on_camera_selected)
        header.addWidget(self.camera_combo, stretch=1)

        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.refresh_btn.clicked.connect(lambda: self.refresh(auto_start=True))
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        self.stack = QtWidgets.QStackedWidget()
        self.message_label = QtWidgets.QLabel("No USB camera found")
        self.message_label.setAlignment(QtCore.Qt.AlignCenter)
        self.message_label.setStyleSheet(
            "color: #8888a0; font-size: 16px; font-weight: 700;"
            "border: 1px solid #1e1e2a; border-radius: 8px;"
            "background: #0a0a0f; padding: 24px;"
        )
        self.stack.addWidget(self.message_label)

        try:
            from PyQt6.QtMultimediaWidgets import QVideoWidget

            self.video_widget = QVideoWidget()
            self.video_widget.setStyleSheet("background: #050508;")
            self.stack.addWidget(self.video_widget)
            self._multimedia_available = True
        except Exception as e:
            self.video_widget = None
            self._multimedia_available = False
            self.message_label.setText(f"Camera view unavailable: {e}")

        layout.addWidget(self.stack, stretch=1)
        self.refresh(auto_start=False)

    def refresh(self, auto_start=False):
        self.stop()
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        self._camera_devices = []

        if not self._multimedia_available:
            self.camera_combo.setEnabled(False)
            self.stack.setCurrentWidget(self.message_label)
            self.camera_combo.blockSignals(False)
            return

        try:
            from PyQt6.QtMultimedia import QMediaDevices

            self._camera_devices = list(QMediaDevices.videoInputs())
        except Exception as e:
            self.message_label.setText(f"Could not enumerate cameras: {e}")
            self.camera_combo.setEnabled(False)
            self.stack.setCurrentWidget(self.message_label)
            self.camera_combo.blockSignals(False)
            return

        if not self._camera_devices:
            self.message_label.setText("No USB camera found")
            self.camera_combo.addItem("No camera")
            self.camera_combo.setEnabled(False)
            self.stack.setCurrentWidget(self.message_label)
            self.camera_combo.blockSignals(False)
            return

        for camera in self._camera_devices:
            self.camera_combo.addItem(camera.description())
        self.camera_combo.setEnabled(True)
        self.camera_combo.blockSignals(False)
        if auto_start:
            self._start_camera(self.camera_combo.currentIndex())
        else:
            self.message_label.setText("Camera available")
            self.stack.setCurrentWidget(self.message_label)

    def activate(self):
        if not self._camera_devices:
            self.refresh(auto_start=True)
        else:
            self._start_camera(max(0, self.camera_combo.currentIndex()))

    def deactivate(self):
        self.stop()
        if self._camera_devices:
            self.message_label.setText("Camera available")
            self.stack.setCurrentWidget(self.message_label)

    def stop(self):
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
        self._camera = None
        self._capture_session = None

    def _on_camera_selected(self, index):
        if index >= 0 and self.isVisible():
            self._start_camera(index)

    def _start_camera(self, index):
        if not self._multimedia_available or not (0 <= index < len(self._camera_devices)):
            return
        self.stop()
        try:
            from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession

            self._camera = QCamera(self._camera_devices[index])
            try:
                self._camera.errorOccurred.connect(self._on_camera_error)
            except Exception:
                pass
            self._capture_session = QMediaCaptureSession()
            self._capture_session.setCamera(self._camera)
            self._capture_session.setVideoOutput(self.video_widget)
            self._camera.start()
            self.stack.setCurrentWidget(self.video_widget)
            QtCore.QTimer.singleShot(250, self._check_camera_error)
        except Exception as e:
            self.message_label.setText(f"Could not start camera: {e}")
            self.stack.setCurrentWidget(self.message_label)

    def _check_camera_error(self):
        if self._camera is None:
            return
        app = QtWidgets.QApplication.instance()
        if app is not None and hasattr(QtCore, "QCameraPermission"):
            try:
                permission = app.checkPermission(QtCore.QCameraPermission())
                if permission != QtCore.Qt.PermissionStatus.Granted:
                    self._on_camera_error(
                        None,
                        "Camera permission is not granted",
                    )
                    return
            except Exception:
                pass
        try:
            from PyQt6.QtMultimedia import QCamera

            if self._camera.error() != QCamera.Error.NoError:
                message = self._camera.errorString() or "Camera could not be started"
                self._on_camera_error(self._camera.error(), message)
        except Exception:
            pass

    def _on_camera_error(self, _error, message):
        if not message:
            message = "Camera could not be started"
        self.message_label.setText(message)
        self.stack.setCurrentWidget(self.message_label)


class DeviceConfigDialog(QtWidgets.QDialog):
    """Manual device config with best-effort USB discovery."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Device Configuration")
        self.resize(760, 560)
        self._parent = parent

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(10)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        form_box = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(form_box)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        form.setFormAlignment(QtCore.Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.rp_edit = QtWidgets.QLineEdit(parent.rp_edit.text())
        self.laser_edit = QtWidgets.QLineEdit(parent.laser_edit.text())
        self.mot_coil_edit = QtWidgets.QLineEdit(parent.mot_coil_port_edit.text())
        self.dispenser_edit = QtWidgets.QLineEdit(parent.dispenser_port_edit.text())

        for edit in (
            self.rp_edit,
            self.laser_edit,
            self.mot_coil_edit,
            self.dispenser_edit,
        ):
            edit.setMinimumWidth(250)

        form.addRow("Linien RP:", self.rp_edit)
        form.addRow("Koheron:", self.laser_edit)
        form.addRow("MOT Coil DPS5005:", self.mot_coil_edit)
        form.addRow("Dispenser DPS5020:", self.dispenser_edit)

        splitter.addWidget(form_box)

        discovered_box = QtWidgets.QWidget()
        discovered_layout = QtWidgets.QVBoxLayout(discovered_box)
        discovered_layout.setContentsMargins(0, 0, 0, 0)
        discovered_layout.addWidget(make_section_title("Discovered USB Devices"))
        self.discovered_list = QtWidgets.QListWidget()
        self.discovered_list.setStyleSheet(
            "QListWidget { background: #0e0e16; color: #c0c0d0;"
            "border: 1px solid #1e1e2a; border-radius: 6px; }"
            "QListWidget::item { padding: 4px; }"
        )
        discovered_layout.addWidget(self.discovered_list, stretch=1)
        splitter.addWidget(discovered_box)
        splitter.setSizes([430, 330])

        self.hint_label = QtWidgets.QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #8888a0; border: none;")
        root.addWidget(self.hint_label)

        button_row = QtWidgets.QHBoxLayout()
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_discovery)
        self.best_guess_btn = QtWidgets.QPushButton("Apply Best Guess")
        self.best_guess_btn.clicked.connect(lambda: self.apply_best_guess(force=True))
        button_row.addWidget(self.refresh_btn)
        button_row.addWidget(self.best_guess_btn)
        button_row.addStretch()
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.clicked.connect(self.accept)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.apply_btn)
        root.addLayout(button_row)

        self.refresh_discovery()

    def values(self):
        return {
            "rp": self.rp_edit.text().strip(),
            "laser": self.laser_edit.text().strip(),
            "mot_coil": self.mot_coil_edit.text().strip(),
            "dispenser": self.dispenser_edit.text().strip(),
        }

    def refresh_discovery(self):
        self._serial_ports = discover_serial_ports()
        self.discovered_list.clear()
        for port in self._serial_ports:
            self.discovered_list.addItem(_serial_port_label(port))
        if not self._serial_ports:
            self.discovered_list.addItem("No USB serial devices found")
        self.apply_best_guess(force=False)

    def apply_best_guess(self, force=False):
        current = {
            "laser": "" if force else self.laser_edit.text().strip(),
            "mot_coil": "" if force else self.mot_coil_edit.text().strip(),
            "dispenser": "" if force else self.dispenser_edit.text().strip(),
        }
        guesses = infer_serial_assignments(self._serial_ports, current)
        if (force or not self.laser_edit.text().strip()) and guesses.get("laser"):
            self.laser_edit.setText(guesses["laser"])
        if (force or not self.mot_coil_edit.text().strip()) and guesses.get("mot_coil"):
            self.mot_coil_edit.setText(guesses["mot_coil"])
        if (force or not self.dispenser_edit.text().strip()) and guesses.get("dispenser"):
            self.dispenser_edit.setText(guesses["dispenser"])

        likely_count = len([port for port in self._serial_ports if _is_likely_usb_serial(port)])
        self.hint_label.setText(
            f"Found {likely_count} likely USB serial device(s). "
            "Best guesses fill empty fields only unless you press Apply Best Guess."
        )


class DispenserOverrideDialog(QtWidgets.QDialog):
    """Change the dispenser fire current/duration (confirmed afterwards)."""

    def __init__(self, parent, current_a, fire_seconds):
        super().__init__(parent)
        self.setWindowTitle("Dispenser Override")

        from mot_workers import DPS_MODELS

        max_current = DPS_MODELS["DPS5020"]["max_current"]

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(10)

        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.current_spin = QtWidgets.QDoubleSpinBox()
        self.current_spin.setRange(0.1, max_current)
        self.current_spin.setDecimals(2)
        self.current_spin.setSingleStep(0.1)
        self.current_spin.setSuffix(" A")
        self.current_spin.setValue(float(current_a))

        self.duration_spin = QtWidgets.QDoubleSpinBox()
        self.duration_spin.setRange(1.0, 120.0)
        self.duration_spin.setDecimals(0)
        self.duration_spin.setSingleStep(1.0)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(float(fire_seconds))

        form.addRow("Fire current:", self.current_spin)
        form.addRow("Fire duration:", self.duration_spin)
        root.addLayout(form)

        note = QtWidgets.QLabel(
            "The dispenser runs constant-current; the default of "
            f"{DispenserWorker.DISPENSE_CURRENT_A:.1f} A for "
            f"{DispenserWorker.FIRE_SECONDS:.0f} s is the safe operating "
            "point. Higher settings risk burning out the dispenser or "
            "overloading the pump."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #cc8830; border: none; font-size: 12px;")
        root.addWidget(note)

        button_row = QtWidgets.QHBoxLayout()
        defaults_btn = QtWidgets.QPushButton(
            f"Defaults ({DispenserWorker.DISPENSE_CURRENT_A:.1f} A / "
            f"{DispenserWorker.FIRE_SECONDS:.0f} s)"
        )
        defaults_btn.clicked.connect(self._fill_defaults)
        button_row.addWidget(defaults_btn)
        button_row.addStretch()
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QtWidgets.QPushButton("Apply...")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self.accept)
        button_row.addWidget(cancel_btn)
        button_row.addWidget(apply_btn)
        root.addLayout(button_row)

    def _fill_defaults(self):
        self.current_spin.setValue(DispenserWorker.DISPENSE_CURRENT_A)
        self.duration_spin.setValue(DispenserWorker.FIRE_SECONDS)

    def values(self):
        return self.current_spin.value(), self.duration_spin.value()


class OpenQuantumControlsWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        rp_host=None,
        laser_port=None,
        mot_coil_port=None,
        dispenser_port=None,
        mot_coil_address=1,
        dispenser_address=1,
        vtune_min=0.0,
        vtune_max=5.0,
    ):
        super().__init__()
        self._settings = QtCore.QSettings("OQ-Controls", "OpenQuantumControls")

        self._rp_host = rp_host or self._settings.value("rp/host", "")
        self._laser_port = laser_port or self._settings.value("laser/port", "")
        self._mot_coil_port = mot_coil_port or self._settings.value(
            "dps/mot_coil_port", ""
        )
        self._dispenser_port = dispenser_port or self._settings.value(
            "dps/dispenser_port", ""
        )
        self._mot_coil_address = int(
            self._settings.value("dps/mot_coil_address", mot_coil_address)
        )
        self._dispenser_address = int(
            self._settings.value("dps/dispenser_address", dispenser_address)
        )
        # DAC Click full scale is 0-5 V (hardware Vref on the Click board).
        self._vtune_min = float(self._settings.value("dac/vtune_min", vtune_min))
        self._vtune_max = float(self._settings.value("dac/vtune_max", vtune_max))
        if self._vtune_min > self._vtune_max:
            self._vtune_min, self._vtune_max = self._vtune_max, self._vtune_min
        self._dispenser_cooldown_until = float(
            self._settings.value("dispenser/cooldown_until", 0.0) or 0.0
        )
        self._dispenser_fire_current = self._float_setting(
            "dispenser/fire_current_a", DispenserWorker.DISPENSE_CURRENT_A
        )
        self._dispenser_fire_seconds = self._float_setting(
            "dispenser/fire_seconds", DispenserWorker.FIRE_SECONDS
        )
        self._dispenser_phase = "idle"
        self._vtune_slider_synced = False

        self._event_log = EventLog(os.path.join(HERE, "logs"))
        self._readback_log_t = {}
        self._readback_log_output = {}
        self._readback_log_protection = {}
        self._log_debounce = {}
        self._last_logged_vtune = None
        self._event_log.log("app_start")

        self.setWindowTitle("OpenQuantum Controls")
        self.resize(1540, 960)
        self._build_menu()

        central = QtWidgets.QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._build_spectroscopy_column(root)
        self._build_control_column(root)

        # Route event-log records into the log pane (GUI thread only).
        self._event_log.on_record = self._on_log_record

        # Wheel events must never change an unfocused control (this covers
        # every slider/combo/spinbox, including the Linien sweep slider).
        self._wheel_guard = WheelGuard(self)
        for widget in self.findChildren(
            (
                QtWidgets.QAbstractSlider,
                QtWidgets.QComboBox,
                QtWidgets.QAbstractSpinBox,
            )
        ):
            if isinstance(widget, QtWidgets.QScrollBar):
                continue  # scrollbars keep native wheel scrolling
            widget.setFocusPolicy(QtCore.Qt.StrongFocus)
            widget.installEventFilter(self._wheel_guard)

        self._restore_ui_state()

        QtCore.QTimer.singleShot(0, self._deferred_start)

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def _int_setting(self, key, default, lo=None, hi=None):
        try:
            value = int(float(self._settings.value(key, default)))
        except (TypeError, ValueError):
            value = int(default)
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        return value

    def _float_setting(self, key, default):
        try:
            return float(self._settings.value(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _bool_setting(self, key, default):
        value = self._settings.value(key, default)
        return str(value).lower() not in ("false", "0", "no", "none")

    # ------------------------------------------------------------------
    # Small UI helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tip(text, *widgets, help_cursor=False):
        """Attach a hover tooltip; help_cursor shows a (?) cursor on hover."""
        for widget in widgets:
            widget.setToolTip(text)
            if help_cursor:
                widget.setCursor(QtCore.Qt.WhatsThisCursor)

    def _set_chip(self, key, text, color):
        chip = self.health_chips[key]
        chip.setText(text)
        chip.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 700;"
            "border: 1px solid #1e1e2a; border-radius: 6px;"
            "padding: 5px 10px; background: #0e0e16;"
            f'font-family: "{TITLE_FONT_FAMILY}";'
        )

    def _restore_ui_state(self):
        geometry = self._settings.value("ui/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter_sizes = self._settings.value("ui/splitter_sizes")
        if splitter_sizes:
            try:
                self.main_splitter.setSizes([int(s) for s in splitter_sizes])
            except (TypeError, ValueError):
                pass
        if self._bool_setting("ui/camera_visible", False):
            self.camera_toggle.setChecked(True)
        self.usb_card.set_expanded(
            self._bool_setting("ui/usb_card_expanded", False)
        )
        self.log_card.set_expanded(
            self._bool_setting("ui/log_card_expanded", False)
        )
        # Persist expansion choices as they happen.
        self.usb_card.toggle_btn.toggled.connect(
            lambda expanded: self._settings.setValue(
                "ui/usb_card_expanded", bool(expanded)
            )
        )
        self.log_card.toggle_btn.toggled.connect(
            lambda expanded: self._settings.setValue(
                "ui/log_card_expanded", bool(expanded)
            )
        )

    def _save_ui_state(self):
        self._settings.setValue("ui/geometry", self.saveGeometry())
        self._settings.setValue("ui/splitter_sizes", self.main_splitter.sizes())
        self._settings.setValue(
            "ui/camera_visible", self.camera_toggle.isChecked()
        )

    def _on_log_record(self, record):
        """Mirror an event-log record into the collapsible log pane."""
        if not hasattr(self, "log_view"):
            return
        timestamp = record.get("t", "")[11:19]
        extras = " ".join(
            f"{k}={v}" for k, v in record.items() if k not in ("t", "event")
        )
        line = f"{timestamp}  {record.get('event', '?')}"
        if extras:
            line += f"  {extras}"
        self.log_view.appendPlainText(line[:300])
        summary = f"{timestamp} {record.get('event', '?')}"
        if len(summary) > 28:
            summary = summary[:27] + "\N{HORIZONTAL ELLIPSIS}"
        self.log_card.set_status(summary)

    # ------------------------------------------------------------------
    # Event logging helpers
    # ------------------------------------------------------------------

    READBACK_LOG_PERIOD_S = 10.0

    def _log_connection(self, device, status):
        print(f"{device}: {status}")
        self._event_log.log("connection", device=device, status=status)

    def _log_fault(self, device, message):
        print(f"! {device} fault: {message}")
        self._event_log.log("fault", device=device, message=str(message))

    def _debounced_log(self, key, event, payload_fn, delay_ms=1500):
        """Log once after a burst of changes (e.g. a slider drag) settles."""
        timer = self._log_debounce.get(key)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            self._log_debounce[key] = timer
        try:
            timer.timeout.disconnect()
        except TypeError:
            pass
        timer.timeout.connect(lambda: self._event_log.log(event, **payload_fn()))
        timer.start(delay_ms)

    def _log_readback(self, channel, state):
        """Decimated supply readback logging: every 10 s, plus immediately
        on any output-enable or protection change."""
        now = time.monotonic()
        output = bool(state.get("output_enabled"))
        protection = int(state.get("protection", 0) or 0)
        due = (
            now - self._readback_log_t.get(channel, -1e9)
            >= self.READBACK_LOG_PERIOD_S
        )
        output_changed = self._readback_log_output.get(channel) != output
        protection_changed = (
            self._readback_log_protection.get(channel) != protection
        )
        if not (due or output_changed or protection_changed):
            return
        self._readback_log_t[channel] = now
        self._readback_log_output[channel] = output
        self._readback_log_protection[channel] = protection
        self._event_log.log(
            "readback",
            channel=channel,
            v_out=state.get("v_out"),
            i_out=state.get("i_out"),
            power=state.get("power"),
            output=output,
            cv_cc=int(state.get("cv_cc", 0) or 0),
            protection=protection,
        )
        if protection_changed and protection:
            self._event_log.log(
                "protection_trip",
                channel=channel,
                kind=DPS_PROTECTION.get(protection, str(protection)),
            )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_menu(self):
        self.menuBar().setStyleSheet("""
            QMenuBar {
                background: #0a0a0f;
                color: #c0c0d0;
                border-bottom: 1px solid #1e1e2a;
            }
            QMenuBar::item {
                background: transparent;
                padding: 4px 10px;
            }
            QMenuBar::item:selected {
                background: #1e1e2e;
                color: white;
            }
            QMenu {
                background: #12121a;
                color: #c0c0d0;
                border: 1px solid #1e1e2a;
            }
            QMenu::item:selected {
                background: #2a4a80;
                color: white;
            }
        """)
        devices_menu = self.menuBar().addMenu("Devices")

        configure_action = QtGui.QAction("Configure Devices...", self)
        configure_action.triggered.connect(self._open_device_config_dialog)
        devices_menu.addAction(configure_action)

        discover_action = QtGui.QAction("Auto Discover USB Devices", self)
        discover_action.triggered.connect(lambda: self._run_usb_probe(startup=False))
        devices_menu.addAction(discover_action)

    def _build_spectroscopy_column(self, root):
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)

        title_row = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel("OpenQuantum")
        self.title_label.setStyleSheet(
            f"color: {NEON_BLUE}; font-size: 22px; font-weight: 700;"
            "letter-spacing: 3px; border: none; padding: 4px 6px;"
            f'font-family: "{TITLE_FONT_FAMILY}";'
        )
        title_row.addWidget(self.title_label)
        title_row.addStretch()

        # Health strip: one glance from across the lab tells you the state
        # of the lock, the USB devices, the coils, and the dispenser.
        self.health_chips = {}
        for key in ("lock", "usb", "coils", "disp"):
            chip = QtWidgets.QLabel("--")
            title_row.addWidget(chip)
            self.health_chips[key] = chip
        self._set_chip("lock", "LOCK --", "#8888a0")
        self._set_chip("usb", "USB --", "#8888a0")
        self._set_chip("coils", "COILS OFF", "#8888a0")
        self._set_chip("disp", "DISP IDLE", "#8888a0")
        self._tip(
            "System health: laser lock, USB devices, MOT coils, dispenser.",
            *self.health_chips.values(),
            help_cursor=True,
        )
        left.addLayout(title_row)

        view_bar = QtWidgets.QHBoxLayout()
        view_bar.setSpacing(8)
        spectroscopy_label = QtWidgets.QLabel(
            "Laser Spectroscopy / Repump Error Signal"
        )
        spectroscopy_label.setStyleSheet(
            f"color: {NEON_BLUE}; font-size: 12px; font-weight: 700;"
            "letter-spacing: 1px; border: none; padding: 4px 6px;"
            f'font-family: "{TITLE_FONT_FAMILY}";'
        )
        view_bar.addWidget(spectroscopy_label)
        view_bar.addStretch()
        self.camera_toggle = QtWidgets.QCheckBox("Show USB Camera")
        self.camera_toggle.setCursor(QtCore.Qt.PointingHandCursor)
        self.camera_toggle.setStyleSheet(
            "QCheckBox { color: #c0c0d0; font-size: 12px; font-weight: 700;"
            "border: none; padding: 4px 6px; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
        )
        self.camera_toggle.toggled.connect(self._toggle_camera_view)
        self._tip(
            "Show the USB camera pane below the spectroscopy plot.",
            self.camera_toggle,
        )
        view_bar.addWidget(self.camera_toggle)
        left.addLayout(view_bar)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStyleSheet("""
            QSplitter::handle {
                background: #1e1e2a;
                height: 4px;
            }
            QSplitter::handle:hover {
                background: #2a4a80;
            }
        """)
        self.linien_widget = LinienWidget(
            linien_host=self._rp_host,
            v_to_freq_ghz=V_TO_GHZ,
        )
        self.camera_widget = CameraViewWidget()
        self.camera_widget.setVisible(False)
        self.main_splitter.addWidget(self.linien_widget)
        self.main_splitter.addWidget(self.camera_widget)
        self.main_splitter.setSizes([700, 260])
        left.addWidget(self.main_splitter, stretch=1)

        root.addLayout(left, stretch=3)

    def _toggle_camera_view(self, visible):
        self.camera_widget.setVisible(visible)
        self._settings.setValue("ui/camera_visible", bool(visible))
        if visible:
            self.main_splitter.setSizes([650, 300])
            self.camera_widget.activate()
        else:
            self.camera_widget.deactivate()

    def _build_control_column(self, root):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        # Wide enough that no card ever needs horizontal scrolling; the
        # smoke test asserts the content fits at the minimum width.
        scroll.setMinimumWidth(460)
        scroll.setMaximumWidth(600)

        inner = QtWidgets.QWidget()
        inner.setStyleSheet("background: transparent;")
        layout = QtWidgets.QVBoxLayout(inner)
        layout.setAlignment(QtCore.Qt.AlignTop)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 4, 0)
        scroll.setWidget(inner)

        self._add_connection_card(layout)
        self._add_lock_card(layout)
        self._add_laser_card(layout)
        self._add_repump_card(layout)
        self._add_dps_card(layout)
        self._add_log_card(layout)
        layout.addStretch()

        root.addWidget(scroll, stretch=1)

    def _add_connection_card(self, parent):
        card = CollapsibleCard("Device Connections", collapsed=True)
        self.usb_card = card
        layout = card.content_layout

        self._tip(
            "USB device assignments. Verified automatically at startup with\n"
            "read-only probes; the pop-up only appears if something is\n"
            "missing.",
            card.toggle_btn,
        )
        self.rp_edit = self._add_line_edit(
            layout, "Linien RP:", self._rp_host, "rp-XXXXXX.local"
        )
        self._tip(
            "Red Pitaya hostname or IP running linien-server (also carries\n"
            "the DAC Click V_tune and the repump gate).",
            self.rp_edit,
        )
        self.laser_edit, self.laser_usb_status = self._add_port_row(
            layout, "Koheron:", self._laser_port, "/dev/ttyUSB0"
        )
        self._tip("Koheron CTL200 serial port.", self.laser_edit)
        self.mot_coil_port_edit, self.coil_usb_status = self._add_port_row(
            layout, "Coil DPS5005:", self._mot_coil_port, "/dev/ttyUSB1"
        )
        self._tip("MOT coil DPS5005 serial port.", self.mot_coil_port_edit)
        self.dispenser_port_edit, self.dispenser_usb_status = self._add_port_row(
            layout, "Disp DPS5020:", self._dispenser_port, "/dev/ttyUSB2"
        )
        self._tip("Dispenser DPS5020 serial port.", self.dispenser_port_edit)

        addr_row = QtWidgets.QHBoxLayout()
        addr_row.setSpacing(8)
        addr_row.addWidget(self._small_label("DPS Addr:"))
        self.mot_coil_addr_spin = QtWidgets.QSpinBox()
        self.mot_coil_addr_spin.setRange(1, 247)
        self.mot_coil_addr_spin.setValue(self._mot_coil_address)
        self.dispenser_addr_spin = QtWidgets.QSpinBox()
        self.dispenser_addr_spin.setRange(1, 247)
        self.dispenser_addr_spin.setValue(self._dispenser_address)
        addr_row.addWidget(self._tiny_label("Coil"))
        addr_row.addWidget(self.mot_coil_addr_spin)
        addr_row.addWidget(self._tiny_label("Disp"))
        addr_row.addWidget(self.dispenser_addr_spin)
        layout.addLayout(addr_row)
        self._tip(
            "Modbus slave addresses of the DPS supplies (front panel\n"
            "setting; usually 1).",
            self.mot_coil_addr_spin,
            self.dispenser_addr_spin,
        )

        button_row = QtWidgets.QHBoxLayout()
        button_row.setSpacing(6)
        self.device_dialog_btn = QtWidgets.QPushButton("Devices...")
        self.device_dialog_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.device_dialog_btn.clicked.connect(self._open_device_config_dialog)
        self._tip(
            "Open the device configuration dialog (discovered ports,\n"
            "manual entry).",
            self.device_dialog_btn,
        )
        self.auto_discover_btn = QtWidgets.QPushButton("Auto Discover")
        self.auto_discover_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.auto_discover_btn.clicked.connect(
            lambda: self._run_usb_probe(startup=False)
        )
        self._tip(
            "Re-run the USB probe: identify each device with read-only\n"
            "commands and reassign ports (briefly disconnects the serial\n"
            "workers).",
            self.auto_discover_btn,
        )
        button_row.addWidget(self.device_dialog_btn)
        button_row.addWidget(self.auto_discover_btn)
        layout.addLayout(button_row)

        self.connect_btn = QtWidgets.QPushButton("Connect / Reconnect")
        self.connect_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.connect_btn.clicked.connect(self._connect_all)
        self._tip(
            "Restart every connection (Linien, DAC, gate, and all serial\n"
            "workers) using the fields above.",
            self.connect_btn,
        )
        layout.addWidget(self.connect_btn)
        parent.addWidget(card)

    def _add_log_card(self, parent):
        card = CollapsibleCard("Event Log", collapsed=True)
        self.log_card = card
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFixedHeight(150)
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background: #0a0a0f; color: #9a9ab0;"
            "border: 1px solid #1e1e2a; border-radius: 6px;"
            "font-size: 11px; padding: 4px; }"
        )
        self._tip(
            "Recent events (fires, faults, protection trips, lock changes).\n"
            "The full history is in logs/mot-YYYYMMDD.jsonl.",
            self.log_view,
        )
        card.content_layout.addWidget(self.log_view)
        parent.addWidget(card)

    def _add_port_row(self, layout, label_text, value, placeholder):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._small_label(label_text))
        edit = QtWidgets.QLineEdit(str(value or ""))
        edit.setPlaceholderText(placeholder)
        row.addWidget(edit, stretch=1)
        status = QtWidgets.QLabel("--")
        status.setStyleSheet(
            "color: #8888a0; font-size: 12px; font-weight: 700; border: none;"
        )
        status.setFixedWidth(84)
        status.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        row.addWidget(status)
        layout.addLayout(row)
        return edit, status

    def _add_lock_card(self, parent):
        card = make_card()
        layout = card.layout()
        layout.addWidget(make_section_title("Linien Lock"))

        self.mod_freq_slider, self.mod_freq_edit = add_slider_row(
            layout,
            "Mod Freq:",
            1,
            50000,
            self._int_setting("linien/mod_freq_khz", 15000, 1, 50000),
            single=1,
            page=1000,
            decimals=3,
            scale=1000.0,
            suffix=" MHz",
        )
        self.mod_freq_slider.valueChanged.connect(self._on_mod_freq)
        self._tip(
            "Lock-in modulation frequency (MHz). Type a value or drag;\n"
            "applied to the Linien server immediately.",
            self.mod_freq_slider,
            self.mod_freq_edit,
        )

        self.mod_phase_slider, self.mod_phase_edit = add_slider_row(
            layout,
            "Mod Phase:",
            0,
            360,
            self._int_setting("linien/mod_phase_deg", 0, 0, 360),
            single=1,
            page=10,
            suffix="\N{DEGREE SIGN}",
        )
        self.mod_phase_slider.valueChanged.connect(self._on_mod_phase)
        self._tip(
            "Demodulation phase (degrees). Rotates the error signal shape.",
            self.mod_phase_slider,
            self.mod_phase_edit,
        )

        self.mod_strength_slider, self.mod_strength_edit = add_slider_row(
            layout,
            "Mod Strength:",
            0,
            1000,
            self._int_setting("linien/mod_strength_mv", 0, 0, 1000),
            single=1,
            page=50,
            suffix=" mV",
        )
        self.mod_strength_slider.valueChanged.connect(self._on_mod_strength)
        self._tip(
            "Modulation amplitude (mV) driving the current dither.\n"
            "0 mV disables the dither (no error signal).",
            self.mod_strength_slider,
            self.mod_strength_edit,
        )

        speed_row = QtWidgets.QHBoxLayout()
        speed_row.setSpacing(8)
        speed_row.addWidget(self._small_label("Sweep Speed:"))
        self.sweep_speed_combo = QtWidgets.QComboBox()
        _style_combo(self.sweep_speed_combo)
        for value, label in [
            (0, "3800 Hz"),
            (1, "1900 Hz"),
            (2, "950 Hz"),
            (3, "475 Hz"),
            (4, "237.5 Hz"),
            (5, "118.75 Hz"),
            (6, "59.4 Hz"),
            (7, "29.7 Hz"),
            (8, "14.8 Hz"),
            (9, "7.4 Hz"),
            (10, "3.7 Hz"),
            (11, "1.86 Hz"),
            (12, "0.93 Hz"),
            (13, "0.46 Hz"),
            (14, "0.23 Hz"),
            (15, "0.116 Hz"),
        ]:
            self.sweep_speed_combo.addItem(label, value)
        self.sweep_speed_combo.setCurrentIndex(
            self._int_setting("linien/sweep_speed_index", 8, 0, 15)
        )
        self.sweep_speed_combo.currentIndexChanged.connect(self._on_sweep_speed)
        speed_row.addWidget(self.sweep_speed_combo, stretch=1)
        layout.addLayout(speed_row)
        self._tip(
            "Linien sweep repetition rate. Slower sweeps show narrower\n"
            "features; faster sweeps update the trace more often.",
            self.sweep_speed_combo,
        )

        self.lock_readout = add_readout_row(layout, "Laser:", "Unlocked")
        self._tip(
            "Lock state reported by Linien. Use the plot's Lock Laser\n"
            "button after selecting a lock region on the trace.",
            self.lock_readout,
            help_cursor=True,
        )
        parent.addWidget(card)

    def _add_laser_card(self, parent):
        card = make_card()
        layout = card.layout()
        layout.addWidget(make_section_title("Koheron Laser Setpoints"))

        self.laser_status = add_readout_row(layout, "Koheron:", "No port set")
        self._tip(
            "Koheron CTL200 connection state.",
            self.laser_status,
            help_cursor=True,
        )
        self.laser_btn = QtWidgets.QPushButton("Laser Off")
        self.laser_btn.setCheckable(True)
        self.laser_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.laser_btn.toggled.connect(self._toggle_laser)
        layout.addWidget(self.laser_btn)
        self._tip("Toggle the laser diode output (lason).", self.laser_btn)

        self.current_slider, self.current_edit = add_slider_row(
            layout,
            "Current:",
            0,
            200,
            self._int_setting("laser/current_ma", 130, 0, 200),
            single=1,
            page=5,
            suffix=" mA",
        )
        self.current_slider.valueChanged.connect(self._on_current)
        self._tip(
            "Laser diode current setpoint (mA). Type a value or drag.",
            self.current_slider,
            self.current_edit,
        )
        self.therm_slider, self.therm_edit = add_slider_row(
            layout,
            "Thermistor:",
            5000,
            10000,
            self._int_setting("laser/thermistor_ohm", 6215, 5000, 10000),
            single=1,
            page=50,
            suffix=" \N{OHM SIGN}",
        )
        self.therm_slider.valueChanged.connect(self._on_thermistor)
        self._tip(
            "TEC thermistor setpoint (ohms). Lower resistance = warmer\n"
            "diode; see the estimated temperature below.",
            self.therm_slider,
            self.therm_edit,
        )
        self.therm_temp_readback = add_readout_row(layout, "Temp (est):", "--")
        self._update_therm_temp(self.therm_slider.value())
        self._tip(
            "Setpoint converted with the 10 k NTC Steinhart-Hart curve\n"
            "(estimate, not a measurement).",
            self.therm_temp_readback,
            help_cursor=True,
        )
        parent.addWidget(card)

    def _add_dps_card(self, parent):
        card = make_card()
        self.dps_card = card
        layout = card.layout()
        layout.addWidget(make_section_title("Power Supplies"))

        layout.addWidget(make_section_title("MOT Coils — DPS5005"))
        self.mot_coil_readback = add_readout_row(layout, "Readback:", "--")
        self._tip(
            "Measured coil supply output (V / A / W). Turns red if the\n"
            "supply trips a protection (OVP/OCP/OPP).",
            self.mot_coil_readback,
            help_cursor=True,
        )
        self.mot_coil_output_btn = QtWidgets.QPushButton("Coils Off")
        self.mot_coil_output_btn.setCheckable(True)
        self.mot_coil_output_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.mot_coil_output_btn.toggled.connect(self._toggle_mot_coils)
        layout.addWidget(self.mot_coil_output_btn)
        self._tip("Toggle the MOT coil supply output.", self.mot_coil_output_btn)
        self.mot_coil_voltage_slider, self.mot_coil_voltage_edit = add_slider_row(
            layout,
            "Voltage:",
            0,
            50000,
            self._int_setting("coil/voltage_mv", 5000, 0, 50000),
            single=10,
            page=1000,
            suffix=" V",
            decimals=3,
            scale=1000.0,
        )
        self.mot_coil_voltage_slider.valueChanged.connect(self._on_mot_coil_voltage)
        self._tip(
            "Coil supply voltage limit (V). With the coils' low resistance\n"
            "the supply usually runs current-limited; this is headroom.",
            self.mot_coil_voltage_slider,
            self.mot_coil_voltage_edit,
        )
        self.mot_coil_current_slider, self.mot_coil_current_edit = add_slider_row(
            layout,
            "Current:",
            0,
            5000,
            self._int_setting("coil/current_ma", 1000, 0, 5000),
            single=10,
            page=100,
            suffix=" A",
            decimals=3,
            scale=1000.0,
        )
        self.mot_coil_current_slider.valueChanged.connect(self._on_mot_coil_current)
        self._tip(
            "Coil current setpoint (A) — sets the MOT magnetic field\n"
            "gradient. Type a value or drag.",
            self.mot_coil_current_slider,
            self.mot_coil_current_edit,
        )

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setStyleSheet("color: #1e1e2a;")
        layout.addWidget(sep)

        layout.addWidget(make_section_title("Dispenser — DPS5020"))
        self.dispenser_readback = add_readout_row(layout, "Readback:", "--")
        self._tip(
            "Measured dispenser supply output (V / A / W) and CV/CC mode.\n"
            "Turns red if the supply trips a protection.",
            self.dispenser_readback,
            help_cursor=True,
        )
        # The dispense operating point defaults to 7.5 A constant-current
        # for 30 s and is only changeable through the confirmed Override
        # dialog; there are no free-form controls.
        self.dispenser_setpoint_label = add_readout_row(layout, "Setpoint:", "--")
        self._tip(
            "Fixed dispense operating point. Change it only through\n"
            "Override... (confirmation required).",
            self.dispenser_setpoint_label,
            help_cursor=True,
        )
        self.dispenser_history_label = add_readout_row(layout, "Fires:", "--")
        self._update_dispenser_history_label()
        self._tip(
            "Lifetime fire count and total seconds — tracks dispenser wear.",
            self.dispenser_history_label,
            help_cursor=True,
        )
        self.dispenser_fire_label = add_readout_row(layout, "Fire:", "Idle")
        self._tip(
            "Fire phase. After each fire a 15 min cooldown blocks re-firing\n"
            "(it survives an app restart).",
            self.dispenser_fire_label,
            help_cursor=True,
        )

        fire_row = QtWidgets.QHBoxLayout()
        fire_row.setSpacing(6)
        self.dispenser_fire_btn = QtWidgets.QPushButton(
            f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
        )
        self.dispenser_fire_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.dispenser_fire_btn.clicked.connect(self._fire_dispenser_clicked)
        fire_row.addWidget(self.dispenser_fire_btn, stretch=1)
        self._tip(
            "Run the dispenser at the fixed operating point, then auto-off.\n"
            "Click again mid-fire to abort (cooldown still applies).",
            self.dispenser_fire_btn,
        )
        self.dispenser_override_btn = QtWidgets.QPushButton("Override...")
        self.dispenser_override_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.dispenser_override_btn.clicked.connect(
            self._open_dispenser_override_dialog
        )
        fire_row.addWidget(self.dispenser_override_btn)
        self._tip(
            "Change the fire current/duration (confirmation required;\n"
            "locked while firing).",
            self.dispenser_override_btn,
        )
        layout.addLayout(fire_row)

        # Time-remaining bar: drains during a fire (orange) and during the
        # cooldown (blue); hidden when idle.
        self.dispenser_progress = QtWidgets.QProgressBar()
        self.dispenser_progress.setRange(0, 1000)
        self.dispenser_progress.setTextVisible(False)
        self.dispenser_progress.setFixedHeight(6)
        self.dispenser_progress.setVisible(False)
        layout.addWidget(self.dispenser_progress)
        self._tip(
            "Time remaining in the current fire or cooldown.",
            self.dispenser_progress,
        )

        self._update_dispenser_setpoint_text()
        parent.addWidget(card)

    def _add_repump_card(self, parent):
        card = make_card()
        self.repump_card = card
        layout = card.layout()
        layout.addWidget(make_section_title("Repump Frequency Tuning"))

        self.vtune_readback = add_readout_row(layout, "V_tune:", "--")
        self._tip(
            "Actual DAC Click output voltage read back from the Linien\n"
            "server (each write takes ~0.3 s).",
            self.vtune_readback,
            help_cursor=True,
        )
        self.vtune_freq_readback = add_readout_row(layout, "VCO Freq:", "--")
        self._tip(
            "Repump VCO output frequency from the datasheet tuning curve\n"
            "(V_tune -> MHz, 25 \N{DEGREE SIGN}C).",
            self.vtune_freq_readback,
            help_cursor=True,
        )
        vtune_min_mv = int(round(self._vtune_min * 1000))
        vtune_max_mv = int(round(self._vtune_max * 1000))
        vtune_initial_mv = self._int_setting(
            "dac/vtune_mv", 0, vtune_min_mv, vtune_max_mv
        )
        self.vtune_slider, self.vtune_edit = add_slider_row(
            layout,
            "V_tune:",
            vtune_min_mv,
            vtune_max_mv,
            vtune_initial_mv,
            single=1,
            page=100,
            suffix=" V",
            decimals=3,
            scale=1000.0,
        )
        self.vtune_slider.valueChanged.connect(self._on_vtune)
        self._tip(
            "Repump VCO tune voltage via the DAC Click (0-5 V full scale).\n"
            "Type a value or drag; the slider adopts the hardware value on\n"
            "connect.",
            self.vtune_slider,
            self.vtune_edit,
        )

        self.repump_gpio_btn = QtWidgets.QPushButton("Repump Gate Off")
        self.repump_gpio_btn.setCheckable(True)
        self.repump_gpio_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.repump_gpio_btn.toggled.connect(self._toggle_repump_gpio)
        layout.addWidget(self.repump_gpio_btn)
        self._tip(
            "Repump RF amplifier enable — Linien GPIO DIO1_N, active-low\n"
            "(fixed mapping).",
            self.repump_gpio_btn,
        )
        parent.addWidget(card)

    def _add_line_edit(self, layout, label_text, value, placeholder):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._small_label(label_text))
        edit = QtWidgets.QLineEdit(str(value or ""))
        edit.setPlaceholderText(placeholder)
        row.addWidget(edit, stretch=1)
        layout.addLayout(row)
        return edit

    @staticmethod
    def _small_label(text):
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(
            "color: #8888a0; font-size: 12px; font-weight: 600; border: none;"
        )
        label.setFixedWidth(120)
        return label

    @staticmethod
    def _tiny_label(text):
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(
            "color: #6a6a80; font-size: 11px; font-weight: 600; border: none;"
        )
        return label

    # ------------------------------------------------------------------
    # Startup / reconnect
    # ------------------------------------------------------------------

    def _deferred_start(self):
        if self._rp_host:
            self.linien_widget.start()
        self._connect_lock_signal()
        self._start_repump_workers()
        # Serial workers start once the USB probe has decided which port
        # hosts which device (see _on_probe_result).
        self._run_usb_probe(startup=True)

    def _connect_lock_signal(self):
        try:
            self.linien_widget.lock_state_changed.disconnect(self._on_lock_state)
        except TypeError:
            pass
        self.linien_widget.lock_state_changed.connect(self._on_lock_state)

    def _connect_all(self):
        self._save_connection_settings()

        rp_host = self.rp_edit.text().strip()
        if rp_host:
            self.linien_widget.set_host(rp_host)
        else:
            self.linien_widget.stop()
        self._connect_lock_signal()

        for attr in ("vtune_worker", "repump_gpio_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.stop()
        self._start_repump_workers()

        self._stop_serial_workers()
        self._start_serial_workers()

    # ------------------------------------------------------------------
    # USB probe / verified port assignment
    # ------------------------------------------------------------------

    _USB_STATUS_ROWS = ("laser_usb_status", "coil_usb_status", "dispenser_usb_status")

    def _run_usb_probe(self, startup=False):
        probe = getattr(self, "probe_worker", None)
        if probe is not None and probe.isRunning():
            return
        # Serial workers hold their ports open; release them for probing.
        self._stop_serial_workers()

        self._set_chip("usb", "USB ...", "#8888a0")
        self.usb_card.set_status("Probing…")
        for attr in self._USB_STATUS_ROWS:
            self._set_colored_text(getattr(self, attr), "Probing…", "#8888a0")

        self.probe_worker = SerialProbeWorker(
            saved={
                "laser": self.laser_edit.text().strip(),
                "mot_coil": self.mot_coil_port_edit.text().strip(),
                "dispenser": self.dispenser_port_edit.text().strip(),
            },
            coil_address=self.mot_coil_addr_spin.value(),
            dispenser_address=self.dispenser_addr_spin.value(),
        )
        self.probe_worker.result.connect(
            lambda res: self._on_probe_result(res, startup)
        )
        self.probe_worker.start()

    def _on_probe_result(self, res, startup=False):
        assignments = res.get("assignments", {})
        status = res.get("status", {})
        self._event_log.log(
            "usb_probe",
            ok=bool(res.get("ok")),
            assignments=assignments,
            status=status,
        )
        for key, edit in (
            ("laser", self.laser_edit),
            ("mot_coil", self.mot_coil_port_edit),
            ("dispenser", self.dispenser_port_edit),
        ):
            if assignments.get(key):
                edit.setText(assignments[key])

        for key, attr in (
            ("laser", "laser_usb_status"),
            ("mot_coil", "coil_usb_status"),
            ("dispenser", "dispenser_usb_status"),
        ):
            if status.get(key) == "verified":
                self._set_colored_text(getattr(self, attr), "Verified", "#40dd80")
            else:
                self._set_colored_text(getattr(self, attr), "Not found", "#dd5050")

        if res.get("ok"):
            self._set_chip("usb", "USB 3/3", "#40dd80")
            self.usb_card.set_status("3/3 verified", "#40dd80")
        else:
            n_ok = sum(
                1
                for key in ("laser", "mot_coil", "dispenser")
                if status.get(key) == "verified"
            )
            self._set_chip("usb", f"USB {n_ok}/3", "#dd5050")
            # Compact summary: the auto-expanded card's per-row statuses
            # show which device is missing.
            self.usb_card.set_status(f"{n_ok}/3 verified", "#dd5050")
            self.usb_card.set_expanded(True)

        self._save_connection_settings()

        if startup and not res.get("ok"):
            # Only interrupt with the pop-up when auto-detection failed.
            self._open_device_config_dialog()
        self._start_serial_workers()

    def _stop_serial_workers(self):
        if self._dispenser_phase == "firing":
            # Stopping the worker mid-fire skips its cooldown transition
            # (the output is still forced off on stop), so persist the
            # lockout here.
            self._on_dispenser_cooldown_started(
                time.time() + DispenserWorker.COOLDOWN_SECONDS
            )
        for attr in ("laser_worker", "mot_coil_worker", "dispenser_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.stop()
                setattr(self, attr, None)

    def _start_serial_workers(self):
        self._start_laser_worker()
        self._start_dps_workers()

    def _open_device_config_dialog(self):
        dialog = DeviceConfigDialog(self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self._apply_device_values(dialog.values())
        self._save_connection_settings()

    def _apply_device_values(self, values):
        self.rp_edit.setText(values["rp"])
        self.laser_edit.setText(values["laser"])
        self.mot_coil_port_edit.setText(values["mot_coil"])
        self.dispenser_port_edit.setText(values["dispenser"])

    def _save_connection_settings(self):
        self._settings.setValue("rp/host", self.rp_edit.text().strip())
        self._settings.setValue("laser/port", self.laser_edit.text().strip())
        self._settings.setValue(
            "dps/mot_coil_port", self.mot_coil_port_edit.text().strip()
        )
        self._settings.setValue(
            "dps/dispenser_port", self.dispenser_port_edit.text().strip()
        )
        self._settings.setValue(
            "dps/mot_coil_address", self.mot_coil_addr_spin.value()
        )
        self._settings.setValue(
            "dps/dispenser_address", self.dispenser_addr_spin.value()
        )
        self._settings.setValue("dac/vtune_min", self._vtune_min)
        self._settings.setValue("dac/vtune_max", self._vtune_max)

    def _start_laser_worker(self):
        self.laser_worker = LaserSetpointWorker(port=self.laser_edit.text().strip())
        self.laser_worker.initial_values.connect(self._on_laser_init)
        self.laser_worker.connection_status.connect(
            lambda status: self._set_status_label(self.laser_status, status)
        )
        self.laser_worker.connection_status.connect(
            lambda status: self._set_usb_status_label(self.laser_usb_status, status)
        )
        self.laser_worker.connection_status.connect(
            lambda status: self._log_connection("laser", status)
        )
        self.laser_worker.start()

    def _start_dps_workers(self):
        self.mot_coil_worker = RidenDPSWorker(
            port=self.mot_coil_port_edit.text().strip(),
            address=self.mot_coil_addr_spin.value(),
            model="DPS5005",
        )
        self.mot_coil_worker.connection_status.connect(
            lambda status: self._set_usb_status_label(self.coil_usb_status, status)
        )
        self.mot_coil_worker.connection_status.connect(
            lambda status: self._log_connection("coil", status)
        )
        self.mot_coil_worker.readback.connect(self._on_mot_coil_readback)
        self.mot_coil_worker.fault.connect(
            lambda msg: self._show_fault(self.coil_usb_status, "coil", msg)
        )
        self.mot_coil_worker.start()

        self.dispenser_worker = DispenserWorker(
            port=self.dispenser_port_edit.text().strip(),
            address=self.dispenser_addr_spin.value(),
            model="DPS5020",
            cooldown_until=self._dispenser_cooldown_until,
            fire_seconds=self._dispenser_fire_seconds,
            dispense_current_a=self._dispenser_fire_current,
        )
        self.dispenser_worker.connection_status.connect(
            lambda status: self._set_usb_status_label(self.dispenser_usb_status, status)
        )
        self.dispenser_worker.connection_status.connect(
            lambda status: self._log_connection("dispenser", status)
        )
        self.dispenser_worker.readback.connect(self._on_dispenser_readback)
        self.dispenser_worker.fire_state.connect(self._on_dispenser_fire_state)
        self.dispenser_worker.fire_summary.connect(self._on_dispenser_fire_summary)
        self.dispenser_worker.cooldown_started.connect(
            self._on_dispenser_cooldown_started
        )
        self.dispenser_worker.fault.connect(
            lambda msg: self._show_fault(self.dispenser_usb_status, "dispenser", msg)
        )
        self.dispenser_worker.start()

    def _start_repump_workers(self):
        self._vtune_slider_synced = False
        self.vtune_worker = DacClickWorker(
            host=self.rp_edit.text().strip(),
            min_voltage=self._vtune_min,
            max_voltage=self._vtune_max,
        )
        self.vtune_worker.connection_status.connect(
            lambda status: self._log_connection("dac_click", status)
        )
        self.vtune_worker.voltage_readback.connect(self._on_vtune_readback)
        self.vtune_worker.fault.connect(
            lambda msg: self._log_fault("dac_click", msg)
        )
        self.vtune_worker.start()

        # The repump RF gate is fixed at DIO1_N (bank n, bit 1), active-low
        # — the empirically verified mapping from qmot-linien-controls.
        self.repump_gpio_worker = LinienGpioWorker(
            host=self.rp_edit.text().strip(),
            bank="n",
            bit=1,
            active_high=False,
        )
        self.repump_gpio_worker.connection_status.connect(
            lambda status: self._log_connection("repump_gate", status)
        )
        self.repump_gpio_worker.output_state.connect(self._on_repump_gpio_state)
        self.repump_gpio_worker.fault.connect(
            lambda msg: self._log_fault("repump_gate", msg)
        )
        self.repump_gpio_worker.start()

    def _stop_hardware_workers(self):
        for attr in (
            "laser_worker",
            "mot_coil_worker",
            "dispenser_worker",
            "vtune_worker",
            "repump_gpio_worker",
        ):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.stop()

    # ------------------------------------------------------------------
    # Linien lock controls
    # ------------------------------------------------------------------

    def _get_lock_worker(self):
        return getattr(self.linien_widget, "linien_thread", None)

    def _on_mod_freq(self, value):
        self.mod_freq_edit.setText(f"{value / 1000.0:.3f} MHz")
        self._settings.setValue("linien/mod_freq_khz", value)
        self._debounced_log(
            "mod_freq",
            "setpoint",
            lambda: {"name": "mod_freq_khz", "value": self.mod_freq_slider.value()},
        )
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_modulation_frequency(value)

    def _on_mod_phase(self, value):
        self.mod_phase_edit.setText(f"{value}\N{DEGREE SIGN}")
        self._settings.setValue("linien/mod_phase_deg", value)
        self._debounced_log(
            "mod_phase",
            "setpoint",
            lambda: {"name": "mod_phase_deg", "value": self.mod_phase_slider.value()},
        )
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_modulation_phase(value)

    def _on_mod_strength(self, value):
        self.mod_strength_edit.setText(f"{value} mV")
        self._settings.setValue("linien/mod_strength_mv", value)
        self._debounced_log(
            "mod_strength",
            "setpoint",
            lambda: {
                "name": "mod_strength_mv",
                "value": self.mod_strength_slider.value(),
            },
        )
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_modulation_amplitude(value)

    def _on_sweep_speed(self, combo_index):
        self._settings.setValue("linien/sweep_speed_index", combo_index)
        self._event_log.log(
            "setpoint", name="sweep_speed_index", value=combo_index
        )
        worker = self._get_lock_worker()
        speed = self.sweep_speed_combo.currentData()
        if worker is not None and speed is not None:
            worker.set_sweep_speed(speed)

    def _on_lock_state(self, locked):
        self._set_lock_label(self.lock_readout, locked)
        if locked:
            self._set_chip("lock", "LOCKED", "#40dd80")
        else:
            self._set_chip("lock", "UNLOCKED", "#dd5050")
        self._event_log.log("lock", locked=bool(locked))

    # ------------------------------------------------------------------
    # Koheron laser controls
    # ------------------------------------------------------------------

    def _on_laser_init(self, values):
        self.current_slider.blockSignals(True)
        self.current_slider.setValue(int(values["current"]))
        self.current_slider.blockSignals(False)
        self.current_edit.setText(f"{int(values['current'])} mA")

        self.therm_slider.blockSignals(True)
        self.therm_slider.setValue(int(values["thermistor"]))
        self.therm_slider.blockSignals(False)
        self.therm_edit.setText(f"{int(values['thermistor'])} \N{OHM SIGN}")
        self._update_therm_temp(int(values["thermistor"]))
        self._event_log.log(
            "laser_init",
            current_ma=int(values["current"]),
            thermistor_ohm=int(values["thermistor"]),
        )

    def _toggle_laser(self, checked):
        self.laser_btn.setText("Laser On" if checked else "Laser Off")
        self._event_log.log("laser_output", on=bool(checked))
        worker = getattr(self, "laser_worker", None)
        if worker is not None:
            worker.laser_on(checked)

    def _on_current(self, value):
        self.current_edit.setText(f"{value} mA")
        self._settings.setValue("laser/current_ma", value)
        self._debounced_log(
            "laser_current",
            "setpoint",
            lambda: {"name": "laser_current_ma", "value": self.current_slider.value()},
        )
        worker = getattr(self, "laser_worker", None)
        if worker is not None:
            worker.set_current(value)

    def _on_thermistor(self, value):
        self.therm_edit.setText(f"{value} \N{OHM SIGN}")
        self._settings.setValue("laser/thermistor_ohm", value)
        self._update_therm_temp(value)
        self._debounced_log(
            "laser_thermistor",
            "setpoint",
            lambda: {
                "name": "laser_thermistor_ohm",
                "value": self.therm_slider.value(),
                "temp_c": round(thermistor_ohms_to_c(self.therm_slider.value()), 2),
            },
        )
        worker = getattr(self, "laser_worker", None)
        if worker is not None:
            worker.set_thermistor(value)

    def _update_therm_temp(self, ohms):
        self.therm_temp_readback.setText(
            f"{thermistor_ohms_to_c(ohms):.1f} \N{DEGREE SIGN}C"
        )

    # ------------------------------------------------------------------
    # DPS5005 controls
    # ------------------------------------------------------------------

    def _toggle_mot_coils(self, checked):
        self.mot_coil_output_btn.setText("Coils On" if checked else "Coils Off")
        worker = getattr(self, "mot_coil_worker", None)
        if worker is not None:
            worker.set_output(checked)

    def _fire_dispenser_clicked(self):
        worker = getattr(self, "dispenser_worker", None)
        if worker is None:
            return
        if self._dispenser_phase == "firing":
            worker.abort_fire()
        else:
            worker.fire()

    def _open_dispenser_override_dialog(self):
        dialog = DispenserOverrideDialog(
            self, self._dispenser_fire_current, self._dispenser_fire_seconds
        )
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        current_a, seconds = dialog.values()
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Confirm Dispenser Override",
            f"Set the dispenser fire to {current_a:.2f} A for "
            f"{seconds:.0f} s?\n\n"
            f"The safe default is {DispenserWorker.DISPENSE_CURRENT_A:.1f} A "
            f"for {DispenserWorker.FIRE_SECONDS:.0f} s. Excessive current or "
            "duration can burn out the dispenser or overload the pump.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        self._apply_dispenser_override(current_a, seconds)

    def _apply_dispenser_override(self, current_a, seconds):
        self._dispenser_fire_current = float(current_a)
        self._dispenser_fire_seconds = float(seconds)
        self._settings.setValue(
            "dispenser/fire_current_a", self._dispenser_fire_current
        )
        self._settings.setValue(
            "dispenser/fire_seconds", self._dispenser_fire_seconds
        )
        worker = getattr(self, "dispenser_worker", None)
        if worker is not None:
            worker.set_fire_parameters(
                self._dispenser_fire_current, self._dispenser_fire_seconds
            )
        self._event_log.log(
            "dispenser_override",
            current_a=self._dispenser_fire_current,
            seconds=self._dispenser_fire_seconds,
        )
        self._update_dispenser_setpoint_text()
        if self._dispenser_phase == "idle":
            self.dispenser_fire_btn.setText(
                f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
            )

    def _update_dispenser_setpoint_text(self):
        current_a = self._dispenser_fire_current
        seconds = self._dispenser_fire_seconds
        is_default = (
            abs(current_a - DispenserWorker.DISPENSE_CURRENT_A) < 0.005
            and abs(seconds - DispenserWorker.FIRE_SECONDS) < 0.5
        )
        text = f"{current_a:.2f} A CC / {seconds:.0f} s fire"
        if is_default:
            self._set_colored_text(self.dispenser_setpoint_label, text, "#8888a0")
        else:
            self._set_colored_text(
                self.dispenser_setpoint_label, text + "  (OVERRIDE)", "#ff9f40"
            )

    def _on_mot_coil_voltage(self, value):
        volts = value / 1000.0
        self.mot_coil_voltage_edit.setText(f"{volts:.3f} V")
        self._settings.setValue("coil/voltage_mv", value)
        self._debounced_log(
            "coil_voltage",
            "setpoint",
            lambda: {
                "name": "coil_voltage_v",
                "value": self.mot_coil_voltage_slider.value() / 1000.0,
            },
        )
        worker = getattr(self, "mot_coil_worker", None)
        if worker is not None:
            worker.set_voltage(volts)

    def _on_mot_coil_current(self, value):
        amps = value / 1000.0
        self.mot_coil_current_edit.setText(f"{amps:.3f} A")
        self._settings.setValue("coil/current_ma", value)
        self._debounced_log(
            "coil_current",
            "setpoint",
            lambda: {
                "name": "coil_current_a",
                "value": self.mot_coil_current_slider.value() / 1000.0,
            },
        )
        worker = getattr(self, "mot_coil_worker", None)
        if worker is not None:
            worker.set_current(amps)

    def _on_mot_coil_readback(self, state):
        self._sync_output_button(
            self.mot_coil_output_btn, state, "Coils On", "Coils Off"
        )
        self._set_readback_text(self.mot_coil_readback, state)
        if int(state.get("protection", 0) or 0):
            self._set_chip("coils", "COILS TRIP", "#dd5050")
        elif state.get("output_enabled"):
            self._set_chip("coils", f"COILS {state['i_out']:.1f} A", "#40dd80")
        else:
            self._set_chip("coils", "COILS OFF", "#8888a0")
        self._log_readback("coil", state)

    def _on_dispenser_readback(self, state):
        # The fire button is driven by the worker's fire_state, not by the
        # raw output flag, so only the numeric readback is updated here.
        self._set_readback_text(self.dispenser_readback, state, show_mode=True)
        self._log_readback("dispenser", state)

    def _set_readback_text(self, label, state, show_mode=False):
        protection = int(state.get("protection", 0) or 0)
        if protection:
            # Keep this short so the row never widens the control column;
            # power/mode are meaningless once the supply has tripped.
            kind = DPS_PROTECTION.get(protection, f"PROT {protection}")
            self._set_colored_text(
                label,
                f"{kind} TRIPPED — {state['v_out']:.2f} V / "
                f"{state['i_out']:.2f} A",
                "#dd5050",
            )
            return
        text = (
            f"{state['v_out']:.2f} V / {state['i_out']:.2f} A / "
            f"{state['power']:.2f} W"
        )
        if show_mode and state.get("output_enabled"):
            text += " / CC" if state.get("cv_cc") else " / CV"
        label.setText(text)
        label.setStyleSheet("color: #b0b0c8; font-size: 13px; border: none;")

    @staticmethod
    def _sync_output_button(button, state, on_text, off_text):
        button.blockSignals(True)
        button.setChecked(bool(state["output_enabled"]))
        button.setText(on_text if state["output_enabled"] else off_text)
        button.blockSignals(False)

    def _on_dispenser_fire_state(self, state):
        phase = state.get("phase", "idle")
        remaining = float(state.get("remaining_s", 0.0))
        if phase != self._dispenser_phase:
            if phase == "firing":
                self._event_log.log(
                    "dispenser_fire_start",
                    current_a=self._dispenser_fire_current,
                    planned_s=self._dispenser_fire_seconds,
                )
            elif phase == "idle" and self._dispenser_phase == "cooldown":
                self._event_log.log("dispenser_ready")
        self._dispenser_phase = phase
        self.dispenser_override_btn.setEnabled(phase != "firing")
        self._update_dispenser_progress(phase, remaining)
        if phase == "firing":
            self.dispenser_fire_btn.setEnabled(True)
            self.dispenser_fire_btn.setText(f"Abort Fire ({remaining:.0f} s)")
            self._set_colored_text(
                self.dispenser_fire_label,
                f"FIRING — {remaining:.0f} s left",
                "#ff9f40",
            )
            self._set_chip("disp", "DISP FIRING", "#ff9f40")
        elif phase == "cooldown":
            minutes, seconds = divmod(int(round(remaining)), 60)
            self.dispenser_fire_btn.setEnabled(False)
            self.dispenser_fire_btn.setText(f"Cooldown {minutes:d}:{seconds:02d}")
            self._set_colored_text(
                self.dispenser_fire_label,
                f"Cooldown {minutes:d}:{seconds:02d}",
                "#00d4ff",
            )
            self._set_chip("disp", f"DISP {minutes:d}:{seconds:02d}", "#00d4ff")
        else:
            self.dispenser_fire_btn.setEnabled(True)
            self.dispenser_fire_btn.setText(
                f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
            )
            self._set_colored_text(self.dispenser_fire_label, "Idle", "#8888a0")
            self._set_chip("disp", "DISP IDLE", "#8888a0")

    def _update_dispenser_progress(self, phase, remaining):
        if phase == "firing":
            total = max(1.0, self._dispenser_fire_seconds)
            color = "#ff9f40"
        elif phase == "cooldown":
            total = DispenserWorker.COOLDOWN_SECONDS
            color = "#00d4ff"
        else:
            self.dispenser_progress.setVisible(False)
            return
        fraction = max(0.0, min(1.0, remaining / total))
        self.dispenser_progress.setValue(int(round(1000 * fraction)))
        self.dispenser_progress.setStyleSheet(
            "QProgressBar { background: #0e0e16; border: none;"
            "border-radius: 3px; }"
            f"QProgressBar::chunk {{ background: {color};"
            "border-radius: 3px; }"
        )
        self.dispenser_progress.setVisible(True)

    def _on_dispenser_cooldown_started(self, until_epoch):
        self._dispenser_cooldown_until = float(until_epoch)
        self._settings.setValue(
            "dispenser/cooldown_until", self._dispenser_cooldown_until
        )
        self._event_log.log(
            "dispenser_cooldown", until_epoch=self._dispenser_cooldown_until
        )

    def _on_dispenser_fire_summary(self, summary):
        self._event_log.log("dispenser_fire_end", **summary)
        fires = self._int_setting("dispenser/total_fires", 0) + 1
        total_s = self._float_setting("dispenser/total_fire_s", 0.0) + float(
            summary.get("elapsed_s", 0.0)
        )
        self._settings.setValue("dispenser/total_fires", fires)
        self._settings.setValue("dispenser/total_fire_s", total_s)
        self._update_dispenser_history_label()

    def _update_dispenser_history_label(self):
        fires = self._int_setting("dispenser/total_fires", 0)
        total_s = self._float_setting("dispenser/total_fire_s", 0.0)
        self.dispenser_history_label.setText(
            f"{fires} / {total_s:.0f} s total"
        )

    @staticmethod
    def _set_colored_text(label, text, color):
        label.setText(text)
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 700; border: none;"
        )

    # ------------------------------------------------------------------
    # Repump tune / gate
    # ------------------------------------------------------------------

    def _on_vtune(self, value):
        volts = value / 1000.0
        self.vtune_edit.setText(f"{volts:.3f} V")
        self._settings.setValue("dac/vtune_mv", value)
        # Live target frequency while dragging; the readback (actual)
        # overwrites it as soon as the ~0.3 s DAC write completes.
        self.vtune_freq_readback.setText(f"{vtune_to_vco_mhz(volts):.1f} MHz")
        worker = getattr(self, "vtune_worker", None)
        if worker is not None:
            worker.set_voltage(volts)

    def _on_vtune_readback(self, volts):
        self.vtune_readback.setText(f"{volts:.3f} V")
        self.vtune_freq_readback.setText(f"{vtune_to_vco_mhz(volts):.1f} MHz")
        if (
            self._last_logged_vtune is None
            or abs(volts - self._last_logged_vtune) >= 0.001
        ):
            self._last_logged_vtune = volts
            self._event_log.log(
                "vtune",
                volts=round(volts, 4),
                vco_mhz=round(vtune_to_vco_mhz(volts), 2),
            )
        # The linien server holds the DAC value across app restarts; adopt
        # it into the slider once per connect so the first drag doesn't
        # jump the VCO away from its current tune point.
        if not self._vtune_slider_synced:
            self._vtune_slider_synced = True
            self.vtune_slider.blockSignals(True)
            self.vtune_slider.setValue(int(round(volts * 1000)))
            self.vtune_slider.blockSignals(False)
            self.vtune_edit.setText(f"{volts:.3f} V")

    def _toggle_repump_gpio(self, checked):
        self.repump_gpio_btn.setText(
            "Repump Gate On" if checked else "Repump Gate Off"
        )
        self._event_log.log("repump_gate", on=bool(checked))
        worker = getattr(self, "repump_gpio_worker", None)
        if worker is not None:
            worker.set_output(checked)

    def _on_repump_gpio_state(self, enabled):
        self.repump_gpio_btn.blockSignals(True)
        self.repump_gpio_btn.setChecked(enabled)
        self.repump_gpio_btn.setText(
            "Repump Gate On" if enabled else "Repump Gate Off"
        )
        self.repump_gpio_btn.blockSignals(False)

    # ------------------------------------------------------------------
    # Shared status helpers / cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def _set_usb_status_label(label, status):
        """Compact status for the fixed-width USB card rows.

        The full status string (port path, error detail) goes in the
        tooltip so the label never overflows its 84 px column.
        """
        if status.startswith("connected"):
            text, color = "OK", "#40dd80"
        elif status in ("no_port", "no_host"):
            text, color = "No port", "#8888a0"
        elif status.startswith("error"):
            text, color = "Error", "#dd5050"
        else:
            text, color = "--", "#8888a0"
        label.setText(text)
        label.setToolTip(status)
        label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 700; border: none;"
        )

    @staticmethod
    def _set_status_label(label, status):
        if status.startswith("connected:"):
            text = status.split(":", 1)[1]
            color = "#40dd80"
        elif status == "connected":
            text = "Connected"
            color = "#40dd80"
        elif status in ("no_port", "no_host"):
            text = "Not configured"
            color = "#8888a0"
        elif status.startswith("error:"):
            text = "Error"
            color = "#dd5050"
        else:
            text = "Disconnected"
            color = "#dd5050"
        label.setText(text)
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 700; border: none;"
        )

    def _show_fault(self, label, device, message):
        label.setText("Fault")
        label.setStyleSheet(
            "color: #dd5050; font-size: 13px; font-weight: 700; border: none;"
        )
        self._log_fault(device, message)

    @staticmethod
    def _set_lock_label(label, locked):
        text = "Locked" if locked else "Unlocked"
        color = "#40dd80" if locked else "#dd5050"
        label.setText(text)
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 700; border: none;"
        )

    def closeEvent(self, event):
        print("Shutting down OpenQuantum Controls...")
        self._save_ui_state()
        self._event_log.log("app_stop")
        if self._dispenser_phase == "firing":
            # Closing mid-fire skips the worker's cooldown transition; the
            # output is still forced off on stop, but persist the lockout
            # here so a restart cannot immediately re-fire.
            self._settings.setValue(
                "dispenser/cooldown_until",
                time.time() + DispenserWorker.COOLDOWN_SECONDS,
            )
        self.camera_widget.stop()
        self.linien_widget.stop()
        self._stop_hardware_workers()
        probe = getattr(self, "probe_worker", None)
        if probe is not None:
            probe.wait(3000)
        event.accept()


# Backward-compatible name for scripts/tests that imported the first version.
MOTControlWindow = OpenQuantumControlsWindow


def main():
    import argparse

    parser = argparse.ArgumentParser(description="OpenQuantum Controls UI")
    parser.add_argument("--rp-host", default=None, help="Linien Red Pitaya host")
    parser.add_argument("--laser-port", default=None, help="Koheron serial port")
    parser.add_argument("--mot-coil-port", default=None)
    parser.add_argument("--dispenser-port", default=None)
    parser.add_argument("--mot-coil-address", type=int, default=1)
    parser.add_argument("--dispenser-address", type=int, default=1)
    parser.add_argument(
        "--vtune-min",
        type=float,
        default=0.0,
        help="DAC Click V_tune slider minimum (V)",
    )
    parser.add_argument(
        "--vtune-max",
        type=float,
        default=5.0,
        help="DAC Click V_tune slider maximum (V, hardware full scale is 5)",
    )
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)

    load_app_fonts()
    # setFont covers non-stylesheet text (e.g. pyqtgraph axis labels).
    app.setFont(QtGui.QFont(BODY_FONT_FAMILY))
    app.setStyleSheet(build_app_stylesheet())
    pg.setConfigOptions(useOpenGL=False, enableExperimental=False)

    window = OpenQuantumControlsWindow(
        rp_host=args.rp_host,
        laser_port=args.laser_port,
        mot_coil_port=args.mot_coil_port,
        dispenser_port=args.dispenser_port,
        mot_coil_address=args.mot_coil_address,
        dispenser_address=args.dispenser_address,
        vtune_min=args.vtune_min,
        vtune_max=args.vtune_max,
    )
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
