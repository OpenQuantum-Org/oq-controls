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

import json
import os
import sys
import time
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

# Self-contained: the Linien widget/worker, styles, and the Koheron
# worker are vendored in this repo (originally from eit-controls).
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from linien_widget import LinienWidget
from styles import (
    ACCENT,
    APP_STYLESHEET,
    UI_FONT_PX,
    NEON_BLUE,
    add_readout_row,
    add_slider_row as _eit_add_slider_row,
    make_card,
    make_section_title as _eit_make_section_title,
    plot_label_style,
    style_plot_axes,
)

from mot_logging import EventLog
from mot_workers import (
    DacClickWorker,
    DispenserWorker,
    LaserSetpointWorker,
    LinienGpioWorker,
    PsdWorker,
    RidenDPSWorker,
    SerialProbeWorker,
)

# Colors cycled through for successive PSD measurement runs.
PSD_RUN_COLORS = (
    "#c4c4c4", "#b9924f", "#6f9e7c", "#9a7ba8",
    "#b8ab63", "#7a80b0", "#b56a6a", "#7ba8a8",
)

# Riden DPS protection register (reg 7) values.
DPS_PROTECTION = {1: "OVP", 2: "OCP", 3: "OPP"}

def resource_path(*parts):
    """Absolute path to a bundled resource (fonts, etc.).

    Works both from a source checkout and from a frozen (PyInstaller)
    build, where bundled files are unpacked under ``sys._MEIPASS``.
    """
    base = getattr(sys, "_MEIPASS", HERE)
    return os.path.join(base, *parts)


def app_data_dir():
    """Writable per-user data directory.

    From a source checkout this is the repo directory (so logs/ lands
    next to the code, as before). A frozen build must not write next to
    the executable (often read-only once installed), so it uses the OS
    per-user application-data location instead.
    """
    if getattr(sys, "frozen", False):
        base = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.AppDataLocation
        )
        if base:
            return base
    return HERE


def logs_dir():
    return os.path.join(app_data_dir(), "logs")


# App typography: IBM Plex Sans for body text, Space Grotesk for titles.
FONTS_DIR = resource_path("fonts")
BODY_FONT_FAMILY = "Space Grotesk"
TITLE_FONT_FAMILY = "Space Grotesk"
# Header wordmark height. The asset is 2048x568, so this keeps the mark
# about the same optical weight as the 22px text it replaced.
LOGO_HEIGHT_PX = 62


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


# The original eit-controls text colors were too dim for lab use; the
# vendored linien_widget/styles copies were brightened with this mapping
# when they were imported into this repo. The dict is kept as the
# guard list: the smoke test asserts no widget stylesheet ever carries
# one of the dim colors again.
CONTRAST_REMAP = {
    "#6a6a80": "#969696",  # tiny/de-emphasized labels
    "#7a7a90": "#ababab",  # section-title class
    "#8888a0": "#ababab",  # row labels / idle statuses
    "#9a9ab0": "#b2b2b2",  # log text
    "#a0a0b8": "#c4c4c4",  # linien overlay labels
    "#b0b0c8": "#e6e6e6",  # readout values
    "#c0c0d0": "#e6e6e6",  # button/combo/edit text
}


def build_app_stylesheet():
    return APP_STYLESHEET + (
        "\n* {"
        f' font-family: "{BODY_FONT_FAMILY}", "Helvetica Neue",'
        ' "Segoe UI", sans-serif; }\n'
        # Input affordance: white outlines + bright text on every text /
        # number / dropdown input. QSpinBox in particular otherwise
        # inherits the platform palette (black on dark = unreadable).
        "QLineEdit { border: 1px solid #404040; color: #efefef; }\n"
        "QLineEdit:focus { border-color: #949494; }\n"
        "QAbstractSpinBox {"
        " background: #171717; color: #efefef; font-size: 13px;"
        " border: 1px solid #404040; border-radius: 0px;"
        " padding: 2px 6px;"
        " selection-background-color: #383838; }\n"
        "QAbstractSpinBox:focus { border-color: #949494; }\n"
        "QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {"
        " background: #5a5a70; border: none; width: 14px; }\n"
        "QAbstractSpinBox::up-button { border-top-right-radius: 3px; }\n"
        "QAbstractSpinBox::down-button { border-bottom-right-radius: 3px; }\n"
        "QAbstractSpinBox::up-button:hover,"
        " QAbstractSpinBox::down-button:hover { background: #6a6a82; }\n"
        "QComboBox { border: 1px solid #404040; }\n"
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

# Repump VCO tuning range: the DAC's 0-5 V V_tune span covers
# 2800-2970 MHz (measured on this VCO — the old qMOT-os datasheet table
# did not match the actual hardware). Linear, clamped at the ends.
VCO_FREQ_MIN_MHZ = 2800.0
VCO_FREQ_MAX_MHZ = 2970.0
VCO_VTUNE_SPAN_V = 5.0


def vtune_to_vco_mhz(vtune_v: float) -> float:
    """Convert a repump V_tune voltage to the VCO output frequency in MHz."""
    fraction = max(0.0, min(1.0, vtune_v / VCO_VTUNE_SPAN_V))
    return VCO_FREQ_MIN_MHZ + fraction * (VCO_FREQ_MAX_MHZ - VCO_FREQ_MIN_MHZ)


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
        background: #171717;
        color: #e6e6e6;
        border: 1px solid #404040;
        border-radius: 0px;
        padding: 2px 6px;
        font-size: 13px;
    }
    QComboBox:focus {
        color: white;
        border-color: #5e5e5e;
    }
    QComboBox::drop-down {
        border: none;
        width: 18px;
    }
    QComboBox QAbstractItemView {
        background: #171717;
        color: #e6e6e6;
        selection-background-color: #383838;
        selection-color: white;
        outline: none;
    }
"""


def _style_combo(combo):
    combo.setStyleSheet(COMBO_STYLE)
    try:
        combo.view().setStyleSheet(
            "background: #171717; color: #e6e6e6;"
            "selection-background-color: #383838; selection-color: white;"
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


def _short_port_label(port):
    """Device path plus just enough to tell two ports apart in a dropdown.

    The full description goes in the tooltip -- see PortSelect.set_available.
    """
    detail = str(port.manufacturer or port.description or "").strip()
    if detail in ("", "n/a"):
        return str(port.device)
    return f"{port.device} \N{EN DASH} {detail}"


def discover_serial_ports():
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    return list(list_ports.comports())


class PortSelect(QtWidgets.QComboBox):
    """Serial port chooser with a QLineEdit-compatible API.

    The ports are enumerable, so picking one from a list beats typing a
    device path. ``text()``/``setText()``/``textChanged`` are kept so every
    existing consumer -- workers, config snapshots, saved settings -- works
    against this unchanged.

    A port that is saved but not currently present stays selectable rather
    than being silently dropped: the device may just be unplugged.
    """

    NOT_SET = "\N{EM DASH} not set \N{EM DASH}"

    textChanged = QtCore.pyqtSignal(str)

    def __init__(self, value="", parent=None):
        super().__init__(parent)
        _style_combo(self)
        # A port description can be far wider than the sidebar. Without
        # these the combo demands its longest item's width and drags the
        # whole control column into horizontal scrolling.
        self.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(12)
        self.addItem(self.NOT_SET, "")
        self.setText(value)
        self.currentIndexChanged.connect(
            lambda _index: self.textChanged.emit(self.text())
        )

    def text(self):
        return str(self.currentData() or "")

    def setText(self, value):
        value = str(value or "").strip()
        if not value:
            self.setCurrentIndex(0)
            return
        index = self.findData(value)
        if index < 0:
            self.addItem(value, value)
            index = self.count() - 1
        self.setCurrentIndex(index)

    def set_available(self, ports):
        """Replace the offered ports, preserving the current selection.

        ``ports`` is a sequence of (device, label, detail) triples. The
        device path is the stored value, the short label tells two
        otherwise identical COM ports apart, and the full detail goes in
        the item tooltip rather than into the widget's width.
        """
        current = self.text()
        self.blockSignals(True)
        self.clear()
        self.addItem(self.NOT_SET, "")
        for device, label, detail in ports:
            self.addItem(label, device)
            self.setItemData(
                self.count() - 1, detail, QtCore.Qt.ToolTipRole
            )
        self.blockSignals(False)
        self.setText(current)


class CollapsibleCard(QtWidgets.QFrame):
    """Card with a clickable header that shows/hides its content."""

    def __init__(self, title, collapsed=True, parent=None, framed=True):
        super().__init__(parent)
        if framed:
            self.setStyleSheet("""
                QFrame {
                    background-color: #151515;
                    border: 1px solid #282828;
                    border-radius: 0px;
                }
            """)
        else:
            # Nested inside another card: no frame of its own, and no
            # margins, so it reads as a section rather than a second box.
            # Scoped by objectName — a bare "QFrame {...}" rule cascades
            # into every descendant frame and breaks their painting.
            self.setObjectName("nestedSection")
            self.setStyleSheet(
                "QFrame#nestedSection { border: none; background: transparent; }"
            )
            # A nested section sizes to its content. Without this it is the
            # last widget in the parent card's layout and soaks up all the
            # slack, leaving a tall blank gap under a collapsed header.
            self.setSizePolicy(
                QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum
            )
        outer = QtWidgets.QVBoxLayout(self)
        margin = (12, 10, 12, 10) if framed else (0, 0, 0, 0)
        outer.setContentsMargins(*margin)
        outer.setSpacing(6)
        # Cards size to their content. Without this the last card in a
        # top-aligned column absorbs the leftover height and shows a tall
        # blank area under a collapsed section.
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum
        )

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        self.toggle_btn = QtWidgets.QToolButton()
        # "&&" escapes the mnemonic marker: an unescaped "&" makes Qt
        # underline the next character and swallow the ampersand.
        self.toggle_btn.setText(title.upper().replace("&", "&&"))
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(not collapsed)
        self.toggle_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.toggle_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle_btn.setArrowType(
            QtCore.Qt.RightArrow if collapsed else QtCore.Qt.DownArrow
        )
        self.toggle_btn.setStyleSheet(
            f"QToolButton {{ color: {NEON_BLUE}; font-size: 13px;"
            "font-weight: 600; letter-spacing: 1px; border: none;"
            "background: transparent; padding: 2px 4px;"
            f' font-family: "{TITLE_FONT_FAMILY}"; }}'
        )
        self.toggle_btn.toggled.connect(self._on_toggled)
        header.addWidget(self.toggle_btn)
        header.addStretch()
        self.header_status = QtWidgets.QLabel("")
        self.header_status.setStyleSheet(
            "color: #ababab; font-size: 13px; font-weight: 600; border: none;"
        )
        header.addWidget(self.header_status)
        outer.addLayout(header)

        # NOTE: no selector-less stylesheet here — those cascade to every
        # descendant and override app-level input styling (borders,
        # spinbox backgrounds). A plain QWidget is transparent anyway.
        self.content = QtWidgets.QWidget()
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

    def set_status(self, text, color="#ababab"):
        self.header_status.setText(text)
        self.header_status.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 600; border: none;"
        )


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
            f"color: {NEON_BLUE}; font-size: 13px; font-weight: 700;"
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
            "color: #ababab; font-size: 13px; font-weight: 700;"
            "border: 1px solid #282828; border-radius: 0px;"
            "background: #0b0b0b; padding: 24px;"
        )
        self.stack.addWidget(self.message_label)

        try:
            from PyQt6.QtMultimediaWidgets import QVideoWidget

            self.video_widget = QVideoWidget()
            self.video_widget.setStyleSheet("background: #080809;")
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
        note.setStyleSheet("color: #cc8830; border: none; font-size: 13px;")
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


class PidDialog(QtWidgets.QDialog):
    """Set the Linien lock PID gains (each 0-8191)."""

    def __init__(self, parent, current=None):
        super().__init__(parent)
        self.setWindowTitle("Lock PID")

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(10)

        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        p_value, i_value, d_value = current if current else (50, 5, 0)
        self.p_spin = QtWidgets.QSpinBox()
        self.i_spin = QtWidgets.QSpinBox()
        self.d_spin = QtWidgets.QSpinBox()
        for spin, value in (
            (self.p_spin, p_value),
            (self.i_spin, i_value),
            (self.d_spin, d_value),
        ):
            spin.setRange(0, 8191)
            spin.setValue(int(value))
        form.addRow("P:", self.p_spin)
        form.addRow("I:", self.i_spin)
        form.addRow("D:", self.d_spin)
        root.addLayout(form)

        note = QtWidgets.QLabel(
            "Applied to the Red Pitaya immediately on Apply "
            "(linien range 0-8191 each)."
            + ("" if current else " Current values could not be read.")
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #969696; border: none; font-size: 13px;")
        root.addWidget(note)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addStretch()
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QtWidgets.QPushButton("Apply")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self.accept)
        button_row.addWidget(cancel_btn)
        button_row.addWidget(apply_btn)
        root.addLayout(button_row)

    def values(self):
        return self.p_spin.value(), self.i_spin.value(), self.d_spin.value()


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
        self._dispenser_fire_voltage = self._float_setting(
            "dispenser/fire_voltage_v", DispenserWorker.DISPENSE_VOLTAGE_V
        )
        self._dispenser_phase = "idle"
        self._vtune_slider_synced = False
        self._psd_runs = {}
        self._psd_color_index = 0
        self._psd_prev_status = {}
        self._filters_syncing = False

        self._chip_state = {}
        self._event_log = EventLog(logs_dir())
        self._readback_log_t = {}
        self._readback_log_output = {}
        self._readback_log_protection = {}
        self._log_debounce = {}
        self._last_logged_vtune = None
        self._event_log.log("app_start")

        self.setWindowTitle("OpenQuantum Controls")
        self.resize(1540, 960)

        central = QtWidgets.QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        # The header spans the window so the plot and the sidebar both
        # start beneath it and line up without a tuned spacer.
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        root = QtWidgets.QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._build_header(outer)
        outer.addLayout(root, stretch=1)
        self._build_spectroscopy_column(root)
        self._build_control_column(root)

        # Route event-log records into the log pane (GUI thread only).
        self._event_log.on_record = self._on_log_record

        # Pull the filter settings from the device once Linien connects.
        self.linien_widget.connection_status_changed.connect(
            self._on_linien_connection
        )

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

    # Each status shows in the header of the card that owns it, rather
    # than in a separate strip. Coils and dispenser share one card, so
    # their two statuses are joined into that header.
    _CHIP_CARDS = {
        "lock": "lock_card",
        "usb": "usb_card",
        "coils": "coils_card",
        "disp": "dispenser_card",
        "repump": "repump_card",
        "laser": "laser_card",
    }
    _CHIP_NEUTRAL = "#ababab"

    def _set_chip(self, key, text, color):
        self._chip_state[key] = (text, color)
        self._refresh_card_status(self._CHIP_CARDS[key])

    def chip_text(self, key):
        """Current status text for a key, independent of how it is shown."""
        return self._chip_state.get(key, ("", ""))[0]

    def _refresh_card_status(self, card_attr):
        card = getattr(self, card_attr, None)
        if card is None:
            return  # card not built yet during startup
        parts = [
            self._chip_state[key]
            for key in self._CHIP_CARDS
            if self._CHIP_CARDS[key] == card_attr and key in self._chip_state
        ]
        if not parts:
            return
        text = "  ".join(part[0] for part in parts)
        # Whichever part is not neutral wins, so a trip is never hidden
        # behind an idle sibling.
        color = next(
            (c for _t, c in parts if c != self._CHIP_NEUTRAL),
            self._CHIP_NEUTRAL,
        )
        card.set_status(text, color)

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
        # Persist the USB card's expansion; the event log deliberately
        # always starts collapsed.
        self.usb_card.toggle_btn.toggled.connect(
            lambda expanded: self._settings.setValue(
                "ui/usb_card_expanded", bool(expanded)
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

    def _build_header(self, outer):
        """Logo left, view controls right, spanning the whole window."""
        self._header_row = QtWidgets.QHBoxLayout()
        self._header_row.setContentsMargins(6, 10, 6, 0)
        self._header_row.setSpacing(8)

        self.title_label = QtWidgets.QLabel()
        self.title_label.setStyleSheet("border: none;")
        logo = QtGui.QPixmap(resource_path("assets", "full-horizontal.png"))
        if logo.isNull():
            # Bundled asset missing (broken build): fall back to the
            # wordmark as text rather than showing an empty header.
            self.title_label.setText("OpenQuantum")
            self.title_label.setStyleSheet(
                f"color: {ACCENT}; font-size: 20px; font-weight: 700;"
                "letter-spacing: 3px; border: none;"
                f'font-family: "{TITLE_FONT_FAMILY}";'
            )
        else:
            # The asset is white on transparent, sized for print; scale to
            # the header height by device pixel ratio so it stays crisp.
            ratio = self.devicePixelRatioF() or 1.0
            scaled = logo.scaledToHeight(
                int(LOGO_HEIGHT_PX * ratio),
                QtCore.Qt.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(ratio)
            self.title_label.setPixmap(scaled)
        self.title_label.setToolTip("OpenQuantum Controls")
        self._header_row.addWidget(self.title_label)
        self._header_row.addStretch()

        self.camera_toggle = QtWidgets.QCheckBox("Show USB Camera")
        self.camera_toggle.setCursor(QtCore.Qt.PointingHandCursor)
        self.camera_toggle.setStyleSheet(
            "QCheckBox { color: #e6e6e6; font-size: 13px; font-weight: 700;"
            "border: none; padding: 4px 6px; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
        )
        self.camera_toggle.toggled.connect(self._toggle_camera_view)
        self._tip(
            "Show the USB camera pane below the spectroscopy plot.",
            self.camera_toggle,
        )
        self._header_row.addWidget(self.camera_toggle)
        outer.addLayout(self._header_row)

        # Statuses live in each card's header, not in a strip up here.
        self._set_chip("lock", "LOCK --", self._CHIP_NEUTRAL)
        self._set_chip("usb", "DEVICES --", self._CHIP_NEUTRAL)
        self._set_chip("coils", "COILS OFF", self._CHIP_NEUTRAL)
        self._set_chip("disp", "DISP IDLE", self._CHIP_NEUTRAL)
        self._set_chip("repump", "REPUMP OFF", self._CHIP_NEUTRAL)
        self._set_chip("laser", "LASER OFF", self._CHIP_NEUTRAL)

    def _build_spectroscopy_column(self, root):
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)

        normal_view = QtWidgets.QWidget()
        normal_layout = QtWidgets.QVBoxLayout(normal_view)
        normal_layout.setContentsMargins(0, 0, 0, 0)
        normal_layout.setSpacing(6)

        # Sweep speed sits with the sweep slider it belongs to, directly
        # above the plot, rather than in the lock card.
        sweep_row = QtWidgets.QHBoxLayout()
        sweep_row.setContentsMargins(20, 0, 8, 0)
        sweep_row.setSpacing(8)
        sweep_row.addStretch()
        sweep_row.addWidget(self._small_label("Sweep Speed:"))
        self.sweep_speed_combo = QtWidgets.QComboBox()
        _style_combo(self.sweep_speed_combo)
        self.sweep_speed_combo.setFixedWidth(110)
        for value, label in [
            (0, "3800 Hz"), (1, "1900 Hz"), (2, "950 Hz"), (3, "475 Hz"),
            (4, "237.5 Hz"), (5, "118.75 Hz"), (6, "59.4 Hz"), (7, "29.7 Hz"),
            (8, "14.8 Hz"), (9, "7.4 Hz"), (10, "3.7 Hz"), (11, "1.86 Hz"),
            (12, "0.93 Hz"), (13, "0.46 Hz"), (14, "0.23 Hz"), (15, "0.116 Hz"),
        ]:
            self.sweep_speed_combo.addItem(label, value)
        self.sweep_speed_combo.setCurrentIndex(
            self._int_setting("linien/sweep_speed_index", 8, 0, 15)
        )
        self.sweep_speed_combo.currentIndexChanged.connect(self._on_sweep_speed)
        sweep_row.addWidget(self.sweep_speed_combo)
        self._tip(
            "Linien sweep repetition rate. Slower sweeps show narrower\n"
            "features; faster sweeps update the trace more often.",
            self.sweep_speed_combo,
        )
        normal_layout.addLayout(sweep_row)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStyleSheet("""
            QSplitter::handle {
                background: #282828;
                height: 4px;
            }
            QSplitter::handle:hover {
                background: #383838;
            }
        """)
        self.linien_widget = LinienWidget(
            linien_host=self._rp_host,
            v_to_freq_ghz=V_TO_GHZ,
        )
        # Built by the Linien widget (it owns the range logic) but shown in
        # the header beside the camera toggle, with the other view controls.
        self._header_row.insertWidget(
            self._header_row.count() - 1, self.linien_widget.autozoom_check
        )
        self._tip(
            "Keep the vertical range tracking the signal. Uncheck to hold\n"
            "the current zoom.",
            self.linien_widget.autozoom_check,
        )
        self.camera_widget = CameraViewWidget()
        self.camera_widget.setVisible(False)
        self.main_splitter.addWidget(self.linien_widget)
        self.main_splitter.addWidget(self.camera_widget)
        self.main_splitter.setSizes([700, 260])
        normal_layout.addWidget(self.main_splitter, stretch=1)

        # PSD view: swapped in when measuring lock noise.
        psd_view = QtWidgets.QWidget()
        psd_layout = QtWidgets.QVBoxLayout(psd_view)
        psd_layout.setContentsMargins(0, 0, 0, 0)
        psd_layout.setSpacing(6)
        psd_bar = QtWidgets.QHBoxLayout()
        psd_bar.setSpacing(8)
        psd_title = QtWidgets.QLabel("Lock Noise PSD")
        psd_title.setStyleSheet(
            f"color: {NEON_BLUE}; font-size: 13px; font-weight: 700;"
            "letter-spacing: 1px; border: none; padding: 4px 6px;"
            f'font-family: "{TITLE_FONT_FAMILY}";'
        )
        psd_bar.addWidget(psd_title)
        psd_bar.addStretch()
        self.psd_view_status = QtWidgets.QLabel("")
        self.psd_view_status.setStyleSheet(
            "color: #ababab; font-size: 13px; font-weight: 700;"
            "border: none; padding: 4px 6px;"
        )
        psd_bar.addWidget(self.psd_view_status)
        psd_layout.addLayout(psd_bar)

        self.psd_plot = pg.PlotWidget(background="#0b0b0b")
        self.psd_plot.setLogMode(True, True)
        self.psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self.psd_plot.setLabel("bottom", "Frequency (Hz)")
        self.psd_plot.setLabel(
            "left", "Error signal ASD (counts/\N{SQUARE ROOT}Hz)"
        )
        self._psd_legend = self.psd_plot.addLegend()
        self._tip(
            "Error-signal amplitude spectral density per measurement run;\n"
            "colors distinguish runs (see legend for their PID settings).",
            self.psd_plot,
        )
        psd_layout.addWidget(self.psd_plot, stretch=1)

        self.view_stack = QtWidgets.QStackedWidget()
        self.view_stack.addWidget(normal_view)
        self.view_stack.addWidget(psd_view)
        left.addWidget(self.view_stack, stretch=1)

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

        inner = QtWidgets.QWidget()
        inner.setObjectName("controlColumnInner")
        # Scoped by objectName: a selector-less rule would cascade into
        # every card child and override the input styling.
        inner.setStyleSheet(
            "QWidget#controlColumnInner { background: transparent; }"
        )
        layout = QtWidgets.QVBoxLayout(inner)
        layout.setAlignment(QtCore.Qt.AlignTop)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 4, 0)
        scroll.setWidget(inner)

        self._add_connection_card(layout)
        self._add_laser_card(layout)
        self._add_repump_card(layout)
        self._add_lock_card(layout)
        self._add_dps_card(layout)
        self._add_config_card(layout)
        self._add_log_card(layout)
        for card_attr in set(self._CHIP_CARDS.values()):
            self._refresh_card_status(card_attr)
        layout.addStretch()

        # The sidebar swaps between the normal control cards and the PSD
        # measurement panel. Wide enough that no card ever needs
        # horizontal scrolling; the smoke test asserts the content fits
        # at the minimum width.
        self.sidebar_stack = QtWidgets.QStackedWidget()
        self.sidebar_stack.setMinimumWidth(460)
        self.sidebar_stack.setMaximumWidth(600)
        self.sidebar_stack.addWidget(scroll)
        self.sidebar_stack.addWidget(self._build_psd_sidebar())
        root.addWidget(self.sidebar_stack, stretch=1)

    def _build_psd_sidebar(self):
        container = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(container)
        outer.setAlignment(QtCore.Qt.AlignTop)
        outer.setSpacing(8)
        outer.setContentsMargins(0, 0, 4, 0)

        card = make_card()
        layout = card.layout()
        layout.addWidget(make_section_title("Lock PSD Measurement"))

        self.psd_status_label = add_readout_row(layout, "Status:", "Idle")
        self._tip(
            "PSD acquisition state. A measurement steps through\n"
            "decimations; each stage takes ~16x longer than the last.",
            self.psd_status_label,
            help_cursor=True,
        )
        self.psd_lock_label = add_readout_row(layout, "Laser:", "--")
        self._tip(
            "Measure while locked — the point is error-signal noise\n"
            "under lock.",
            self.psd_lock_label,
            help_cursor=True,
        )

        algo_row = QtWidgets.QHBoxLayout()
        algo_row.setSpacing(8)
        algo_row.addWidget(self._small_label("Algorithm:"))
        self.psd_algo_combo = QtWidgets.QComboBox()
        _style_combo(self.psd_algo_combo)
        self.psd_algo_combo.addItem("LPSD", 1)
        self.psd_algo_combo.addItem("Welch", 0)
        saved_algo = self._int_setting("psd/algorithm", 1, 0, 1)
        index = self.psd_algo_combo.findData(saved_algo)
        self.psd_algo_combo.setCurrentIndex(max(0, index))
        self.psd_algo_combo.currentIndexChanged.connect(self._on_psd_algorithm)
        algo_row.addWidget(self.psd_algo_combo, stretch=1)
        layout.addLayout(algo_row)
        self._tip(
            "PSD estimator: LPSD gives log-spaced frequency bins (best\n"
            "for wide spans); Welch is the classic linear-bin estimate.",
            self.psd_algo_combo,
        )

        decim_row = QtWidgets.QHBoxLayout()
        decim_row.setSpacing(8)
        decim_row.addWidget(self._small_label("Max Decimation:"))
        self.psd_decimation_spin = QtWidgets.QSpinBox()
        self.psd_decimation_spin.setRange(1, 32)
        self.psd_decimation_spin.setValue(
            self._int_setting("psd/max_decimation", 18, 1, 32)
        )
        self.psd_decimation_spin.valueChanged.connect(self._on_psd_decimation)
        decim_row.addWidget(self.psd_decimation_spin)
        decim_row.addStretch()
        layout.addLayout(decim_row)
        self._tip(
            "How deep the decimation ladder goes: higher reaches lower\n"
            "Fourier frequencies but takes much longer (~16x per +4).\n"
            "Default 18; try 10-14 for quick checks.",
            self.psd_decimation_spin,
        )

        self.psd_fitness_label = add_readout_row(layout, "Fitness:", "--")
        self._tip(
            "Integrated error-signal noise of the last completed run —\n"
            "lower is better; use it to compare PID settings.",
            self.psd_fitness_label,
            help_cursor=True,
        )
        self.psd_pid_label = add_readout_row(layout, "PID (P/I/D):", "--")
        self._tip(
            "Live PID parameters on the Linien server (the optimizer\n"
            "changes these while it runs).",
            self.psd_pid_label,
            help_cursor=True,
        )

        self.psd_run_btn = QtWidgets.QPushButton("Run PSD Measurement")
        self.psd_run_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.psd_run_btn.clicked.connect(self._run_psd)
        layout.addWidget(self.psd_run_btn)
        self._tip(
            "Record one PSD with the current settings. The spectroscopy\n"
            "trace pauses while the raw acquisition runs.",
            self.psd_run_btn,
        )

        self.psd_opt_btn = QtWidgets.QPushButton("Optimize PID")
        self.psd_opt_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.psd_opt_btn.clicked.connect(self._run_pid_optimization)
        layout.addWidget(self.psd_opt_btn)
        self._tip(
            "Run linien's PID optimizer: repeated PSD measurements while\n"
            "actively changing the lock's P/I gains (confirmed first).",
            self.psd_opt_btn,
        )

        self.psd_clear_btn = QtWidgets.QPushButton("Clear Plot")
        self.psd_clear_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.psd_clear_btn.clicked.connect(self._clear_psd_plot)
        layout.addWidget(self.psd_clear_btn)

        self.psd_exit_btn = QtWidgets.QPushButton("Exit PSD Mode")
        self.psd_exit_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.psd_exit_btn.clicked.connect(self._exit_psd_mode)
        layout.addWidget(self.psd_exit_btn)
        self._tip(
            "Return to the normal spectroscopy view. An in-flight\n"
            "acquisition finishes on the server by itself.",
            self.psd_exit_btn,
        )

        outer.addWidget(card)
        outer.addStretch()
        return container

    def _add_connection_card(self, parent):
        card = CollapsibleCard("Devices", collapsed=True)
        self.usb_card = card
        layout = card.content_layout

        self._tip(
            "Device assignments and connection status. Ports are verified\n"
            "automatically at startup with read-only probes.",
            card.toggle_btn,
        )

        # The edits stay the source of truth for every consumer (workers,
        # config snapshots, settings) but live in Manual setup below. What
        # shows here is status: name, current port, verified state.
        manual = CollapsibleCard("Manual setup", collapsed=True, framed=False)
        self.manual_setup_card = manual
        manual_layout = manual.content_layout

        self.rp_edit = self._add_line_edit(
            manual_layout, "Red Pitaya:", self._rp_host, "rp-XXXXXX.local"
        )
        self._tip(
            "Red Pitaya hostname or IP running linien-server (also carries\n"
            "the DAC Click V_tune and the repump gate).",
            self.rp_edit,
        )
        self.laser_edit = self._add_port_select_row(
            manual_layout, "Koheron:", self._laser_port
        )
        self._tip("Koheron CTL200 serial port.", self.laser_edit)
        self.mot_coil_port_edit = self._add_port_select_row(
            manual_layout, "Coils PSU:", self._mot_coil_port
        )
        self._tip("MOT coil DPS5005 serial port.", self.mot_coil_port_edit)
        self.dispenser_port_edit = self._add_port_select_row(
            manual_layout, "Dispenser PSU:", self._dispenser_port
        )
        self._tip("Dispenser DPS5020 serial port.", self.dispenser_port_edit)

        self.rp_status_value, self.rp_usb_status = self._add_device_status_row(
            layout, "Red Pitaya:", self.rp_edit
        )
        self.laser_status_value, self.laser_usb_status = (
            self._add_device_status_row(layout, "Koheron:", self.laser_edit)
        )
        self.coil_status_value, self.coil_usb_status = (
            self._add_device_status_row(
                layout, "Coils PSU:", self.mot_coil_port_edit
            )
        )
        self.dispenser_status_value, self.dispenser_usb_status = (
            self._add_device_status_row(
                layout, "Dispenser PSU:", self.dispenser_port_edit
            )
        )


        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.connect_btn.clicked.connect(self._connect_all)
        self._tip(
            "Restart every connection (Linien, DAC, gate, and all serial\n"
            "workers) using the fields above.",
            self.connect_btn,
        )
        manual_layout.addWidget(self.connect_btn)

        self.auto_discover_btn = QtWidgets.QPushButton("Auto-connect")
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
        layout.addWidget(self.auto_discover_btn)
        layout.addWidget(manual)
        self._refresh_discovery()

        parent.addWidget(card)

    def _add_port_select_row(self, layout, label_text, value):
        """Labelled port dropdown. Options are filled by _refresh_discovery."""
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._small_label(label_text))
        select = PortSelect(value)
        row.addWidget(select, stretch=1)
        layout.addLayout(row)
        return select

    def _add_device_status_row(self, layout, label_text, edit):
        """One status line: device name, its current port, verified state.

        The port is shown as text mirroring ``edit`` -- the edit itself is
        the source of truth and lives in Manual setup, so the common case
        reads as status rather than as a form to fill in.
        """
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._small_label(label_text))
        value = QtWidgets.QLabel(edit.text())
        value.setStyleSheet(
            "color: #e6e6e6; font-size: 13px; border: none;"
        )
        value.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        edit.textChanged.connect(value.setText)
        row.addWidget(value, stretch=1)
        status = QtWidgets.QLabel("--")
        status.setStyleSheet(
            "color: #ababab; font-size: 13px; font-weight: 700; border: none;"
        )
        # _set_usb_status_label abbreviates to fit this column.
        status.setFixedWidth(84)
        status.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        row.addWidget(status)
        layout.addLayout(row)
        return value, status

    def _refresh_discovery(self):
        """Refill the port dropdowns. Read-only: enumeration only, no port
        is opened."""
        self._serial_ports = discover_serial_ports()
        ports = [
            (port.device, _short_port_label(port), _serial_port_label(port))
            for port in self._serial_ports
        ]
        for select in (
            self.laser_edit,
            self.mot_coil_port_edit,
            self.dispenser_port_edit,
        ):
            select.set_available(ports)

    def _add_filter_section(self, parent):
        """Error-signal filters, inline in the Linien Lock card.

        Not collapsible: a dropdown inside a dropdown is a level of
        nesting too many, and the whole card already collapses.
        """
        layout = parent
        title = make_section_title("Error Signal Filters")
        layout.addWidget(title)
        self._tip(
            "Linien's two IIR filter stages applied to the demodulated\n"
            "error signal (channel A).",
            title,
            help_cursor=True,
        )

        for index in (1, 2):
            row = QtWidgets.QHBoxLayout()
            # Same margins and label width as add_slider_row, so these rows
            # sit on the same rhythm as every other control in the card.
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(8)
            enabled = QtWidgets.QCheckBox(f"Filter {index}")
            enabled.setCursor(QtCore.Qt.PointingHandCursor)
            enabled.setFixedWidth(120)
            enabled.setStyleSheet(
                "QCheckBox { color: #e6e6e6; font-size: 13px; border: none; }"
                "QCheckBox::indicator { width: 14px; height: 14px; }"
            )
            type_combo = QtWidgets.QComboBox()
            _style_combo(type_combo)
            type_combo.addItem("Low pass", 0)
            type_combo.addItem("High pass", 1)
            freq_spin = QtWidgets.QSpinBox()
            freq_spin.setRange(1, 10_000_000)
            freq_spin.setValue(10000)
            freq_spin.setSuffix(" Hz")
            freq_spin.setFixedWidth(112)
            row.addWidget(enabled)
            row.addWidget(type_combo, stretch=1)
            row.addWidget(freq_spin)
            layout.addLayout(row)
            enabled.toggled.connect(self._on_filter_changed)
            type_combo.currentIndexChanged.connect(self._on_filter_changed)
            freq_spin.valueChanged.connect(self._on_filter_changed)
            setattr(self, f"filter{index}_enabled_check", enabled)
            setattr(self, f"filter{index}_type_combo", type_combo)
            setattr(self, f"filter{index}_freq_spin", freq_spin)
            self._tip(
                f"IIR stage {index}: enable, choose low/high pass, and set\n"
                "the corner frequency. A disabled stage stays in the signal\n"
                "path with unity gain (keeps the propagation delay fixed).",
                enabled,
                type_combo,
                freq_spin,
            )


    def _add_config_card(self, parent):
        card = CollapsibleCard("Config", collapsed=True)
        layout = card.content_layout
        config_row = QtWidgets.QHBoxLayout()
        config_row.setSpacing(6)
        self.save_config_btn = QtWidgets.QPushButton("Save Config As...")
        self.save_config_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.save_config_btn.clicked.connect(self._save_config_dialog)
        config_row.addWidget(self.save_config_btn)
        self._tip(
            "Snapshot every control setting (connections, lock + PID,\n"
            "filters, laser, repump, supplies, dispenser, PSD) to a JSON\n"
            "file.",
            self.save_config_btn,
        )
        self.load_config_btn = QtWidgets.QPushButton("Load Config...")
        self.load_config_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.load_config_btn.clicked.connect(self._load_config_dialog)
        config_row.addWidget(self.load_config_btn)
        self._tip(
            "Apply a saved config: setpoints go to the hardware where\n"
            "connected. Dispenser settings ask for confirmation; port\n"
            "changes take effect on Connect / Reconnect.",
            self.load_config_btn,
        )
        layout.addLayout(config_row)
        parent.addWidget(card)

    def _add_log_card(self, parent):
        card = CollapsibleCard("Log", collapsed=True)
        self.log_card = card
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFixedHeight(150)
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background: #0b0b0b; color: #b2b2b2;"
            "border: 1px solid #282828; border-radius: 0px;"
            "font-size: 13px; padding: 4px; }"
        )
        self._tip(
            "Recent events (fires, faults, protection trips, lock changes).\n"
            "The full history is in logs/mot-YYYYMMDD.jsonl.",
            self.log_view,
        )
        card.content_layout.addWidget(self.log_view)
        parent.addWidget(card)

    def _add_lock_card(self, parent):
        card = CollapsibleCard("Error Signal & Lock", collapsed=True)
        self.lock_card = card
        layout = card.content_layout

        # Modulation master switch. Linien has no separate enable, so off
        # means amplitude 0 -- the slider keeps its value for when it is
        # switched back on.
        self.mod_enable_btn = QtWidgets.QPushButton("Modulation On")
        self.mod_enable_btn.setCheckable(True)
        self.mod_enable_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._tip(
            "Turn the current dither on or off. Off drives the amplitude\n"
            "to zero and greys out the modulation controls.",
            self.mod_enable_btn,
        )

        # Stored and sent in mV; shown in volts peak-to-peak, which is how
        # the modulation depth is specified. 1 mV of slider resolution.
        self.mod_strength_slider, self.mod_strength_edit = add_slider_row(
            layout,
            "Mod Amplitude:",
            0,
            1000,
            # Non-zero default: at 0 the dither is off and there is no
            # error signal, which reads as "modulation does nothing".
            self._int_setting("linien/mod_strength_mv", 100, 0, 1000),
            single=1,
            page=50,
            suffix=" Vpp",
            decimals=3,
            scale=1000.0,
        )
        self.mod_strength_slider.valueChanged.connect(self._on_mod_strength)
        self._tip(
            "Modulation amplitude driving the current dither, 0-1 Vp-p.",
            self.mod_strength_slider,
            self.mod_strength_edit,
        )

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
            suffix="°",
        )
        self.mod_phase_slider.valueChanged.connect(self._on_mod_phase)
        self._tip(
            "Demodulation phase (degrees). Rotates the error signal shape.",
            self.mod_phase_slider,
            self.mod_phase_edit,
        )

        # Built above so the slider handlers can reference it, but shown
        # under the three rows it governs.
        layout.addWidget(self.mod_enable_btn)

        self.mod_enable_btn.toggled.connect(self._on_mod_enabled)
        self.mod_enable_btn.setChecked(
            self._bool_setting("linien/mod_enabled", True)
        )
        self._apply_mod_enabled(self.mod_enable_btn.isChecked())

        self._add_filter_section(layout)

        self.lock_readout = add_readout_row(layout, "Laser:", "Unlocked")
        self._tip(
            "Lock state reported by Linien. Lock after selecting a region\n"
            "on the error signal trace.",
            self.lock_readout,
            help_cursor=True,
        )

        # Lock/Unlock are built by the Linien widget (they carry its lock
        # wiring) but belong here, not floating over the traces.
        layout.addWidget(self.linien_widget.lock_controls)
        self._tip(
            "Engage or release the laser lock. Locking uses the two\n"
            "positions picked on the error signal.",
            self.linien_widget.lock_controls,
        )

        lock_button_row = QtWidgets.QHBoxLayout()
        lock_button_row.setSpacing(6)
        self.pid_btn = QtWidgets.QPushButton("Set PID...")
        self.pid_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.pid_btn.clicked.connect(self._open_pid_dialog)
        lock_button_row.addWidget(self.pid_btn)
        self._tip(
            "Read and set the lock's P/I/D gains on the Red Pitaya\n"
            "(each 0-8191).",
            self.pid_btn,
        )
        self.psd_mode_btn = QtWidgets.QPushButton("Measure Lock PSD...")
        self.psd_mode_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.psd_mode_btn.clicked.connect(self._enter_psd_mode)
        lock_button_row.addWidget(self.psd_mode_btn)
        self._tip(
            "Switch to the lock-noise PSD view: measure the error-signal\n"
            "noise spectrum under lock and optimize the PID from it.",
            self.psd_mode_btn,
        )
        layout.addLayout(lock_button_row)
        parent.addWidget(card)

    def _apply_mod_enabled(self, on):
        """Grey out the modulation controls when the dither is off."""
        self.mod_enable_btn.setText(
            "Modulation On" if on else "Modulation Off"
        )
        for slider in (
            self.mod_strength_slider,
            self.mod_freq_slider,
            self.mod_phase_slider,
        ):
            slider.setEnabled(on)
        # Read-only rather than disabled: disabling these edits segfaults
        # Qt on teardown, and the value should stay legible either way.
        for edit in (
            self.mod_strength_edit,
            self.mod_freq_edit,
            self.mod_phase_edit,
        ):
            edit.setReadOnly(not on)

    def _on_mod_enabled(self, on):
        self._apply_mod_enabled(on)
        self._settings.setValue("linien/mod_enabled", bool(on))
        self._event_log.log("setpoint", name="mod_enabled", value=bool(on))
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_modulation_amplitude(
                self.mod_strength_slider.value() if on else 0
            )

    def _add_laser_card(self, parent):
        card = CollapsibleCard("Laser Setpoints", collapsed=True)
        self.laser_card = card
        layout = card.content_layout

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

        # The laser runs at a fixed operating point. Both setpoints start
        # locked every session so neither can be moved by a stray drag;
        # unlocking is deliberate and is never persisted.
        lock_row = QtWidgets.QHBoxLayout()
        lock_row.setSpacing(8)
        self.laser_unlock_box = QtWidgets.QCheckBox("Unlock setpoints")
        self.laser_unlock_box.setCursor(QtCore.Qt.PointingHandCursor)
        self.laser_unlock_box.setStyleSheet(
            "QCheckBox { color: #e6e6e6; font-size: 13px; border: none; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
        )
        self.laser_unlock_box.toggled.connect(self._on_laser_unlock)
        lock_row.addWidget(self.laser_unlock_box)
        lock_row.addStretch()
        self.laser_lock_label = QtWidgets.QLabel()
        lock_row.addWidget(self.laser_lock_label)
        layout.addLayout(lock_row)
        self._tip(
            "Laser current and thermistor are locked at the operating point.\n"
            "Tick to enable the sliders for this session only.",
            self.laser_unlock_box,
        )

        self.current_slider, self.current_edit = add_slider_row(
            layout,
            "Current:",
            50,
            100,
            self._int_setting("laser/current_ma", 80, 50, 100),
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
            15000,
            # 10 k on the 10 k NTC curve is 25 C, the intended operating point.
            # The previous 6215 default sat at 36 C.
            self._int_setting("laser/thermistor_ohm", 10000, 5000, 15000),
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
        self._apply_laser_lock(False)
        self._tip(
            "Setpoint converted with the 10 k NTC Steinhart-Hart curve\n"
            "(estimate, not a measurement).",
            self.therm_temp_readback,
            help_cursor=True,
        )
        parent.addWidget(card)

    def _add_dps_card(self, parent):
        """Coils and dispenser: two cards, one per supply."""
        card = CollapsibleCard("Coils", collapsed=True)
        self.coils_card = card
        layout = card.content_layout

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

        parent.addWidget(card)

        card = CollapsibleCard("Dispenser", collapsed=True)
        self.dispenser_card = card
        layout = card.content_layout

        self.dispenser_voltage_slider, self.dispenser_voltage_edit = (
            add_slider_row(
                layout,
                "Voltage:",
                0,
                50000,
                int(round(self._dispenser_fire_voltage * 1000)),
                single=10,
                page=1000,
                suffix=" V",
                decimals=3,
                scale=1000.0,
            )
        )
        self.dispenser_voltage_slider.valueChanged.connect(
            self._on_dispenser_voltage
        )
        self._tip(
            "Dispenser supply voltage limit (V). The dispenser is a\n"
            "near-short, so the supply runs current-limited and this is\n"
            "only headroom.",
            self.dispenser_voltage_slider,
            self.dispenser_voltage_edit,
        )
        self.dispenser_current_slider, self.dispenser_current_edit = (
            add_slider_row(
                layout,
                "Current:",
                0,
                20000,
                int(round(self._dispenser_fire_current * 1000)),
                single=10,
                page=100,
                suffix=" A",
                decimals=3,
                scale=1000.0,
            )
        )
        self.dispenser_current_slider.valueChanged.connect(
            self._on_dispenser_current
        )
        self._tip(
            "Dispense current setpoint (A) — the supply runs here in CC\n"
            "for the fire duration.",
            self.dispenser_current_slider,
            self.dispenser_current_edit,
        )
        self.dispenser_time_slider, self.dispenser_time_edit = add_slider_row(
            layout,
            "Fire Time:",
            int(DispenserWorker.MIN_FIRE_SECONDS),
            120,
            int(round(self._dispenser_fire_seconds)),
            single=1,
            page=5,
            suffix=" s",
        )
        self.dispenser_time_slider.valueChanged.connect(
            self._on_dispenser_time
        )
        self._tip(
            "How long a fire runs before the supply is forced off.\n"
            "A 15 min cooldown follows every fire.",
            self.dispenser_time_slider,
            self.dispenser_time_edit,
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

        parent.addWidget(card)

    def _on_dispenser_voltage(self, millivolts):
        self._dispenser_fire_voltage = millivolts / 1000.0
        self._settings.setValue(
            "dispenser/fire_voltage_v", self._dispenser_fire_voltage
        )
        self._debounced_log(
            "dispenser_voltage",
            "setpoint",
            lambda: {
                "name": "dispenser_voltage_v",
                "value": self._dispenser_fire_voltage,
            },
        )
        self._push_dispenser_fire_parameters()

    def _on_dispenser_current(self, milliamps):
        self._dispenser_fire_current = milliamps / 1000.0
        self._settings.setValue(
            "dispenser/fire_current_a", self._dispenser_fire_current
        )
        self._debounced_log(
            "dispenser_current",
            "setpoint",
            lambda: {
                "name": "dispenser_current_a",
                "value": self._dispenser_fire_current,
            },
        )
        self._push_dispenser_fire_parameters()

    def _on_dispenser_time(self, seconds):
        self._dispenser_fire_seconds = float(seconds)
        self._settings.setValue(
            "dispenser/fire_seconds", self._dispenser_fire_seconds
        )
        self._debounced_log(
            "dispenser_time",
            "setpoint",
            lambda: {
                "name": "dispenser_fire_seconds",
                "value": self._dispenser_fire_seconds,
            },
        )
        if self._dispenser_phase == "idle":
            self.dispenser_fire_btn.setText(
                f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
            )
        self._push_dispenser_fire_parameters()

    def _push_dispenser_fire_parameters(self):
        worker = getattr(self, "dispenser_worker", None)
        if worker is not None:
            worker.set_fire_parameters(
                self._dispenser_fire_current,
                self._dispenser_fire_seconds,
                self._dispenser_fire_voltage,
            )

    def _add_repump_card(self, parent):
        card = CollapsibleCard("Repump", collapsed=True)
        self.repump_card = card
        layout = card.content_layout

        # No V_tune readout row: the slider's own value box below shows it,
        # and it adopts the hardware value on connect.
        self.vtune_freq_readback = add_readout_row(layout, "VCO Freq:", "--")
        self._tip(
            "Repump VCO output frequency: 0-5 V V_tune spans\n"
            f"{VCO_FREQ_MIN_MHZ:.0f}-{VCO_FREQ_MAX_MHZ:.0f} MHz (linear).",
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

        self.repump_gpio_btn = QtWidgets.QPushButton("Repump Off")
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
            "color: #ababab; font-size: 13px; font-weight: 600; border: none;"
        )
        label.setFixedWidth(120)
        return label

    @staticmethod
    def _tiny_label(text):
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(
            "color: #969696; font-size: 13px; font-weight: 600; border: none;"
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

        self._set_chip("usb", "DEVICES ...", "#ababab")
        for attr in self._USB_STATUS_ROWS:
            self._set_colored_text(getattr(self, attr), "Probing…", "#ababab")

        self.probe_worker = SerialProbeWorker(
            saved={
                "laser": self.laser_edit.text().strip(),
                "mot_coil": self.mot_coil_port_edit.text().strip(),
                "dispenser": self.dispenser_port_edit.text().strip(),
            },
            coil_address=self._mot_coil_address,
            dispenser_address=self._dispenser_address,
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
                self._set_colored_text(getattr(self, attr), "Verified", "#6f9e7c")
            else:
                self._set_colored_text(getattr(self, attr), "Not found", "#b56a6a")

        if res.get("ok"):
            self._set_chip("usb", "DEVICES 3/3", "#6f9e7c")
        else:
            n_ok = sum(
                1
                for key in ("laser", "mot_coil", "dispenser")
                if status.get(key) == "verified"
            )
            # The card is left as the user had it; the header count and
            # the per-row statuses inside say which device is missing.
            self._set_chip("usb", f"DEVICES {n_ok}/3", "#b56a6a")

        self._save_connection_settings()

        if startup and not res.get("ok"):
            # No pop-up: the Device Connections card expands itself above
            # and shows which device is missing, in place.
            self._refresh_discovery()
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
            "dps/mot_coil_address", self._mot_coil_address
        )
        self._settings.setValue(
            "dps/dispenser_address", self._dispenser_address
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
            address=self._mot_coil_address,
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
            address=self._dispenser_address,
            model="DPS5020",
            cooldown_until=self._dispenser_cooldown_until,
            fire_seconds=self._dispenser_fire_seconds,
            dispense_current_a=self._dispenser_fire_current,
            dispense_voltage_v=self._dispenser_fire_voltage,
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
            "psd_worker",
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
        self.mod_strength_edit.setText(f"{value / 1000.0:.3f} Vpp")
        self._settings.setValue("linien/mod_strength_mv", value)
        self._debounced_log(
            "mod_strength",
            "setpoint",
            lambda: {
                "name": "mod_strength_mv",
                "value": self.mod_strength_slider.value(),
            },
        )
        # With modulation off the device stays at zero regardless; the
        # slider only records what to restore when it is switched on.
        if not self.mod_enable_btn.isChecked():
            return
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
            self._set_chip("lock", "LOCKED", "#6f9e7c")
        else:
            self._set_chip("lock", "UNLOCKED", "#b56a6a")
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
        self._set_chip(
            "laser",
            "LASER ON" if checked else "LASER OFF",
            "#6f9e7c" if checked else self._CHIP_NEUTRAL,
        )
        self._event_log.log("laser_output", on=bool(checked))
        worker = getattr(self, "laser_worker", None)
        if worker is not None:
            worker.laser_on(checked)
        # The repump is only meaningful with the laser running, so it
        # follows the laser off. It does not follow the laser back on --
        # turning it on is always a deliberate act.
        repump_btn = getattr(self, "repump_gpio_btn", None)
        if not checked and repump_btn is not None and repump_btn.isChecked():
            self._event_log.log("repump_gate_auto_off", reason="laser_off")
            repump_btn.setChecked(False)

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

    def _apply_laser_lock(self, unlocked):
        """Enable or disable the laser setpoint controls and update the badge."""
        for slider in (self.current_slider, self.therm_slider):
            slider.setEnabled(unlocked)
        # Read-only rather than disabled: the value must stay legible when
        # locked, and disabling these edits segfaults Qt on teardown.
        for edit in (self.current_edit, self.therm_edit):
            edit.setReadOnly(not unlocked)
        self._set_colored_text(
            self.laser_lock_label,
            "UNLOCKED" if unlocked else "LOCKED",
            "#b9924f" if unlocked else "#888888",
        )

    def _on_laser_unlock(self, unlocked):
        self._apply_laser_lock(unlocked)
        self._event_log.log("laser_setpoints_unlocked", unlocked=bool(unlocked))

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
        # Keep the current slider showing what the override set, without
        # bouncing back through _on_dispenser_current.
        self.dispenser_current_slider.blockSignals(True)
        self.dispenser_current_slider.setValue(
            int(round(self._dispenser_fire_current * 1000))
        )
        self.dispenser_current_edit.setText(
            f"{self._dispenser_fire_current:.3f} A"
        )
        self.dispenser_current_slider.blockSignals(False)
        self.dispenser_time_slider.blockSignals(True)
        self.dispenser_time_slider.setValue(
            int(round(self._dispenser_fire_seconds))
        )
        self.dispenser_time_edit.setText(
            f"{self._dispenser_fire_seconds:.0f} s"
        )
        self.dispenser_time_slider.blockSignals(False)
        self._push_dispenser_fire_parameters()
        self._event_log.log(
            "dispenser_override",
            current_a=self._dispenser_fire_current,
            seconds=self._dispenser_fire_seconds,
        )
        if self._dispenser_phase == "idle":
            self.dispenser_fire_btn.setText(
                f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
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
            self._set_chip("coils", "COILS TRIP", "#b56a6a")
        elif state.get("output_enabled"):
            self._set_chip("coils", f"COILS {state['i_out']:.1f} A", "#6f9e7c")
        else:
            self._set_chip("coils", "COILS OFF", "#ababab")
        self._log_readback("coil", state)

    def _on_dispenser_readback(self, state):
        # No readback row on this card -- the fire button and the header
        # chip carry the state. Protections still reach the event log.
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
                "#b56a6a",
            )
            return
        text = (
            f"{state['v_out']:.2f} V / {state['i_out']:.2f} A / "
            f"{state['power']:.2f} W"
        )
        if show_mode and state.get("output_enabled"):
            text += " / CC" if state.get("cv_cc") else " / CV"
        label.setText(text)
        label.setStyleSheet("color: #e6e6e6; font-size: 13px; border: none;")

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
            self._set_chip("disp", "DISP FIRING", "#b9924f")
        elif phase == "cooldown":
            minutes, seconds = divmod(int(round(remaining)), 60)
            self.dispenser_fire_btn.setEnabled(False)
            self.dispenser_fire_btn.setText(f"Cooldown {minutes:d}:{seconds:02d}")
            self._set_chip("disp", f"DISP {minutes:d}:{seconds:02d}", "#c4c4c4")
        else:
            self.dispenser_fire_btn.setEnabled(True)
            self.dispenser_fire_btn.setText(
                f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
            )
            self._set_chip("disp", "DISP IDLE", "#ababab")

    def _update_dispenser_progress(self, phase, remaining):
        if phase == "firing":
            total = max(1.0, self._dispenser_fire_seconds)
            color = "#b9924f"
        elif phase == "cooldown":
            total = DispenserWorker.COOLDOWN_SECONDS
            color = "#c4c4c4"
        else:
            self.dispenser_progress.setVisible(False)
            return
        fraction = max(0.0, min(1.0, remaining / total))
        self.dispenser_progress.setValue(int(round(1000 * fraction)))
        self.dispenser_progress.setStyleSheet(
            "QProgressBar { background: #111111; border: none;"
            "border-radius: 0px; }"
            f"QProgressBar::chunk {{ background: {color};"
            "border-radius: 0px; }"
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
        self._set_repump_ui(checked)
        self._event_log.log("repump_gate", on=bool(checked))
        worker = getattr(self, "repump_gpio_worker", None)
        if worker is not None:
            worker.set_output(checked)

    def _on_repump_gpio_state(self, enabled):
        self.repump_gpio_btn.blockSignals(True)
        self.repump_gpio_btn.setChecked(enabled)
        self._set_repump_ui(enabled)
        self.repump_gpio_btn.blockSignals(False)

    def _set_repump_ui(self, on):
        self.repump_gpio_btn.setText("Repump On" if on else "Repump Off")
        self._set_chip(
            "repump",
            "REPUMP ON" if on else "REPUMP OFF",
            "#6f9e7c" if on else self._CHIP_NEUTRAL,
        )

    # ------------------------------------------------------------------
    # Error signal filters
    # ------------------------------------------------------------------

    def _filters_from_ui(self):
        return {
            "automatic": False,
            "filter_1_enabled": self.filter1_enabled_check.isChecked(),
            "filter_1_type": int(self.filter1_type_combo.currentData()),
            "filter_1_frequency": self.filter1_freq_spin.value(),
            "filter_2_enabled": self.filter2_enabled_check.isChecked(),
            "filter_2_type": int(self.filter2_type_combo.currentData()),
            "filter_2_frequency": self.filter2_freq_spin.value(),
        }

    def _set_filters_ui(self, filters):
        self._filters_syncing = True
        try:
            for index in (1, 2):
                enabled = getattr(self, f"filter{index}_enabled_check")
                type_combo = getattr(self, f"filter{index}_type_combo")
                freq_spin = getattr(self, f"filter{index}_freq_spin")
                enabled.setChecked(bool(filters.get(f"filter_{index}_enabled", False)))
                combo_index = type_combo.findData(
                    int(filters.get(f"filter_{index}_type", 0))
                )
                if combo_index >= 0:
                    type_combo.setCurrentIndex(combo_index)
                freq_spin.setValue(
                    int(filters.get(f"filter_{index}_frequency", 10000))
                )
        finally:
            self._filters_syncing = False

    def _on_filter_changed(self, *_args):
        if self._filters_syncing:
            return
        self._debounced_log(
            "error_filters",
            "setpoint",
            lambda: {"name": "error_filters", **self._filters_from_ui()},
        )
        # Coalesce bursts (typing a frequency) into one hardware write.
        timer = self._log_debounce.get("_filter_push")
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._push_filters)
            self._log_debounce["_filter_push"] = timer
        timer.start(400)

    def _push_filters(self):
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_filters(self._filters_from_ui())

    def _sync_filters_from_device(self):
        worker = self._get_lock_worker()
        filters = worker.get_filters() if worker is not None else None
        if filters is None:
            print("! error filter read skipped: Linien not connected")
            return
        self._set_filters_ui(filters)

    def _on_linien_connection(self, status):
        self._set_usb_status_label(self.rp_usb_status, status)
        if status.startswith("connected"):
            # Give the worker a moment to finish its own connect-time setup.
            QtCore.QTimer.singleShot(1500, self._sync_filters_from_device)

    # ------------------------------------------------------------------
    # Lock PID / config snapshot
    # ------------------------------------------------------------------

    def _open_pid_dialog(self):
        worker = self._get_lock_worker()
        current = worker.get_pid() if worker is not None else None
        dialog = PidDialog(self, current)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        p_value, i_value, d_value = dialog.values()
        if worker is None:
            QtWidgets.QMessageBox.warning(
                self,
                "Linien not connected",
                "The PID could not be written: no Linien connection.\n"
                "Set the Red Pitaya host and press Connect / Reconnect.",
            )
            return
        worker.set_pid(p_value, i_value, d_value)
        self._event_log.log("pid_set", p=p_value, i=i_value, d=d_value)

    def _collect_config(self):
        """Snapshot of every control setting, for the JSON export."""
        worker = self._get_lock_worker()
        pid = worker.get_pid() if worker is not None else None
        vtune_v = self.vtune_slider.value() / 1000.0
        return {
            "saved_at": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "connection": {
                "rp_host": self.rp_edit.text().strip(),
                "laser_port": self.laser_edit.text().strip(),
                "mot_coil_port": self.mot_coil_port_edit.text().strip(),
                "dispenser_port": self.dispenser_port_edit.text().strip(),
                "mot_coil_address": self._mot_coil_address,
                "dispenser_address": self._dispenser_address,
            },
            "linien": {
                "mod_freq_khz": self.mod_freq_slider.value(),
                "mod_phase_deg": self.mod_phase_slider.value(),
                "mod_strength_mv": self.mod_strength_slider.value(),
                "sweep_speed_index": self.sweep_speed_combo.currentIndex(),
                "pid": (
                    {"p": pid[0], "i": pid[1], "d": pid[2]} if pid else None
                ),
                "filters": self._filters_from_ui(),
            },
            "laser": {
                "current_ma": self.current_slider.value(),
                "thermistor_ohm": self.therm_slider.value(),
                "temp_est_c": round(
                    thermistor_ohms_to_c(self.therm_slider.value()), 2
                ),
            },
            "repump": {
                "vtune_v": vtune_v,
                "vco_mhz": round(vtune_to_vco_mhz(vtune_v), 1),
            },
            "coils": {
                "voltage_v": self.mot_coil_voltage_slider.value() / 1000.0,
                "current_a": self.mot_coil_current_slider.value() / 1000.0,
            },
            "dispenser": {
                "fire_current_a": self._dispenser_fire_current,
                "fire_seconds": self._dispenser_fire_seconds,
                "total_fires": self._int_setting("dispenser/total_fires", 0),
                "total_fire_s": self._float_setting(
                    "dispenser/total_fire_s", 0.0
                ),
                "cooldown_until_epoch": self._dispenser_cooldown_until,
            },
            "psd": {
                "algorithm": int(self.psd_algo_combo.currentData()),
                "max_decimation": self.psd_decimation_spin.value(),
            },
        }

    def _write_config_json(self, path):
        config = self._collect_config()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        self._event_log.log("config_saved", path=str(path))
        return config

    def _save_config_dialog(self):
        default_name = time.strftime("oq-config-%Y%m%d-%H%M%S.json")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Config",
            os.path.join(app_data_dir(), default_name),
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            self._write_config_json(path)
        except OSError as e:
            self._log_fault("config", e)
            QtWidgets.QMessageBox.warning(
                self, "Save failed", f"Could not write the config:\n{e}"
            )

    def _load_config_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Config", app_data_dir(), "JSON (*.json)"
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("not a config object")
        except (OSError, ValueError) as e:
            self._log_fault("config", e)
            QtWidgets.QMessageBox.warning(
                self, "Load failed", f"Could not read the config:\n{e}"
            )
            return
        self._apply_config(config, path=path)

    def _apply_config(self, config, path=None):
        """Apply a config snapshot to the UI (handlers push to hardware).

        Missing keys are left unchanged, so partial configs are fine.
        Ports/hosts only update the fields — press Connect / Reconnect to
        use them. Dispenser settings are confirmation-gated. The fire
        history and cooldown are device truth and are never loaded.
        """
        connection = config.get("connection", {})
        for key, edit in (
            ("rp_host", self.rp_edit),
            ("laser_port", self.laser_edit),
            ("mot_coil_port", self.mot_coil_port_edit),
            ("dispenser_port", self.dispenser_port_edit),
        ):
            if key in connection:
                edit.setText(str(connection[key]))
        if "mot_coil_address" in connection:
            self._mot_coil_address = int(connection["mot_coil_address"])
        if "dispenser_address" in connection:
            self._dispenser_address = int(connection["dispenser_address"])

        linien = config.get("linien", {})
        for key, slider in (
            ("mod_freq_khz", self.mod_freq_slider),
            ("mod_phase_deg", self.mod_phase_slider),
            ("mod_strength_mv", self.mod_strength_slider),
        ):
            if key in linien:
                slider.setValue(int(linien[key]))
        if "sweep_speed_index" in linien:
            self.sweep_speed_combo.setCurrentIndex(
                max(0, min(15, int(linien["sweep_speed_index"])))
            )
        pid = linien.get("pid")
        if pid:
            worker = self._get_lock_worker()
            if worker is not None:
                p_value = int(pid.get("p", 50))
                i_value = int(pid.get("i", 5))
                d_value = int(pid.get("d", 0))
                worker.set_pid(p_value, i_value, d_value)
                self._event_log.log(
                    "pid_set", p=p_value, i=i_value, d=d_value, source="config"
                )
        filters = linien.get("filters")
        if filters:
            self._set_filters_ui({**self._filters_from_ui(), **filters})
            self._push_filters()

        laser = config.get("laser", {})
        if "current_ma" in laser:
            self.current_slider.setValue(int(laser["current_ma"]))
        if "thermistor_ohm" in laser:
            self.therm_slider.setValue(int(laser["thermistor_ohm"]))

        repump = config.get("repump", {})
        if "vtune_v" in repump:
            self.vtune_slider.setValue(
                int(round(float(repump["vtune_v"]) * 1000))
            )

        coils = config.get("coils", {})
        if "voltage_v" in coils:
            self.mot_coil_voltage_slider.setValue(
                int(round(float(coils["voltage_v"]) * 1000))
            )
        if "current_a" in coils:
            self.mot_coil_current_slider.setValue(
                int(round(float(coils["current_a"]) * 1000))
            )

        psd = config.get("psd", {})
        if "algorithm" in psd:
            index = self.psd_algo_combo.findData(int(psd["algorithm"]))
            if index >= 0:
                self.psd_algo_combo.setCurrentIndex(index)
        if "max_decimation" in psd:
            self.psd_decimation_spin.setValue(int(psd["max_decimation"]))

        dispenser = config.get("dispenser", {})
        new_current = float(
            dispenser.get("fire_current_a", self._dispenser_fire_current)
        )
        new_seconds = float(
            dispenser.get("fire_seconds", self._dispenser_fire_seconds)
        )
        if (
            abs(new_current - self._dispenser_fire_current) > 0.005
            or abs(new_seconds - self._dispenser_fire_seconds) > 0.5
        ):
            confirm = QtWidgets.QMessageBox.question(
                self,
                "Dispenser settings from config",
                f"The config sets the dispenser fire to {new_current:.2f} A "
                f"for {new_seconds:.0f} s (currently "
                f"{self._dispenser_fire_current:.2f} A / "
                f"{self._dispenser_fire_seconds:.0f} s).\n\nApply it?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel,
            )
            if confirm == QtWidgets.QMessageBox.Yes:
                self._apply_dispenser_override(new_current, new_seconds)

        self._event_log.log(
            "config_loaded",
            path=str(path) if path else None,
            saved_at=config.get("saved_at"),
        )

    # ------------------------------------------------------------------
    # Lock PSD mode
    # ------------------------------------------------------------------

    def _enter_psd_mode(self):
        if self.view_stack.currentIndex() == 1:
            return
        self.view_stack.setCurrentIndex(1)
        self.sidebar_stack.setCurrentIndex(1)
        # The header no longer swaps with the view, so hide the controls
        # that do not apply to the PSD page.
        self._set_view_controls_visible(False)
        self._psd_prev_status = {}
        self._event_log.log("psd_mode", entered=True)

        self.psd_worker = PsdWorker(host=self.rp_edit.text().strip())
        self.psd_worker.connection_status.connect(self._on_psd_connection)
        self.psd_worker.status.connect(self._on_psd_status)
        self.psd_worker.psd_data.connect(self._on_psd_data)
        self.psd_worker.fault.connect(lambda msg: self._log_fault("psd", msg))
        # Queued settings apply once the worker connects.
        self.psd_worker.set_algorithm(self.psd_algo_combo.currentData())
        self.psd_worker.set_max_decimation(self.psd_decimation_spin.value())
        self.psd_worker.start()

    def _exit_psd_mode(self):
        worker = getattr(self, "psd_worker", None)
        if worker is not None:
            worker.stop()
            self.psd_worker = None
        self.view_stack.setCurrentIndex(0)
        self.sidebar_stack.setCurrentIndex(0)
        self._set_view_controls_visible(True)
        self._event_log.log("psd_mode", entered=False)

    def _set_view_controls_visible(self, visible):
        """Show/hide the header controls that only apply to the plot view."""
        self.camera_toggle.setVisible(visible)
        self.linien_widget.autozoom_check.setVisible(visible)

    def _run_psd(self):
        worker = getattr(self, "psd_worker", None)
        if worker is None:
            return
        worker.set_algorithm(self.psd_algo_combo.currentData())
        worker.set_max_decimation(self.psd_decimation_spin.value())
        worker.start_psd()
        self._event_log.log(
            "psd_start",
            algorithm=int(self.psd_algo_combo.currentData()),
            max_decimation=self.psd_decimation_spin.value(),
        )
        self._set_colored_text(self.psd_status_label, "Starting...", "#b9924f")

    def _run_pid_optimization(self):
        worker = getattr(self, "psd_worker", None)
        if worker is None:
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Optimize PID",
            "Run linien's PID optimization?\n\n"
            "This actively changes the lock's P/I gains on the Red Pitaya "
            "while it measures — the lock must be engaged, and it can take "
            "several minutes.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        worker.start_pid_optimization()
        self._event_log.log("pid_opt_start")
        self._set_colored_text(self.psd_status_label, "Starting...", "#b9924f")

    def _on_psd_connection(self, status):
        self._log_connection("psd", status)
        if status == "no_host":
            self._set_colored_text(
                self.psd_status_label, "No RP host set", "#b56a6a"
            )
        elif status.startswith("error"):
            self._set_colored_text(self.psd_status_label, "Error", "#b56a6a")
            self.psd_status_label.setToolTip(status)

    def _on_psd_status(self, state):
        acquiring = bool(state.get("acquiring"))
        optimizing = bool(state.get("optimizing"))
        busy = acquiring or optimizing
        if optimizing:
            self._set_colored_text(
                self.psd_status_label, "Optimizing PID...", "#b9924f"
            )
        elif acquiring:
            self._set_colored_text(
                self.psd_status_label, "Acquiring PSD...", "#b9924f"
            )
        else:
            self._set_colored_text(self.psd_status_label, "Idle", "#ababab")
        self.psd_view_status.setText(
            "OPTIMIZING" if optimizing else "ACQUIRING" if acquiring else ""
        )
        self.psd_run_btn.setEnabled(not busy)
        self.psd_opt_btn.setEnabled(not busy)
        self._set_lock_label(self.psd_lock_label, bool(state.get("locked")))
        self.psd_pid_label.setText(
            f"{state.get('p', 0)} / {state.get('i', 0)} / {state.get('d', 0)}"
        )
        previous = self._psd_prev_status
        if previous.get("optimizing") and not optimizing:
            self._event_log.log(
                "pid_opt_done", p=state.get("p"), i=state.get("i")
            )
        self._psd_prev_status = state

    def _on_psd_data(self, payload):
        fitness = payload.get("fitness")
        if fitness is not None:
            self.psd_fitness_label.setText(f"{fitness:.3e}")
        self._plot_psd_run(payload)
        if payload.get("complete"):
            self._event_log.log(
                "psd_complete",
                fitness=float(fitness) if fitness is not None else None,
                p=payload.get("p"),
                i=payload.get("i"),
                d=payload.get("d"),
                uuid=payload.get("uuid"),
            )

    def _plot_psd_run(self, payload):
        uuid = payload.get("uuid") or "run"
        run = self._psd_runs.get(uuid)
        if run is None:
            color = PSD_RUN_COLORS[self._psd_color_index % len(PSD_RUN_COLORS)]
            self._psd_color_index += 1
            run = {"color": color, "curves": [], "legend_added": False}
            self._psd_runs[uuid] = run
        for curve in run["curves"]:
            self.psd_plot.removeItem(curve)
        run["curves"] = []

        pen = pg.mkPen(run["color"], width=2)
        first = True
        for decimation in sorted(payload.get("psds", {})):
            freqs, asd = payload["psds"][decimation]
            freqs = np.asarray(freqs, dtype=float)
            asd = np.asarray(asd, dtype=float)
            mask = (freqs > 0) & (asd > 0)  # log-log plot
            if not mask.any():
                continue
            name = None
            if first and not run["legend_added"]:
                name = f"P={payload.get('p')} I={payload.get('i')}"
                run["legend_added"] = True
            curve = self.psd_plot.plot(
                freqs[mask], asd[mask], pen=pen, name=name
            )
            run["curves"].append(curve)
            first = False

    def _clear_psd_plot(self):
        for run in self._psd_runs.values():
            for curve in run["curves"]:
                self.psd_plot.removeItem(curve)
        self._psd_runs = {}
        self._psd_color_index = 0
        if self._psd_legend is not None:
            self._psd_legend.clear()
        self.psd_fitness_label.setText("--")

    def _on_psd_algorithm(self, _index):
        value = int(self.psd_algo_combo.currentData())
        self._settings.setValue("psd/algorithm", value)
        self._event_log.log("setpoint", name="psd_algorithm", value=value)
        worker = getattr(self, "psd_worker", None)
        if worker is not None:
            worker.set_algorithm(value)

    def _on_psd_decimation(self, value):
        self._settings.setValue("psd/max_decimation", value)
        self._debounced_log(
            "psd_decimation",
            "setpoint",
            lambda: {
                "name": "psd_max_decimation",
                "value": self.psd_decimation_spin.value(),
            },
        )
        worker = getattr(self, "psd_worker", None)
        if worker is not None:
            worker.set_max_decimation(value)

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
            text, color = "OK", "#6f9e7c"
        elif status in ("no_port", "no_host"):
            text, color = "No port", "#ababab"
        elif status.startswith("error"):
            text, color = "Error", "#b56a6a"
        else:
            text, color = "--", "#ababab"
        label.setText(text)
        label.setToolTip(status)
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 700; border: none;"
        )

    @staticmethod
    def _set_status_label(label, status):
        if status.startswith("connected:"):
            text = status.split(":", 1)[1]
            color = "#6f9e7c"
        elif status == "connected":
            text = "Connected"
            color = "#6f9e7c"
        elif status in ("no_port", "no_host"):
            text = "Not configured"
            color = "#ababab"
        elif status.startswith("error:"):
            text = "Error"
            color = "#b56a6a"
        else:
            text = "Disconnected"
            color = "#b56a6a"
        label.setText(text)
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 700; border: none;"
        )

    def _show_fault(self, label, device, message):
        label.setText("Fault")
        label.setStyleSheet(
            "color: #b56a6a; font-size: 13px; font-weight: 700; border: none;"
        )
        self._log_fault(device, message)

    @staticmethod
    def _set_lock_label(label, locked):
        text = "Locked" if locked else "Unlocked"
        color = "#6f9e7c" if locked else "#b56a6a"
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
    app.setOrganizationName("OQ-Controls")
    app.setApplicationName("OpenQuantumControls")

    load_app_fonts()
    # setFont covers non-stylesheet text (e.g. pyqtgraph axis labels).
    app.setFont(QtGui.QFont(BODY_FONT_FAMILY))
    app.setStyleSheet(build_app_stylesheet())
    pg.setConfigOptions(
        useOpenGL=False, enableExperimental=False, foreground="#b2b2b2"
    )

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
