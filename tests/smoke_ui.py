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
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="mot-smoke-")
LOG_DIR = os.path.join(TMP, "logs")
INI = os.path.join(TMP, "smoke-settings.ini")

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

# Both bundled families must register (byte-based loading — the
# path-based Qt API silently fails on this Qt build).
assert "Space Grotesk" in QtGui.QFontDatabase.families()
assert "IBM Plex Sans" in QtGui.QFontDatabase.families()

# Inputs are explicitly themed: spinboxes must not fall back to the
# platform palette (black-on-dark). The outline is a muted grey rather
# than the old near-white, which read as glare against the dark panel.
INPUT_BORDER = "border: 1px solid #404040"
stylesheet = mot_control.build_app_stylesheet()
assert "QAbstractSpinBox {" in stylesheet
assert stylesheet.count(INPUT_BORDER) >= 3  # edit/spin/combo
assert INPUT_BORDER in mot_control.COMBO_STYLE

# Corners are square everywhere -- no rounded panels or controls.
assert "border-radius: 0px" in stylesheet
assert not re.search(r"border-radius:\s*[1-9]", stylesheet)


def resolved_family(widget):
    """The family a widget will ACTUALLY render with (not just the CSS)."""
    widget.ensurePolished()
    return QtGui.QFontInfo(widget.font()).family()


# --- Standalone vendoring: the Linien widget/worker, styles, and the
# Koheron worker come from THIS repo, not a sibling eit-controls checkout.
import linien_widget  # noqa: E402
import linien_worker  # noqa: E402
import styles  # noqa: E402
from mot_workers import LaserSetpointWorker  # noqa: E402,F401

for module in (linien_widget, linien_worker, styles, mot_control):
    module_dir = os.path.dirname(os.path.abspath(module.__file__))
    assert module_dir == ROOT, (module.__name__, module.__file__)
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

    # One family throughout: Space Grotesk for headings and body alike —
    # verified via the resolved font, not the stylesheet text.
    assert resolved_family(window.usb_card.toggle_btn) == "Space Grotesk"
    section = mot_control.make_section_title("PROBE")
    section.show()
    app.processEvents()
    assert resolved_family(section) == "Space Grotesk"
    assert resolved_family(window.laser_status) == "Space Grotesk"
    assert resolved_family(window.dispenser_fire_btn) == "Space Grotesk"

    # Unit suffixes on the previously unitless fields.
    assert window.mod_freq_edit.text().endswith(" MHz")
    assert window.mod_phase_edit.text().endswith("\N{DEGREE SIGN}")
    assert window.therm_edit.text().endswith(" \N{OHM SIGN}")

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

    # Laser slider ranges: current 50-100 mA, thermistor up to 15000 ohm.
    assert window.current_slider.minimum() == 50
    assert window.current_slider.maximum() == 100
    assert window.therm_slider.maximum() == 15000

    # Typed entry: the value boxes now drive the sliders (with clamping,
    # suffix tolerance, and garbage rejection).
    window.current_slider.setValue(90)
    window.current_edit.setText("95")
    window.current_edit.editingFinished.emit()
    assert window.current_slider.value() == 95
    assert window.current_edit.text() == "95 mA"
    window.current_edit.setText("999 mA")
    window.current_edit.editingFinished.emit()
    assert window.current_slider.value() == 100  # clamped to slider max
    assert window.current_edit.text() == "100 mA"
    window.mod_freq_edit.setText("17.5 MHz")
    window.mod_freq_edit.editingFinished.emit()
    assert window.mod_freq_slider.value() == 17500
    assert window.mod_freq_edit.text() == "17.500 MHz"
    window.vtune_slider.setValue(2500)
    window.vtune_edit.setText("garbage")
    window.vtune_edit.editingFinished.emit()
    assert window.vtune_slider.value() == 2500  # unchanged
    assert window.vtune_edit.text() == "2.500 V"  # display restored

    # Wheel guard: a wheel event over an unfocused slider must not change
    # the value (it used to — accidental hardware actuation).
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
    # scrollbars — including them once caused infinite recursion).
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
    assert window.chip_text("lock") == "UNLOCKED"

    # Thermistor row shows an estimated temperature in degrees C.
    expected_c = mot_control.thermistor_ohms_to_c(window.therm_slider.value())
    assert window.therm_temp_readback.text() == f"{expected_c:.1f} \N{DEGREE SIGN}C"
    window.therm_slider.setValue(5000)
    assert window.therm_temp_readback.text() == (
        f"{mot_control.thermistor_ohms_to_c(5000):.1f} \N{DEGREE SIGN}C"
    )

    # Dispenser fire-state UI: button, label, chip, and progress bar.
    window._on_dispenser_fire_state({"phase": "firing", "remaining_s": 15.0})
    assert "Abort Fire" in window.dispenser_fire_btn.text()
    assert not window.dispenser_override_btn.isEnabled()  # locked while firing
    assert window.chip_text("disp") == "DISP FIRING"
    assert window.dispenser_progress.isVisibleTo(window)
    assert window.dispenser_progress.value() == 500  # 15 s of 30 s left
    window._on_dispenser_fire_state({"phase": "cooldown", "remaining_s": 872.0})
    assert not window.dispenser_fire_btn.isEnabled()
    assert "14:32" in window.dispenser_fire_btn.text()
    assert window.chip_text("disp") == "DISP 14:32"
    assert window.dispenser_progress.value() == 969  # 872 s of 900 s left
    window._on_dispenser_fire_state({"phase": "idle", "remaining_s": 0.0})
    assert window.dispenser_fire_btn.isEnabled()
    assert window.dispenser_override_btn.isEnabled()
    assert "Fire Dispenser (30 s)" == window.dispenser_fire_btn.text()
    assert not window.dispenser_progress.isVisibleTo(window)
    assert window.chip_text("disp") == "DISP IDLE"

    # Coil readback shows CC/CV only while the output is on, and DPS
    # protection trips are surfaced in red (and on the chips).
    window._on_mot_coil_readback(
        {"v_out": 0.0, "i_out": 0.0, "power": 0.0, "output_enabled": False,
         "cv_cc": 0, "protection": 2}
    )
    assert window.mot_coil_readback.text().startswith("OCP TRIPPED")
    assert window.chip_text("coils") == "COILS TRIP"
    window._on_mot_coil_readback(
        {"v_out": 5.0, "i_out": 1.0, "power": 5.0, "output_enabled": True,
         "cv_cc": 0, "protection": 0}
    )
    assert "TRIPPED" not in window.mot_coil_readback.text()
    assert window.chip_text("coils") == "COILS 1.0 A"

    # USB row statuses stay compact (the 84 px column must never clip
    # text, which previously showed "Not configured" as "t configured").
    for status, expected in (
        ("no_port", "No port"),
        ("connected:/dev/cu.usbserial-14230@1", "OK"),
        ("error:something long and detailed", "Error"),
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

    # Exercise the V_tune readback slider sync and the VCO frequency
    # display.
    window._vtune_slider_synced = False
    window._on_vtune_readback(3.3)
    assert window.vtune_slider.value() == 3300
    assert window.vtune_freq_readback.text().endswith("MHz")
    window._on_vtune_readback(5.0)
    assert window.vtune_freq_readback.text() == "2970.0 MHz"
    assert window.vtune_slider.value() == 3300  # later readbacks don't move it

    # Repump button wording (no "Gate"), mirrored in the card header.
    assert window.repump_gpio_btn.text() == "Repump Off"
    assert window.chip_text("repump") == "REPUMP OFF"
    window._on_repump_gpio_state(True)
    assert window.repump_gpio_btn.text() == "Repump On"
    assert window.chip_text("repump") == "REPUMP ON"
    window._on_repump_gpio_state(False)
    assert window.repump_gpio_btn.text() == "Repump Off"

    # The repump gate and the dither both follow the laser off -- they are
    # meaningless without it.
    window.laser_btn.setChecked(True)
    window.repump_gpio_btn.setChecked(True)
    window.mod_enable_btn.setChecked(True)
    assert window.repump_gpio_btn.isChecked()
    assert window.mod_enable_btn.isChecked()
    window.laser_btn.setChecked(False)
    assert not window.repump_gpio_btn.isChecked()
    assert not window.mod_enable_btn.isChecked()
    assert window.chip_text("repump") == "REPUMP OFF"
    assert window.mod_enable_btn.text() == "Modulation Off"
    # ...but neither follows it back on: that stays a deliberate act.
    window.laser_btn.setChecked(True)
    assert not window.repump_gpio_btn.isChecked()
    assert not window.mod_enable_btn.isChecked()
    window.laser_btn.setChecked(False)

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
    assert lw._plot_paused  # trace held still while selecting
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

    # Determine signal offset defaults on, matching linien's server default.
    assert window.auto_offset_check.isChecked()
    window.auto_offset_check.setChecked(False)
    assert window._settings.value("linien/determine_offset") in (False, "false")
    window.auto_offset_check.setChecked(True)

    # --- Lock PSD: linien's noise-analysis window ---
    import numpy as np

    window._open_psd_window()  # rp host empty -> worker exits with no_host
    psd = window._psd_window
    assert psd is not None and psd.isVisible()

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

    # --- Dispenser override dialog + apply flow (modal exec skipped) ---
    dlg = mot_control.DispenserOverrideDialog(window, 7.5, 30.0)
    assert dlg.values() == (7.5, 30.0)
    dlg.current_spin.setValue(12.5)
    dlg.duration_spin.setValue(60.0)
    assert dlg.values() == (12.5, 60.0)
    dlg._fill_defaults()
    assert dlg.values() == (7.5, 30.0)

    # An override drives the current slider, which is now the setpoint
    # display as well as the control.
    assert window.dispenser_current_slider.value() == 7500
    window._apply_dispenser_override(10.0, 45.0)
    assert window.dispenser_current_slider.value() == 10000
    assert window.dispenser_current_edit.text() == "10.000 A"
    assert window.dispenser_fire_btn.text() == "Fire Dispenser (45 s)"
    window._apply_dispenser_override(7.5, 30.0)
    assert window.dispenser_current_slider.value() == 7500

    # Log pane mirrored the events, and the header shows the last one.
    assert "dispenser_override" in window.log_view.toPlainText()
    assert "dispenser_fire_end" in window.log_view.toPlainText()
    assert window.log_card.header_status.text() != ""

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
        window.dispenser_override_btn,
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

    # --- Config snapshot + JSON export round-trip ---
    window.current_slider.setValue(85)
    window.mot_coil_current_slider.setValue(2200)
    config = window._collect_config()
    assert config["laser"]["current_ma"] == 85
    assert config["coils"]["current_a"] == 2.2
    assert config["dispenser"]["fire_current_a"] == 7.5
    assert config["psd"]["max_decimation"] == int(
        window._settings.value("psd/max_decimation")
    )
    assert config["repump"]["vco_mhz"] == round(
        mot_control.vtune_to_vco_mhz(window.vtune_slider.value() / 1000.0), 1
    )
    assert config["linien"]["pid"] is None  # no linien connection in smoke
    assert config["saved_at"]
    assert config["linien"]["filters"]["filter_1_frequency"] == 12000
    config_path = os.path.join(TMP, "config-export.json")
    window._write_config_json(config_path)
    with open(config_path, encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["laser"]["current_ma"] == 85
    assert loaded["coils"]["current_a"] == 2.2
    assert loaded["connection"]["mot_coil_address"] == 1

    # --- Config load: change everything, then restore from the file ---
    window.current_slider.setValue(60)
    window.mot_coil_current_slider.setValue(500)
    window.vtune_slider.setValue(1000)
    window._settings.setValue("psd/max_decimation", 14)
    window._set_filters_ui({"automatic": False, "filter_1_frequency": 777})
    window._apply_dispenser_override(12.0, 40.0)
    orig_question = QtWidgets.QMessageBox.question
    QtWidgets.QMessageBox.question = (
        lambda *a, **k: QtWidgets.QMessageBox.Yes
    )
    try:
        window._apply_config(loaded, path=config_path)
    finally:
        QtWidgets.QMessageBox.question = orig_question
    assert window.current_slider.value() == 85
    assert window.mot_coil_current_slider.value() == 2200
    assert int(window._settings.value("psd/max_decimation")) == (
        loaded["psd"]["max_decimation"]
    )
    assert window.filter1_freq_spin.value() == 12000
    assert window._dispenser_fire_current == 7.5  # confirm-gated restore
    assert window._dispenser_fire_seconds == 30.0
    # partial configs apply without touching anything else
    window._apply_config({"laser": {"current_ma": 92}})
    assert window.current_slider.value() == 92
    assert window.mot_coil_current_slider.value() == 2200

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
    assert "DEVICES 3/3" in window.usb_card.header_status.text()
    assert window.chip_text("usb") == "DEVICES 3/3"
    assert window.laser_usb_status.text() == "Verified"
    assert not window.usb_card.content.isVisibleTo(window.usb_card)
    assert hasattr(window, "laser_worker")  # serial workers were started

    window._stop_serial_workers()
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
    assert "DEVICES 1/3" in window.usb_card.header_status.text()
    assert window.chip_text("usb") == "DEVICES 1/3"
    assert window.coil_usb_status.text() == "Not found"
    assert not window.usb_card.content.isVisibleTo(window.usb_card)
    app.processEvents()

    # With worst-case texts everywhere (card expanded, protection tripped,
    # override active, cooldown countdown), the control column must fit
    # its scroll viewport with no horizontal scrolling.
    window._on_dispenser_readback(
        {"v_out": 50.0, "i_out": 20.0, "power": 1000.0, "output_enabled": True,
         "cv_cc": 1, "protection": 2}
    )
    window._on_mot_coil_readback(
        {"v_out": 50.0, "i_out": 5.0, "power": 250.0, "output_enabled": True,
         "cv_cc": 0, "protection": 3}
    )
    window._apply_dispenser_override(10.0, 45.0)
    window._on_dispenser_fire_state({"phase": "cooldown", "remaining_s": 872.0})
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
    window._apply_dispenser_override(7.5, 30.0)
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

    pixmap = window.grab()
    out = os.path.join(ROOT, "mot-ui-smoke.png")
    pixmap.save(out)
    print("saved", out)

    # usb card ends expanded (auto-expand persisted), log card expanded.
    window.close()  # closeEvent saves geometry/splitter/camera state
    app.processEvents()

    # --- Persistent UI state + parameter persistence round-trip ---
    w1 = mot_control.OpenQuantumControlsWindow()
    w1.show()
    app.processEvents()
    # UI state restored from the same isolated settings file — except the
    # event log, which always starts collapsed.
    assert w1.usb_card.content.isVisibleTo(w1.usb_card)  # was left expanded
    assert not w1.log_card.content.isVisibleTo(w1.log_card)
    assert w1._settings.value("ui/geometry") is not None
    assert w1._settings.value("ui/splitter_sizes") is not None
    w1.usb_card.set_expanded(False)
    w1.mod_freq_slider.setValue(23456)
    w1.current_slider.setValue(95)
    w1.therm_slider.setValue(12000)
    w1.mot_coil_voltage_slider.setValue(12000)
    w1.mot_coil_current_slider.setValue(2500)
    w1.vtune_slider.setValue(3300)
    w1.sweep_speed_combo.setCurrentIndex(4)
    w1._apply_dispenser_override(9.0, 20.0)
    w1._settings.sync()
    w1.close()
    app.processEvents()

    w2 = mot_control.OpenQuantumControlsWindow()
    assert w2.mod_freq_slider.value() == 23456
    assert w2.current_slider.value() == 95
    assert w2.therm_slider.value() == 12000
    assert w2.mot_coil_voltage_slider.value() == 12000
    assert w2.mot_coil_current_slider.value() == 2500
    assert w2.vtune_slider.value() == 3300
    assert w2.sweep_speed_combo.currentIndex() == 4
    assert w2._dispenser_fire_current == 9.0
    assert w2._dispenser_fire_seconds == 20.0
    assert w2.dispenser_current_slider.value() == 9000
    assert w2.dispenser_fire_btn.text() == "Fire Dispenser (20 s)"
    assert not w2.usb_card.content.isVisibleTo(w2.usb_card)  # collapsed again
    w2.close()
    app.processEvents()
finally:
    QtCore.QSettings = _orig_qsettings

# --- Event log wrote real JSONL records for the exercised paths ---
log_files = glob.glob(os.path.join(LOG_DIR, "*.jsonl"))
assert log_files, "no event log files written"
logged_events = []
for path in log_files:
    with open(path, encoding="utf-8") as f:
        for line in f:
            logged_events.append(json.loads(line)["event"])
for expected in (
    "app_start",
    "app_stop",
    "usb_probe",
    "readback",
    "protection_trip",
    "dispenser_fire_start",
    "dispenser_fire_end",
    "dispenser_override",
    "vtune",
    "lock",
    "psd_window",
    "psd_complete",
    "config_saved",
    "config_loaded",
):
    assert expected in logged_events, f"missing log event: {expected}"

print("SMOKE OK")
