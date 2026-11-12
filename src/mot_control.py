"""
OpenQuantum Controls UI.

Single-laser rubidium MOT control interface. The one Koheron-controlled laser
is locked with one Linien/Red Pitaya spectroscopy signal. The repump VCO tune
voltage (V_tune) is set by the DAC Click (MCP4921) on the Red Pitaya Click
Shield through the patched linien-server's ``dac_click_voltage`` parameter,
and the repump RF gate is a Linien GPIO bit (DIO1_N, active-low). The MOT
coils run from a DPS5005 supply; the dispenser runs from a DPS5020 with a
timed fire (30 s default, 120 s at most).
"""

import html
import json
import math
import os
import sys
import time
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

# Self-contained: the Linien widget/worker, styles, and the Koheron
# worker are vendored in this repo.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# Code lives in src/; fonts, assets and logs/ stay at the repo root.
ROOT = os.path.dirname(HERE)

from linien_widget import LinienWidget
from styles import (
    ACCENT,
    APP_STYLESHEET,
    PLOT_COLUMN_MARGINS,
    UI_FONT_PX,
    NEON_BLUE,
    add_readout_row,
    add_slider_row as _base_add_slider_row,
    make_card,
    make_section_title as _base_make_section_title,
    plot_label_style,
    style_plot_axes,
)

from mot_logging import EventLog, capture_library_logging
from psd_window import PsdWindow
from mot_meter import (
    METER_INTERVAL_S, MotMeter, RoiItem, VideoView, qimage_to_gray, roi_brightness,
)
from mot_workers import (
    DacClickWorker,
    DispenserWorker,
    LaserSetpointWorker,
    LinienGpioWorker,
    LinienServerRestartWorker,
    PsdWorker,
    RidenDPSWorker,
    SerialPresenceWorker,
    SerialProbeWorker,
)

# linien reports PSD amplitudes in ADC counts; its GUI divides by this to
# plot volts (linien_gui.ui.plot_widget.VOLTS_TO_COUNTS_FACTOR).
PSD_VOLTS_TO_COUNTS = 8192

# Colors cycled through for successive PSD measurement runs.
PSD_RUN_COLORS = (
    "#c4c4c4", "#b9924f", "#6f9e7c", "#9a7ba8",
    "#b8ab63", "#7a80b0", "#b56a6a", "#7ba8a8",
)

# Riden DPS protection register (reg 7) values.
DPS_PROTECTION = {1: "OVP", 2: "OCP", 3: "OPP"}

def apply_dark_titlebar(widget):
    """Ask Windows to draw this window's title bar in dark mode.

    The frame is drawn by the OS, not by Qt, so a light title bar sits
    above the dark UI until DWM is told otherwise. No-op everywhere else
    and on Windows builds that predate the attribute.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        handle = int(widget.winId())
        enabled = ctypes.c_int(1)
        # 20 is DWMWA_USE_IMMERSIVE_DARK_MODE on Windows 10 2004+; 19 is
        # the pre-2004 spelling. Try the current one, then the old one.
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(handle),
                ctypes.c_int(attribute),
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                return True
    except Exception:
        pass  # cosmetic only: never let this stop the app starting
    return False


def resource_path(*parts):
    """Absolute path to a bundled resource (fonts, etc.).

    Works both from a source checkout and from a frozen (PyInstaller)
    build, where bundled files are unpacked under ``sys._MEIPASS``.
    """
    base = getattr(sys, "_MEIPASS", ROOT)
    return os.path.join(base, *parts)


def app_data_dir():
    """Writable per-user data directory.

    From a source checkout this is the repo root (so logs/ stays where
    it always was). A frozen build must not write next to
    the executable (often read-only once installed), so it uses the OS
    per-user application-data location instead.
    """
    if getattr(sys, "frozen", False):
        base = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.AppDataLocation
        )
        if base:
            return base
    return ROOT


def logs_dir():
    return os.path.join(app_data_dir(), "logs")


# App typography: Space Grotesk throughout, Regular and Bold (Qt draws
# weight 600 with Bold).
FONTS_DIR = resource_path("fonts")
BODY_FONT_FAMILY = "Space Grotesk"
TITLE_FONT_FAMILY = "Space Grotesk"
# Header wordmark height. The asset is 2048x568, so this keeps the mark
# about the same optical weight as the 22px text it replaced.
LOGO_HEIGHT_PX = 62


def load_app_fonts():
    """Register the font files bundled in fonts/.

    Space Grotesk ships with the repo under the SIL OFL; additional font
    files dropped into fonts/ are also registered but stay local
    (licensed files are gitignored). Fonts are registered from bytes because the path-based
    QFontDatabase.addApplicationFont silently fails on this Qt build
    (6.11/macOS). Missing families fall back to Helvetica Neue.

    Returns the list of problems found. This runs before the window and
    therefore before the event log exists, so it reports upward rather
    than printing (a windowed build has no stdout); the window logs it.
    """
    problems = []
    if os.path.isdir(FONTS_DIR):
        for name in sorted(os.listdir(FONTS_DIR)):
            if not name.lower().endswith((".ttf", ".otf")):
                continue
            path = os.path.join(FONTS_DIR, name)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                problems.append(f"could not read font {name}: {e}")
                continue
            if QtGui.QFontDatabase.addApplicationFontFromData(data) < 0:
                problems.append(f"could not register font: {name}")
    families = set(QtGui.QFontDatabase.families())
    for family in (BODY_FONT_FAMILY, TITLE_FONT_FAMILY):
        if family not in families:
            problems.append(f"font family unavailable, using fallback: {family}")
    global _FONT_PROBLEMS
    _FONT_PROBLEMS = problems
    return problems


# Set by load_app_fonts(), drained by the window once the log is open.
_FONT_PROBLEMS = []


# These colors were too dim to read in the lab and were brightened with
# this mapping. The dict is kept as the guard list: the smoke test
# asserts no widget stylesheet ever carries one of the dim colors again.
CONTRAST_REMAP = {
    "#6a6a80": "#969696",  # tiny/de-emphasized labels
    "#7a7a90": "#ababab",  # section-title class
    "#8888a0": "#ababab",  # row labels / idle statuses
    "#9a9ab0": "#b2b2b2",  # log text
    "#a0a0b8": "#c4c4c4",  # linien overlay labels
    "#b0b0c8": "#e6e6e6",  # readout values
    "#c0c0d0": "#e6e6e6",  # button/combo/edit text
}


# Qt stylesheet url() wants forward slashes even on Windows.
_SPIN_UP_ARROW = resource_path("assets", "spin-up.svg").replace(chr(92), "/")
_SPIN_DOWN_ARROW = resource_path("assets", "spin-down.svg").replace(chr(92), "/")


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
        " padding: 4px 6px;"
        " selection-background-color: #383838; }\n"
        "QAbstractSpinBox:focus { border-color: #949494; }\n"
        # Square and neutral like every other control. These kept a purple
        # tint and rounded corners: the palette sweep missed the colour,
        # and the corner check only matches a plain "border-radius".
        "QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {"
        " background: #2e2e2e; border: none; border-radius: 0px;"
        " width: 16px; }\n"
        "QAbstractSpinBox::up-button:hover,"
        " QAbstractSpinBox::down-button:hover { background: #454545; }\n"
        # Styling the buttons above turns off their native painting,
        # which left every spin box in the app with two blank grey
        # blocks and nothing to aim at. Qt will not draw the CSS
        # zero-size border triangle, so the glyphs come from files.
        f"QAbstractSpinBox::up-arrow {{ image: url({_SPIN_UP_ARROW});"
        " width: 7px; height: 4px; }\n"
        f"QAbstractSpinBox::down-arrow {{ image: url({_SPIN_DOWN_ARROW});"
        " width: 7px; height: 4px; }\n"
        "QComboBox { border: 1px solid #404040; }\n"
    )


def make_section_title(text):
    label = _base_make_section_title(text)
    label.setStyleSheet(
        label.styleSheet() + f' font-family: "{TITLE_FONT_FAMILY}";'
    )
    return label


def add_slider_row(*args, **kwargs):
    slider, edit = _base_add_slider_row(*args, **kwargs)
    # The shared helper sizes its value edit for the old monospace font;
    # the proportional body font is wider, so give values like "1000 mV"
    # room (the smoke test checks every edit fits its extreme values).
    edit.setFixedWidth(112)

    # Make the value box actually editable: type a number (suffix
    # optional), press Enter or click away, and the slider follows —
    # clamped to the slider's range.
    def commit():
        import re

        # A read-only box still emits editingFinished when it loses focus,
        # and the "normalize the text" emit below then re-sent the value:
        # clicking into the locked laser boxes wrote current and
        # temperature to the Koheron (bench, 2026-11-10: 9077 ohm over the
        # device's 9076). Its text cannot have been edited, so there is
        # nothing to commit.
        if edit.isReadOnly() or not slider.isEnabled():
            return

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
    _ARROW_STEPPER.attach(edit)
    return slider, edit


class ArrowStepper(QtCore.QObject):
    """Up/Down in a value box nudge it by one displayed decimal place.

    The value boxes are QLineEdits, so arrow keys only moved the text
    cursor: you would click into a number, press Up, and nothing would
    happen. Every slider's singleStep is already exactly one visible
    increment, so stepping the slider is the same nudge the arrow keys
    give when the slider itself has focus. PageUp/PageDown use pageStep.
    """

    def attach(self, edit):
        edit.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.KeyPress:
            return False
        slider = getattr(obj, "slider", None)
        if slider is None:
            return False
        # A locked control is a read-only box over a disabled slider, and
        # setValue still fires on a disabled slider: without this, PageUp
        # in the locked Current box wrote 1 mA to a running diode.
        if obj.isReadOnly() or not slider.isEnabled():
            return False
        steps = {
            QtCore.Qt.Key_Up: slider.singleStep(),
            QtCore.Qt.Key_Down: -slider.singleStep(),
            QtCore.Qt.Key_PageUp: slider.pageStep(),
            QtCore.Qt.Key_PageDown: -slider.pageStep(),
        }
        delta = steps.get(event.key())
        if delta is None:
            return False
        # Step from what is typed, not from the slider: an edited but
        # uncommitted box would otherwise jump back to the old value.
        obj.editingFinished.emit()
        slider.setValue(slider.value() + delta)
        return True


_ARROW_STEPPER = ArrowStepper()


class HeldKeyLimiter(QtCore.QObject):
    """Cap how fast a held Up/Down key steps a control.

    Key auto-repeat runs ~30 Hz, so holding Up on the repump swept 3 MHz/s
    while the DAC write takes ~300 ms and coalesces: the VCO jumped in
    ~1 MHz lumps the lock could slip on. On 2026-10-16 the repump moved at
    1-2.5 MHz/s and there was no MOT; the MOT week moved at 0.14-0.3 MHz/s.
    Single presses are never dropped -- only repeats inside the interval.
    """

    def __init__(self, interval_s, parent=None):
        super().__init__(parent)
        self._interval_s = interval_s
        self._last = 0.0

    def attach(self, *widgets):
        # Installed after ArrowStepper, so this filter runs first.
        for w in widgets:
            w.installEventFilter(self)

    def eventFilter(self, obj, event):
        if (
            event.type() != QtCore.QEvent.KeyPress
            or event.key() not in (
                QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
                QtCore.Qt.Key_PageUp, QtCore.Qt.Key_PageDown,
            )
        ):
            return False
        now = time.monotonic()
        if event.isAutoRepeat() and now - self._last < self._interval_s:
            return True
        self._last = now
        return False


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

# Repump VCO (Mini-Circuits ROS-3000-619+) tuning curve: the datasheet's
# typical +25 C table, V_tune (V) -> output (MHz), interpolated linearly
# between rows and clamped to 0-5 V (the DAC Click's full scale). Replaced
# a linear 0-5 V -> 2800-2970 MHz map on 2026-11-03: that was the part's
# headline range, never measured, and read ~25 MHz low near 3.7 V. Still a
# typical part, not this one: off by a few MHz, more if the VCO runs warm.
_VCO_VTUNE_V = [
    0.00, 0.50, 0.75, 1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50,
    2.75, 3.00, 3.25, 3.50, 3.75, 4.00, 4.25, 4.50, 4.75, 5.00,
]
_VCO_FREQ_MHZ = [
    2720.4, 2754.3, 2769.6, 2784.0, 2798.2, 2813.1, 2827.8, 2842.1, 2856.8,
    2871.8, 2887.5, 2903.5, 2920.5, 2937.6, 2954.3, 2970.7, 2987.0, 3003.0,
    3018.4, 3032.6,
]
VCO_FREQ_MIN_MHZ = _VCO_FREQ_MHZ[0]
VCO_FREQ_MAX_MHZ = _VCO_FREQ_MHZ[-1]

# The slider spans the DAC's whole 0-5 V, at the operator's request
# (2026-11-03); it had been narrowed to 1.47-4.41 V on 2026-10-21.
REPUMP_SLIDER_MIN_MHZ = VCO_FREQ_MIN_MHZ
REPUMP_SLIDER_MAX_MHZ = VCO_FREQ_MAX_MHZ


def vco_mhz_to_vtune(mhz: float) -> float:
    """Inverse of vtune_to_vco_mhz (the curve is monotonic)."""
    return float(np.interp(float(mhz), _VCO_FREQ_MHZ, _VCO_VTUNE_V))


def vtune_to_vco_mhz(vtune_v: float) -> float:
    """Convert a repump V_tune voltage to the VCO output frequency in MHz."""
    return float(np.interp(float(vtune_v), _VCO_VTUNE_V, _VCO_FREQ_MHZ))


def thermistor_ohms_to_c(ohms: float) -> float:
    """Steinhart-Hart for the laser head's 10 k NTC thermistor.

    Coefficients carried over from the old qMOT-os rig (same laser head).
    """
    import math

    r = max(1e-3, float(ohms))
    ln_r = math.log(r)
    return 1.0 / (0.00113 + 0.000234 * ln_r + 8.78e-8 * ln_r**3) - 273.15


def thermistor_c_to_ohms(celsius: float) -> float:
    """Inverse of thermistor_ohms_to_c.

    Steinhart-Hart has a closed form inverse, but the curve is smooth and
    strictly decreasing over the range this thermistor is used in, so a
    bisection is shorter and cannot be got subtly wrong. 40 iterations
    over 100 Ohm - 1 MOhm resolves to well under a milliohm.
    """
    target = float(celsius)
    lo, hi = 100.0, 1e6  # higher resistance = colder
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if thermistor_ohms_to_c(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0

COMBO_STYLE = """
    QComboBox {
        background: #171717;
        color: #e6e6e6;
        border: 1px solid #404040;
        border-radius: 0px;
        padding: 4px 6px;
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
        margin = (14, 12, 14, 12) if framed else (0, 0, 0, 0)
        outer.setContentsMargins(*margin)
        outer.setSpacing(8)
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

    # Camera and meter failures, for the event log: on screen alone they
    # left nothing to read back afterwards.
    fault = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera = None
        self._capture_session = None
        self._camera_devices = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._layout = layout

        self.stack = QtWidgets.QStackedWidget()
        self.message_label = QtWidgets.QLabel("No USB camera found")
        self.message_label.setAlignment(QtCore.Qt.AlignCenter)
        self.message_label.setStyleSheet(
            "color: #ababab; font-size: 13px; font-weight: 700;"
            "border: 1px solid #282828; border-radius: 0px;"
            "background: #0b0b0b; padding: 24px;"
        )
        self.stack.addWidget(self.message_label)

        # A graphics-scene video item rather than QVideoWidget: still Qt's
        # native capture path, but the MOT box can be drawn over it.
        self._roi_norm = (0.42, 0.42, 0.58, 0.58)
        self._last_meter = 0.0
        try:
            from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem

            self.video_view = VideoView()
            self.video_item = QGraphicsVideoItem()
            self.video_view.scene().addItem(self.video_item)
            self.roi_item = RoiItem(self._roi_moved)
            self.video_view.scene().addItem(self.roi_item)
            self.video_view.resized.connect(self._layout_video)
            self.video_item.nativeSizeChanged.connect(
                lambda _size: self._layout_video()
            )
            self.video_item.videoSink().videoFrameChanged.connect(self._on_frame)
            self.stack.addWidget(self.video_view)
            self._multimedia_available = True
        except Exception as e:
            self.video_view = None
            self._multimedia_available = False
            self.message_label.setText(f"Camera view unavailable: {e}")

        layout.addWidget(self.stack, stretch=1)

        # Controls sit under the image: the picture is the point, and the
        # bar reads as a caption rather than competing with the header.
        controls = QtWidgets.QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)

        label = QtWidgets.QLabel("Camera:")
        label.setStyleSheet(
            "color: #ababab; font-size: 13px; font-weight: 600; border: none;"
        )
        # Same label column width as add_slider_row, so this row sits
        # on the same grid as every other labelled control.
        label.setFixedWidth(120)
        controls.addWidget(label)

        self.camera_combo = QtWidgets.QComboBox()
        _style_combo(self.camera_combo)
        # Long device names must not widen the pane, same as PortSelect.
        self.camera_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.camera_combo.setMinimumContentsLength(12)
        self.camera_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed
        )
        self.camera_combo.currentIndexChanged.connect(self._on_camera_selected)
        controls.addWidget(self.camera_combo, stretch=1)

        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.refresh_btn.setFixedWidth(96)
        self.refresh_btn.clicked.connect(self._refresh_clicked)
        controls.addWidget(self.refresh_btn)
        layout.addLayout(controls)

        self.meter = MotMeter()
        layout.addWidget(self.meter)

        self.refresh(auto_start=False)

    def set_insets(self, left, right):
        """Indent to match the plot's data area (see _plot_content_inset)."""
        self._layout.setContentsMargins(int(left), 0, int(right), 0)

    def _refresh_clicked(self):
        """Re-enumerate, keeping the selected camera if it is still there."""
        previous = self.camera_combo.currentText()
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("...")
        try:
            self.refresh(auto_start=True, prefer=previous)
        finally:
            self.refresh_btn.setText("Refresh")
            self.refresh_btn.setEnabled(True)

    def refresh(self, auto_start=False, prefer=None):
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
            name = camera.description()
            self.camera_combo.addItem(name)
            self.camera_combo.setItemData(
                self.camera_combo.count() - 1, name, QtCore.Qt.ToolTipRole
            )
        self.camera_combo.setEnabled(True)
        # Keep the camera the user had selected, if it is still present.
        if prefer:
            index = self.camera_combo.findText(prefer)
            if index >= 0:
                self.camera_combo.setCurrentIndex(index)
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
        self.meter.clear()

    def _video_rect(self):
        """Where the picture actually sits inside the (letterboxed) item."""
        size = self.video_item.size()
        native = self.video_item.nativeSize()
        if native.isEmpty():
            return QtCore.QRectF(0, 0, size.width(), size.height())
        scale = min(size.width() / native.width(), size.height() / native.height())
        w, h = native.width() * scale, native.height() * scale
        return QtCore.QRectF((size.width() - w) / 2, (size.height() - h) / 2, w, h)

    def _layout_video(self):
        """Fit the picture to the view and put the box back where it was."""
        viewport = self.video_view.viewport().size()
        self.video_item.setSize(QtCore.QSizeF(viewport))
        self.video_view.setSceneRect(0, 0, viewport.width(), viewport.height())
        video = self._video_rect()
        x0, y0, x1, y1 = self._roi_norm
        self.roi_item.set_bounds(video)
        self.roi_item.setPos(0, 0)
        self.roi_item.setRect(QtCore.QRectF(
            video.left() + x0 * video.width(), video.top() + y0 * video.height(),
            (x1 - x0) * video.width(), (y1 - y0) * video.height(),
        ))

    def _roi_moved(self):
        video = self._video_rect()
        if video.isEmpty():
            return
        r = self.roi_item.mapRectToScene(self.roi_item.rect())
        self._roi_norm = (
            (r.left() - video.left()) / video.width(),
            (r.top() - video.top()) / video.height(),
            (r.right() - video.left()) / video.width(),
            (r.bottom() - video.top()) / video.height(),
        )

    def _on_frame(self, frame):
        now = time.monotonic()
        if now - self._last_meter < METER_INTERVAL_S:
            return
        self._last_meter = now
        try:
            image = frame.toImage()
            if image.isNull():
                return
            value = roi_brightness(qimage_to_gray(image), self._roi_norm)
        except Exception as e:
            # Keep listening: one bad frame (a camera switch, say) used to
            # disconnect the meter for the rest of the session. The next
            # good frame clears the error; each distinct failure is
            # reported once, not at 5 Hz.
            self.meter.show_error(str(e))
            if str(e) != getattr(self, "_meter_error", None):
                self._meter_error = str(e)
                self.fault.emit(f"brightness meter failed: {e}")
            return
        self._meter_error = None
        self.meter.add(value)

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
            self._capture_session.setVideoOutput(self.video_item)
            self._camera.start()
            self.stack.setCurrentWidget(self.video_view)
            self._layout_video()
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
        self.fault.emit(message)
        self.stack.setCurrentWidget(self.message_label)


class PidDialog(QtWidgets.QDialog):
    """Set the Linien lock PID gains (each 0-8191)."""

    def __init__(self, parent, current=None):
        super().__init__(parent)
        self.setWindowTitle("Lock PID")
        apply_dark_titlebar(self)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
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


class LiveOutput:
    """One thing the app can leave energised when it exits.

    ``mandatory`` marks an output that is not the operator's to leave on
    -- the dispenser at 7.5 A into a consumable is the only one.

    ``backed_by`` is how we know it is on: ``"device"`` if a readback said
    so, ``"ui"`` if the only evidence is a control the operator or a
    restored setting left in that position. Without the distinction a
    restored preference got reported as an energised output.
    """

    __slots__ = ("key", "label", "detail", "mandatory", "backed_by")

    def __init__(self, key, label, detail, mandatory=False, backed_by="ui"):
        self.key = key
        self.label = label
        self.detail = detail
        self.mandatory = mandatory
        self.backed_by = backed_by


class ShutdownDialog(QtWidgets.QDialog):
    """Per-output checklist shown when closing with something energised.

    Deliberately not a blanket kill: leaving the laser locked across an
    app restart is a legitimate thing to want, and a dialog that only
    offers "everything off" gets dismissed without being read. Every item
    defaults to being turned off, so the safe path is also the default
    path.
    """

    def __init__(self, parent, outputs):
        super().__init__(parent)
        self.setWindowTitle("Close OpenQuantum Controls")
        apply_dark_titlebar(self)
        self._checks = {}

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        heading = QtWidgets.QLabel(
            f"{len(outputs)} output{'s' if len(outputs) != 1 else ''} "
            "still on. Uncheck anything to leave running:"
        )
        heading.setWordWrap(True)
        heading.setStyleSheet(
            f"color: {ACCENT}; font-size: {UI_FONT_PX}px; border: none;"
        )
        root.addWidget(heading)

        for output in outputs:
            check = QtWidgets.QCheckBox(f"{output.label} — {output.detail}")
            check.setChecked(True)
            if output.mandatory:
                # Not a choice: firing draws 7.5 A through a consumable
                # and nothing would be left running to stop it.
                check.setEnabled(False)
                check.setText(f"{check.text()} — always off")
            root.addWidget(check)
            self._checks[output.key] = check

        note = QtWidgets.QLabel(
            "Anything left on runs unmonitored after the app exits."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: #969696; border: none; font-size: {UI_FONT_PX}px;"
        )
        root.addWidget(note)

        # No explicit cancel button: the checkboxes already decide what
        # survives, so a second "don't close" control read as a third
        # answer to the same question. Esc and the title-bar close still
        # reject the dialog, so backing out of an accidental close is
        # still possible.
        button_row = QtWidgets.QHBoxLayout()
        button_row.addStretch()
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

    def turn_off(self):
        """Keys the operator wants driven safe."""
        return {key for key, check in self._checks.items() if check.isChecked()}


class OpenQuantumControlsWindow(QtWidgets.QMainWindow):
    # Worker threads log directly (a fault that must reach the GUI thread
    # to be recorded is lost when the GUI is what broke), so records reach
    # the log pane through a signal rather than by touching the widget.
    log_record = QtCore.pyqtSignal(object)

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
        self._dispenser_fire_current = self._float_setting(
            "dispenser/fire_current_a", DispenserWorker.DISPENSE_CURRENT_A
        )
        # Clamped so the button label and the worker agree on a stored
        # value outside the slider's range.
        self._dispenser_fire_seconds = max(
            DispenserWorker.MIN_FIRE_SECONDS,
            min(
                self._float_setting(
                    "dispenser/fire_seconds", DispenserWorker.FIRE_SECONDS
                ),
                DispenserWorker.MAX_FIRE_SECONDS,
            ),
        )
        self._dispenser_fire_voltage = self._float_setting(
            "dispenser/fire_voltage_v", DispenserWorker.DISPENSE_VOLTAGE_V
        )
        self._dispenser_phase = "idle"
        self._vtune_slider_synced = False
        self._psd_window = None
        self._psd_prev_status = {}
        self._filters_syncing = False
        # Set once the operator has adjusted a filter control, so the
        # connect-time readback shows the divergence instead of stomping it.
        self._filters_touched = False

        self._chip_state = {}
        self._device_connected = {}
        # key -> (desired_state, deadline). Present only while a write is
        # outstanding; see _sync_output_button.
        self._pending_output = {}
        self._server_stalled = False
        self._server_restarting = False
        self._rp_last_status = "disconnected"
        # Outputs whose "is it on" has come from a device rather than from a
        # restored setting. live_outputs() reports it as backed_by, and the
        # startup report will not name an output that is not in here.
        self._readback_seen = set()
        # Dither amplitude in mV as last reported by linien, None until a
        # connect answers. Not the slider: that is what the operator wants.
        self._mod_actual_mv = None
        # Set once the operator touches the modulation controls, so a
        # connect-time readback cannot overwrite them.
        self._mod_touched = False
        self._startup_report_retried = 0
        # device key -> True once its serial port stopped being enumerated.
        self._port_missing = {}
        self._event_log = EventLog(logs_dir())
        # Installed before anything imports linien: the sweep itself has to
        # run after each import (see capture_library_logging), but the
        # handler has to exist first so those sweeps have somewhere to go.
        capture_library_logging(self._event_log)
        self._readback_log_output = {}
        self._readback_log_protection = {}
        self._log_debounce = {}
        self._push_debounce = {}
        self._last_logged_vtune = None
        self._event_log.install_excepthook()
        self._event_log.session_start(
            rp_host=rp_host,
            laser_port=laser_port,
            mot_coil_port=mot_coil_port,
            dispenser_port=dispenser_port,
            mot_coil_address=mot_coil_address,
            dispenser_address=dispenser_address,
        )
        for problem in _FONT_PROBLEMS:
            self._event_log.fault("font", message=problem)
        self._capture_qt_messages()

        self.setWindowTitle("OpenQuantum Controls")
        self.resize(1540, 960)
        apply_dark_titlebar(self)

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

        # Route event-log records into the log pane. Via the signal, so a
        # record written on a worker thread is queued onto the GUI thread
        # instead of touching log_view from the wrong one.
        self.log_record.connect(self._on_log_record)
        self._event_log.on_record = self.log_record.emit

        # Pull the filter settings from the device once Linien connects.
        self.linien_widget.connection_status_changed.connect(
            self._on_linien_connection
        )
        self.linien_widget.fault.connect(
            lambda msg: self._log_fault("linien", msg)
        )
        self.linien_widget.server_stalled.connect(self._on_server_stalled)
        self.linien_widget.modulation_state.connect(
            self._on_modulation_readback
        )
        self.linien_widget.sweep_state.connect(self._on_sweep_readback)
        # Through the slider, so the box, QSettings and the log all follow;
        # the write it triggers repeats the value the worker already set.
        self.linien_widget.phase_applied.connect(self.mod_phase_slider.setValue)
        self.linien_widget.action_running.connect(self._hold_for_lock_action)

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
        # The camera is an occasional check, not a default view, so it
        # always starts off rather than restoring its last state. The
        # Devices card is the same: it used to reopen however it was left,
        # which made it the one card whose state looked random next to
        # every other card starting collapsed.

    def _save_ui_state(self):
        self._settings.setValue("ui/geometry", self.saveGeometry())
        self._settings.setValue("ui/splitter_sizes", self.main_splitter.sizes())

    # Fields that are plumbing rather than content: they either name the
    # line already (event), belong only to the file (traceback), or are
    # rendered into the source column.
    # "message"/"error" lead the line, so they must not also appear in the
    # field list; the rest either name the line already or belong only to
    # the file.
    _LOG_PANE_SKIP = (
        "t", "kind", "event", "traceback", "logger", "level",
        "message", "error",
    )
    # A minimum, not a maximum: event names run to "startup_outputs_live",
    # and cutting one to fit a column would lose the only word that says
    # what the line is.
    _LOG_SOURCE_WIDTH = 14
    _LOG_VALUE_CHARS = 60
    _LOG_LINE_CHARS = 150
    _LOG_COLOURS = {
        "fault": "#b56a6a",
        "warn": "#b9924f",
        "quiet": "#6b6b6b",
        "plain": "#b2b2b2",
    }
    # linien_client.connection -> linien; paramiko.transport -> paramiko.
    _LOG_LIBRARY_NAMES = {
        "linien_client": "linien",
        "linien_common": "linien",
        "rpyc": "rpyc",
        "paramiko": "ssh",
        "fabric": "ssh",
        "invoke": "ssh",
    }

    def _log_line(self, record):
        """Render one event-log record as (source, text, colour).

        The pane used to join every field as ``k=v`` in dict order and cut
        the result at 300 characters, so the part worth reading was last
        and usually the part that got cut -- and a fault looked exactly
        like a readback.
        """
        event = record.get("event", "?")
        message = record.get("message") or record.get("error") or ""

        if event == "library":
            level = str(record.get("level", "INFO"))
            root = str(record.get("logger", "")).split(".")[0]
            colour = (
                self._LOG_COLOURS["fault"] if level in ("ERROR", "CRITICAL")
                else self._LOG_COLOURS["warn"] if level == "WARNING"
                else self._LOG_COLOURS["quiet"]
            )
            return self._LOG_LIBRARY_NAMES.get(root, root), message, colour

        if event == "fault_repeated":
            text = (
                f"{message}  \N{MULTIPLICATION SIGN}{record.get('count', '?')}"
                f" over {record.get('over_s', '?')} s"
            )
            return (
                record.get("device") or record.get("of", event),
                text,
                self._LOG_COLOURS["warn"],
            )

        # The device takes the source column only when the event name says
        # nothing: "coils  WriteFile failed" beats "fault  device=coils",
        # but "port_vanished" is the whole point of its own record and
        # must not be replaced by the device it happened to.
        is_fault = record.get("kind") == "fault"
        device = record.get("device")
        named_by_device = bool(device) and event in ("fault", "connection")
        source = device if named_by_device else event
        skip = self._LOG_PANE_SKIP + (("device",) if named_by_device else ())
        fields = [
            f"{key}={self._short(value)}"
            for key, value in sorted(record.items())
            if key not in skip
        ]
        text = " ".join(filter(None, [self._short(message), *fields]))
        if len(text) > self._LOG_LINE_CHARS:
            # Per-field trimming is not enough on its own: a record with
            # several long fields (usb_probe carries three dicts) still
            # runs off the pane.
            text = text[: self._LOG_LINE_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"
        return (
            source,
            text,
            self._LOG_COLOURS["fault"] if is_fault else self._LOG_COLOURS["plain"],
        )

    def _short(self, value):
        """One field, trimmed on its own so the line never ends mid-word."""
        text = str(value)
        if len(text) <= self._LOG_VALUE_CHARS:
            return text
        return text[: self._LOG_VALUE_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"

    def _on_log_record(self, record):
        """Mirror an event-log record into the collapsible log pane."""
        if not hasattr(self, "log_view"):
            return
        # Only the end of a transaction goes in the pane: the start half
        # carries no outcome, and pairing them here would halve what fits
        # on screen. Both stay in the file, where the "op" id is what
        # correlates them.
        if record.get("phase") == "start":
            return
        # linien's DEBUG is per-step detail (credentials sent, device file
        # read). It is worth having in the file and not worth a pane line.
        if record.get("event") == "library" and record.get("level") == "DEBUG":
            return
        timestamp = record.get("t", "")[11:19]
        source, text, colour = self._log_line(record)
        line = f"{timestamp}  {str(source):<{self._LOG_SOURCE_WIDTH}}  {text}"
        # Rich text, so severity can carry colour; the padding above only
        # survives with white-space:pre, and &nbsp; is what makes Qt keep
        # the runs of spaces inside it.
        self.log_view.appendHtml(
            f'<span style="color:{colour}; white-space:pre">'
            f'{html.escape(line).replace(" ", "&nbsp;")}</span>'
        )
        # The card header rebuilds on every set_status, so routine device
        # traffic must not drive it -- during a slider drag that is the
        # GUI doing layout work several times a second for a chip nobody
        # is reading. Faults and state changes still surface there.
        # "library" is third-party logging (linien, RPyC, SSH) captured by
        # capture_library_logging: routine traffic, same as a readback.
        if (
            record.get("kind") in ("command", "readback")
            or record.get("event") == "library"
        ):
            return
        summary = f"{timestamp} {record.get('event', '?')}"
        if len(summary) > 28:
            summary = summary[:27] + "\N{HORIZONTAL ELLIPSIS}"
        self.log_card.set_status(summary)

    # ------------------------------------------------------------------
    # Event logging helpers
    # ------------------------------------------------------------------

    def _log_connection(self, device, status):
        self._event_log.log("connection", device=device, status=status)
        if str(status).startswith("error"):
            # Surface it as a fault too: an error buried among routine
            # connect/disconnect events is easy to miss, and for devices
            # with no status row the log is the only place it shows.
            self._log_fault(device, str(status).split(":", 1)[-1].strip())

    def _capture_qt_messages(self):
        """Send Qt's warnings to the event log instead of the console.

        quiet_console() lowers what Qt prints; this makes that lossless.
        Warnings and worse are recorded -- a Qt warning about a failed
        camera or a bad stylesheet is a real diagnostic and should be in
        the file with everything else, not in scrollback.
        """
        levels = {
            QtCore.QtMsgType.QtWarningMsg: "warning",
            QtCore.QtMsgType.QtCriticalMsg: "critical",
            QtCore.QtMsgType.QtFatalMsg: "fatal",
        }
        log = self._event_log

        def handler(mode, context, message):
            level = levels.get(mode)
            if level is None:
                return  # debug/info: not worth a record
            log.fault(
                "qt", level=level, message=message,
                category=getattr(context, "category", None),
            )

        QtCore.qInstallMessageHandler(handler)

    def _log_fault(self, device, message):
        self._event_log.fault("fault", device=device, message=str(message))

    def _debounced_push(self, key, push_fn, delay_ms=250):
        """Coalesce a burst of control changes into one hardware write.

        A slider drag emits valueChanged per step. Each one used to reach
        the device, and since the worker now shares one RPyC connection
        between writes and acquisition, that made the plot stutter for as
        long as the drag lasted. The UI text and the saved setting still
        update per step -- only the device write waits for the value to
        settle.
        """
        timer = self._push_debounce.get(key)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(push_fn)
            self._push_debounce[key] = timer
        timer.start(delay_ms)

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
        """Supply readback logging, on change only.

        There used to be a 10 s heartbeat as well. At 0.0 A into a supply
        nobody is touching it wrote the same line all afternoon, which
        buries the records that mean something. The first readback of a
        session still lands, because the remembered output is None and
        ``None != False`` -- that is what keeps the state-at-connect
        evidence.
        """
        output = bool(state.get("output_enabled"))
        protection = int(state.get("protection", 0) or 0)
        output_changed = self._readback_log_output.get(channel) != output
        protection_changed = (
            self._readback_log_protection.get(channel) != protection
        )
        if not (output_changed or protection_changed):
            return
        self._readback_log_output[channel] = output
        self._readback_log_protection[channel] = protection
        self._event_log.readback(
            "dps",
            channel=channel,
            v_out=state.get("v_out"),
            i_out=state.get("i_out"),
            power=state.get("power"),
            output=output,
            cv_cc=int(state.get("cv_cc", 0) or 0),
            protection=protection,
        )
        if protection_changed and protection:
            # A supply in protection has shut its own output off; that is
            # a fault, not a routine state change.
            self._event_log.fault(
                "protection_trip",
                channel=channel,
                protection=DPS_PROTECTION.get(protection, str(protection)),
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
        # The lock chip's text is display only, and other messages share
        # it (startup report, stall): shutdown read "LOCKED" off it and
        # left a lock engaged unasked once one of those had overwritten it.
        # This is set by the server's lock flag and nothing else.
        self._lock_engaged = False
        self._set_chip("lock", "LOCK --", self._CHIP_NEUTRAL)
        self._set_chip("usb", "DEVICES --", self._CHIP_NEUTRAL)
        self._set_chip("coils", "COILS OFF", self._CHIP_NEUTRAL)
        self._set_chip("disp", "DISPENSER IDLE", self._CHIP_NEUTRAL)
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
        # Shared with the Red Pitaya banner in LinienWidget, so the two line
        # up against the same edge.
        sweep_row.setContentsMargins(*PLOT_COLUMN_MARGINS)
        sweep_row.setSpacing(8)
        sweep_row.addStretch()
        # Held so the Linien widget's sweep-center box can be inserted here
        # once that widget exists, further down.
        self._sweep_row = sweep_row
        sweep_row.addWidget(self._small_label("Sweep Speed:"))
        self.sweep_speed_combo = QtWidgets.QComboBox()
        _style_combo(self.sweep_speed_combo)
        self.sweep_speed_combo.setFixedWidth(110)
        for value, label in [
            (0, "3800 Hz"), (1, "1900 Hz"), (2, "950 Hz"), (3, "475 Hz"),
            (4, "237.5 Hz"), (5, "118.75 Hz"), (6, "59.4 Hz"), (7, "29.7 Hz"),
            (8, "14.8 Hz"), (9, "7.4 Hz"), (10, "3.7 Hz"), (11, "1.86 Hz"),
            (12, "0.93 Hz"), (13, "0.46 Hz"), (14, "0.23 Hz"), (15, "0.116 Hz"),
            # The limit of slowing down, so it sits at the slow end of the
            # list. None rather than a speed: this is linien's sweep_pause
            # parameter, not a sixteenth entry in its speed table.
            (None, "0 Hz (hold)"),
        ]:
            self.sweep_speed_combo.addItem(label, value)
        self.sweep_speed_combo.setCurrentIndex(
            self._int_setting("linien/sweep_speed_index", 8, 0, 16)
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
        # No v_to_freq_ghz: the x axis stays in sweep volts, which is what
        # the sweep slider and the lock region are actually set in.
        self.linien_widget = LinienWidget(
            linien_host=self._rp_host, event_log=self._event_log
        )
        # Which lock buttons are usable is decided in one place,
        # _refresh_lock_buttons, once the cards it reads exist.
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
        # Also the widget's, placed beside the speed it belongs with.
        # Index 1 is after the row's leading stretch and before the
        # "Sweep Speed:" label.
        # Each label hugs its own control, with a wider gap between the two
        # pairs -- at a uniform 8 px the row read as four loose items.
        # A spin box asks for 3 px more height than a combo (two stacked
        # buttons instead of one drop-down), which read as two mismatched
        # boxes on the same row. Taken from the combo rather than hardcoded
        # so it survives a font change.
        self.linien_widget.sweep_center_spin.setFixedHeight(
            self.sweep_speed_combo.sizeHint().height()
        )
        self._sweep_row.insertSpacing(1, 18)
        self._sweep_row.insertWidget(1, self.linien_widget.sweep_center_spin)
        self._sweep_row.insertWidget(1, self._small_label("Sweep Center:"))
        self._tip(
            "Center of the sweep in output volts, to 3 decimals.\n"
            "Arrow keys step 0.001 V. The span is unchanged,\n"
            "so the window slides rather than rescaling.\n"
            "At the full -1 to +1 V span there is no room to slide,\n"
            "so the center stays at 0: narrow the range slider first.",
            self.linien_widget.sweep_center_spin,
        )


        self.camera_widget = CameraViewWidget()
        self.camera_widget.fault.connect(
            lambda msg: self._log_fault("camera", msg)
        )
        self.camera_widget.setVisible(False)
        self.main_splitter.addWidget(self.linien_widget)
        self.main_splitter.addWidget(self.camera_widget)
        self.main_splitter.setSizes([700, 260])
        normal_layout.addWidget(self.main_splitter, stretch=1)

        left.addWidget(normal_view, stretch=1)

        root.addLayout(left, stretch=3)

    def _toggle_camera_view(self, visible):
        self.camera_widget.setVisible(visible)
        if visible:
            # Equal split: the camera is as much the experiment as the
            # spectrum is, so neither gets the larger half by default.
            half = max(1, self.main_splitter.height() // 2)
            self.main_splitter.setSizes([half, half])
            self.camera_widget.set_insets(*self._plot_content_inset())
            self.camera_widget.activate()
        else:
            self.camera_widget.deactivate()

    def _plot_content_inset(self):
        """(left, right) insets of the spectroscopy plot's data area.

        The y-axis pushes the plot content right by however wide its tick
        labels currently are; the camera pane below has to match on both
        edges or the two look shifted against each other.
        """
        try:
            widget = self.linien_widget.pg_widget
            box = self.linien_widget.view_box.sceneBoundingRect()
            return int(box.left()), int(widget.width() - box.right())
        except Exception:
            return 0, 0

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
        self._add_config_log_card(layout)
        for card_attr in set(self._CHIP_CARDS.values()):
            self._refresh_card_status(card_attr)
        self._refresh_lock_buttons()
        layout.addStretch()

        # The sidebar swaps between the normal control cards and the PSD
        # measurement panel. Wide enough that no card ever needs
        # horizontal scrolling; the smoke test asserts the content fits
        # at the minimum width.
        scroll.setMinimumWidth(460)
        scroll.setMaximumWidth(600)
        root.addWidget(scroll, stretch=1)

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

        # Reconnecting does not help a wedged server: it answers RPyC fine,
        # its acquisition and heartbeat threads are just dead. Only a
        # restart of the server process brings it back.
        self.restart_server_btn = QtWidgets.QPushButton("Restart Linien Server")
        self.restart_server_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.restart_server_btn.clicked.connect(self._restart_linien_server)
        self._tip(
            "Restart linien-server on the Red Pitaya over SSH. Use this when\n"
            "the spectrum has stopped and the status says Stalled — a\n"
            "reconnect will not fix that. Drops the lock.",
            self.restart_server_btn,
        )
        manual_layout.addWidget(self.restart_server_btn)

        self.auto_discover_btn = QtWidgets.QPushButton("Auto-connect")
        self.auto_discover_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.auto_discover_btn.clicked.connect(self._auto_connect)
        self._tip(
            "Connect everything that is not connected: the Red Pitaya, and\n"
            "the USB devices via a read-only probe that identifies each one\n"
            "and reassigns ports (briefly disconnects the serial workers).\n"
            "A Red Pitaya that is already connected is left alone, so this\n"
            "never drops a lock.",
            self.auto_discover_btn,
        )
        layout.addWidget(self.auto_discover_btn)
        layout.addWidget(manual)
        self._refresh_discovery()

        parent.addWidget(card)

    def _add_port_select_row(self, layout, label_text, value):
        """Labelled port dropdown. Options are filled by _refresh_discovery."""
        row = QtWidgets.QHBoxLayout()
        # Same rhythm as add_slider_row / add_readout_row.
        row.setContentsMargins(0, 3, 0, 3)
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
        # Same rhythm as add_slider_row / add_readout_row.
        row.setContentsMargins(0, 3, 0, 3)
        row.setSpacing(8)
        row.addWidget(self._small_label(label_text))
        value = QtWidgets.QLabel(edit.text())
        value.setStyleSheet(
            "color: #e6e6e6; font-size: 13px; border: none;"
        )
        value.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        edit.textChanged.connect(value.setText)
        row.addWidget(value, stretch=1)
        # "..." rather than a fourth word for "nothing has happened yet":
        # the workers start moments later, so an attempt really is pending.
        status = QtWidgets.QLabel("...")
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
            row.setContentsMargins(0, 3, 0, 3)
            row.setSpacing(8)
            enabled = QtWidgets.QCheckBox(f"Filter {index}")
            enabled.setCursor(QtCore.Qt.PointingHandCursor)
            enabled.setFixedWidth(120)
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


    def _add_config_log_card(self, parent):
        """Config snapshots and the event log: both are about the session
        as a whole rather than any one device, so they share a card."""
        card = CollapsibleCard("Config & Log", collapsed=True)
        self.log_card = card
        layout = card.content_layout

        layout.addWidget(make_section_title("Config"))
        config_row = QtWidgets.QHBoxLayout()
        config_row.setSpacing(8)
        self.save_config_btn = QtWidgets.QPushButton("Save Config As...")
        self.save_config_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.save_config_btn.clicked.connect(self._save_config_dialog)
        config_row.addWidget(self.save_config_btn)
        self._tip(
            "Snapshot every setpoint (connections, filters, laser, repump,\n"
            "supplies, dispenser, PSD) to a JSON file. Setpoints only: a\n"
            "config never records whether an output is on.",
            self.save_config_btn,
        )
        self.load_config_btn = QtWidgets.QPushButton("Load Config...")
        self.load_config_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.load_config_btn.clicked.connect(self._load_config_dialog)
        config_row.addWidget(self.load_config_btn)
        self._tip(
            "Apply a saved config: setpoints go to the hardware where\n"
            "connected. It never switches an output on or off — the laser,\n"
            "coils, repump, modulation and dispenser stay as they are.\n"
            "Dispenser settings ask for confirmation; port changes take\n"
            "effect on Connect.",
            self.load_config_btn,
        )
        layout.addLayout(config_row)

        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setStyleSheet("color: #282828;")
        layout.addWidget(separator)
        layout.addWidget(make_section_title("Event Log"))

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        # One record, one line. Wrapping spent three of the seven visible
        # lines on a single usb_probe record and broke the column
        # alignment; the horizontal scrollbar is the cheaper cost.
        self.log_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
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
        layout.addWidget(self.log_view)
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
            "Turn the laser current modulation on or off. Off drives the amplitude\n"
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
            "Amplitude of the laser current modulation, 0-1 Vp-p.",
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

        # Restored before the signal is connected, so bringing the UI back
        # cannot actuate: connecting first made setChecked() fire
        # _on_mod_enabled, which writes the dither amplitude to the laser.
        # Restoring persisted state sets the UI only, never hardware, and the
        # default is off -- starting a session by dithering the laser is
        # not something to do without being asked.
        self.mod_enable_btn.setChecked(
            self._bool_setting("linien/mod_enabled", False)
        )
        self.mod_enable_btn.toggled.connect(self._on_mod_enabled)
        self._apply_mod_enabled(self.mod_enable_btn.isChecked())

        self._add_filter_section(layout)

        # Everything above configures the error signal; everything below
        # locks to it. The heading is what makes "Select target line" read
        # as the first step rather than another setting.
        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setStyleSheet("color: #282828;")
        layout.addWidget(separator)
        layout.addWidget(make_section_title("Lock"))

        # No lock readout row: the card header carries LOCKED/UNLOCKED.

        # Lock/Unlock are built by the Linien widget (they carry its lock
        # wiring) but belong here, not floating over the traces.
        # linien's autoOffsetCheckbox, with its tooltip.
        self.auto_offset_check = QtWidgets.QCheckBox(
            "Determine signal offset"
        )
        self.auto_offset_check.setCursor(QtCore.Qt.PointingHandCursor)
        self.auto_offset_check.setChecked(
            self._bool_setting("linien/determine_offset", True)
        )
        self.auto_offset_check.toggled.connect(self._on_auto_offset)
        layout.addWidget(self.auto_offset_check)
        self._tip(
            "Adapt offset such that the lock is in the middle between\n"
            "minimum and maximum. Note: this setting may reduce\n"
            "reproducibility of absolute frequency.",
            self.auto_offset_check,
        )

        # Which feature the operator locks on decides whether tuning the
        # repump moves the carrier at all; keep it on record.
        self.linien_widget.lock_requested.connect(
            lambda sel: self._event_log.log("lock_selection", selection=list(sel))
        )

        layout.addWidget(self.linien_widget.lock_controls)
        self._tip(
            "Select target line, then click and drag over the line you\n"
            "want to lock to.",
            self.linien_widget.lock_controls,
        )

        # linien's auto_mode_activated instruction: only on screen while a
        # selection is actually running, so it reads as a prompt.
        self.lock_hint_label = QtWidgets.QLabel(
            "Click and drag over the line you want to lock to."
        )
        self.lock_hint_label.setWordWrap(True)
        self.lock_hint_label.setStyleSheet(
            "color: #b9924f; font-size: 13px; font-weight: 600;"
            "border: none; padding: 2px 4px;"
        )
        self.lock_hint_label.setVisible(False)
        layout.addWidget(self.lock_hint_label)
        self.linien_widget.selection_state_changed.connect(
            self.lock_hint_label.setVisible
        )

        lock_button_row = QtWidgets.QHBoxLayout()
        lock_button_row.setSpacing(8)
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
        self.psd_mode_btn.clicked.connect(self._open_psd_window)
        lock_button_row.addWidget(self.psd_mode_btn)
        self._tip(
            "Switch to the lock-noise PSD view: measure the error-signal\n"
            "noise spectrum under lock and optimize the PID from it.",
            self.psd_mode_btn,
        )
        layout.addLayout(lock_button_row)
        parent.addWidget(card)

    def _apply_mod_enabled(self, on):
        """Grey out the modulation controls when the modulation is off."""
        self.mod_enable_btn.setText(
            "Modulation On" if on else "Modulation Off"
        )
        self._show_lock_chip()  # the unlocked chip names the modulation
        self._refresh_lock_buttons()
        self._schedule_laser_off_rule()
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

    def _on_auto_offset(self, enabled):
        self._settings.setValue("linien/determine_offset", bool(enabled))
        self._event_log.log(
            "setpoint", name="autolock_determine_offset", value=bool(enabled)
        )
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_determine_offset(enabled)

    def _on_mod_enabled(self, on):
        self._apply_mod_enabled(on)
        self._settings.setValue("linien/mod_enabled", bool(on))
        self._event_log.log("setpoint", name="mod_enabled", value=bool(on))
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_modulation_amplitude(
                self.mod_strength_slider.value() if on else 0
            )
        self._note_mod_touched(pushed=self._lock_worker_connected(worker))

    def _note_mod_touched(self, pushed):
        """Record that the operator set the dither, or that we reconciled.

        ``pushed`` means the value reached a worker, so desired and actual
        agree and the flag can go. Without that half this would be a
        one-way latch: one slider touch and every later reconnect reported
        a divergence instead of adopting the device.
        """
        self._mod_touched = not pushed

    def _on_modulation_readback(self, mv):
        """What the dither is actually doing, reported once per connect.

        The button used to be a saved preference displayed as device state:
        nothing read modulation_amplitude back, so a launch with the Red
        Pitaya unplugged still claimed the dither was running. This is the
        readback that makes it a fact -- the same direction the laser
        sliders take, where connecting adopts the device's own setpoints.
        """
        try:
            actual_mv = float(mv)
        except (TypeError, ValueError):
            return
        self._mod_actual_mv = actual_mv
        on = actual_mv > 0
        ui_mv = self.mod_strength_slider.value()

        if self._mod_touched:
            # Show divergence, do not paper over it: the operator's value
            # stands and the disagreement goes in the record.
            self._event_log.fault(
                "mod_divergence",
                device_mv=round(actual_mv, 3),
                ui_mv=ui_mv,
                ui_enabled=self.mod_enable_btn.isChecked(),
            )
            # Deliberately not device-backed: the device answered, but what
            # is on screen is no longer what it said, so nothing may report
            # the displayed dither as the Red Pitaya's state.
            self._readback_seen.discard("modulation")
            return
        self._readback_seen.add("modulation")

        # Signals blocked and the setting written by hand: routing this
        # through _on_mod_enabled would push the value we just read
        # straight back at the device, which is an actuation nobody asked
        # for (restoring state sets the UI only, never hardware).
        self.mod_enable_btn.blockSignals(True)
        self.mod_enable_btn.setChecked(on)
        self.mod_enable_btn.blockSignals(False)
        self._settings.setValue("linien/mod_enabled", on)
        if on:
            # Clamped for the slider, logged raw: a device set past the
            # slider's range is real and worth seeing, but widening the
            # range is not this change's business.
            self.mod_strength_slider.blockSignals(True)
            self.mod_strength_slider.setValue(
                max(0, min(1000, int(round(actual_mv))))
            )
            self.mod_strength_slider.blockSignals(False)
            self.mod_strength_edit.setText(
                f"{self.mod_strength_slider.value() / 1000.0:.3f} Vpp"
            )
            self._settings.setValue(
                "linien/mod_strength_mv", self.mod_strength_slider.value()
            )
        # Off leaves the slider alone -- it is the level to restore, not the
        # level being driven.
        self._apply_mod_enabled(on)
        self._event_log.log(
            "mod_adopted", device_mv=round(actual_mv, 3), was_ui_mv=ui_mv,
            enabled=on,
        )

    def _add_laser_card(self, parent):
        card = CollapsibleCard("Laser", collapsed=True)
        self.laser_card = card
        layout = card.content_layout

        # No Koheron connection row: the Devices card already carries it.
        # Connection changes and faults go to the event log instead.

        # The laser runs at a fixed operating point. Both setpoints start
        # locked every session so neither can be moved by a stray drag;
        # unlocking is deliberate and is never persisted.
        lock_row = QtWidgets.QHBoxLayout()
        lock_row.setSpacing(8)
        self.laser_unlock_box = QtWidgets.QCheckBox("Unlock setpoints")
        self.laser_unlock_box.setCursor(QtCore.Qt.PointingHandCursor)
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

        # Slider counts 0.01 mA, the same arrow-key step as Temperature;
        # laser/current_ma stays in mA so configs and settings written by
        # either build still read correctly.
        self.current_slider, self.current_edit = add_slider_row(
            layout,
            "Current:",
            5000,
            10000,
            int(round(self._float_setting("laser/current_ma", 80.0) * 100)),
            single=1,
            page=100,
            suffix=" mA",
            decimals=2,
            scale=100.0,
        )
        self.current_slider.valueChanged.connect(self._on_current)
        self._tip(
            "Laser diode current setpoint (mA). Type a value or drag.",
            self.current_slider,
            self.current_edit,
        )
        # Temperature, not resistance: the thermistor value is an
        # implementation detail of how the Koheron holds the setpoint.
        # Stored and sent in ohms; the slider is in 0.01 C steps over the
        # 16-38 C the cell is ever operated across. 0.01 C is ~4 ohms on
        # the 10 k NTC near 27 C, so integer ohms still resolves a step.
        self.therm_slider, self.therm_edit = add_slider_row(
            layout,
            "Temperature:",
            1600,
            3800,
            int(round(
                thermistor_ohms_to_c(
                    self._int_setting("laser/thermistor_ohm", 10000, 5000, 15000)
                ) * 100
            )),
            single=1,
            page=100,
            suffix=" °C",
            decimals=2,
            scale=100.0,
        )
        self.therm_slider.valueChanged.connect(self._on_thermistor)
        self._tip(
            "TEC temperature setpoint. Converted to a thermistor\n"
            "resistance with the 10 k NTC Steinhart-Hart curve before it\n"
            "is sent to the Koheron.",
            self.therm_slider,
            self.therm_edit,
        )
        self._apply_laser_lock(False)

        # The action goes under the settings it acts on, as in Coils and
        # Repump.
        self.laser_btn = QtWidgets.QPushButton("Laser Off")
        self.laser_btn.setCheckable(True)
        self.laser_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.laser_btn.toggled.connect(self._toggle_laser)
        layout.addWidget(self.laser_btn)
        self._tip("Toggle the laser diode output (lason).", self.laser_btn)

        parent.addWidget(card)

    def _add_dps_card(self, parent):
        """Coils and dispenser: two cards, one per supply."""
        card = CollapsibleCard("Coils", collapsed=True)
        self.coils_card = card
        layout = card.content_layout
        self.coil_readback_label = self._supply_readback_label(layout)

        self.mot_coil_voltage_slider, self.mot_coil_voltage_edit = add_slider_row(
            layout,
            "Voltage:",
            0,
            50000,
            self._int_setting("coil/voltage_mv", 5000, 0, 50000),
            # Stored in mV, shown to 0.1 V, so the step matches the display.
            single=100,
            page=1000,
            suffix=" V",
            decimals=1,
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
            single=100,
            page=1000,
            suffix=" A",
            decimals=1,
            scale=1000.0,
        )
        self.mot_coil_current_slider.valueChanged.connect(self._on_mot_coil_current)
        self._tip(
            "Coil current setpoint (A) — sets the MOT magnetic field\n"
            "gradient. Type a value or drag.",
            self.mot_coil_current_slider,
            self.mot_coil_current_edit,
        )

        # The action goes under the settings it acts on, as in the Laser
        # and Repump cards.
        self.mot_coil_output_btn = QtWidgets.QPushButton("Coils Off")
        self.mot_coil_output_btn.setCheckable(True)
        self.mot_coil_output_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.mot_coil_output_btn.toggled.connect(self._toggle_mot_coils)
        layout.addWidget(self.mot_coil_output_btn)
        self._tip("Toggle the MOT coil supply output.", self.mot_coil_output_btn)

        parent.addWidget(card)

        card = CollapsibleCard("Dispenser", collapsed=True)
        self.dispenser_card = card
        layout = card.content_layout
        self.dispenser_readback_label = self._supply_readback_label(layout)

        self.dispenser_voltage_slider, self.dispenser_voltage_edit = (
            add_slider_row(
                layout,
                "Voltage:",
                0,
                50000,
                int(round(self._dispenser_fire_voltage * 1000)),
                single=100,
                page=1000,
                suffix=" V",
                decimals=1,
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
                single=100,
                page=1000,
                suffix=" A",
                decimals=1,
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
            int(DispenserWorker.MAX_FIRE_SECONDS),
            int(round(self._dispenser_fire_seconds)),
            single=1,
            page=5,
            suffix=" s",
        )
        self.dispenser_time_slider.valueChanged.connect(
            self._on_dispenser_time
        )
        self._tip(
            "How long a fire runs before the supply is forced off.",
            self.dispenser_time_slider,
            self.dispenser_time_edit,
        )

        fire_row = QtWidgets.QHBoxLayout()
        fire_row.setSpacing(8)
        self.dispenser_fire_btn = QtWidgets.QPushButton(
            f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
        )
        self.dispenser_fire_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.dispenser_fire_btn.clicked.connect(self._fire_dispenser_clicked)
        fire_row.addWidget(self.dispenser_fire_btn, stretch=1)
        self._tip(
            "Run the dispenser at the fixed operating point, then auto-off.\n"
            "Click again mid-fire to abort.",
            self.dispenser_fire_btn,
        )
        layout.addLayout(fire_row)

        # Time-remaining bar: drains during a fire (orange); hidden
        # otherwise.
        self.dispenser_progress = QtWidgets.QProgressBar()
        self.dispenser_progress.setRange(0, 1000)
        self.dispenser_progress.setTextVisible(False)
        self.dispenser_progress.setFixedHeight(6)
        self.dispenser_progress.setVisible(False)
        layout.addWidget(self.dispenser_progress)
        self._tip(
            "Time remaining in the current fire.",
            self.dispenser_progress,
        )

        parent.addWidget(card)

    def _on_dispenser_voltage(self, millivolts):
        self._dispenser_fire_voltage = millivolts / 1000.0
        self.dispenser_voltage_edit.setText(
            f"{self._dispenser_fire_voltage:.1f} V"
        )
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
        self.dispenser_current_edit.setText(
            f"{self._dispenser_fire_current:.1f} A"
        )
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
        self.dispenser_time_edit.setText(f"{int(seconds)} s")
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

        # Frequency, not V_tune: the tune voltage is how the DAC reaches a
        # frequency, not a number anyone sets on purpose. Stored in mV;
        # the slider is in 0.1 MHz steps.
        vtune_initial_mv = self._int_setting(
            "dac/vtune_mv",
            0,
            int(round(self._vtune_min * 1000)),
            int(round(self._vtune_max * 1000)),
        )
        self.vtune_slider, self.vtune_edit = add_slider_row(
            layout,
            "Frequency:",
            int(round(REPUMP_SLIDER_MIN_MHZ * 10)),
            int(round(REPUMP_SLIDER_MAX_MHZ * 10)),
            int(round(vtune_to_vco_mhz(vtune_initial_mv / 1000.0) * 10)),
            single=1,
            page=10,
            suffix=" MHz",
            decimals=1,
            scale=10.0,
        )
        self.vtune_slider.valueChanged.connect(self._on_vtune)
        # 0.5 s is the error signal's settle time after a repump change
        # (operator, bench), so a held key gives 0.1 MHz per 0.5 s =
        # 0.2 MHz/s, the rate the first MOT was found at.
        self._vtune_key_limiter = HeldKeyLimiter(0.5, self)
        self._vtune_key_limiter.attach(self.vtune_slider, self.vtune_edit)
        self._tip(
            "Repump VCO output frequency. Set with the DAC Click's tune\n"
            f"voltage, whose 0-5 V spans {VCO_FREQ_MIN_MHZ:.0f}-"
            f"{VCO_FREQ_MAX_MHZ:.0f} MHz on the VCO datasheet's typical\n"
            "+25 C curve (a typical part: expect a few MHz of error).\n"
            "The slider adopts the "
            "hardware value on connect.",
            self.vtune_slider,
            self.vtune_edit,
        )

        # Same step and interval as the held-key limiter: 0.1 MHz per 0.5 s
        # is the error signal's settle time and the rate the MOT was found
        # at; faster sweeps crossed the MOT window before it could load.
        self._repump_sweep_timer = QtCore.QTimer(self)
        self._repump_sweep_timer.setInterval(500)
        self._repump_sweep_timer.timeout.connect(self._repump_sweep_step)
        self.repump_sweep_btn = QtWidgets.QPushButton("Sweep ±10 MHz")
        self.repump_sweep_btn.setCheckable(True)
        self.repump_sweep_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.repump_sweep_btn.toggled.connect(self._toggle_repump_sweep)
        layout.addWidget(self.repump_sweep_btn)
        self._tip(
            "Sweep the repump back and forth ±10 MHz around where it is\n"
            "now, at 0.2 MHz/s (0.1 MHz every 0.5 s, 200 s per cycle).\n"
            "Stopping leaves it where it is. Moving the slider stops it.",
            self.repump_sweep_btn,
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
        # Same rhythm as add_slider_row / add_readout_row.
        row.setContentsMargins(0, 3, 0, 3)
        row.setSpacing(8)
        row.addWidget(self._small_label(label_text))
        edit = QtWidgets.QLineEdit(str(value or ""))
        edit.setPlaceholderText(placeholder)
        row.addWidget(edit, stretch=1)
        layout.addLayout(row)
        return edit

    def _supply_readback_label(self, layout):
        # What the supply itself reports, from its poll. The sliders are
        # the app's setpoints, so on loading they said nothing about what
        # the supply was doing (operator, 2026-11-10).
        label = QtWidgets.QLabel("Supply: no reading yet")
        label.setStyleSheet(
            "color: #ababab; font-size: 12px; font-weight: 600; border: none;"
        )
        layout.addWidget(label)
        self._tip(
            "Read back from the supply every poll: output state, measured\n"
            "voltage and current, and the setpoints the supply holds.",
            label,
        )
        return label

    @staticmethod
    def _supply_readback_text(state):
        held = (
            f"set {state.get('v_set', 0.0):.1f} V, "
            f"{state.get('i_set', 0.0):.2f} A"
        )
        trip = DPS_PROTECTION.get(int(state.get("protection", 0) or 0))
        if trip:
            return f"Supply: TRIP ({trip}) · {held}"
        if state.get("output_enabled"):
            return (
                f"Supply: ON · {state.get('v_out', 0.0):.2f} V · "
                f"{state.get('i_out', 0.0):.2f} A ({held})"
            )
        return f"Supply: OFF · {held}"

    @staticmethod
    def _small_label(text):
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(
            "color: #ababab; font-size: 13px; font-weight: 600; border: none;"
        )
        label.setFixedWidth(120)
        return label

    # ------------------------------------------------------------------
    # Startup / reconnect
    # ------------------------------------------------------------------

    def _deferred_start(self):
        if self._rp_host:
            self.linien_widget.start()
        self._connect_lock_signal()
        # The vtune DAC and the repump gate are two more RPyC clients to
        # the same linien-server, so they used to repeat the acquisition
        # client's connect attempt and its failure -- one absent Red Pitaya
        # produced three of everything. They start from
        # _on_linien_connection once that client says the server answered.
        # Serial workers start once the USB probe has decided which port
        # hosts which device (see _on_probe_result).
        self._run_usb_probe(startup=True)
        self._start_port_watch()
        # Outputs survive this app. The repump gate is a Red Pitaya GPIO
        # bit and the dither a linien parameter the server restores, so
        # an exit that skipped teardown -- a crash, a kill -- leaves them
        # running and the next launch simply adopts them. Give the
        # workers time to report, then say what was found rather than
        # leaving it to be noticed.
        QtCore.QTimer.singleShot(4000, self._report_startup_outputs)

    # Devices with a serial port: the status row each one owns, and the
    # live-output key whose state stops being device-backed if it vanishes.
    _SERIAL_DEVICES = (
        ("laser", "laser_edit", "laser_usb_status", "laser"),
        ("mot_coil", "mot_coil_port_edit", "coil_usb_status", "coils"),
        ("dispenser", "dispenser_port_edit", "dispenser_usb_status", "dispenser"),
    )

    def _start_port_watch(self):
        self.port_watch_worker = SerialPresenceWorker()
        self.port_watch_worker.ports_present.connect(self._on_ports_present)
        self.port_watch_worker.start()

    def _on_ports_present(self, present):
        """A serial device's port has appeared or vanished.

        The supplies find out by failing to poll, but nothing polls the
        Koheron -- so without this, unplugging the laser changed nothing on
        screen. Enumeration is not device I/O, so this costs no traffic and
        works for a device that is only ever written to.

        Reports only. Re-opening a port that came back is a connection, and
        connections stay the operator's call (the Auto-connect button).
        """
        for key, edit_attr, row_attr, output_key in self._SERIAL_DEVICES:
            port = getattr(self, edit_attr).text().strip()
            if not port:
                continue
            gone = port not in present
            if gone == self._port_missing.get(key, False):
                continue  # no edge, nothing to say
            self._port_missing[key] = gone
            row = getattr(self, row_attr)
            if gone:
                self._set_usb_status_label(
                    row, f"error:port {port} is not there", key
                )
                # What is on screen for it is no longer the device's state.
                self._readback_seen.discard(output_key)
                self._event_log.fault(
                    "port_vanished", device=key, port=port,
                )
            else:
                # Still Error: the port exists again, but nothing has
                # reopened it. The tooltip is what says so.
                self._set_usb_status_label(
                    row, f"error:port {port} is back — press Auto-connect", key
                )
                self._event_log.log("port_returned", device=key, port=port)

    def _report_startup_outputs(self):
        """Name anything that was already energised when we connected.

        Reports only; nothing is actuated. Switching an output off at
        startup would be an actuation nobody asked for, and it would
        stomp a state left on deliberately.

        Only device-backed outputs are named. This used to report whatever
        the UI showed, so a restored dither setting was announced as a
        running dither one second before the connect to the Red Pitaya
        failed. What we could not check is recorded as unknown instead,
        which is a different claim from "nothing was on".
        """
        live = self.live_outputs()
        unknown = [o for o in live if o.backed_by != "device"]
        if unknown and self._await_startup_answer():
            return
        live = [o for o in live if o.backed_by == "device"]
        if unknown:
            self._event_log.log(
                "startup_outputs_unknown",
                outputs=[o.key for o in unknown],
                detail="; ".join(f"{o.label} {o.detail}" for o in unknown),
                rp_status=self._rp_last_status,
            )
        if not live:
            return
        self._event_log.fault(
            "startup_outputs_live",
            outputs=[o.key for o in live],
            detail="; ".join(f"{o.label} {o.detail}" for o in live),
        )
        # Log only. It used to write "N OUTPUTS WERE ON" into the lock
        # chip, which then hid the lock state (operator, 2026-11-10).

    # Three re-checks at 6 s covers three connect attempts 2 s apart; a
    # host that is simply off still gets reported, just later.
    _STARTUP_REPORT_RETRIES = 3

    def _await_startup_answer(self):
        """Re-arm the report while the Red Pitaya is still being tried.

        The first attempt fires 4 s in and a connect run takes longer than
        that, so the report used to run before the RP had said anything at
        all -- and then call it unknown.
        """
        if self._startup_report_retried >= self._STARTUP_REPORT_RETRIES:
            return False
        if not self._rp_host or not self._rp_last_status.startswith("connecting"):
            return False
        self._startup_report_retried += 1
        QtCore.QTimer.singleShot(6000, self._report_startup_outputs)
        return True

    def _connect_lock_signal(self):
        try:
            self.linien_widget.lock_state_changed.disconnect(self._on_lock_state)
        except TypeError:
            pass
        self.linien_widget.lock_state_changed.connect(self._on_lock_state)

    def _auto_connect(self):
        """Connect whatever is not connected: the Red Pitaya, then USB.

        The button used to run the USB probe alone, so a Red Pitaya that
        was down at startup could only be brought up from Manual setup --
        the one place an operator would not think to look for "connect".

        A Red Pitaya that is already connected is left alone on purpose:
        restarting the acquisition client runs LinienWorker.stop(), which
        starts a sweep unless unlock_on_stop is False, and that drops a
        lock. Manual Connect still does the full restart, and says so.
        """
        self._save_connection_settings()
        rp_host = self.rp_edit.text().strip()
        if rp_host and not self._rp_last_status.startswith("connected"):
            self.linien_widget.set_host(rp_host)
            self._connect_lock_signal()
        # Restarts the serial workers itself once it knows the ports.
        self._run_usb_probe(startup=False)

    def _connect_all(self):
        self._save_connection_settings()

        rp_host = self.rp_edit.text().strip()
        if rp_host:
            self.linien_widget.set_host(rp_host)
        else:
            self.linien_widget.stop()
        self._connect_lock_signal()

        # PSD too: it kept its client to the old host after a host change.
        for attr in ("vtune_worker", "repump_gpio_worker", "psd_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.stop()
                setattr(self, attr, None)
        if self._psd_window is not None:
            self._psd_window.set_worker(None)
        # Not started here: set_host() above restarts the acquisition
        # client, and these follow it once the server has answered.

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
            self._set_usb_status_label(
                getattr(self, attr), "connecting:probing the USB ports"
            )

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

        # Through the same label helper as the live handlers, so a probe
        # result and a worker reconnect can never disagree about the count.
        for key, attr in (
            ("laser", "laser_usb_status"),
            ("mot_coil", "coil_usb_status"),
            ("dispenser", "dispenser_usb_status"),
        ):
            verified = status.get(key) == "verified"
            self._set_usb_status_label(
                getattr(self, attr),
                f"connected:{assignments.get(key, '')} (verified by probe)"
                if verified
                else f"error:not found by the USB probe ({status.get(key)})",
                key,
            )

        self._save_connection_settings()

        if startup and not res.get("ok"):
            # No pop-up: the Device Connections card expands itself above
            # and shows which device is missing, in place.
            self._refresh_discovery()
        self._start_serial_workers()

    def _stop_serial_workers(self):
        # Only reconnects come here (teardown is _stop_hardware_workers), so
        # the laser and the coils keep running: a stop with the default
        # disable_on_stop cut both on every Auto-connect, which the app asks
        # for whenever a port comes back. The new workers read the outputs
        # back. The dispenser still goes off -- its auto-off timer lives in
        # the worker being stopped.
        kept = []
        for attr in ("laser_worker", "mot_coil_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.disable_on_stop = False
                if hasattr(worker, "left_on_reason"):
                    worker.left_on_reason = "reconnect"
                kept.append(attr)
        if kept:
            self._event_log.log("outputs_kept_across_reconnect", workers=kept)
        for attr in ("laser_worker", "mot_coil_worker", "dispenser_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.stop()
                setattr(self, attr, None)

    def _start_serial_workers(self):
        self._start_laser_worker()
        self._start_dps_workers()

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
        self.laser_worker = LaserSetpointWorker(
            port=self.laser_edit.text().strip(),
            event_log=self._event_log,
        )
        self.laser_worker.initial_values.connect(self._on_laser_init)
        self.laser_worker.connection_status.connect(
            lambda status: self._set_usb_status_label(
                self.laser_usb_status, status, "laser"
            )
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
            event_log=self._event_log,
            log_name="coil",
        )
        self.mot_coil_worker.connection_status.connect(
            lambda status: self._set_usb_status_label(
                self.coil_usb_status, status, "mot_coil"
            )
        )
        self.mot_coil_worker.connection_status.connect(
            lambda status: self._log_connection("coil", status)
        )
        self.mot_coil_worker.readback.connect(self._on_mot_coil_readback)
        self.mot_coil_worker.fault.connect(
            lambda msg: self._show_fault(
                self.coil_usb_status, "coil", msg, "mot_coil"
            )
        )
        self.mot_coil_worker.start()

        self.dispenser_worker = DispenserWorker(
            port=self.dispenser_port_edit.text().strip(),
            address=self._dispenser_address,
            model="DPS5020",
            fire_seconds=self._dispenser_fire_seconds,
            dispense_current_a=self._dispenser_fire_current,
            dispense_voltage_v=self._dispenser_fire_voltage,
            event_log=self._event_log,
            log_name="dispenser",
        )
        self.dispenser_worker.connection_status.connect(
            lambda status: self._set_usb_status_label(
                self.dispenser_usb_status, status, "dispenser"
            )
        )
        self.dispenser_worker.connection_status.connect(
            lambda status: self._log_connection("dispenser", status)
        )
        self.dispenser_worker.readback.connect(self._on_dispenser_readback)
        self.dispenser_worker.fire_state.connect(self._on_dispenser_fire_state)
        self.dispenser_worker.fire_summary.connect(self._on_dispenser_fire_summary)
        self.dispenser_worker.fault.connect(
            lambda msg: self._show_fault(
                self.dispenser_usb_status, "dispenser", msg, "dispenser"
            )
        )
        self.dispenser_worker.start()

    def _start_repump_workers(self):
        """Start the two other RPyC clients, once the RP is known to answer.

        Each is checked on its own. The guard used to return if *either*
        was alive, which meant one survivor masked one corpse: when the
        server dropped the gate client and the DAC client's thread kept
        running (faulting every poll), the gate was never rebuilt and the
        repump button silently did nothing for the rest of the session.
        """
        self._start_vtune_worker()
        self._start_gate_worker()

    @staticmethod
    def _is_running(worker):
        return worker is not None and worker.isRunning()

    def _start_vtune_worker(self):
        # LinkHealth re-emits "connected" on every up edge, so this must be
        # idempotent or a blip would spawn a second client on the same DAC.
        if self._is_running(getattr(self, "vtune_worker", None)):
            return
        self._vtune_slider_synced = False
        self.vtune_worker = DacClickWorker(
            host=self.rp_edit.text().strip(),
            min_voltage=self._vtune_min,
            max_voltage=self._vtune_max,
            event_log=self._event_log,
        )
        self.vtune_worker.connection_status.connect(
            lambda status: self._log_connection("dac_click", status)
        )
        self.vtune_worker.voltage_readback.connect(self._on_vtune_readback)
        self.vtune_worker.fault.connect(
            lambda msg: self._log_fault("dac_click", msg)
        )
        self.vtune_worker.start()

    def _start_gate_worker(self):
        if self._is_running(getattr(self, "repump_gpio_worker", None)):
            return
        # The repump RF gate is fixed at DIO1_N (bank n, bit 1), active-low
        # — the empirically verified mapping from qmot-linien-controls.
        self.repump_gpio_worker = LinienGpioWorker(
            host=self.rp_edit.text().strip(),
            bank="n",
            bit=1,
            active_high=False,
            event_log=self._event_log,
        )
        self.repump_gpio_worker.connection_status.connect(
            lambda status: self._on_repump_gate_status(status)
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

    @staticmethod
    def _lock_worker_connected(worker):
        # The worker exists from start() to stop(), including while it is
        # still connecting or has given up; a write then is refused. So
        # "a worker exists" did not mean "the write landed", and the
        # modulation touched flag cleared on a write that never happened.
        return worker is not None and bool(getattr(worker, "connected", False))

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
            self._note_mod_touched(pushed=False)
            return
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_modulation_amplitude(value)
        self._note_mod_touched(pushed=self._lock_worker_connected(worker))

    def _on_sweep_speed(self, combo_index):
        self._settings.setValue("linien/sweep_speed_index", combo_index)
        self._event_log.log(
            "setpoint", name="sweep_speed_index", value=combo_index
        )
        self._refresh_lock_buttons()
        worker = self._get_lock_worker()
        if worker is None:
            return
        speed = self.sweep_speed_combo.currentData()
        if speed is None:
            worker.set_sweep_pause(True)
            return
        # Unpause before setting the rate, so leaving the hold always
        # produces a moving trace rather than a paused one at a new speed.
        worker.set_sweep_pause(False)
        worker.set_sweep_speed(speed)

    def _on_sweep_readback(self, state):
        """Show the sweep the Red Pitaya is actually running, after connect.

        Signals blocked: the box is reporting the device here, not
        commanding it (restoring state never pushes to hardware).
        """
        paused, speed = state
        index = 16 if paused else max(0, min(15, int(speed)))
        was = self.sweep_speed_combo.currentIndex()
        if index != was:
            self.sweep_speed_combo.blockSignals(True)
            self.sweep_speed_combo.setCurrentIndex(index)
            self.sweep_speed_combo.blockSignals(False)
            self._settings.setValue("linien/sweep_speed_index", index)
            self._event_log.log("sweep_adopted", device_index=index, was_ui_index=was)
        self._refresh_lock_buttons()

    def _lock_prerequisites_missing(self):
        """What has to be switched on before a lock can be started.

        A lock, a phase optimisation, a target-line selection or a lock PSD
        with the modulation or the repump off measures nothing: on the
        bench Optimize phase with the modulation off moved Mod Phase on
        pure noise (operator, 2026-11-10).
        """
        missing = []
        mod = getattr(self, "mod_enable_btn", None)
        if mod is None or not mod.isChecked():
            missing.append("Modulation")
        gate = getattr(self, "repump_gpio_btn", None)
        if gate is None or not gate.isChecked():
            missing.append("Repump")
        return missing

    def _refresh_lock_buttons(self):
        """The one place that decides which lock-start buttons are usable.

        All three need the Red Pitaya connected and Modulation and Repump
        on; Lock at center and Optimize phase also need the sweep held;
        none while a lock action runs. A click that could only be refused
        is greyed instead, with the reason on the hint line.
        """
        lw = getattr(self, "linien_widget", None)
        if lw is None or not hasattr(lw, "center_lock_button"):
            return
        connected = self._rp_last_status.startswith("connected")
        missing = self._lock_prerequisites_missing()
        idle = not getattr(self, "_lock_action_running", False)
        held = self.sweep_speed_combo.currentData() is None
        ready = connected and idle and not missing
        lw.lock_button.setEnabled(ready)
        lw.center_lock_button.setEnabled(ready and held)
        lw.phase_button.setEnabled(ready and held)
        if self._psd_window is not None:
            self._psd_window.set_lock_prerequisites(missing)
        if not connected:
            hint = "Connect the Red Pitaya to lock."
        elif missing:
            hint = f"Switch on {' and '.join(missing)} to lock."
        else:
            hint = ""
        # Only a hint this method put there is replaced or cleared: a lock
        # result on that line stays until the next action.
        shown = lw.lock_message.text()
        ours = shown == getattr(self, "_lock_hint", "")
        if hint and (not shown or ours):
            lw.show_lock_message(hint, problem=False)
        elif not hint and shown and ours:
            lw.show_lock_message("")
        self._lock_hint = hint

    def _hold_for_lock_action(self, busy):
        """Hold what a running lock action reads and then restores.

        A speed picked mid-Optimize-phase un-held the sweep under the probe,
        and the action's `finally` then put the old speed back under a combo
        showing the new one. Mod Phase is the value Optimize phase writes;
        Load Config pushes sweep, phase and PID all at once. Each control
        gets back the state it had, not a forced "enabled".
        """
        self._lock_action_running = bool(busy)
        if busy:
            self._held_for_action = {
                w: w.isEnabled()
                for w in (
                    self.sweep_speed_combo,
                    self.mod_phase_slider,
                    self.load_config_btn,
                )
            }
            self._held_for_action[self.mod_phase_edit] = (
                not self.mod_phase_edit.isReadOnly()
            )
            for w in self._held_for_action:
                if w is self.mod_phase_edit:
                    # Read-only, not disabled: see _apply_mod_enabled.
                    w.setReadOnly(True)
                else:
                    w.setEnabled(False)
            return
        for w, was in getattr(self, "_held_for_action", {}).items():
            if w is self.mod_phase_edit:
                w.setReadOnly(not was)
            else:
                w.setEnabled(was)
        self._held_for_action = {}
        self._refresh_lock_buttons()

    def _on_lock_state(self, locked):
        self._lock_engaged = bool(locked)
        self._readback_seen.add("lock")
        self._show_lock_chip()
        self._event_log.log("lock", locked=bool(locked))

    def _show_lock_chip(self):
        """LOCKED, or UNLOCKED with the modulation state, and nothing else.

        The modulation only matters while unlocked: it is what decides
        whether a lock can be started (operator, 2026-11-10).
        """
        if "lock" not in getattr(self, "_readback_seen", ()):
            return  # stays "LOCK --" until the server has said
        if self._lock_engaged:
            self._set_chip("lock", "LOCKED", "#6f9e7c")
        else:
            mod = "ON" if self.mod_enable_btn.isChecked() else "OFF"
            self._set_chip("lock", f"UNLOCKED · MODULATION {mod}", "#b56a6a")

    # ------------------------------------------------------------------
    # Koheron laser controls
    # ------------------------------------------------------------------

    def _on_laser_init(self, values):
        # The Koheron answered, so the laser button and these sliders are
        # its state rather than a restored preference -- unless the diode
        # is dark, in which case the saved setpoints go the other way.
        self._readback_seen.add("laser")
        # The button is what shutdown and the startup report read, so it
        # must show a diode that is already lit: it used to stay "Laser
        # Off", and closing the app then left the laser on unasked.
        # Signals blocked -- this reports the device, it writes nothing.
        output_on = bool(values.get("output_on", True))
        self.laser_btn.blockSignals(True)
        self.laser_btn.setChecked(output_on)
        self.laser_btn.blockSignals(False)
        self._show_laser_output(output_on)
        if not output_on:
            if self._restore_laser_setpoints():
                return
        self.current_slider.blockSignals(True)
        self.current_slider.setValue(int(round(float(values["current"]) * 100)))
        self.current_slider.blockSignals(False)
        self.current_edit.setText(f"{float(values['current']):.2f} mA")

        therm_c = thermistor_ohms_to_c(int(values["thermistor"]))
        self.therm_slider.blockSignals(True)
        self.therm_slider.setValue(int(round(therm_c * 100)))
        self.therm_slider.blockSignals(False)
        self.therm_edit.setText(f"{therm_c:.2f} \N{DEGREE SIGN}C")
        self._event_log.log(
            "laser_init",
            current_ma=round(float(values["current"]), 2),
            thermistor_ohm=int(values["thermistor"]),
            output_on=bool(values.get("output_on", True)),
            adopted=True,
        )

    def _restore_laser_setpoints(self):
        """Write the saved current/temperature to a Koheron that is dark.

        Reversing 2026-10-16 the direction set by d441b65: a power cycle
        leaves the CTL200 at 0 mA / 9999 ohm, and adopting that threw away
        the operating point every time. Only while the output is off --
        this is still a laser current write, and doing it to a running
        diode unasked is the thing that decision was protecting.

        Returns True when the push happened, so the caller knows not to
        overwrite the UI with the device's values afterwards.
        """
        worker = getattr(self, "laser_worker", None)
        if worker is None:
            return False
        milliamps = self.current_slider.value() / 100.0
        ohms = int(round(thermistor_c_to_ohms(self.therm_slider.value() / 100.0)))
        worker.set_current(milliamps)
        worker.set_thermistor(ohms)
        self._event_log.log(
            "laser_setpoints_restored",
            current_ma=milliamps,
            thermistor_ohm=ohms,
            temp_c=round(self.therm_slider.value() / 100.0, 2),
        )
        return True

    def _show_laser_output(self, on):
        self._schedule_laser_off_rule()
        self.laser_btn.setText("Laser On" if on else "Laser Off")
        self._set_chip(
            "laser",
            "LASER ON" if on else "LASER OFF",
            "#6f9e7c" if on else self._CHIP_NEUTRAL,
        )

    def _toggle_laser(self, checked):
        self._show_laser_output(checked)
        self._event_log.log("laser_output", on=bool(checked))
        worker = getattr(self, "laser_worker", None)
        if worker is not None:
            worker.laser_on(checked)
        # The repump gate and the current dither are both meaningless with
        # the laser off, so they follow it off. Neither follows the laser
        # back on -- switching those on stays a deliberate act.
        if not checked:
            self._follow_laser_off()

    def _apply_laser_off_rule(self):
        """Laser off means Modulation and Repump off (operator, 2026-11-10).

        Both buttons are greyed while the Koheron reports the diode dark,
        and either one found on then -- a readback at connect, say -- is
        switched off, which writes to the Red Pitaya. Nothing is enforced
        before the Koheron has answered: "not connected" is not "off".
        Scheduled rather than called from the readback handlers, which
        update these buttons with signals blocked: switching one off in
        there would move the button and never send the write.
        """
        laser_off = (
            "laser" in self._readback_seen and not self.laser_btn.isChecked()
        )
        for attr in ("mod_enable_btn", "repump_gpio_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.setEnabled(not laser_off)
        if laser_off:
            self._follow_laser_off()

    def _schedule_laser_off_rule(self):
        QtCore.QTimer.singleShot(0, self._apply_laser_off_rule)

    def _follow_laser_off(self):
        for attr, reason_key in (
            ("repump_gpio_btn", "repump_gate_auto_off"),
            ("mod_enable_btn", "modulation_auto_off"),
        ):
            btn = getattr(self, attr, None)
            if btn is not None and btn.isChecked():
                self._event_log.log(reason_key, reason="laser_off")
                btn.setChecked(False)

    def _on_current(self, value):
        # Slider counts 0.01 mA; the Koheron's ilaser takes mA.
        milliamps = value / 100.0
        self.current_edit.setText(f"{milliamps:.2f} mA")
        self._settings.setValue("laser/current_ma", milliamps)
        self._debounced_log(
            "laser_current",
            "setpoint",
            lambda: {"name": "laser_current_ma",
                     "value": self.current_slider.value() / 100.0},
        )
        worker = getattr(self, "laser_worker", None)
        if worker is not None:
            worker.set_current(milliamps)

    def _on_thermistor(self, value):
        # Slider is centi-degrees; the Koheron still speaks ohms.
        celsius = value / 100.0
        ohms = int(round(thermistor_c_to_ohms(celsius)))
        self.therm_edit.setText(f"{celsius:.2f} \N{DEGREE SIGN}C")
        self._settings.setValue("laser/thermistor_ohm", ohms)
        self._debounced_log(
            "laser_thermistor",
            "setpoint",
            lambda: {
                "name": "laser_thermistor_ohm",
                "value": ohms,
                "temp_c": round(celsius, 2),
            },
        )
        worker = getattr(self, "laser_worker", None)
        if worker is not None:
            worker.set_thermistor(ohms)

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

    # ------------------------------------------------------------------
    # DPS5005 controls
    # ------------------------------------------------------------------

    def _toggle_mot_coils(self, checked):
        self.mot_coil_output_btn.setText("Coils On" if checked else "Coils Off")
        self._expect_output("coils", checked)
        self._event_log.user("coil_output", on=bool(checked))
        worker = getattr(self, "mot_coil_worker", None)
        if worker is not None:
            worker.set_output(checked)

    def _fire_dispenser_clicked(self):
        worker = getattr(self, "dispenser_worker", None)
        if worker is None or not worker.isRunning():
            # Said, not silent: a press that did nothing read as a fire
            # that had not started yet.
            self._log_fault(
                "dispenser",
                "not connected — the press did not reach the supply; "
                "press Auto-connect",
            )
            return
        if self._dispenser_phase == "firing":
            worker.abort_fire()
        else:
            worker.fire()

    def _set_dispenser_fire(self, current_a, seconds):
        self._dispenser_fire_current = float(current_a)
        self._dispenser_fire_seconds = max(
            DispenserWorker.MIN_FIRE_SECONDS,
            min(float(seconds), DispenserWorker.MAX_FIRE_SECONDS),
        )
        self._settings.setValue(
            "dispenser/fire_current_a", self._dispenser_fire_current
        )
        self._settings.setValue(
            "dispenser/fire_seconds", self._dispenser_fire_seconds
        )
        # Drive the sliders without bouncing back through their handlers.
        self.dispenser_current_slider.blockSignals(True)
        self.dispenser_current_slider.setValue(
            int(round(self._dispenser_fire_current * 1000))
        )
        self.dispenser_current_edit.setText(
            f"{self._dispenser_fire_current:.1f} A"
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
            "dispenser_setpoint",
            current_a=self._dispenser_fire_current,
            seconds=self._dispenser_fire_seconds,
        )
        if self._dispenser_phase == "idle":
            self.dispenser_fire_btn.setText(
                f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
            )

    def _on_mot_coil_voltage(self, value):
        volts = value / 1000.0
        self.mot_coil_voltage_edit.setText(f"{volts:.1f} V")
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
        self.mot_coil_current_edit.setText(f"{amps:.1f} A")
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
        # No readback row: the header chip carries the current and the
        # trip state, and the measurements still reach the event log.
        self._sync_output_button(
            self.mot_coil_output_btn, state, "Coils On", "Coils Off", "coils"
        )
        if int(state.get("protection", 0) or 0):
            self._set_chip("coils", "COILS TRIP", "#b56a6a")
        elif state.get("output_enabled"):
            self._set_chip("coils", f"COILS {state['i_out']:.1f} A", "#6f9e7c")
        else:
            self._set_chip("coils", "COILS OFF", "#ababab")
        self._readback_seen.add("coils")
        self.coil_readback_label.setText(self._supply_readback_text(state))
        self._log_readback("coil", state)

    def _on_dispenser_readback(self, state):
        # No readback row on this card -- the fire button and the header
        # chip carry the state. Protections still reach the event log.
        self._readback_seen.add("dispenser")
        self.dispenser_readback_label.setText(
            self._supply_readback_text(state)
        )
        self._log_readback("dispenser", state)

    def _sync_output_button(self, button, state, on_text, off_text, key):
        """Show the supply's actual output state, without fighting a click.

        The write takes a poll or two to reach the supply and come back,
        so a naive sync snaps the button back to the old state and then
        forward again -- the click reads as a flicker with a lag. While a
        write is outstanding the readback is ignored; once the device
        agrees, or the grace period expires, it drives the button again.
        Showing the truth after a failed write matters more than hiding
        the lag, hence the timeout rather than a latch.
        """
        actual = bool(state["output_enabled"])
        desired, deadline = self._pending_output.get(key, (None, 0.0))
        if desired is not None:
            if actual == desired:
                self._pending_output.pop(key)  # the device caught up
            elif time.monotonic() < deadline:
                return  # write still in flight
            else:
                self._pending_output.pop(key)
                self._log_fault(
                    key, f"output did not reach {'on' if desired else 'off'}"
                )
        button.blockSignals(True)
        button.setChecked(actual)
        button.setText(on_text if actual else off_text)
        button.blockSignals(False)

    # A supply write is applied on the worker's next poll (500 ms) and
    # confirmed on the one after, so two polls plus slack.
    OUTPUT_SETTLE_S = 2.0

    def _expect_output(self, key, enabled):
        """Record that the user asked for a state the device has not reached."""
        self._pending_output[key] = (
            bool(enabled), time.monotonic() + self.OUTPUT_SETTLE_S
        )

    def _on_dispenser_fire_state(self, state):
        phase = state.get("phase", "idle")
        remaining = float(state.get("remaining_s", 0.0))
        self._readback_seen.add("dispenser")
        if phase != self._dispenser_phase:
            if phase == "firing":
                self._event_log.log(
                    "dispenser_fire_start",
                    current_a=self._dispenser_fire_current,
                    planned_s=self._dispenser_fire_seconds,
                )
            elif phase == "idle" and self._dispenser_phase == "firing":
                self._event_log.log("dispenser_ready")
        self._dispenser_phase = phase
        self._update_dispenser_progress(
            phase, remaining, state.get("planned_s")
        )
        if phase == "firing":
            self.dispenser_fire_btn.setEnabled(True)
            self.dispenser_fire_btn.setText(f"Abort Fire ({remaining:.0f} s)")
            self._set_chip("disp", "DISPENSER FIRING", "#b9924f")
        else:
            self.dispenser_fire_btn.setEnabled(True)
            self.dispenser_fire_btn.setText(
                f"Fire Dispenser ({self._dispenser_fire_seconds:.0f} s)"
            )
            self._set_chip("disp", "DISPENSER IDLE", "#ababab")

    def _update_dispenser_progress(self, phase, remaining, planned=None):
        if phase == "firing":
            # The length this fire started with, from the worker: the
            # slider's current value made the bar jump when Fire Time was
            # moved mid-fire.
            total = max(1.0, planned or self._dispenser_fire_seconds)
            color = "#b9924f"
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
        # Slider is in 0.1 MHz; the DAC still takes volts.
        mhz = value / 10.0
        volts = vco_mhz_to_vtune(mhz)
        self.vtune_edit.setText(f"{mhz:.1f} MHz")
        self._settings.setValue("dac/vtune_mv", int(round(volts * 1000)))
        worker = getattr(self, "vtune_worker", None)
        if worker is not None:
            worker.set_voltage(volts)

    def _toggle_repump_sweep(self, checked):
        slider = self.vtune_slider
        if checked:
            center = slider.value()
            # Slider units are 0.1 MHz, so 100 is 10 MHz.
            self._repump_sweep_lo = max(slider.minimum(), center - 100)
            self._repump_sweep_hi = min(slider.maximum(), center + 100)
            self._repump_sweep_dir = 1
            self._repump_sweep_expected = center
            self._repump_sweep_timer.start()
            self.repump_sweep_btn.setText("Stop Sweep")
            self._event_log.user("repump_sweep", on=True, center_mhz=center / 10.0)
        else:
            self._repump_sweep_timer.stop()
            self.repump_sweep_btn.setText("Sweep ±10 MHz")
            self._event_log.user(
                "repump_sweep", on=False, stopped_mhz=slider.value() / 10.0)

    def _repump_sweep_step(self):
        slider = self.vtune_slider
        if slider.value() != self._repump_sweep_expected:
            # Someone else moved it (operator, Load Config): theirs wins.
            self.repump_sweep_btn.setChecked(False)
            return
        nxt = slider.value() + self._repump_sweep_dir
        if not self._repump_sweep_lo <= nxt <= self._repump_sweep_hi:
            self._repump_sweep_dir = -self._repump_sweep_dir
            nxt = slider.value() + self._repump_sweep_dir
        self._repump_sweep_expected = nxt
        slider.setValue(nxt)

    def _on_vtune_readback(self, volts):
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
            mhz = vtune_to_vco_mhz(volts)
            self.vtune_slider.blockSignals(True)
            self.vtune_slider.setValue(int(round(mhz * 10)))
            self.vtune_slider.blockSignals(False)
            self.vtune_edit.setText(f"{mhz:.1f} MHz")

    def _toggle_repump_gpio(self, checked):
        self._set_repump_ui(checked)
        self._expect_output("repump", checked)
        self._event_log.user("repump_gate", on=bool(checked))
        worker = getattr(self, "repump_gpio_worker", None)
        if worker is None or not worker.isRunning():
            # The press used to end here in silence: the button moved, the
            # log recorded the intent, and the GPIO bit never heard about
            # it. Say so, and stop the chip claiming the gate followed.
            self._log_fault(
                "repump_gate",
                "gate not connected — the press did not reach the Red "
                "Pitaya; press Auto-connect",
            )
            self._readback_seen.discard("repump")
            self._set_chip("repump", "REPUMP NO LINK", "#b56a6a")
            return
        worker.set_output(checked)

    def _on_repump_gate_status(self, status):
        """The gate's own connection, which has no status row of its own."""
        self._log_connection("repump_gate", status)
        if not str(status).startswith("connected"):
            # Whatever the button shows is no longer the GPIO bit.
            self._readback_seen.discard("repump")

    def _on_repump_gpio_state(self, enabled):
        # Logged on change, with what the laser was known to be doing: a
        # repump on next to a dark laser was seen after a power cycle
        # (operator, 2026-11-10) and nothing recorded the gate's state.
        # The gate is active-low, so a rebooted Red Pitaya may bring it up
        # open; this is the record that would show it.
        if getattr(self, "_repump_logged", None) != bool(enabled):
            self._repump_logged = bool(enabled)
            self._event_log.readback(
                "repump_gate", on=bool(enabled),
                laser_known="laser" in self._readback_seen,
                laser_on=self.laser_btn.isChecked(),
            )
        # Same rule as the supplies: a readback must not overwrite a
        # button the user just pressed while the write is in flight.
        self._readback_seen.add("repump")
        desired, deadline = self._pending_output.get("repump", (None, 0.0))
        if desired is not None:
            if bool(enabled) == desired:
                self._pending_output.pop("repump")
            elif time.monotonic() < deadline:
                return
            else:
                self._pending_output.pop("repump")
                self._log_fault(
                    "repump_gate",
                    f"gate did not reach {'open' if desired else 'closed'}",
                )
        self.repump_gpio_btn.blockSignals(True)
        self.repump_gpio_btn.setChecked(enabled)
        self._set_repump_ui(enabled)
        self.repump_gpio_btn.blockSignals(False)

    def _set_repump_ui(self, on):
        self.repump_gpio_btn.setText("Repump On" if on else "Repump Off")
        self._refresh_lock_buttons()
        self._schedule_laser_off_rule()
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
        # Deliberately not _filters_syncing: that flag is re-entrancy
        # protection for _set_filters_ui, and it is False again by the time
        # a connect-time readback could land on top of the operator.
        self._filters_touched = True
        self._debounced_log(
            "error_filters",
            "setpoint",
            lambda: {"name": "error_filters", **self._filters_from_ui()},
        )
        # Coalesce bursts (typing a frequency) into one hardware write.
        self._debounced_push("filters", self._push_filters, 400)

    def _push_filters(self):
        worker = self._get_lock_worker()
        if worker is not None:
            worker.set_filters(self._filters_from_ui())
            # Desired and actual agree now, so a later readback is welcome
            # to drive these again. Without this the flag would be a
            # one-way latch and every reconnect would report a divergence.
            self._filters_touched = False

    def _sync_filters_from_device(self):
        worker = self._get_lock_worker()
        filters = worker.get_filters() if worker is not None else None
        if filters is None:
            self._log_fault("linien", "error filter read skipped: not connected")
            return
        if self._filters_touched:
            # A readback must never overwrite a control the operator is
            # working in. Show the divergence instead: it means a failed
            # write, a front-panel change, or a device in protection.
            self._event_log.fault(
                "filter_divergence",
                device=filters,
                ui=self._filters_from_ui(),
            )
            return
        self._set_filters_ui(filters)

    def _on_server_stalled(self, stalled):
        """The Red Pitaya answers but its worker threads have died.

        A frozen plot has three possible causes -- a stalled server, a
        dropped connection, or a selection holding the trace -- and they
        look identical. This is what names the first one.
        """
        self._server_stalled = bool(stalled)
        if stalled:
            # The row says Error, with the reason on hover. The lock chip
            # keeps showing the lock: "RP STALLED" there read as nonsense
            # over a trace that was still moving (operator, 2026-11-10).
            self._set_usb_status_label(
                self.rp_usb_status,
                "error:server heartbeat stopped — it is wedged, restart it",
                "rp",
            )
        else:
            # Restore what the connection actually last reported. Claiming
            # "connected" here would also silently bump DEVICES n/4.
            self._set_usb_status_label(
                self.rp_usb_status, self._rp_last_status, "rp"
            )

    def _restart_linien_server(self):
        """Shut the server down and bring it back, without an SSH session."""
        if self._server_restarting:
            return
        host = self.rp_edit.text().strip()
        if not host:
            self._log_fault("linien", "restart skipped: no Red Pitaya host set")
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Restart linien server",
            f"Restart linien-server on {host}?\n\n"
            "This drops the lock and interrupts acquisition for a few "
            "seconds. Use it when the spectrum has stalled.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return

        self._server_restarting = True
        self.restart_server_btn.setEnabled(False)
        self.restart_server_btn.setText("Restarting…")
        self._event_log.user("linien_server_restart", host=host)

        # Ask the server to exit while we still have a connection to ask
        # on. It raises SystemExit after joining its threads, so the call
        # comes back as an EOF -- that is success, not a failure.
        worker = self._get_lock_worker()
        if worker is not None:
            try:
                worker.client.connection.root.exposed_shutdown()
            except EOFError:
                pass  # the server exiting mid-call: the expected answer
            except Exception as e:
                self._log_fault("linien", f"server shutdown request: {e}")
        self.linien_widget.stop()
        # The other three clients point at the process being restarted.
        # Their loops swallow the errors and keep running, so the
        # "already running" guards in _start_vtune_worker/_start_gate_worker
        # skipped them after the restart and repump tune, gate and PSD were
        # dead until a full Connect. The new server brings the first two
        # back through _on_linien_connection; PSD is recreated below.
        for attr in ("vtune_worker", "repump_gpio_worker", "psd_worker"):
            stale = getattr(self, attr, None)
            if stale is not None:
                stale.stop()
                setattr(self, attr, None)
        if self._psd_window is not None:
            self._psd_window.set_worker(None)

        self.server_restart_worker = LinienServerRestartWorker(
            host=host, event_log=self._event_log
        )
        self.server_restart_worker.finished_ok.connect(
            self._on_server_restarted
        )
        self.server_restart_worker.start()

    def _on_server_restarted(self, ok, message):
        self._server_restarting = False
        self.restart_server_btn.setEnabled(True)
        self.restart_server_btn.setText("Restart Linien Server")
        if not ok:
            self._log_fault("linien", f"server restart failed: {message}")
            return
        self._event_log.log("linien_server_restarted", message=message)
        self._server_stalled = False
        # An open PSD window gets its new worker from _on_linien_connection,
        # once the restarted server has answered.
        self.linien_widget.start()

    def _on_linien_connection(self, status):
        self._rp_last_status = status
        self._refresh_lock_buttons()
        self._set_usb_status_label(self.rp_usb_status, status, "rp")
        if status.startswith("connected"):
            # The server answered, so the two other clients to it have
            # somewhere to connect. Guarded against the repeat emissions
            # LinkHealth makes on recovery.
            self._start_repump_workers()
            # An open PSD window lost its worker to Connect or a restart;
            # give it one for the server that just answered.
            if (self._psd_window is not None and self._psd_window.isVisible()
                    and getattr(self, "psd_worker", None) is None):
                self._open_psd_window()
            # Give the worker a moment to finish its own connect-time setup.
            QtCore.QTimer.singleShot(1500, self._sync_filters_from_device)
            # Push the saved offset preference; the server default is True
            # and we must not silently disagree with the checkbox.
            QtCore.QTimer.singleShot(
                1500,
                lambda: self._on_auto_offset(
                    self.auto_offset_check.isChecked()
                ),
            )

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
        if worker is None or not worker.set_pid(p_value, i_value, d_value):
            # Not remembered either: Save Config would otherwise record
            # gains the Red Pitaya never had.
            QtWidgets.QMessageBox.warning(
                self,
                "PID not written",
                "The PID could not be written to the Red Pitaya; the\n"
                "Event Log has the reason. Connect it and try again.",
            )
            return
        self._remember_pid(p_value, i_value, d_value)
        self._event_log.log("pid_set", p=p_value, i=i_value, d=d_value)

    def _remember_pid(self, p_value, i_value, d_value):
        """Keep the gains the operator last set, for Save Config.

        The gains live on the server, and a config records what the
        operator set rather than reading a device, so this is the copy
        the config is written from.
        """
        self._settings.setValue("linien/pid_p", int(p_value))
        self._settings.setValue("linien/pid_i", int(i_value))
        self._settings.setValue("linien/pid_d", int(d_value))

    def _remembered_pid(self):
        if self._settings.value("linien/pid_p") is None:
            return None
        return {
            "p": self._int_setting("linien/pid_p", 50, 0, 8191),
            "i": self._int_setting("linien/pid_i", 5, 0, 8191),
            "d": self._int_setting("linien/pid_d", 0, 0, 8191),
        }

    def _collect_config(self):
        """Snapshot of every control setpoint, for the JSON export.

        Setpoints only. Nothing here may describe whether an output is on
        -- no laser enable, no coil output, no repump gate, no lock state,
        no fire -- so that loading a config can never switch anything on or
        off. _reject_state_keys enforces that; see _CONFIG_STATE_KEYS.

        Every value comes from the UI or from QSettings. Nothing is read
        off a device, which is what makes this a record of what the
        operator set rather than of what the hardware was doing.
        """
        # Sliders read in physical units now; the config keeps hardware
        # units so files written by either build stay interchangeable.
        vtune_v = vco_mhz_to_vtune(self.vtune_slider.value() / 10.0)
        therm_ohm = int(round(thermistor_c_to_ohms(self.therm_slider.value() / 100.0)))
        config = {
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
                "filters": self._filters_from_ui(),
            },
            "laser": {
                "current_ma": self.current_slider.value() / 100.0,
                "thermistor_ohm": therm_ohm,
                "temp_est_c": round(self.therm_slider.value() / 100.0, 2),
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
                "fire_voltage_v": self._dispenser_fire_voltage,
                "total_fires": self._int_setting("dispenser/total_fires", 0),
                "total_fire_s": self._float_setting(
                    "dispenser/total_fire_s", 0.0
                ),
            },
            "psd": {
                "algorithm": self._int_setting("psd/algorithm", 1, 0, 1),
                "max_decimation": self._int_setting(
                    "psd/max_decimation", 18, 12, 32
                ),
            },
        }
        # The gains last set from this app (Set PID... or a loaded config),
        # not a read of the server. Absent until they have been set once.
        pid = self._remembered_pid()
        if pid is not None:
            config["linien"]["pid"] = pid
        return config

    # Key names that describe an output rather than a setpoint. A config
    # may not carry one: loading a file must never switch the laser, the
    # coils, the repump gate, the dither or the dispenser on or off.
    # "fire_current_a"/"fire_seconds" are absent on purpose -- they size a
    # fire the operator presses, they do not start one.
    _CONFIG_STATE_KEYS = frozenset({
        "output", "enabled", "locked", "lock", "firing", "fire_now",
    })
    _CONFIG_STATE_SUFFIXES = ("_on", "_enabled", "_output", "_locked")
    # Match the suffix rule but switch no output: they choose which filter
    # stage shapes the error signal. Dropping them lost the filter setup
    # on every save and logged a fault for it.
    _CONFIG_NOT_STATE_KEYS = frozenset({"filter_1_enabled", "filter_2_enabled"})

    def _reject_state_keys(self, config, path=()):
        """Drop any output state from a config, and say so.

        The rule held by accident for as long as nobody added such a key.
        This is what makes it hold on purpose: a new "output_enabled" in
        _collect_config would otherwise make loading a file energise a
        supply, and no test would be looking.
        """
        for key in list(config):
            value = config[key]
            if key not in self._CONFIG_NOT_STATE_KEYS and (
                key in self._CONFIG_STATE_KEYS
                or key.endswith(self._CONFIG_STATE_SUFFIXES)
            ):
                del config[key]
                self._event_log.fault(
                    "config_state_key_dropped",
                    key=".".join(path + (key,)),
                    value=str(value),
                )
            elif isinstance(value, dict):
                self._reject_state_keys(value, path + (key,))
        return config

    def _write_config_json(self, path):
        config = self._reject_state_keys(self._collect_config())
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

    # What _apply_config reads, and the conversion it applies to each.
    _CONFIG_SCHEMA = {
        "connection": {
            "rp_host": str, "laser_port": str, "mot_coil_port": str,
            "dispenser_port": str, "mot_coil_address": int,
            "dispenser_address": int,
        },
        "linien": {
            "mod_freq_khz": int, "mod_phase_deg": int, "mod_strength_mv": int,
            "sweep_speed_index": int, "pid": dict, "filters": dict,
        },
        "laser": {"current_ma": float, "thermistor_ohm": float},
        "repump": {"vtune_v": float},
        "coils": {"voltage_v": float, "current_a": float},
        "psd": {"algorithm": int, "max_decimation": int},
        "dispenser": {
            "fire_current_a": float, "fire_seconds": float,
            "fire_voltage_v": float,
        },
    }

    @classmethod
    def _config_problems(cls, config):
        """Every value _apply_config would choke on, as readable lines."""
        problems = []

        def check(where, value, kind):
            try:
                if kind is dict:
                    if not isinstance(value, dict):
                        raise ValueError("not a section")
                    return
                converted = kind(value)
                if kind is float and not math.isfinite(converted):
                    raise ValueError("not a finite number")
            except (TypeError, ValueError) as e:
                problems.append(f"{where} = {value!r}: {e}")

        for section, keys in cls._CONFIG_SCHEMA.items():
            block = config.get(section, {})
            if not isinstance(block, dict):
                problems.append(f"{section}: not a section")
                continue
            for key, kind in keys.items():
                if key in block:
                    check(f"{section}.{key}", block[key], kind)
        linien = config.get("linien", {})
        if isinstance(linien, dict):
            pid = linien.get("pid")
            if isinstance(pid, dict):
                for key in ("p", "i", "d"):
                    if key in pid:
                        check(f"linien.pid.{key}", pid[key], int)
            filters = linien.get("filters")
            if isinstance(filters, dict):
                for key, value in filters.items():
                    if key.endswith(("_type", "_frequency")):
                        check(f"linien.filters.{key}", value, int)
        return problems

    def _apply_config(self, config, path=None):
        """Apply a config snapshot to the UI (handlers push to hardware).

        Setpoints only, and every one of them reaches its device
        immediately where that device is connected -- that is what loading
        a config is for. What it must never do is switch an output on or
        off, so nothing here touches the laser, coil, repump, dither or
        fire controls; state keys are stripped on the way in as well as on
        the way out (see _CONFIG_STATE_KEYS).

        Missing keys are left unchanged, so partial configs are fine.
        Ports/hosts only update the fields — press Connect / Reconnect to
        use them. Dispenser settings are confirmation-gated. The fire
        history is device truth and is never loaded.
        """
        # Everything is checked before anything is written: a bad value
        # used to raise halfway through, after the modulation, PID and
        # filters had already gone to the Red Pitaya -- the worst outcome
        # for a load that pushes without asking.
        problems = self._config_problems(config)
        if problems:
            self._event_log.fault(
                "config_rejected", path=str(path) if path else None,
                problems=problems,
            )
            if path:
                QtWidgets.QMessageBox.warning(
                    self, "Config not loaded",
                    "Nothing was changed. The file has values this app "
                    "cannot use:\n\n" + "\n".join(problems[:10]),
                )
            return
        # A hand-edited or older file could carry one; nothing below reads
        # it, but the record should say it was there and ignored.
        self._reject_state_keys(config)
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
                max(0, min(16, int(linien["sweep_speed_index"])))
            )
        pid = linien.get("pid")
        if pid:
            worker = self._get_lock_worker()
            p_value = int(pid.get("p", 50))
            i_value = int(pid.get("i", 5))
            d_value = int(pid.get("d", 0))
            if worker is not None and worker.set_pid(p_value, i_value, d_value):
                self._remember_pid(p_value, i_value, d_value)
                self._event_log.log(
                    "pid_set", p=p_value, i=i_value, d=d_value, source="config"
                )
            else:
                # It used to be skipped in silence with no Red Pitaya.
                self._log_fault(
                    "linien", "config PID not written: Red Pitaya not connected"
                )
        filters = linien.get("filters")
        if filters:
            self._set_filters_ui({**self._filters_from_ui(), **filters})
            self._push_filters()

        laser = config.get("laser", {})
        if "current_ma" in laser:
            self.current_slider.setValue(int(round(float(laser["current_ma"]) * 100)))
        if "thermistor_ohm" in laser:
            # Config stores ohms; the slider is in centi-degrees.
            self.therm_slider.setValue(
                int(round(
                    thermistor_ohms_to_c(float(laser["thermistor_ohm"])) * 100
                ))
            )

        repump = config.get("repump", {})
        if "vtune_v" in repump:
            # Config stores volts; the slider is in deci-MHz.
            self.vtune_slider.setValue(
                int(round(vtune_to_vco_mhz(float(repump["vtune_v"])) * 10))
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

        # PSD settings live in the noise-analysis window, which may not be
        # open; write them to QSettings so it picks them up when built, and
        # push them into it if it already exists.
        psd = config.get("psd", {})
        if "algorithm" in psd:
            self._settings.setValue("psd/algorithm", int(psd["algorithm"]))
        if "max_decimation" in psd:
            self._settings.setValue(
                "psd/max_decimation", int(psd["max_decimation"])
            )
        if psd and self._psd_window is not None:
            self._psd_window.apply_settings()

        dispenser = config.get("dispenser", {})
        if "fire_voltage_v" in dispenser:
            # Headroom only -- the dispenser runs current-limited -- so it
            # loads like any setpoint; current and time keep the prompt.
            # It was neither saved nor loaded before.
            self.dispenser_voltage_slider.setValue(
                int(round(float(dispenser["fire_voltage_v"]) * 1000))
            )
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
                self._set_dispenser_fire(new_current, new_seconds)

        self._event_log.log(
            "config_loaded",
            path=str(path) if path else None,
            saved_at=config.get("saved_at"),
        )

    # ------------------------------------------------------------------
    # Lock PSD mode
    # ------------------------------------------------------------------

    def _open_psd_window(self):
        """Show linien's noise-analysis window, connecting a worker for it."""
        if self._psd_window is None:
            self._psd_window = PsdWindow(
                self, settings=self._settings, event_log=self._event_log
            )
            self._psd_window.setStyleSheet(self.styleSheet())

        worker = getattr(self, "psd_worker", None)
        # A worker whose connect failed has exited; reusing it queued every
        # Start into a dead thread and nothing happened.
        if worker is None or not worker.isRunning():
            self._psd_prev_status = {}
            self.psd_worker = PsdWorker(host=self.rp_edit.text().strip())
            self.psd_worker.connection_status.connect(self._on_psd_connection)
            self.psd_worker.status.connect(self._on_psd_status)
            self.psd_worker.psd_data.connect(self._on_psd_data)
            self.psd_worker.fault.connect(
                lambda msg: self._log_fault("psd", msg)
            )
            self.psd_worker.start()
        self._psd_window.set_worker(self.psd_worker)
        self._refresh_lock_buttons()  # carries Modulation/Repump to Start
        self._psd_window.set_locked(self._lock_engaged)
        # Push the window's current settings so the server matches the UI.
        self._psd_window._on_algorithm_changed(
            self._psd_window.algo_combo.currentIndex()
        )
        self._psd_window._on_time_changed(
            self._psd_window.time_combo.currentIndex()
        )

        self._psd_window.show()
        apply_dark_titlebar(self._psd_window)
        self._psd_window.raise_()
        self._psd_window.activateWindow()
        self._event_log.log("psd_window", opened=True)

    def _on_psd_connection(self, status):
        self._log_connection("psd", status)

    def _on_psd_status(self, state):
        if self._psd_window is not None:
            self._psd_window.on_status(state)
        previous = self._psd_prev_status
        if previous.get("optimizing") and not state.get("optimizing"):
            self._event_log.log(
                "pid_opt_done", p=state.get("p"), i=state.get("i")
            )
        self._psd_prev_status = state

    def _on_psd_data(self, payload):
        if self._psd_window is not None:
            self._psd_window.psd_data_received(payload)
        if payload.get("complete"):
            fitness = payload.get("fitness")
            self._event_log.log(
                "psd_complete",
                fitness=float(fitness) if fitness is not None else None,
                p=payload.get("p"),
                i=payload.get("i"),
                d=payload.get("d"),
                uuid=payload.get("uuid"),
            )

    def _set_usb_status_label(self, label, status, key=None):
        """The status of one device row. Three outcomes, and only three.

        OK means connected, "..." means an attempt is in flight, and
        everything else is Error. The rows used to carry nine different
        words -- No port, Probing, Verified, Not found, Unplugged, Port
        back, Stalled, Fault -- which is more vocabulary than a glance can
        hold, and most of them were written straight onto the label by
        other methods, bypassing the _device_connected accounting below.
        The reason is not lost: it is the whole status string, and it is on
        the tooltip.
        """
        if status.startswith("connected"):
            text, color = "OK", "#6f9e7c"
        elif status.startswith("connecting"):
            # Amber, not green: an attempt in progress is not a device.
            text, color = "...", "#b9924f"
        else:
            text, color = "Error", "#b56a6a"
        label.setText(text)
        label.setToolTip(status)
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 700; border: none;"
        )
        if key is not None:
            self._device_connected[key] = text == "OK"
            if text == "OK":
                # Connected, so whatever port went missing before is
                # history. Kept, a device back on a different port (COM4
                # -> COM5) flipped to Error at the next USB change while
                # it was running fine.
                self._port_missing.pop(key, None)
            if text != "OK":
                # Its last readback is stale now, so nothing may keep
                # reporting what is on screen as this device's own state.
                for output in self._DEVICE_OUTPUTS.get(key, ()):
                    self._readback_seen.discard(output)
            self._refresh_device_count()

    # Every device with a status row, so the count matches what is listed.
    _DEVICE_KEYS = ("rp", "laser", "mot_coil", "dispenser")

    # Which live outputs each device is the authority for. Used to drop the
    # device-backed claim when that device stops answering.
    # Not "repump": the gate is a separate RPyC connection of its own
    # (LinienGpioWorker), so the acquisition row says nothing about it. Its
    # own status handler drops that claim -- see _start_repump_workers.
    _DEVICE_OUTPUTS = {
        "rp": ("lock", "modulation"),
        "laser": ("laser",),
        "mot_coil": ("coils",),
        "dispenser": ("dispenser",),
    }

    def _refresh_device_count(self):
        """Header count, derived from live connection state.

        It used to be set only by the USB probe, which meant it latched
        whatever startup found -- usually before anything had connected --
        and never revised, so the header could read 1/3 while the rows
        below it all said OK.
        """
        total = len(self._DEVICE_KEYS)
        n_ok = sum(
            1 for key in self._DEVICE_KEYS if self._device_connected.get(key)
        )
        self._set_chip(
            "usb",
            f"DEVICES {n_ok}/{total}",
            "#6f9e7c" if n_ok == total else
            self._CHIP_NEUTRAL if n_ok == 0 else "#b9924f",
        )

    def _show_fault(self, label, device, message, key=None):
        """A worker fault, on its row and in the log.

        Through _set_usb_status_label like everything else: writing the
        label directly left the row red while the DEVICES count still had
        the device as connected.
        """
        self._set_usb_status_label(label, f"error:{message}", key)
        self._log_fault(device, message)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def live_outputs(self):
        """Everything currently energised, newest hazard first.

        Read from the UI's own state rather than by querying devices: the
        close path must not block on a device that has stopped answering,
        which is exactly when you most want to be told what is still on.

        Two callers, and they want different things from ``backed_by``:
        ``closeEvent`` ignores it deliberately -- an output we cannot verify
        still has to be offered for driving safe -- while
        ``_report_startup_outputs`` reports only device-backed entries,
        because announcing a restored setting as an energised output is what
        made a launch with the Red Pitaya unplugged accuse it of dithering.
        Anything added here needs its ``backed_by`` decided on the same
        rule: did a readback say so, or is a control the only evidence?
        """
        def source(key):
            return "device" if key in self._readback_seen else "ui"

        outputs = []
        if self._dispenser_phase == "firing":
            outputs.append(LiveOutput(
                "dispenser", "Dispenser",
                f"firing, {self._dispenser_fire_current:.1f} A",
                mandatory=True,
                # The phase only reaches "firing" through the worker.
                backed_by="device",
            ))
        if self.laser_btn.isChecked():
            outputs.append(LiveOutput(
                "laser", "Laser",
                f"{self.current_slider.value() / 100.0:.2f} mA",
                backed_by=source("laser"),
            ))
        if self._lock_engaged:
            outputs.append(LiveOutput(
                "lock", "Lock", "engaged",
                backed_by=source("lock"),
            ))
        # The slider holds a value even when the dither is switched off --
        # it is the level to restore, not the level being driven, and
        # _on_mod_enabled writes 0 to the device on the way off. Reading
        # the slider alone reported a dither that was not running.
        if self.mod_enable_btn.isChecked() and self.mod_strength_slider.value() > 0:
            outputs.append(LiveOutput(
                "modulation", "Modulation",
                f"{self.mod_strength_slider.value() / 1000.0:.3f} Vpp",
                backed_by=source("modulation"),
            ))
        if self.mot_coil_output_btn.isChecked():
            outputs.append(LiveOutput(
                "coils", "Coils",
                f"{self.mot_coil_current_slider.value() / 1000.0:.1f} A",
                backed_by=source("coils"),
            ))
        if self.repump_gpio_btn.isChecked():
            outputs.append(LiveOutput(
                "repump", "Repump", "on", backed_by=source("repump"),
            ))
        return outputs

    def _drive_outputs_safe(self, keys):
        """Turn off what the operator chose, and let the rest survive us.

        Each worker already drives its own output safe on stop; the flags
        here are how an explicit "leave it on" reaches that teardown.
        """
        lock_worker = self._get_lock_worker()
        laser_worker = getattr(self, "laser_worker", None)

        if "modulation" in keys:
            # A linien parameter, not a worker output, so nothing else
            # would zero it -- it would keep dithering after we exit.
            # Driven through the button rather than straight at the worker:
            # _on_mod_enabled also clears the persisted linien/mod_enabled,
            # and writing 0 without clearing it meant the next launch came
            # up claiming a dither that had been switched off on the way
            # out. It runs with no worker at all, which is the case that
            # left the flag set for days.
            self._event_log.log("modulation_auto_off", reason="app_exit")
            self.mod_enable_btn.setChecked(False)
        if lock_worker is not None:
            lock_worker.unlock_on_stop = "lock" in keys
        if laser_worker is not None:
            laser_worker.disable_on_stop = "laser" in keys
        for key, attr in (
            ("coils", "mot_coil_worker"),
            ("repump", "repump_gpio_worker"),
        ):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.disable_on_stop = key in keys

        self._event_log.log(
            "shutdown_choice",
            turned_off=sorted(keys),
            left_on=sorted(
                o.key for o in self.live_outputs() if o.key not in keys
            ),
        )

    def closeEvent(self, event):
        outputs = self.live_outputs()
        if outputs:
            dialog = ShutdownDialog(self, outputs)
            if dialog.exec() != QtWidgets.QDialog.Accepted:
                self._event_log.user("close_cancelled")
                event.ignore()
                return
            # Mandatory items are not the operator's to leave on.
            keys = dialog.turn_off() | {
                o.key for o in outputs if o.mandatory
            }
        else:
            keys = set()

        # Before the workers go, or a tick lands on a stopped DAC worker.
        self._repump_sweep_timer.stop()
        self._save_ui_state()
        # Before app_stop, so a still-open run of suppressed faults is
        # counted in the record rather than dying with the process.
        self._event_log.flush_repeats()
        self._event_log.log("app_stop")
        self._drive_outputs_safe(keys)

        self.camera_widget.stop()
        self.linien_widget.stop()
        self._stop_hardware_workers()
        watcher = getattr(self, "port_watch_worker", None)
        if watcher is not None:
            watcher.stop()
        probe = getattr(self, "probe_worker", None)
        if probe is not None:
            probe.wait(3000)
        event.accept()



def quiet_console():
    """Keep third-party chatter off the console.

    Qt's multimedia backend announces its FFmpeg build on the first
    camera query. It is not ours and not actionable, but the console is
    where a real traceback shows up, so noise there costs something.

    Nothing is discarded: Qt messages are re-emitted through the event
    log once the window opens (see OpenQuantumControlsWindow), and this
    only lowers what reaches stdout. Must run before QApplication --
    the multimedia backend prints during its own initialisation.

    linien's own chatter is not handled here. It cannot be: linien_client
    installs a console handler and sets its module loggers to DEBUG when
    it is imported, which happens long after this runs, so anything done
    to those loggers now is overwritten. capture_library_logging() does
    it after the import instead.
    """
    os.environ.setdefault(
        "QT_LOGGING_RULES",
        "qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false",
    )
    # FFmpeg's own logger writes at the C level, beneath Qt's categories.
    os.environ.setdefault("QT_FFMPEG_DEBUG", "0")
    os.environ.setdefault("AV_LOG_FORCE_NOCOLOR", "1")


def main():
    import argparse

    quiet_console()

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
    # Re-apply after show: on some Qt/Windows combinations the frame is
    # painted light once before DWM picks the attribute up.
    apply_dark_titlebar(window)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
