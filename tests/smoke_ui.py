"""Offscreen UI smoke test: construct the full UI without touching hardware.

Run: python tests/smoke_ui.py
Refreshes mot-ui-smoke.png. Uses an isolated settings file and log
directory so nothing real is read from hardware or written to the user's
QSettings.
"""

import glob
import json
import os
import re
import sys
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
TMP = tempfile.mkdtemp(prefix="mot-smoke-")
LOG_DIR = os.path.join(TMP, "logs")
INI = os.path.join(TMP, "smoke-settings.ini")


def _records():
    """Every event-log record written so far, oldest first."""
    out = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            out.extend(json.loads(line) for line in f)
    return out

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

import mot_control
import mot_logging
import psd_window

# Keep the smoke test inert: never start hardware workers (they would read
# real saved QSettings ports/hosts on this machine), log to a temp dir,
# and use an isolated settings file for EVERY QSettings in the app.
mot_control.OpenQuantumControlsWindow._deferred_start = lambda self: None


def _fake_port(device, description, manufacturer, serial_number):
    return types.SimpleNamespace(
        device=device,
        description=description,
        manufacturer=manufacturer,
        serial_number=serial_number,
        product=None,
        interface=None,
    )


# Port enumeration is read-only, but it still reads this machine -- stub it
# so the test and its screenshot do not depend on what is plugged in.
# Descriptions are deliberately long: a port dropdown that sizes itself to
# its longest entry drags the whole control column into horizontal
# scrolling, and the width assertion at the end of this file is what
# catches it.
mot_control.discover_serial_ports = lambda: [
    _fake_port("COM3", "USB Serial Port (COM3)", "FTDI", "DK0HOJF9A"),
    _fake_port("COM5", "USB-SERIAL CH340 (COM5)", "wch.cn", None),
    _fake_port("COM7", "Silicon Labs CP210x USB to UART Bridge (COM7)",
               "Silicon Laboratories", "0001EF12"),
]
mot_control.EventLog = lambda _directory: mot_logging.EventLog(LOG_DIR)
_orig_qsettings = QtCore.QSettings


class IniSettings(_orig_qsettings):
    def __init__(self, *args, **kwargs):
        super().__init__(INI, _orig_qsettings.IniFormat)


QtCore.QSettings = IniSettings

app = QtWidgets.QApplication([])

mot_control.load_app_fonts()
app.setFont(QtGui.QFont(mot_control.BODY_FONT_FAMILY))
app.setStyleSheet(mot_control.build_app_stylesheet())
pg.setConfigOptions(
    useOpenGL=False, enableExperimental=False, foreground="#c8c8da"
)

# The bundled family must register (byte-based loading -- the
# path-based Qt API silently fails on this Qt build).
assert "Space Grotesk" in QtGui.QFontDatabase.families()

# Inputs are explicitly themed: spinboxes must not fall back to the
# platform palette (black-on-dark). The outline is a muted grey rather
# than the old near-white, which read as glare against the dark panel.
INPUT_BORDER = "border: 1px solid #404040"
stylesheet = mot_control.build_app_stylesheet()
assert "QAbstractSpinBox {" in stylesheet
assert stylesheet.count(INPUT_BORDER) >= 3  # edit/spin/combo
assert INPUT_BORDER in mot_control.COMBO_STYLE

# Corners are square everywhere -- no rounded panels or controls. The
# per-corner spellings count too: border-top-right-radius on the spinbox
# buttons slipped past a check that only looked for "border-radius".
# Dialogs are top-level windows, so they miss the QMainWindow rule and
# render as a light panel in a dark app unless styled explicitly. Their
# plain QLabels need it too -- the PID dialog's P/I/D captions were
# rendering in the platform's near-black, invisible on the dark panel.
assert "QDialog {" in stylesheet
assert "QDialog QLabel {" in stylesheet

assert "border-radius: 0px" in stylesheet
assert not re.search(r"[\w-]*border[\w-]*-?radius:\s*[1-9]", stylesheet)
for sheet in (mot_control.COMBO_STYLE, mot_control.APP_STYLESHEET):
    assert not re.search(r"[\w-]*border[\w-]*-?radius:\s*[1-9]", sheet)


def resolved_family(widget):
    """The family a widget will ACTUALLY render with (not just the CSS)."""
    widget.ensurePolished()
    return QtGui.QFontInfo(widget.font()).family()


# --- Standalone vendoring: the Linien widget/worker, styles, and the
# Koheron worker come from THIS repo, not a sibling checkout.
import linien_widget  # noqa: E402
import linien_worker  # noqa: E402
import styles  # noqa: E402
from mot_workers import LaserSetpointWorker  # noqa: E402,F401

for module in (linien_widget, linien_worker, styles, mot_control):
    module_dir = os.path.dirname(os.path.abspath(module.__file__))
    assert module_dir == SRC, (module.__name__, module.__file__)
assert not hasattr(mot_control, "EIT_CONTROLS")

# --- Frozen-build paths: resource_path honors PyInstaller's _MEIPASS...
assert mot_control.FONTS_DIR == os.path.join(ROOT, "fonts")
assert os.path.isdir(mot_control.FONTS_DIR)
sys._MEIPASS = os.path.join(TMP, "bundle")
try:
    assert mot_control.resource_path("fonts") == os.path.join(
        TMP, "bundle", "fonts"
    )
finally:
    del sys._MEIPASS
assert mot_control.resource_path("fonts") == os.path.join(ROOT, "fonts")

# ...and a frozen build writes logs to the per-user data dir instead of
# next to the (read-only) executable.
assert mot_control.logs_dir() == os.path.join(ROOT, "logs")
sys.frozen = True
try:
    frozen_logs = mot_control.logs_dir()
finally:
    del sys.frozen
assert frozen_logs != os.path.join(ROOT, "logs")
appdata = QtCore.QStandardPaths.writableLocation(
    QtCore.QStandardPaths.AppDataLocation
)
assert frozen_logs == os.path.join(appdata, "logs"), frozen_logs


class _FakeMouse:
    """Minimal stand-in for a QMouseEvent at a given data-space x."""

    def __init__(self, widget, data_x):
        point = widget.view_box.mapViewToScene(QtCore.QPointF(data_x, 0.0))
        self._pos = widget.pg_widget.mapFromScene(point)

    def pos(self):
        return self._pos


try:
    window = mot_control.OpenQuantumControlsWindow()
    window.show()
    app.processEvents()

    # Master actions, status rows, and GPIO config are gone.
    for gone in (
        "mot_ready_btn",
        "all_off_btn",
        "vtune_status",
        "mot_coil_status",
        "dispenser_status",
        "repump_gpio_status",
        "repump_gpio_bank_combo",
        "master_state_label",
    ):
        assert not hasattr(window, gone), gone

    # USB card is collapsible and collapsed by default.
    assert not window.usb_card.content.isVisibleTo(window.usb_card)
    # Manual setup is nested inside and starts collapsed: the common case
    # is four status rows and one button, not a form.
    assert not window.manual_setup_card.content.isVisibleTo(
        window.manual_setup_card
    )
    window.manual_setup_card.set_expanded(True)
    assert window.manual_setup_card.content.isVisibleTo(
        window.manual_setup_card
    )
    window.manual_setup_card.set_expanded(False)

    # A port dropdown must not size itself to its contents: a long
    # description would demand its own width and drag the whole control
    # column into horizontal scrolling. Tested as a property rather than a
    # pixel threshold so it holds whatever the font is.
    probe = mot_control.PortSelect("")
    unloaded = probe.sizeHint().width()
    probe.set_available(
        [("COM9", "COM9 " + "x" * 200, "pathological description")]
    )
    assert probe.sizeHint().width() <= unloaded + 40, (
        unloaded,
        probe.sizeHint().width(),
    )
    # Long detail belongs in the tooltip, not the visible label.
    assert len(window.laser_edit.currentText()) < 40, (
        window.laser_edit.currentText()
    )

    window.usb_card.set_expanded(True)
    app.processEvents()
    assert window.usb_card.content.isVisibleTo(window.usb_card)
    window.usb_card.set_expanded(False)
    app.processEvents()
    assert not window.usb_card.content.isVisibleTo(window.usb_card)

    # Log pane exists, collapsed by default.
    assert not window.log_card.content.isVisibleTo(window.log_card)

    # Repump card sits above the power-supply card.
    assert window.repump_card.y() < window.coils_card.y()
    assert window.coils_card.y() < window.dispenser_card.y()

    # The header carries the logo asset, not text. A null pixmap means the
    # asset failed to load and the header silently fell back to a wordmark.
    assert window.title_label.text() == ""
    logo = window.title_label.pixmap()
    assert logo is not None and not logo.isNull()
    assert logo.height() >= mot_control.LOGO_HEIGHT_PX
    assert logo.width() > logo.height()  # horizontal lockup

    # One family throughout: Space Grotesk for headings and body alike â€”
    # verified via the resolved font, not the stylesheet text.
    assert resolved_family(window.usb_card.toggle_btn) == "Space Grotesk"
    section = mot_control.make_section_title("PROBE")
    section.show()
    app.processEvents()
    assert resolved_family(section) == "Space Grotesk"
    assert resolved_family(window.laser_btn) == "Space Grotesk"
    assert resolved_family(window.dispenser_fire_btn) == "Space Grotesk"

    # Unit suffixes on the previously unitless fields.
    assert window.mod_freq_edit.text().endswith(" MHz")
    assert window.mod_phase_edit.text().endswith("\N{DEGREE SIGN}")
    assert window.therm_edit.text().endswith(" \N{DEGREE SIGN}C")

    # Every slider value edit must fit its own extreme values in the
    # current font (the proportional body font clipped "130 mA" once).
    for edit in (
        window.mod_freq_edit,
        window.mod_phase_edit,
        window.mod_strength_edit,
        window.current_edit,
        window.therm_edit,
        window.mot_coil_voltage_edit,
        window.mot_coil_current_edit,
        window.vtune_edit,
    ):
        fm = edit.fontMetrics()
        for val in (edit.min_val, edit.max_val):
            if edit.decimals > 0:
                text = f"{val / edit.scale:.{edit.decimals}f}{edit.suffix}"
            else:
                text = f"{val}{edit.suffix}"
            needed = fm.horizontalAdvance(text) + 20  # padding + border
            assert needed <= edit.width(), (text, needed, edit.width())

    # Laser slider ranges: current 50-100 mA in 0.1 mA steps, temperature
    # 16-38 C in 0.01 C steps. Both read in physical units, so the slider
    # counts are the physical range times the scale.
    assert window.current_slider.minimum() == 5000
    assert window.current_slider.maximum() == 10000
    assert (window.current_edit.decimals, window.current_edit.scale) == (2, 100.0)
    # Current and Temperature step the same amount per arrow press.
    assert window.current_slider.singleStep() == window.therm_slider.singleStep()
    assert window.therm_slider.minimum() == 1600
    assert window.therm_slider.maximum() == 3800
    assert (window.therm_edit.decimals, window.therm_edit.scale) == (2, 100.0)

    # Supply setpoints read to 0.1 V / 0.1 A, and the slider steps match
    # that so a single arrow-key press moves the displayed number.
    for edit, slider in (
        (window.mot_coil_voltage_edit, window.mot_coil_voltage_slider),
        (window.mot_coil_current_edit, window.mot_coil_current_slider),
        (window.dispenser_voltage_edit, window.dispenser_voltage_slider),
        (window.dispenser_current_edit, window.dispenser_current_slider),
    ):
        assert edit.decimals == 1, edit.text()
        assert slider.singleStep() == 100, (edit.text(), slider.singleStep())

    # Typed entry: the value boxes now drive the sliders (with clamping,
    # suffix tolerance, and garbage rejection). Unlocked first: a locked
    # box commits nothing (it wrote to the Koheron on focus-out once).
    window.laser_unlock_box.setChecked(True)
    window.current_slider.setValue(9000)
    window.current_edit.setText("95")
    window.current_edit.editingFinished.emit()
    assert window.current_slider.value() == 9500
    assert window.current_edit.text() == "95.00 mA"
    # Both new decimal places have to survive a round trip through the box.
    window.current_edit.setText("87.42 mA")
    window.current_edit.editingFinished.emit()
    assert window.current_slider.value() == 8742
    assert window.current_edit.text() == "87.42 mA"
    window.current_edit.setText("999 mA")
    window.current_edit.editingFinished.emit()
    assert window.current_slider.value() == 10000  # clamped to slider max
    assert window.current_edit.text() == "100.00 mA"
    window.laser_unlock_box.setChecked(False)
    window._apply_mod_enabled(True)  # UI only: the boxes are editable
    window.mod_freq_edit.setText("17.5 MHz")
    window.mod_freq_edit.editingFinished.emit()
    assert window.mod_freq_slider.value() == 17500
    assert window.mod_freq_edit.text() == "17.500 MHz"
    window._apply_mod_enabled(False)
    window.vtune_slider.setValue(28500)
    window.vtune_edit.setText("garbage")
    window.vtune_edit.editingFinished.emit()
    assert window.vtune_slider.value() == 28500  # unchanged
    assert window.vtune_edit.text() == "2850.0 MHz"  # display restored

    # A held key must not outrun the repump DAC: auto-repeats inside the
    # limiter's interval are dropped, a fresh press always steps.
    def vtune_key(repeat):
        QtWidgets.QApplication.sendEvent(window.vtune_edit, QtGui.QKeyEvent(
            QtCore.QEvent.KeyPress, QtCore.Qt.Key_Up,
            QtCore.Qt.NoModifier, "", repeat))
    vtune_key(False)
    for _ in range(10):
        vtune_key(True)
    assert window.vtune_slider.value() == 28501, window.vtune_slider.value()
    vtune_key(False)
    assert window.vtune_slider.value() == 28502, window.vtune_slider.value()

    # Wheel guard: a wheel event over an unfocused slider must not change
    # the value (it used to â€” accidental hardware actuation).
    before = window.current_slider.value()
    assert not window.current_slider.hasFocus()
    wheel = QtGui.QWheelEvent(
        QtCore.QPointF(10, 5),
        QtCore.QPointF(100, 100),
        QtCore.QPoint(0, 0),
        QtCore.QPoint(0, 120),
        QtCore.Qt.NoButton,
        QtCore.Qt.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QtWidgets.QApplication.sendEvent(window.current_slider, wheel)
    assert window.current_slider.value() == before

    # ...but scrolling the column itself still works (guard must exempt
    # scrollbars â€” including them once caused infinite recursion).
    scroll_area = window.findChild(QtWidgets.QScrollArea)
    vbar = scroll_area.verticalScrollBar()
    # Cards open collapsed, so expand enough of them to overflow the
    # viewport -- otherwise there is nothing to scroll and the guard below
    # would pass vacuously.
    for card in (
        window.lock_card,
        window.coils_card,
        window.dispenser_card,
        window.repump_card,
    ):
        card.set_expanded(True)
    app.processEvents()
    assert vbar.maximum() > 0  # column is taller than the viewport
    vbar.setValue(0)
    wheel_down = QtGui.QWheelEvent(
        QtCore.QPointF(10, 10),
        QtCore.QPointF(100, 100),
        QtCore.QPoint(0, 0),
        QtCore.QPoint(0, -120),
        QtCore.Qt.NoButton,
        QtCore.Qt.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QtWidgets.QApplication.sendEvent(scroll_area.viewport(), wheel_down)
    assert vbar.value() > 0

    # Health chips: lock, USB, coils, dispenser.
    window._on_lock_state(True)
    assert window.chip_text("lock") == "LOCKED"
    window._on_lock_state(False)
    # Unlocked, the chip names the modulation: it decides whether a lock
    # can start. Only lock and modulation ever appear here.
    assert window.chip_text("lock") == "UNLOCKED · MODULATION OFF"
    window._apply_mod_enabled(True)
    window.mod_enable_btn.blockSignals(True)
    window.mod_enable_btn.setChecked(True)
    window.mod_enable_btn.blockSignals(False)
    window._show_lock_chip()
    assert window.chip_text("lock") == "UNLOCKED · MODULATION ON"
    window.mod_enable_btn.blockSignals(True)
    window.mod_enable_btn.setChecked(False)
    window.mod_enable_btn.blockSignals(False)
    window._apply_mod_enabled(False)
    assert window.chip_text("lock") == "UNLOCKED · MODULATION OFF"

    # The laser setpoint is a temperature; the ohms the Koheron wants are
    # derived from it and are what gets persisted.
    window.therm_slider.setValue(3000)  # move off the default first
    window.therm_slider.setValue(2500)  # 25.00 C
    assert window.therm_edit.text() == "25.00 \N{DEGREE SIGN}C"
    saved_ohms = int(window._settings.value("laser/thermistor_ohm"))
    assert abs(mot_control.thermistor_ohms_to_c(saved_ohms) - 25.0) < 0.05
    # The two conversions must agree across the slider's range.
    for celsius in (16.0, 25.0, 42.0):
        ohms = mot_control.thermistor_c_to_ohms(celsius)
        assert abs(mot_control.thermistor_ohms_to_c(ohms) - celsius) < 0.01

    # Dispenser fire-state UI: button, label, chip, and progress bar.
    window._on_dispenser_fire_state({"phase": "firing", "remaining_s": 15.0})
    assert "Abort Fire" in window.dispenser_fire_btn.text()
    assert window.chip_text("disp") == "DISPENSER FIRING"
    assert window.dispenser_progress.isVisibleTo(window)
    assert window.dispenser_progress.value() == 500  # 15 s of 30 s left
    # No cooldown since 2026-10-16: a fire ends straight back at idle,
    # re-armed, with nothing blocking the next one.
    window._on_dispenser_fire_state({"phase": "idle", "remaining_s": 0.0})
    assert window.dispenser_fire_btn.isEnabled()
    assert "Fire Dispenser" in window.dispenser_fire_btn.text()
    assert not window.dispenser_progress.isVisibleTo(window)
    window._on_dispenser_fire_state({"phase": "idle", "remaining_s": 0.0})
    assert window.dispenser_fire_btn.isEnabled()
    assert "Fire Dispenser (30 s)" == window.dispenser_fire_btn.text()
    assert not window.dispenser_progress.isVisibleTo(window)
    assert window.chip_text("disp") == "DISPENSER IDLE"

    # Arrow keys in a value box nudge it by one displayed decimal. The
    # boxes are QLineEdits, so without this arrows only moved the text
    # cursor -- you would click into a number, press Up, and nothing.
    def _key(widget, key):
        QtWidgets.QApplication.sendEvent(
            widget,
            QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key, QtCore.Qt.NoModifier),
        )

    # The clamp check above left the current slider pinned at its maximum,
    # where Up cannot move it.
    window.current_slider.setValue(8000)
    # Locked setpoints ignore the keys: PageUp in the locked Current box
    # once wrote 1 mA to a running diode (setValue works on a disabled
    # slider).
    assert not window.laser_unlock_box.isChecked()
    # Clicking away from a locked box (editingFinished) must not write
    # either: on the bench it sent current and 9077 ohm to the Koheron.
    sent = []
    window.laser_worker = types.SimpleNamespace(
        set_current=lambda ma: sent.append(("current", ma)),
        set_thermistor=lambda ohms: sent.append(("therm", ohms)),
    )
    window.current_edit.editingFinished.emit()
    window.therm_edit.editingFinished.emit()
    window.laser_worker = None
    assert sent == [], sent
    for key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_PageUp):
        _key(window.current_edit, key)
        _key(window.therm_edit, key)
    assert window.current_slider.value() == 8000
    therm_locked = window.therm_slider.value()
    _key(window.therm_edit, QtCore.Qt.Key_PageUp)
    assert window.therm_slider.value() == therm_locked
    # Same for the dither boxes while Modulation is off: Up there pushed
    # the amplitude to the RP.
    assert not window.mod_enable_btn.isChecked()
    strength = window.mod_strength_slider.value()
    _key(window.mod_strength_edit, QtCore.Qt.Key_Up)
    assert window.mod_strength_slider.value() == strength
    window.laser_unlock_box.setChecked(True)
    for slider, edit in (
        (window.current_slider, window.current_edit),          # 0.01 mA
        (window.therm_slider, window.therm_edit),              # 0.01 C
        (window.mot_coil_current_slider, window.mot_coil_current_edit),  # 0.1 A
    ):
        start = slider.value()
        _key(edit, QtCore.Qt.Key_Up)
        assert slider.value() == start + slider.singleStep(), (
            edit.text(), start, slider.value()
        )
        _key(edit, QtCore.Qt.Key_Down)
        assert slider.value() == start
        # One arrow press must move the *visible* number, not a digit
        # below what is shown.
        before = edit.text()
        _key(edit, QtCore.Qt.Key_Up)
        assert edit.text() != before, (before, edit.text())
        _key(edit, QtCore.Qt.Key_Down)

    # PageUp uses the coarser page step.
    start = window.therm_slider.value()
    _key(window.therm_edit, QtCore.Qt.Key_PageUp)
    assert window.therm_slider.value() == start + window.therm_slider.pageStep()
    window.therm_slider.setValue(start)
    window.laser_unlock_box.setChecked(False)

    assert window.laser_card.toggle_btn.text() == "LASER", (
        window.laser_card.toggle_btn.text()
    )

    # A readback must not overwrite a button the user just pressed. The
    # write takes a poll or two to land, so a naive sync snaps the button
    # back and then forward again -- the click reads as a lagging flicker.
    window.mot_coil_output_btn.setChecked(True)
    stale = {"v_out": 0.0, "i_out": 0.0, "power": 0.0, "output_enabled": False,
             "cv_cc": 0, "protection": 0}
    window._on_mot_coil_readback(stale)
    assert window.mot_coil_output_btn.isChecked(), "stale readback won"
    # Once the supply agrees, the readback drives the button again.
    window._on_mot_coil_readback(dict(stale, output_enabled=True, i_out=1.0))
    assert window.mot_coil_output_btn.isChecked()
    # The card shows what the supply reports, not just the setpoints.
    assert window.coil_readback_label.text().startswith(
        "Supply: ON · 0.00 V · 1.00 A"
    ), window.coil_readback_label.text()
    window._on_mot_coil_readback(stale)
    assert not window.mot_coil_output_btn.isChecked(), (
        "readback ignored after the write settled"
    )
    # A write that never lands must surface, not be hidden forever.
    window.mot_coil_output_btn.setChecked(True)
    window._pending_output["coils"] = (True, 0.0)  # deadline already passed
    window._on_mot_coil_readback(stale)
    assert not window.mot_coil_output_btn.isChecked()

    # No readback rows on the supply cards: a DPS protection trip has to
    # reach the header chip and the event log instead.
    window._on_mot_coil_readback(
        {"v_out": 0.0, "i_out": 0.0, "power": 0.0, "output_enabled": False,
         "cv_cc": 0, "protection": 2}
    )
    assert window.chip_text("coils") == "COILS TRIP"
    assert "OCP" in window.log_view.toPlainText()
    window._on_mot_coil_readback(
        {"v_out": 5.0, "i_out": 1.0, "power": 5.0, "output_enabled": True,
         "cv_cc": 0, "protection": 0}
    )
    assert window.chip_text("coils") == "COILS 1.0 A"

    # Three words, and only three: OK when connected, "..." while an
    # attempt is in flight, Error for everything else. Nine different words
    # across four rows was more vocabulary than a glance can hold, and the
    # reason still reaches the operator through the tooltip. The 84 px
    # column must also never clip (it once showed "Not configured" as
    # "t configured").
    for status, expected in (
        ("no_port", "Error"),
        ("no_host", "Error"),
        ("connecting:probing the USB ports", "..."),
        ("connected:/dev/cu.usbserial-14230@1", "OK"),
        ("error:something long and detailed", "Error"),
        ("anything else at all", "Error"),
    ):
        window._set_usb_status_label(window.coil_usb_status, status)
        assert window.coil_usb_status.text() == expected
        fm = window.coil_usb_status.fontMetrics()
        assert fm.horizontalAdvance(window.coil_usb_status.text()) <= 84
        assert window.coil_usb_status.toolTip() == status  # detail on hover

    # Fire history still accumulates in settings (no longer displayed).
    window._on_dispenser_fire_summary(
        {"elapsed_s": 12.0, "planned_s": 30.0, "current_a": 7.5, "aborted": True}
    )
    window._on_dispenser_fire_summary(
        {"elapsed_s": 30.0, "planned_s": 30.0, "current_a": 7.5, "aborted": False}
    )
    assert window._int_setting("dispenser/total_fires", 0) == 2
    assert window._float_setting("dispenser/total_fire_s", 0.0) == 42.0

    # The repump slider is in MHz; a DAC readback in volts is converted
    # once per connect to adopt the hardware's tune point.
    window._vtune_slider_synced = False
    window._on_vtune_readback(3.3)
    expected = round(mot_control.vtune_to_vco_mhz(3.3) * 10)
    assert window.vtune_slider.value() == expected
    assert window.vtune_edit.text().endswith(" MHz")
    window._on_vtune_readback(5.0)
    assert window.vtune_slider.value() == expected  # later readbacks ignored

    # Repump button wording (no "Gate"), mirrored in the card header.
    assert window.repump_gpio_btn.text() == "Repump Off"
    assert window.chip_text("repump") == "REPUMP OFF"
    window._on_repump_gpio_state(True)
    assert window.repump_gpio_btn.text() == "Repump On"
    assert window.chip_text("repump") == "REPUMP ON"
    window._on_repump_gpio_state(False)
    assert window.repump_gpio_btn.text() == "Repump Off"

    # A device error surfaces as a fault, not only as a connection note:
    # the Koheron has no status row of its own any more, so the log is
    # where it has to show up.
    window._log_connection("laser", "error:could not open port")
    log_text = window.log_view.toPlainText()
    assert "fault" in log_text
    assert "could not open port" in log_text

    # The repump gate and the dither both follow the laser off -- they are
    # meaningless without it. With a live gate client, so this tests the
    # follow-off rule rather than the no-link path below.
    class _StubGate:
        def __init__(self):
            self.writes = []

        def isRunning(self):
            return True

        def set_output(self, enabled):
            self.writes.append(bool(enabled))

    window.repump_gpio_worker = _StubGate()
    window.laser_btn.setChecked(True)
    window.repump_gpio_btn.setChecked(True)
    window.mod_enable_btn.setChecked(True)
    assert window.repump_gpio_btn.isChecked()
    assert window.mod_enable_btn.isChecked()
    window.laser_btn.setChecked(False)
    assert not window.repump_gpio_btn.isChecked()
    assert not window.mod_enable_btn.isChecked()
    assert window.chip_text("repump") == "REPUMP OFF"
    assert window.repump_gpio_worker.writes == [True, False], (
        window.repump_gpio_worker.writes
    )
    assert window.mod_enable_btn.text() == "Modulation Off"
    # ...but neither follows it back on: that stays a deliberate act.
    window.laser_btn.setChecked(True)
    assert not window.repump_gpio_btn.isChecked()
    assert not window.mod_enable_btn.isChecked()
    window.laser_btn.setChecked(False)
    window.repump_gpio_worker = None

    # --- Autolock line selection ---
    # The bug this replaced: each selection added marker lines that were
    # never removed, so they piled up on the plot across lock/unlock. The
    # overlays are now created once and shown/hidden, so the plot's item
    # count must be identical before and after any number of selections.
    lw = window.linien_widget
    baseline = len(lw.plot.items)

    assert not lw.selection_running
    assert lw.lock_button.isVisibleTo(lw.lock_controls)
    assert not lw.abort_button.isVisibleTo(lw.lock_controls)

    lw.start_selection()
    assert lw.selection_running
    # The trace keeps running until a drag actually starts -- pausing for
    # the whole of selection mode is what left the spectrum frozen after
    # a click too short to count as a selection.
    assert not lw._plot_paused
    assert not lw.lock_button.isVisibleTo(lw.lock_controls)
    assert lw.abort_button.isVisibleTo(lw.lock_controls)
    assert all(o.isVisible() for o in lw.boundary_overlays)

    lw.abort_selection()
    assert not lw.selection_running
    assert not lw._plot_paused
    assert not lw.overlay.isVisible()
    assert not any(o.isVisible() for o in lw.boundary_overlays)
    assert len(lw.plot.items) == baseline

    # A full press/drag/release cycle, repeated: still no accumulation.
    for _ in range(3):
        lw.start_selection()
        lo, hi = lw.selection_boundaries
        lw._touch_start = lo
        lw.overlay.setRegion((lo, hi))
        lw.overlay.setVisible(True)
        lw._disable_area_selection()
        lw._resume_plot()
        assert not lw.overlay.isVisible()
        assert len(lw.plot.items) == baseline

    # A click, or a drag too short to count: the plot must come back.
    # This left the spectrum frozen with the lock buttons stuck in
    # selection mode and no way out but a restart.
    lw.start_selection()
    lo, hi = lw.selection_boundaries
    lw._pause_plot()
    lw._touch_start = lo
    lw._on_selection_release(_FakeMouse(lw, lo))  # released where it started
    assert not lw._plot_paused, "a click left the spectrum frozen"
    assert lw.selection_running, "selection should stay armed for a real drag"
    lw.abort_selection()
    assert not lw._plot_paused

    # A press whose release never arrives (cursor leaves the widget) must
    # not strand the trace either.
    lw._pause_plot()
    lw._touch_start = None
    lw._update_plot({
        "voltages": np.linspace(-1, 1, 8),
        "error_signal": np.zeros(8),
        "monitor_signal": np.zeros(8),
    })
    assert not lw._plot_paused, "orphaned pause never cleared"

    # A drag across the whole visible trace selects the whole spectrum,
    # whatever the sweep span. It was mapped against the slider's fixed
    # ±1 V, so at ±0.1 V it picked samples ~922-1125 of 2048.
    locks = []
    lw.linien_thread = types.SimpleNamespace(
        lock_laser=lambda i0, i1, n: locks.append((i0, i1, n))
    )
    lw._update_plot({
        "voltages": np.linspace(-0.1, 0.1, 2048),
        "error_signal": np.zeros(2048),
        "monitor_signal": np.zeros(2048),
    })
    lw._start_autolock(-0.1, 0.1)
    lw.linien_thread = None
    assert locks == [(0, 2047, 2048)], locks

    # A running lock action holds the controls it reads and restores in
    # its `finally`; a mid-probe Sweep Center nudge was silently undone.
    held = (lw.sweep_center_spin, lw.xrange_slider,
            window.sweep_speed_combo, window.load_config_btn)
    assert all(w.isEnabled() for w in held)
    phase_was_editable = not window.mod_phase_edit.isReadOnly()
    lw._on_action_busy("Lock at center")
    assert not any(w.isEnabled() for w in held)
    assert window.mod_phase_edit.isReadOnly()
    lw._on_action_busy("")
    assert all(w.isEnabled() for w in held)
    assert (not window.mod_phase_edit.isReadOnly()) == phase_was_editable

    # No lock, phase optimisation or target line without Modulation and
    # Repump both on: on the bench Optimize phase with the modulation off
    # rewrote Mod Phase from noise.
    def _set_ui_only(button, on, apply):
        button.blockSignals(True)
        button.setChecked(on)
        button.blockSignals(False)
        apply(on)

    lock_starts = (lw.lock_button, lw.center_lock_button, lw.phase_button)
    speed_was = window.sweep_speed_combo.currentIndex()
    window.sweep_speed_combo.setCurrentIndex(16)  # held
    _set_ui_only(window.mod_enable_btn, False, window._apply_mod_enabled)
    _set_ui_only(window.repump_gpio_btn, False, window._set_repump_ui)
    # Not connected comes first: a click could only be refused.
    rp_status_was = window._rp_last_status
    window._rp_last_status = "disconnected"
    window._refresh_lock_buttons()
    assert not any(b.isEnabled() for b in lock_starts)
    assert lw.lock_message.text() == "Connect the Red Pitaya to lock."
    window._rp_last_status = "connected:rp-test.local"
    window._refresh_lock_buttons()
    assert not any(b.isEnabled() for b in lock_starts)
    assert lw.lock_message.text() == "Switch on Modulation and Repump to lock."
    _set_ui_only(window.mod_enable_btn, True, window._apply_mod_enabled)
    assert not any(b.isEnabled() for b in lock_starts)
    assert lw.lock_message.text() == "Switch on Repump to lock."
    _set_ui_only(window.repump_gpio_btn, True, window._set_repump_ui)
    assert all(b.isEnabled() for b in lock_starts)
    assert lw.lock_message.text() == ""
    # ...and a running action still holds them, then hands back to the rule.
    lw._on_action_busy("Optimizing phase")
    assert not any(b.isEnabled() for b in lock_starts)
    lw._on_action_busy("")
    assert all(b.isEnabled() for b in lock_starts)
    _set_ui_only(window.mod_enable_btn, False, window._apply_mod_enabled)
    _set_ui_only(window.repump_gpio_btn, False, window._set_repump_ui)
    assert not lw.center_lock_button.isEnabled()
    window.sweep_speed_combo.setCurrentIndex(speed_was)
    window._rp_last_status = rp_status_was
    window._refresh_lock_buttons()

    # --- Lock snapshot ---
    # While locked the arrays are a time trace, so the last sweeping frame
    # is held behind them; the flat live traces crossing it are the lock
    # point. No marker (see _update_lockpoint). Items are created once and
    # shown/hidden, so the item count must not move.
    lp_baseline = len(lw.plot.items)
    sweep_v = np.linspace(-1, 1, 64)
    sweep_err = np.linspace(-0.02, 0.02, 64)
    sweep_mon = np.full(64, 0.07)
    lw._update_plot({
        "voltages": sweep_v, "error_signal": sweep_err,
        "monitor_signal": sweep_mon, "locked": False,
        "control_mean": None, "combined_offset_v": 0.0,
    })
    assert lw._last_sweep is not None
    assert not lw.frozen_error_curve.isVisible()

    # The autolock rewrites combined_offset on engaging (autolock.py:235),
    # after the frozen sweep was taken; the snapshot has to follow it or
    # it shows a zero crossing the PID no longer sees.
    lw._update_plot({
        "voltages": sweep_v, "error_signal": np.full(64, 0.001),
        "monitor_signal": np.full(64, 0.055), "locked": True,
        "control_mean": -0.25, "combined_offset_v": 0.1,
    })
    assert lw.frozen_error_curve.isVisible()
    assert lw.frozen_monitor_curve.isVisible()
    assert np.allclose(lw.frozen_error_curve.yData, sweep_err + 0.1)
    assert np.allclose(lw.frozen_monitor_curve.yData, sweep_mon)
    assert len(lw.plot.items) == lp_baseline
    lock_records = [r for r in _records() if r.get("event") == "lock_point"]
    assert len(lock_records) == 1, lock_records
    assert abs(lock_records[0]["lock_v"] - (-0.25)) < 1e-6
    assert abs(lock_records[0]["offset_shift_v"] - 0.1) < 1e-6

    # Unlocking clears it, and a second lock logs a second record.
    lw._update_plot({
        "voltages": sweep_v, "error_signal": sweep_err,
        "monitor_signal": sweep_mon, "locked": False,
        "control_mean": None, "combined_offset_v": 0.1,
    })
    assert not lw.frozen_error_curve.isVisible()
    lw._update_plot({
        "voltages": sweep_v, "error_signal": np.full(64, 0.001),
        "monitor_signal": np.full(64, 0.055), "locked": True,
        "control_mean": 0.4, "combined_offset_v": 0.1,
    })
    assert len([r for r in _records() if r.get("event") == "lock_point"]) == 2

    # Leave the plot as the orphaned-pause check above left it, so this
    # block does not quietly redraw the reference screenshot.
    lw._update_plot({
        "voltages": np.linspace(-1, 1, 8),
        "error_signal": np.zeros(8),
        "monitor_signal": np.zeros(8),
    })
    assert not lw.frozen_error_curve.isVisible()

    # --- Laser setpoints survive a power cycle ---
    # A power-cycled CTL200 comes back at 0 mA / 9999 ohm. Adopting that
    # threw the operating point away every time, so when the diode is dark
    # the saved setpoints go to the device instead (reversing d441b65 for
    # this one case, 2026-10-16). Never while the output is on.
    pushed = []
    window.laser_worker = types.SimpleNamespace(
        set_current=lambda ma: pushed.append(("current", ma)),
        set_thermistor=lambda ohms: pushed.append(("therm", ohms)),
    )
    window.laser_unlock_box.setChecked(True)
    window.current_slider.setValue(8700)   # 87.00 mA
    window.therm_slider.setValue(2719)     # 27.19 C
    saved_therm = int(window._settings.value("laser/thermistor_ohm"))
    pushed.clear()  # the slider moves above pushed too; only the restore counts

    window._on_laser_init({"current": 0.0, "thermistor": 9999, "output_on": False})
    assert pushed == [("current", 87.0), ("therm", saved_therm)], pushed
    # The device's 0 mA must not land in the UI: that is the value being
    # overridden, not adopted.
    assert window.current_slider.value() == 8700, window.current_slider.value()
    assert window.therm_slider.value() == 2719, window.therm_slider.value()
    assert any(r.get("event") == "laser_setpoints_restored" for r in _records())

    # Laser already running: adopt, never write. Pushing current into a
    # live diode unasked is exactly what this must not do.
    pushed.clear()
    window._on_laser_init({"current": 62.5, "thermistor": 9999, "output_on": True})
    assert pushed == [], pushed
    assert window.current_slider.value() == 6250, window.current_slider.value()
    # The button shows the lit diode (the fake has no laser_on, so a
    # write-back would raise), and shutdown therefore offers it. It used
    # to stay "Laser Off" and the close dialog left the laser on unasked.
    assert window.laser_btn.isChecked()
    assert window.chip_text("laser") == "LASER ON", window.chip_text("laser")
    assert "laser" in [o.key for o in window.live_outputs()]

    # An unreadable output state counts as on, so silence is never taken
    # for permission to write.
    window.current_slider.setValue(8700)
    pushed.clear()  # that slider move pushed too
    window._on_laser_init({"current": 73.0, "thermistor": 9999})
    assert pushed == [], pushed
    assert window.current_slider.value() == 7300, window.current_slider.value()

    window._on_laser_init({"current": 73.0, "thermistor": 9999, "output_on": False})
    assert not window.laser_btn.isChecked()
    assert window.chip_text("laser") == "LASER OFF", window.chip_text("laser")
    # Laser off means Modulation and Repump off: a modulation found on is
    # switched off, and neither button can be switched on meanwhile.
    window.mod_enable_btn.blockSignals(True)
    window.mod_enable_btn.setChecked(True)
    window.mod_enable_btn.blockSignals(False)
    window._apply_laser_off_rule()
    assert not window.mod_enable_btn.isChecked()
    assert not window.mod_enable_btn.isEnabled()
    assert not window.repump_gpio_btn.isEnabled()
    assert any(r.get("event") == "modulation_auto_off" for r in _records())
    # Back to "laser not reported", where nothing is enforced.
    window._readback_seen.discard("laser")
    window._apply_laser_off_rule()
    assert window.mod_enable_btn.isEnabled()
    assert window.repump_gpio_btn.isEnabled()

    window.laser_worker = None
    window.current_slider.setValue(8700)
    window.laser_unlock_box.setChecked(False)

    # --- Sweep center: slide the window without rescaling it ---
    # The range slider sets center and span together; this box moves only
    # the center, to 3 decimals, as linien's sweepCenterSpinBox does.
    spin = lw.sweep_center_spin
    assert spin.decimals() == 3, spin.decimals()
    assert (spin.minimum(), spin.maximum()) == (-1.0, 1.0)
    # One click is one displayed decimal place, not linien's 0.1 V.
    assert spin.singleStep() == 0.001, spin.singleStep()
    moved = []
    lw.linien_thread = types.SimpleNamespace(
        request_sweep_change=lambda c, a: moved.append((round(c, 4), round(a, 4)))
    )
    lw.xrange_slider.setValue((-400, 200))   # center -0.1, amplitude 0.3
    assert abs(spin.value() - (-0.1)) < 1e-9, spin.value()
    moved.clear()
    spin.setValue(0.25)
    # Span preserved, center moved: that is the whole point of the box.
    assert moved == [(0.25, 0.3)], moved
    assert lw.xrange_slider.value() == (-50, 550), lw.xrange_slider.value()
    # A center that would push an edge past the sweep bounds is clamped, so
    # the plot cannot be asked to follow somewhere the sweep never goes.
    moved.clear()
    spin.setValue(0.95)
    assert moved == [(0.7, 0.3)], moved
    assert abs(spin.value() - 0.7) < 1e-9, spin.value()
    # Dragging the slider updates the box without echoing back out.
    moved.clear()
    lw.xrange_slider.setValue((-1000, 0))
    assert abs(spin.value() - (-0.5)) < 1e-9, spin.value()
    assert moved == [(-0.5, 0.5)], moved
    lw.linien_thread = None
    lw.xrange_slider.setValue((-1000, 1000))

    # --- Sweep speed: the slowest entry is a hold, not a speed ---
    # 0 Hz is linien's sweep_pause parameter, so it must not be sent down
    # the speed path -- sweep_speed only takes 0-15, and 0 there is the
    # *fastest* sweep.
    sweep_calls = []
    window.linien_widget.linien_thread = types.SimpleNamespace(
        set_sweep_speed=lambda v: sweep_calls.append(("speed", v)),
        set_sweep_pause=lambda v: sweep_calls.append(("pause", v)),
    )
    hold_index = window.sweep_speed_combo.count() - 1
    assert window.sweep_speed_combo.itemData(hold_index) is None
    assert "0 Hz" in window.sweep_speed_combo.itemText(hold_index)
    window.sweep_speed_combo.setCurrentIndex(hold_index)
    assert sweep_calls == [("pause", True)], sweep_calls
    # Leaving the hold has to un-pause as well as set the rate, or the
    # trace stays frozen at the new speed.
    sweep_calls.clear()
    window.sweep_speed_combo.setCurrentIndex(8)
    assert sweep_calls == [("pause", False), ("speed", 8)], sweep_calls
    window.linien_widget.linien_thread = None

    # Connecting takes seconds; the row must say so rather than sitting
    # at "--", and a connect that never succeeds must read as an error
    # rather than the same "--".
    window._set_usb_status_label(window.rp_usb_status, "connecting:rp.local")
    assert window.rp_usb_status.text() == "..."
    window._set_usb_status_label(window.rp_usb_status, "error:no route to host")
    assert window.rp_usb_status.text() == "Error"
    assert "no route" in window.rp_usb_status.toolTip()
    # An attempt in flight is not a connected device.
    window._set_usb_status_label(
        window.rp_usb_status, "connecting:rp.local", "rp"
    )
    assert not window._device_connected["rp"]

    # Giving up on the Red Pitaya has to say what to do about it. A red
    # "Error" in a collapsed Devices card never did.
    assert not lw.rp_banner.isVisible()
    lw._on_connection_status("error:GeneralConnectionError (after 3 attempts)")
    assert lw.rp_banner.isVisible()
    banner = lw.rp_banner.text()
    assert lw._linien_host in banner, banner
    assert "Auto-connect" in banner, banner
    assert "3 attempts" in banner, banner
    # Trying again clears it, without waiting for the attempt to land.
    lw._on_connection_status("connecting:rp.local")
    assert not lw.rp_banner.isVisible()

    # The vtune DAC and the repump gate are two more clients to the same
    # server, so they wait for it to answer instead of repeating the same
    # failed connect. LinkHealth re-emits "connected" on every recovery,
    # which must not spawn a second pair on one GPIO bit.
    window.vtune_worker = None
    window.repump_gpio_worker = None
    started = []
    orig_start_repump = window._start_repump_workers
    window._start_repump_workers = lambda: started.append(1)
    try:
        window._on_linien_connection("error:nope")
        assert started == [], "started them for a Red Pitaya that never answered"
        window._on_linien_connection("connected:rp.local")
        assert started == [1]
    finally:
        window._start_repump_workers = orig_start_repump

    # The guard itself, against the real method: a second "connected" must
    # not build a second pair on top of the running one.
    class _Running:
        def isRunning(self):
            return True

    window.vtune_worker = _Running()
    window.repump_gpio_worker = _Running()
    window._start_repump_workers()
    assert isinstance(window.vtune_worker, _Running), "spawned a duplicate"
    window.vtune_worker = None
    window.repump_gpio_worker = None
    # Put the connection state back: the checks further down read it, and
    # _on_linien_connection above moved it to "connected".
    window._rp_last_status = "disconnected"
    window._device_connected["rp"] = False
    window._readback_seen.discard("modulation")

    # Unplugging a device has to show. Nothing polls the Koheron -- it reads
    # two setpoints at connect and then only writes -- so its row read OK for
    # the rest of the session no matter what happened to the cable.
    window.laser_edit.setText("COM77")
    window._device_connected["laser"] = True
    window._readback_seen.add("laser")
    # Everything else stays plugged in, so only the laser's edge is under
    # test here.
    others = {
        window.mot_coil_port_edit.text().strip(),
        window.dispenser_port_edit.text().strip(),
    }
    window._on_ports_present(others | {"COM77"})
    assert "not there" not in window.laser_usb_status.toolTip()
    before = len(_records())
    window._on_ports_present(others)  # COM77 gone
    # The row says Error like every other way a device stops being usable;
    # which way it was is on the tooltip.
    assert window.laser_usb_status.text() == "Error"
    assert "COM77 is not there" in window.laser_usb_status.toolTip()
    assert not window._device_connected["laser"]
    # Its last readback is stale, so it is no longer the device's state.
    assert "laser" not in window._readback_seen
    vanished = next(
        r for r in _records()[before:] if r["event"] == "port_vanished"
    )
    assert vanished["kind"] == "fault" and vanished["port"] == "COM77"

    # Said once, not once per sweep of the port list.
    before = len(_records())
    window._on_ports_present(others)
    window._on_ports_present(others)
    assert len(_records()) == before, _records()[before:]

    # And the return is reported too, without reconnecting anything: opening
    # a port is a connection, and connections stay the operator's call.
    window._on_ports_present(others | {"COM77"})
    # Still Error: the port exists again but nothing has reopened it, and
    # the tooltip is what says what to do about that.
    assert window.laser_usb_status.text() == "Error"
    assert "Auto-connect" in window.laser_usb_status.toolTip()
    assert any(r["event"] == "port_returned" for r in _records()[before:])
    window.laser_edit.setText("")

    # Anything already energised when the app connects gets named. The
    # repump gate is a Red Pitaya GPIO bit and the dither a linien
    # parameter the server restores, so an exit that skipped teardown
    # leaves them on and the next launch adopts them silently.
    # With a live gate client: an output is only reportable as live if a
    # device said so, and a press that cannot reach the Red Pitaya drops
    # that claim (see the no-link check above).
    window.repump_gpio_worker = _StubGate()
    window._on_repump_gpio_state(True)
    window.repump_gpio_btn.setChecked(True)
    before = len(_records())
    window._report_startup_outputs()
    found = next(
        r for r in _records()[before:] if r["event"] == "startup_outputs_live"
    )
    assert found["kind"] == "fault"
    assert "repump" in found["outputs"], found
    assert "Repump on" in found["detail"], found
    assert window.chip_text("lock").startswith("UNLOCKED")  # log only
    window.repump_gpio_btn.setChecked(False)
    window.repump_gpio_worker = None

    # A restored setting is not a readback. With the dither on in the UI and
    # nothing yet heard from the Red Pitaya, the report must say it could not
    # tell -- it used to name the dither as live one second before failing to
    # connect to the RP at all.
    window.mod_enable_btn.setChecked(True)
    window._mod_touched = False  # as if restored, not typed
    assert "modulation" not in window._readback_seen
    before = len(_records())
    window._report_startup_outputs()
    fresh = _records()[before:]
    # The coils did report, so they are still named; the dither must not be.
    assert not any(
        "modulation" in r.get("outputs", [])
        for r in fresh
        if r["event"] == "startup_outputs_live"
    ), fresh
    unknown = next(r for r in fresh if r["event"] == "startup_outputs_unknown")
    assert "modulation" in unknown["outputs"], unknown
    assert unknown["rp_status"] == "disconnected", unknown

    # Once the device has answered, the same state is a fact and reportable.
    window._on_modulation_readback(400.0)
    before = len(_records())
    window._report_startup_outputs()
    live = next(
        r for r in _records()[before:] if r["event"] == "startup_outputs_live"
    )
    assert "modulation" in live["outputs"], live
    window.mod_enable_btn.setChecked(False)

    # Nothing energised: no report, no chip change.
    window.mod_enable_btn.setChecked(False)
    window.mot_coil_output_btn.setChecked(False)
    window.laser_btn.setChecked(False)
    window._on_lock_state(False)
    assert window.live_outputs() == [], [
        (o.key, o.detail) for o in window.live_outputs()
    ]
    before = len(_records())
    window._report_startup_outputs()
    assert len(_records()) == before
    assert window.chip_text("lock").startswith("UNLOCKED")

    # A locked RP at startup is reported (log) and offered at shutdown;
    # the chip keeps saying LOCKED rather than "N OUTPUTS WERE ON".
    window._on_lock_state(True)
    window._report_startup_outputs()
    assert window.chip_text("lock") == "LOCKED"
    assert "lock" in [o.key for o in window.live_outputs()]
    window._on_lock_state(False)

    # A worker fault drops the device out of the count too. It used to
    # write "Fault" straight onto the label, so the row went red while the
    # header still counted the device as connected.
    window._set_usb_status_label(
        window.coil_usb_status, "connected:/dev/cu.fakeB@1", "mot_coil"
    )
    assert window._device_connected["mot_coil"]
    window._show_fault(
        window.coil_usb_status, "coil", "WriteFile failed", "mot_coil"
    )
    assert window.coil_usb_status.text() == "Error"
    assert "WriteFile failed" in window.coil_usb_status.toolTip()
    assert not window._device_connected["mot_coil"]

    # Auto-connect brings up the Red Pitaya as well as probing USB: it used
    # to run the USB probe alone, so an RP that was down at startup could
    # only be reached from Manual setup.
    hosts = []
    orig_set_host = window.linien_widget.set_host
    orig_probe = window._run_usb_probe
    orig_rp_host = window.rp_edit.text()
    window.linien_widget.set_host = hosts.append
    window._run_usb_probe = lambda startup=False: None
    try:
        window.rp_edit.setText("rp-auto.local")
        window._rp_last_status = "error:nope"
        window._auto_connect()
        assert hosts == ["rp-auto.local"], hosts
        # ...but a Red Pitaya that is already connected is left alone:
        # restarting the client sweeps, and that drops a lock.
        window._rp_last_status = "connected:rp-auto.local"
        window._auto_connect()
        assert hosts == ["rp-auto.local"], hosts
    finally:
        window.linien_widget.set_host = orig_set_host
        window._run_usb_probe = orig_probe
        window._rp_last_status = "disconnected"
        window.rp_edit.setText(orig_rp_host)  # the screenshot shows this row

    # One live Red Pitaya client must not mask a dead one. The start guard
    # used to return if *either* was running, so when the server dropped
    # the gate client while the DAC client's thread stayed up faulting, the
    # gate was never rebuilt and the repump button silently did nothing for
    # the rest of the session (2026-08-17, 16:24 onward).
    class _FakeRPWorker:
        def __init__(self, *args, **kwargs):
            self.started = False

        def __getattr__(self, name):  # every signal: .connect(...)
            return types.SimpleNamespace(connect=lambda *a, **k: None)

        def isRunning(self):
            return False

        def start(self):
            self.started = True

    class _LiveWorker(_FakeRPWorker):
        def isRunning(self):
            return True

    orig_gate_cls = mot_control.LinienGpioWorker
    orig_dac_cls = mot_control.DacClickWorker
    mot_control.LinienGpioWorker = _FakeRPWorker
    mot_control.DacClickWorker = _FakeRPWorker
    try:
        live_dac = _LiveWorker()
        window.vtune_worker = live_dac
        window.repump_gpio_worker = None  # its thread exited on a failed connect
        window._start_repump_workers()
        assert window.vtune_worker is live_dac, "rebuilt a client that was fine"
        assert isinstance(window.repump_gpio_worker, _FakeRPWorker)
        assert window.repump_gpio_worker.started, "the dead gate was not rebuilt"

        # And the reverse, so neither can hide behind the other.
        live_gate = _LiveWorker()
        window.vtune_worker = None
        window.repump_gpio_worker = live_gate
        window._start_repump_workers()
        assert window.repump_gpio_worker is live_gate
        assert window.vtune_worker.started
    finally:
        mot_control.LinienGpioWorker = orig_gate_cls
        mot_control.DacClickWorker = orig_dac_cls
        # Not left behind: a later slider change would call set_voltage on
        # the fake and raise inside a Qt slot.
        window.vtune_worker = None
        window.repump_gpio_worker = None

    # A press that cannot reach the Red Pitaya must not look like one that
    # did: the button moved, the log recorded the intent, and the GPIO bit
    # never heard about it.
    window.repump_gpio_worker = None
    window.repump_gpio_btn.setChecked(True)
    assert "NO LINK" in window.chip_text("repump"), window.chip_text("repump")
    gate_fault = next(
        r for r in reversed(_records())
        if r["event"] == "fault" and r.get("device") == "repump_gate"
    )
    assert "not connected" in gate_fault["message"], gate_fault
    assert "repump" not in window._readback_seen
    window.repump_gpio_btn.setChecked(False)
    window._set_repump_ui(False)

    # A wedged server answers RPyC but its threads are dead, so the plot
    # just stops. Reconnecting does not help; only a restart does, and
    # the UI has to say which failure this is.
    window._on_server_stalled(True)
    # The row is Error with the reason on hover; the lock chip keeps the
    # lock state ("RP STALLED" there was removed at the operator's request).
    assert window.rp_usb_status.text() == "Error"
    assert "wedged" in window.rp_usb_status.toolTip()
    assert not window._device_connected["rp"]
    assert window.chip_text("lock").startswith("UNLOCKED")
    window._on_server_stalled(False)
    assert "wedged" not in window.rp_usb_status.toolTip()
    assert window.chip_text("lock").startswith("UNLOCKED")

    # The restart action guards against a double press.
    assert window.restart_server_btn.isEnabled()
    window._server_restarting = True
    window._restart_linien_server()  # must return without prompting
    window._server_restarting = False
    # ...and reports a missing host rather than SSHing to nothing.
    saved_host = window.rp_edit.text()
    window.rp_edit.setText("")
    window._restart_linien_server()
    assert not window._server_restarting
    window.rp_edit.setText(saved_host)

    # A failed restart re-enables the button and says so, rather than
    # leaving it stuck on "Restarting...". The success path is not driven
    # here: it reconnects, and this test must never touch hardware.
    window._server_restarting = True
    window.restart_server_btn.setEnabled(False)
    window._on_server_restarted(False, "ssh refused")
    assert window.restart_server_btn.isEnabled()
    assert not window._server_restarting
    assert window.restart_server_btn.text() == "Restart Linien Server"
    assert any(
        r["kind"] == "fault" and "ssh refused" in str(r.get("message", ""))
        for r in _records()
    )

    # Determine signal offset defaults on, matching linien's server default.
    assert window.auto_offset_check.isChecked()
    window.auto_offset_check.setChecked(False)
    assert window._settings.value("linien/determine_offset") in (False, "false")
    window.auto_offset_check.setChecked(True)

    # --- Lock PSD: linien's noise-analysis window ---
    import numpy as np

    # The PSD worker's first status arrives after the window opens, so
    # the lock state has to be seeded from what the host already knows --
    # otherwise the first Start click refuses and the second works.
    window._on_lock_state(True)
    window._open_psd_window()  # rp host empty -> worker exits with no_host
    psd = window._psd_window
    assert psd is not None and psd.isVisible()
    assert psd._locked, "PSD opened believing the laser was unlocked"
    window._on_lock_state(False)
    window._open_psd_window()
    assert not psd._locked
    window._on_lock_state(True)
    window._open_psd_window()

    # Run controls swap on acquisition state, as linien's containers do.
    window._on_psd_status(
        {"acquiring": True, "optimizing": False, "locked": True,
         "p": 100, "i": 50, "d": 0}
    )
    assert psd.running_container.isVisibleTo(psd)
    assert not psd.not_running_container.isVisibleTo(psd)

    payload = {
        "uuid": "run1", "time": 1.0, "p": 100, "i": 50, "d": 0,
        "signals": {},
        "psds": {
            1: (np.array([1e2, 1e3, 1e4]), np.array([1e-3, 1e-4, 1e-5])),
            5: (np.array([1e1, 5e1]), np.array([1e-2, 5e-3])),
        },
        "fitness": 0.0111, "complete": True,
    }
    window._on_psd_data(payload)
    assert len(psd.plot.curves) == 1
    assert len(psd.plot.curves["run1"]) == 2
    assert psd.table.rowCount() == 1
    assert psd.table.item(0, 6).text() == "0.0111"

    # Decimation stages are stitched, not overlaid: the high-decimation
    # stage supplies the low frequencies and the next picks up above it,
    # with the join point prepended. Counts are shown as volts. The plot
    # is in log mode, so getData returns log10 of the values.
    lo_x, lo_y = psd.plot.curves["run1"][0].getData()
    hi_x, _hi_y = psd.plot.curves["run1"][1].getData()
    assert np.allclose(10**lo_x, [1e1, 5e1])  # decimation 5 first
    assert np.isclose(10**lo_y[0], 1e-2 / psd_window.V)
    assert hi_x[0] == lo_x[-1]  # segments join, no gap and no overlap
    assert (np.diff(np.concatenate([lo_x, hi_x[1:]])) > 0).all()

    # A late partial payload must not overwrite a completed curve.
    window._on_psd_data(
        dict(payload, psds={1: (np.array([1e2]), np.array([9e9]))},
             complete=False)
    )
    assert len(psd.plot.curves["run1"]) == 2  # unchanged

    window._on_psd_data(dict(payload, uuid="run2", p=200))  # second run
    assert len(psd.plot.curves) == 2
    assert psd.table.rowCount() == 2

    # The Display checkbox hides that run's curves.
    psd.table.cellWidget(0, 0).setChecked(False)
    assert not psd.plot.curves["run1"][0].isVisible()
    psd.table.cellWidget(0, 0).setChecked(True)
    assert psd.plot.curves["run1"][0].isVisible()

    # Delete removes the selected run from both plot and table.
    psd.table.setCurrentCell(0, 0)
    psd.delete_curve()
    assert "run1" not in psd.plot.curves
    assert psd.table.rowCount() == 1

    # Starting unlocked is refused with a dialog rather than a silent no-op.
    orig_critical = QtWidgets.QMessageBox.critical
    refusals = []
    QtWidgets.QMessageBox.critical = lambda *a, **k: refusals.append(a)
    try:
        window._on_psd_status(
            {"acquiring": False, "optimizing": False, "locked": False,
             "p": 100, "i": 50, "d": 0}
        )
        psd.start_psd()
    finally:
        QtWidgets.QMessageBox.critical = orig_critical
    assert len(refusals) == 1
    assert psd.not_running_container.isVisibleTo(psd)

    # Measurement time maps to decimation 12 + index, as in linien's .ui.
    psd.time_combo.setCurrentIndex(6)
    assert int(window._settings.value("psd/max_decimation")) == 18
    psd.clear_curves()
    assert psd.table.rowCount() == 0
    psd.close()  # hides rather than destroys, so traces survive
    assert not psd.isVisible()

    # --- Dispenser setpoints ---
    # There is no override dialog: the sliders are the setpoints, and
    # _set_dispenser_fire (used by config load) drives them.
    assert window.dispenser_current_slider.value() == 7500
    window._set_dispenser_fire(10.0, 45.0)
    assert window.dispenser_current_slider.value() == 10000
    assert window.dispenser_current_edit.text() == "10.0 A"
    assert window.dispenser_fire_btn.text() == "Fire Dispenser (45 s)"
    window._set_dispenser_fire(7.5, 30.0)
    assert window.dispenser_current_slider.value() == 7500

    # Log pane mirrored the events, and the header shows the last one.
    assert "dispenser_setpoint" in window.log_view.toPlainText()
    assert "dispenser_fire_end" in window.log_view.toPlainText()
    assert window.log_card.header_status.text() != ""

    # --- Log pane formatting ---
    # The pane used to join every field as k=v in dict order and cut the
    # line at 300 chars, so the part worth reading was last and usually the
    # part that got cut -- and a fault read exactly like a readback.
    RED = window._LOG_COLOURS["fault"]
    AMBER = window._LOG_COLOURS["warn"]

    # A generic fault is named by its device, and is red.
    source, text, colour = window._log_line({
        "kind": "fault", "event": "fault", "device": "coils",
        "message": "WriteFile failed",
    })
    assert (source, colour) == ("coils", RED)
    assert text == "WriteFile failed"

    # A collapsed run says how many and over how long, in amber.
    source, text, colour = window._log_line({
        "kind": "fault", "event": "fault_repeated", "of": "fault",
        "device": "coils", "message": "WriteFile failed",
        "count": 148, "over_s": 30.0,
    })
    assert (source, colour) == ("coils", AMBER)
    assert "148" in text and "30.0" in text

    # An event that names itself keeps its name: the device must not take
    # the column and leave "laser  port=COM6" saying nothing.
    source, text, _ = window._log_line({
        "kind": "state", "event": "port_vanished",
        "device": "laser", "port": "COM6",
    })
    assert source == "port_vanished", source
    assert "laser" in text and "COM6" in text

    # Library records are the library's short name and its message.
    source, text, colour = window._log_line({
        "kind": "state", "event": "library", "level": "INFO",
        "logger": "linien_client.connection",
        "message": "Try to connect to rp-f0f2b5.local:18862",
    })
    assert source == "linien" and text.startswith("Try to connect")
    assert colour == window._LOG_COLOURS["quiet"]
    assert window._log_line({
        "kind": "fault", "event": "library", "level": "ERROR",
        "logger": "linien_client.connection", "message": "host not found",
    })[2] == RED

    # DEBUG belongs in the file, not in a 500-line pane.
    before = window.log_view.toPlainText()
    window._on_log_record({
        "t": "2026-08-17T13:00:00.000", "kind": "state", "event": "library",
        "level": "DEBUG", "logger": "linien_client.connection",
        "message": "Sent authentication hash",
    })
    assert window.log_view.toPlainText() == before

    # No field runs off the pane, and neither does the whole line.
    _, text, _ = window._log_line({
        "kind": "state", "event": "usb_probe",
        "assignments": {"laser": "COM%d" % i for i in range(40)},
        "status": {"laser": "verified" * 40},
    })
    assert len(text) <= window._LOG_LINE_CHARS, len(text)

    # High-contrast palette: after the remap sweep and all the runtime
    # status updates above, no widget stylesheet may still carry the old
    # dim text colors.
    for widget in window.findChildren(QtWidgets.QWidget):
        sheet = widget.styleSheet()
        for dim in mot_control.CONTRAST_REMAP:
            assert dim not in sheet, (type(widget).__name__, dim, sheet[:80])

    # Tooltips exist across the app (hover help).
    for tipped in (
        window.current_slider,
        window.vtune_slider,
        window.dispenser_fire_btn,
        window.mot_coil_output_btn,
        window.repump_gpio_btn,
        window.sweep_speed_combo,
        window.connect_btn,
        window.log_view,
    ):
        assert tipped.toolTip(), tipped

    # --- PID dialog (modal exec skipped) ---
    pid_dlg = mot_control.PidDialog(window, (120, 40, 3))
    assert pid_dlg.values() == (120, 40, 3)
    pid_dlg.p_spin.setValue(9999)  # clamped to linien's max
    assert pid_dlg.values() == (8191, 40, 3)
    pid_default = mot_control.PidDialog(window, None)
    assert pid_default.values() == (50, 5, 0)  # linien defaults

    # --- Error signal filter card ---
    # Filters are inline in the Linien Lock card, not a nested dropdown.
    assert not hasattr(window, "filter_card")
    assert not hasattr(window, "filter_auto_check")  # no automatic mode
    assert window.filter1_freq_spin.isEnabled()  # stages are always live
    window.filter1_enabled_check.setChecked(True)
    window.filter1_type_combo.setCurrentIndex(1)  # high pass
    window.filter1_freq_spin.setValue(25000)
    filters = window._filters_from_ui()
    assert filters["automatic"] is False
    assert filters["filter_1_enabled"] is True
    assert filters["filter_1_type"] == 1
    assert filters["filter_1_frequency"] == 25000
    assert filters["filter_2_enabled"] is False
    window._set_filters_ui(
        {"automatic": True, "filter_1_frequency": 12000}
    )  # partial sync from device
    assert window.filter1_freq_spin.value() == 12000
    assert window.filter1_freq_spin.isEnabled()
    window._sync_filters_from_device()  # no linien connection: harmless no-op

    # A connect-time readback must not overwrite filter controls the
    # operator is working in. It lands 1.5 s after connect, which is well
    # inside the time it takes to type a frequency.
    device_filters = {
        "automatic": False,
        "filter_1_enabled": True,
        "filter_1_type": 0,
        "filter_1_frequency": 3000,
        "filter_2_enabled": False,
        "filter_2_type": 0,
        "filter_2_frequency": 10000,
    }
    window.linien_widget.linien_thread = types.SimpleNamespace(
        get_filters=lambda: device_filters,
        set_filters=lambda settings: None,
    )
    window.filter1_freq_spin.setValue(31000)  # the operator, mid-edit
    assert window._filters_touched
    before = len(_records())
    window._sync_filters_from_device()
    assert window.filter1_freq_spin.value() == 31000, "readback overwrote it"
    divergence = next(
        r for r in _records()[before:] if r["event"] == "filter_divergence"
    )
    assert divergence["kind"] == "fault"
    assert divergence["device"]["filter_1_frequency"] == 3000
    assert divergence["ui"]["filter_1_frequency"] == 31000

    # Once their own value has been pushed, the device may drive these again.
    window._push_filters()
    assert not window._filters_touched
    window._sync_filters_from_device()
    assert window.filter1_freq_spin.value() == 3000
    window.linien_widget.linien_thread = None
    window._set_filters_ui({"automatic": True, "filter_1_frequency": 12000})
    window._filters_touched = False

    # --- Config snapshot + JSON export round-trip ---
    window.current_slider.setValue(8570)   # 85.70 mA
    window.mot_coil_current_slider.setValue(2200)
    config = window._collect_config()
    # The config keeps physical units, so the extra decimal has to survive
    # the snapshot rather than being rounded back to whole mA.
    assert config["laser"]["current_ma"] == 85.7
    assert config["coils"]["current_a"] == 2.2
    assert config["dispenser"]["fire_current_a"] == 7.5
    assert config["psd"]["max_decimation"] == int(
        window._settings.value("psd/max_decimation")
    )
    assert config["repump"]["vco_mhz"] == round(
        window.vtune_slider.value() / 10.0, 1
    )
    # PID is the gains last set from the app, never a read of the server:
    # absent until set, then carried.
    assert "pid" not in config["linien"], config["linien"]
    window._remember_pid(200, 100, 0)
    config = window._collect_config()
    assert config["linien"]["pid"] == {"p": 200, "i": 100, "d": 0}
    assert config["saved_at"]
    assert config["linien"]["filters"]["filter_1_frequency"] == 12000
    config_path = os.path.join(TMP, "config-export.json")
    window._write_config_json(config_path)
    with open(config_path, encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["laser"]["current_ma"] == 85.7
    assert loaded["coils"]["current_a"] == 2.2
    assert loaded["connection"]["mot_coil_address"] == 1

    # --- Config load: change everything, then restore from the file ---
    window.current_slider.setValue(6000)
    window.mot_coil_current_slider.setValue(500)
    window.vtune_slider.setValue(28100)
    window._settings.setValue("psd/max_decimation", 14)
    window._set_filters_ui({"automatic": False, "filter_1_frequency": 777})
    window._set_dispenser_fire(12.0, 40.0)
    orig_question = QtWidgets.QMessageBox.question
    QtWidgets.QMessageBox.question = (
        lambda *a, **k: QtWidgets.QMessageBox.Yes
    )
    try:
        window._apply_config(loaded, path=config_path)
    finally:
        QtWidgets.QMessageBox.question = orig_question
    assert window.current_slider.value() == 8570
    assert window.mot_coil_current_slider.value() == 2200
    assert int(window._settings.value("psd/max_decimation")) == (
        loaded["psd"]["max_decimation"]
    )
    assert window.filter1_freq_spin.value() == 12000
    assert window._dispenser_fire_current == 7.5  # confirm-gated restore
    assert window._dispenser_fire_seconds == 30.0
    # partial configs apply without touching anything else
    window._apply_config({"laser": {"current_ma": 92}})
    assert window.current_slider.value() == 9200
    assert window.mot_coil_current_slider.value() == 2200

    # --- A config is setpoints. It can never switch an output. ---
    # Nothing in _collect_config describes an output today, but that held
    # by accident: one key called "output_enabled" would have made loading
    # a file energise a supply.
    outputs = (
        window.laser_btn,
        window.mot_coil_output_btn,
        window.repump_gpio_btn,
        window.mod_enable_btn,
    )
    switched = []

    class _RecordingWorker:
        def __getattr__(self, name):
            def record(*args):
                if name in ("set_output", "laser_on", "fire", "start_fire"):
                    switched.append((name, args))
            return record

    window.laser_worker = _RecordingWorker()
    window.mot_coil_worker = _RecordingWorker()
    window.repump_gpio_worker = _RecordingWorker()
    window.linien_widget.linien_thread = _RecordingWorker()
    try:
        for button in outputs:
            button.setChecked(True)
        switched.clear()
        on_path = os.path.join(TMP, "config-outputs-on.json")
        window._write_config_json(on_path)
        with open(on_path, encoding="utf-8") as f:
            on_config = json.load(f)

        for button in outputs:
            button.setChecked(False)
        switched.clear()
        window._apply_config(on_config, path=on_path)
        assert not any(b.isChecked() for b in outputs), [
            b.text() for b in outputs if b.isChecked()
        ]
        assert switched == [], switched

        # The load side has to hold on its own. A file saved by this app
        # cannot contain an output key, so round-tripping one proves
        # nothing about _apply_config -- hand it a file that says, as
        # loudly as JSON can, "turn everything on".
        hostile = {
            "coils": {"current_a": 1.0, "output_enabled": True},
            "laser": {"current_ma": 85, "laser_on": True},
            "repump": {"gate_on": True},
            "linien": {"mod_enabled": True, "locked": True},
            "dispenser": {"firing": True, "fire_now": True},
        }
        switched.clear()
        window._apply_config(hostile)
        assert not any(b.isChecked() for b in outputs), [
            b.text() for b in outputs if b.isChecked()
        ]
        assert switched == [], switched
        # ...and the setpoints in that same file still landed.
        assert window.current_slider.value() == 8500
        assert window.mot_coil_current_slider.value() == 1000

        # It must not switch a running experiment off either.
        for button in outputs:
            button.setChecked(True)
        switched.clear()
        window._apply_config(hostile)
        assert all(b.isChecked() for b in outputs)
        assert switched == [], switched
        for button in outputs:
            button.setChecked(False)

        # A key that describes an output never reaches the file, and its
        # removal is recorded rather than silent.
        # A key name of its own: identical faults collapse into a count, and
        # the hostile config above already dropped coils.output_enabled.
        orig_collect = window._collect_config
        window._collect_config = lambda: dict(
            orig_collect(), coils={"current_a": 1.0, "mot_coil_output": True}
        )
        try:
            before = len(_records())
            planted = window._write_config_json(
                os.path.join(TMP, "config-planted.json")
            )
        finally:
            window._collect_config = orig_collect
        assert "mot_coil_output" not in planted["coils"], planted["coils"]
        # Filter enables end in "_enabled" but are settings, not outputs;
        # the suffix rule used to strip them from every saved config.
        assert {"filter_1_enabled", "filter_2_enabled"} <= set(
            planted["linien"]["filters"]
        ), planted["linien"]["filters"]
        dropped = next(
            r for r in _records()[before:]
            if r["event"] == "config_state_key_dropped"
        )
        assert dropped["key"] == "coils.mot_coil_output", dropped
    finally:
        window.laser_worker = None
        window.mot_coil_worker = None
        window.repump_gpio_worker = None
        window.linien_widget.linien_thread = None

    # --- Probe-result handling ---
    window._on_probe_result(
        {
            "ok": True,
            "assignments": {
                "laser": "/dev/cu.fakeA",
                "mot_coil": "/dev/cu.fakeB",
                "dispenser": "/dev/cu.fakeC",
            },
            "status": {k: "verified" for k in ("laser", "mot_coil", "dispenser")},
        },
        startup=False,
    )
    assert window.laser_edit.text() == "/dev/cu.fakeA"
    assert window.mot_coil_port_edit.text() == "/dev/cu.fakeB"
    # Status rows mirror the edits, which remain the source of truth.
    assert window.laser_status_value.text() == "/dev/cu.fakeA"
    assert window.coil_status_value.text() == "/dev/cu.fakeB"
    assert "DEVICES 3/4" in window.usb_card.header_status.text()
    assert window.chip_text("usb") == "DEVICES 3/4"
    # ...and the count follows live connection state, not just the probe:
    # this is what used to latch at whatever startup found.
    window._on_linien_connection("connected:rp-test.local")
    assert window.chip_text("usb") == "DEVICES 4/4"
    window._set_usb_status_label(
        window.coil_usb_status, "error:port gone", "mot_coil"
    )
    assert window.chip_text("usb") == "DEVICES 3/4"
    window._set_usb_status_label(
        window.coil_usb_status, "connected:COM5", "mot_coil"
    )
    assert window.chip_text("usb") == "DEVICES 4/4"
    assert window.laser_usb_status.text() == "OK"
    assert "verified by probe" in window.laser_usb_status.toolTip()
    assert not window.usb_card.content.isVisibleTo(window.usb_card)
    assert hasattr(window, "laser_worker")  # serial workers were started

    # A reconnect keeps the laser and coils running (Auto-connect used to
    # send "lason 0" and switch the coils off); the dispenser still goes
    # off, because its auto-off timer dies with its worker.
    old = {a: getattr(window, a) for a in
           ("laser_worker", "mot_coil_worker", "dispenser_worker")}
    window._stop_serial_workers()
    assert old["laser_worker"].disable_on_stop is False
    assert old["mot_coil_worker"].disable_on_stop is False
    assert old["dispenser_worker"].disable_on_stop is True
    window._on_probe_result(
        {
            "ok": False,
            "assignments": {"laser": "/dev/cu.fakeA", "mot_coil": "", "dispenser": ""},
            "status": {
                "laser": "verified",
                "mot_coil": "missing",
                "dispenser": "missing",
            },
        },
        startup=False,  # startup=True would open the modal dialog
    )
    assert "DEVICES 2/4" in window.usb_card.header_status.text()
    assert window.chip_text("usb") == "DEVICES 2/4"
    assert window.coil_usb_status.text() == "Error"
    assert "not found by the USB probe" in window.coil_usb_status.toolTip()
    assert not window.usb_card.content.isVisibleTo(window.usb_card)
    app.processEvents()

    # With worst-case texts everywhere (card expanded, protection tripped,
    # override active, firing countdown), the control column must fit
    # its scroll viewport with no horizontal scrolling.
    window._on_dispenser_readback(
        {"v_out": 50.0, "i_out": 20.0, "power": 1000.0, "output_enabled": True,
         "cv_cc": 1, "protection": 2}
    )
    window._on_mot_coil_readback(
        {"v_out": 50.0, "i_out": 5.0, "power": 250.0, "output_enabled": True,
         "cv_cc": 0, "protection": 3}
    )
    window._set_dispenser_fire(10.0, 45.0)
    window._on_dispenser_fire_state({"phase": "firing", "remaining_s": 44.0})
    window.log_card.set_expanded(True)
    # Manual setup expanded too: the port dropdowns carry the longest
    # strings in the column, so they must be laid out for this to mean
    # anything.
    window.usb_card.set_expanded(True)
    window.manual_setup_card.set_expanded(True)
    app.processEvents()
    scroll = window.findChild(QtWidgets.QScrollArea)
    inner = scroll.widget()
    assert inner.minimumSizeHint().width() <= scroll.viewport().width(), (
        inner.minimumSizeHint().width(),
        scroll.viewport().width(),
    )
    assert not scroll.horizontalScrollBar().isVisible()
    window._set_dispenser_fire(7.5, 30.0)
    window._on_dispenser_fire_state({"phase": "idle", "remaining_s": 0.0})
    # Back to the default shape so the reference screenshot shows what the
    # app actually opens with: every card collapsed.
    for card in (
        window.manual_setup_card,
        window.log_card,
        window.lock_card,
        window.coils_card,
        window.dispenser_card,
        window.repump_card,
    ):
        card.set_expanded(False)
    # The wheel-guard check above leaves the column scrolled; put it back
    # so the reference screenshot is not clipped at the top.
    window.findChild(QtWidgets.QScrollArea).verticalScrollBar().setValue(0)
    app.processEvents()

    pixmap = window.grab()
    out = os.path.join(ROOT, "mot-ui-smoke.png")
    pixmap.save(out)
    print("saved", out)

    # --- Safe shutdown: nothing energised leaves quietly, anything
    # energised has to be acknowledged first ---
    # Earlier steps left the dither and the coils on; with everything off
    # there is nothing to ask about and the app must close silently.
    window.mod_strength_slider.setValue(0)
    window.mot_coil_output_btn.setChecked(False)
    window.repump_gpio_btn.setChecked(False)
    window.laser_btn.setChecked(False)
    window._on_lock_state(False)
    assert window.live_outputs() == [], [
        (o.key, o.detail) for o in window.live_outputs()
    ]

    # A dither slider above zero is not a running dither: switching
    # modulation off writes 0 to the device and the slider only holds the
    # level to restore. Reporting it as live was a lie.
    window.mod_enable_btn.setChecked(False)
    window.mod_strength_slider.setValue(400)
    assert not any(o.key == "modulation" for o in window.live_outputs())
    window.mod_enable_btn.setChecked(True)
    assert any(o.key == "modulation" for o in window.live_outputs())

    # --- The dither answers to the device once there is one ---
    # Adoption, not actuation: the readback sets the UI and must never push
    # the value it just read back at the Red Pitaya.
    pushed = []
    window.linien_widget.linien_thread = types.SimpleNamespace(
        set_modulation_amplitude=lambda mv: pushed.append(mv),
    )
    window._mod_touched = False
    window._on_modulation_readback(300.0)
    assert window.mod_enable_btn.isChecked()
    assert window.mod_strength_slider.value() == 300
    assert pushed == [], pushed
    assert window._mod_actual_mv == 300.0

    # Zero means the dither is not running. The slider keeps its value --
    # that is the level to restore, not the level being driven.
    window._on_modulation_readback(0.0)
    assert not window.mod_enable_btn.isChecked()
    assert window.mod_strength_slider.value() == 300
    assert pushed == [], pushed

    # A control the operator has touched is not overwritten by a readback:
    # the disagreement is recorded and their value stands.
    window.mod_strength_slider.setValue(250)  # a real touch, with a worker
    assert pushed == [] or pushed[-1] == 250, pushed
    window._mod_touched = True
    before = len(_records())
    window._on_modulation_readback(900.0)
    assert window.mod_strength_slider.value() == 250, "readback overwrote it"
    divergence = next(
        r for r in _records()[before:] if r["event"] == "mod_divergence"
    )
    assert divergence["kind"] == "fault"
    assert divergence["device_mv"] == 900.0 and divergence["ui_mv"] == 250
    # ...and nothing may report the displayed dither as the device's state.
    assert "modulation" not in window._readback_seen
    window.linien_widget.linien_thread = None
    window._mod_touched = False
    window.mod_enable_btn.setChecked(True)
    window.mod_strength_slider.setValue(400)

    window.mot_coil_output_btn.setChecked(True)
    window._on_dispenser_fire_state({"phase": "firing", "remaining_s": 30.0})
    live = {o.key: o for o in window.live_outputs()}
    assert set(live) == {"modulation", "coils", "dispenser"}, set(live)
    # Labels are the operator's words, not the wiring's: the repump is
    # "on", not "gate open".
    window.repump_gpio_btn.setChecked(True)
    repump = next(o for o in window.live_outputs() if o.key == "repump")
    assert (repump.label, repump.detail) == ("Repump", "on")
    window.repump_gpio_btn.setChecked(False)
    # The dispenser is not the operator's to leave running.
    assert live["dispenser"].mandatory
    assert not live["coils"].mandatory
    assert "0.400 Vpp" in live["modulation"].detail

    # Every item defaults to being turned off, so the safe path is the
    # default path -- but unchecking one really does leave it energised.
    dialog = mot_control.ShutdownDialog(window, list(live.values()))
    assert dialog.turn_off() == {"modulation", "coils", "dispenser"}
    dialog._checks["coils"].setChecked(False)
    assert dialog.turn_off() == {"modulation", "dispenser"}
    # A mandatory item cannot be unchecked at all.
    assert not dialog._checks["dispenser"].isEnabled()
    dialog.reject()

    # Cancelling the dialog must abort the close, not fall through it.
    mot_control.ShutdownDialog.exec = lambda self: QtWidgets.QDialog.Rejected
    window.close()
    assert window.isVisible(), "cancelled shutdown still closed the window"

    # Accepting drives the chosen outputs safe. "coils" is left out here,
    # so its worker must be told not to force the output off on stop.
    mot_control.ShutdownDialog.exec = lambda self: QtWidgets.QDialog.Accepted
    mot_control.ShutdownDialog.turn_off = lambda self: {"modulation"}
    window._on_dispenser_fire_state({"phase": "idle", "remaining_s": 0.0})
    window.close()
    app.processEvents()
    choice = next(
        r for r in reversed(_records()) if r["event"] == "shutdown_choice"
    )
    assert "modulation" in choice["turned_off"]
    assert "coils" in choice["left_on"]

    # --- Persistent UI state + parameter persistence round-trip ---
    w1 = mot_control.OpenQuantumControlsWindow()
    w1.show()
    app.processEvents()
    # UI state restored from the same isolated settings file. Cards are
    # not part of that: every one of them starts collapsed, including
    # Devices, which used to reopen however it was left and so looked
    # random next to the rest.
    assert not w1.usb_card.content.isVisibleTo(w1.usb_card)
    assert not w1.log_card.content.isVisibleTo(w1.log_card)
    assert w1._settings.value("ui/geometry") is not None
    assert w1._settings.value("ui/splitter_sizes") is not None
    w1.usb_card.set_expanded(False)
    w1.mod_freq_slider.setValue(23456)
    w1.current_slider.setValue(9550)   # 95.50 mA
    w1.therm_slider.setValue(2050)  # 20.50 C
    w1.mot_coil_voltage_slider.setValue(12000)
    w1.mot_coil_current_slider.setValue(2500)
    w1.vtune_slider.setValue(29000)
    w1.sweep_speed_combo.setCurrentIndex(4)
    w1._set_dispenser_fire(9.0, 20.0)
    w1._settings.sync()

    # Closing must silence the dither. The workers drive their own
    # outputs safe on stop, but modulation is a linien parameter rather
    # than a worker output, so nothing else would turn it off.
    zeroed = []

    class _FakeLockWorker:
        def set_modulation_amplitude(self, value):
            zeroed.append(value)

        def stop(self):
            pass

    # Checked before the fake worker is installed, so only the close's own
    # write lands in `zeroed`.
    w1.mod_enable_btn.setChecked(True)
    w1.linien_widget.linien_thread = _FakeLockWorker()
    w1.close()
    assert zeroed == [0], zeroed
    app.processEvents()

    w2 = mot_control.OpenQuantumControlsWindow()
    assert w2.mod_freq_slider.value() == 23456
    assert w2.current_slider.value() == 9550
    # Temperature persists as ohms, so the restored slider count can land
    # one 0.01 C step away from where it was set.
    assert abs(w2.therm_slider.value() - 2050) <= 1, w2.therm_slider.value()
    assert w2.mot_coil_voltage_slider.value() == 12000
    assert w2.mot_coil_current_slider.value() == 2500
    assert w2.vtune_slider.value() == 29000
    assert w2.sweep_speed_combo.currentIndex() == 4
    assert w2._dispenser_fire_current == 9.0
    assert w2._dispenser_fire_seconds == 20.0
    assert w2.dispenser_current_slider.value() == 9000
    assert w2.dispenser_fire_btn.text() == "Fire Dispenser (20 s)"
    assert not w2.usb_card.content.isVisibleTo(w2.usb_card)
    # The dither was silenced on the way out, so this launch must not claim
    # it is running. Writing 0 to the device while leaving
    # linien/mod_enabled True is what made every launch since 2026-08-14
    # report a dither that had been switched off.
    assert not w2.mod_enable_btn.isChecked(), "exit left the dither flag set"
    assert not any(o.key == "modulation" for o in w2.live_outputs())

    # Restoring the UI must never actuate. Connecting the toggled signal
    # before setChecked() made startup write a dither amplitude to the
    # laser that nobody asked for, so restoring "on" must set the button
    # without reaching a worker -- and with no setting at all the dither
    # starts off rather than on.
    w2._settings.remove("linien/mod_enabled")
    w3 = mot_control.OpenQuantumControlsWindow()
    assert not w3.mod_enable_btn.isChecked(), "dither defaulted on"
    assert not any(o.key == "modulation" for o in w3.live_outputs())
    w3.close()
    w2.close()
    app.processEvents()
finally:
    QtCore.QSettings = _orig_qsettings

# --- Event log wrote real JSONL records for the exercised paths ---
log_files = glob.glob(os.path.join(LOG_DIR, "*.jsonl"))
assert log_files, "no event log files written"
records = []
for path in log_files:
    with open(path, encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))
logged_events = [r["event"] for r in records]

# Every record is categorised, and the categories are the ones a reader
# filters on. A field named "kind" or "event" must not be able to
# clobber the record's own -- that silently destroys the category.
KINDS = {"session", "user", "command", "readback", "state", "fault"}
for r in records:
    assert r.get("kind") in KINDS, r
    assert r.get("event"), r

# The session header must let the log stand alone months later.
header = next(r for r in records if r["event"] == "session_start")
assert header["kind"] == "session"
for field in ("schema", "python", "platform", "machine", "pid", "frozen"):
    assert field in header, (field, header)

# A protection trip is a fault, and its name survives despite arriving
# in a field that used to be called "kind".
trip = next(r for r in records if r["event"] == "protection_trip")
assert trip["kind"] == "fault", trip
assert trip["protection"] == "OCP", trip

for expected in (
    "session_start",
    "app_stop",
    "usb_probe",
    "dps",
    "protection_trip",
    "dispenser_fire_start",
    "dispenser_fire_end",
    "dispenser_setpoint",
    "vtune",
    "lock",
    "psd_window",
    "psd_complete",
    "config_saved",
    "config_loaded",
):
    assert expected in logged_events, f"missing log event: {expected}"

print("SMOKE OK")
