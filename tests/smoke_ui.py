"""Offscreen UI smoke test: construct the full UI without touching hardware.

Run: python tests/smoke_ui.py
Refreshes mot-ui-smoke.png. Uses an isolated settings file and log
directory so nothing real is read from hardware or written to the user's
QSettings.
"""

import glob
import json
import os
import sys
import tempfile

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

# Keep the smoke test inert: never start hardware workers (they would read
# real saved QSettings ports/hosts on this machine), log to a temp dir,
# and use an isolated settings file for EVERY QSettings in the app.
mot_control.OpenQuantumControlsWindow._deferred_start = lambda self: None
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


def resolved_family(widget):
    """The family a widget will ACTUALLY render with (not just the CSS)."""
    widget.ensurePolished()
    return QtGui.QFontInfo(widget.font()).family()


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
        "dispenser_voltage_slider",
        "dispenser_current_slider",
        "master_state_label",
    ):
        assert not hasattr(window, gone), gone

    # USB card is collapsible and collapsed by default.
    assert not window.usb_card.content.isVisibleTo(window.usb_card)
    window.usb_card.set_expanded(True)
    app.processEvents()
    assert window.usb_card.content.isVisibleTo(window.usb_card)
    window.usb_card.set_expanded(False)
    app.processEvents()
    assert not window.usb_card.content.isVisibleTo(window.usb_card)

    # Log pane exists, collapsed by default.
    assert not window.log_card.content.isVisibleTo(window.log_card)

    # Repump card sits above the power-supply card.
    assert window.repump_card.y() < window.dps_card.y()

    # Title text and fonts: titles render in Space Grotesk, body in IBM
    # Plex Sans — verified via the resolved font, not the stylesheet text.
    assert window.title_label.text() == "OpenQuantum"
    assert resolved_family(window.title_label) == "Space Grotesk"
    assert resolved_family(window.usb_card.toggle_btn) == "Space Grotesk"
    section = mot_control.make_section_title("PROBE")
    section.show()
    app.processEvents()
    assert resolved_family(section) == "Space Grotesk"
    assert resolved_family(window.laser_status) == "IBM Plex Sans"
    assert resolved_family(window.dispenser_fire_btn) == "IBM Plex Sans"

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
    assert window.health_chips["lock"].text() == "LOCKED"
    window._on_lock_state(False)
    assert window.health_chips["lock"].text() == "UNLOCKED"

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
    assert window.health_chips["disp"].text() == "DISP FIRING"
    assert window.dispenser_progress.isVisibleTo(window)
    assert window.dispenser_progress.value() == 500  # 15 s of 30 s left
    window._on_dispenser_fire_state({"phase": "cooldown", "remaining_s": 872.0})
    assert not window.dispenser_fire_btn.isEnabled()
    assert "14:32" in window.dispenser_fire_btn.text()
    assert window.health_chips["disp"].text() == "DISP 14:32"
    assert window.dispenser_progress.value() == 969  # 872 s of 900 s left
    window._on_dispenser_fire_state({"phase": "idle", "remaining_s": 0.0})
    assert window.dispenser_fire_btn.isEnabled()
    assert window.dispenser_override_btn.isEnabled()
    assert "Fire Dispenser (30 s)" == window.dispenser_fire_btn.text()
    assert not window.dispenser_progress.isVisibleTo(window)
    assert window.health_chips["disp"].text() == "DISP IDLE"

    # Dispenser readback shows CC/CV only while the output is on, and DPS
    # protection trips are surfaced in red on both supplies (and chips).
    window._on_dispenser_readback(
        {"v_out": 0.08, "i_out": 7.5, "power": 0.6, "output_enabled": True,
         "cv_cc": 1, "protection": 0}
    )
    assert window.dispenser_readback.text().endswith("/ CC")
    window._on_dispenser_readback(
        {"v_out": 0.0, "i_out": 0.0, "power": 0.0, "output_enabled": False,
         "cv_cc": 0, "protection": 1}
    )
    assert window.dispenser_readback.text().startswith("OVP TRIPPED")
    window._on_mot_coil_readback(
        {"v_out": 0.0, "i_out": 0.0, "power": 0.0, "output_enabled": False,
         "cv_cc": 0, "protection": 2}
    )
    assert window.mot_coil_readback.text().startswith("OCP TRIPPED")
    assert window.health_chips["coils"].text() == "COILS TRIP"
    window._on_mot_coil_readback(
        {"v_out": 5.0, "i_out": 1.0, "power": 5.0, "output_enabled": True,
         "cv_cc": 0, "protection": 0}
    )
    assert "TRIPPED" not in window.mot_coil_readback.text()
    assert window.health_chips["coils"].text() == "COILS 1.0 A"

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

    # Fire history counter accumulates from fire summaries.
    window._on_dispenser_fire_summary(
        {"elapsed_s": 12.0, "planned_s": 30.0, "current_a": 7.5, "aborted": True}
    )
    assert window.dispenser_history_label.text() == "1 / 12 s total"
    window._on_dispenser_fire_summary(
        {"elapsed_s": 30.0, "planned_s": 30.0, "current_a": 7.5, "aborted": False}
    )
    assert window.dispenser_history_label.text() == "2 / 42 s total"

    # Exercise the V_tune readback slider sync and the VCO frequency
    # display.
    window._vtune_slider_synced = False
    window._on_vtune_readback(3.3)
    assert window.vtune_slider.value() == 3300
    assert window.vtune_freq_readback.text().endswith("MHz")
    window._on_vtune_readback(5.0)
    assert window.vtune_freq_readback.text() == "2970.0 MHz"
    assert window.vtune_slider.value() == 3300  # later readbacks don't move it

    # Repump button wording (no "Gate").
    assert window.repump_gpio_btn.text() == "Repump Off"
    window._on_repump_gpio_state(True)
    assert window.repump_gpio_btn.text() == "Repump On"
    window._on_repump_gpio_state(False)
    assert window.repump_gpio_btn.text() == "Repump Off"

    # --- Dispenser override dialog + apply flow (modal exec skipped) ---
    dlg = mot_control.DispenserOverrideDialog(window, 7.5, 30.0)
    assert dlg.values() == (7.5, 30.0)
    dlg.current_spin.setValue(12.5)
    dlg.duration_spin.setValue(60.0)
    assert dlg.values() == (12.5, 60.0)
    dlg._fill_defaults()
    assert dlg.values() == (7.5, 30.0)

    assert "7.50 A CC / 30 s fire" in window.dispenser_setpoint_label.text()
    assert "OVERRIDE" not in window.dispenser_setpoint_label.text()
    window._apply_dispenser_override(10.0, 45.0)
    assert (
        "10.00 A CC / 45 s fire  (OVERRIDE)"
        == window.dispenser_setpoint_label.text()
    )
    assert window.dispenser_fire_btn.text() == "Fire Dispenser (45 s)"
    window._apply_dispenser_override(7.5, 30.0)  # defaults, marker clears
    assert "OVERRIDE" not in window.dispenser_setpoint_label.text()

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
    assert window.usb_card.header_status.text() == "3/3 verified"
    assert window.health_chips["usb"].text() == "USB 3/3"
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
    assert "1/3 verified" == window.usb_card.header_status.text()
    assert window.health_chips["usb"].text() == "USB 1/3"
    assert window.coil_usb_status.text() == "Not found"
    assert window.usb_card.content.isVisibleTo(window.usb_card)  # auto-expanded
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
    assert (
        "9.00 A CC / 20 s fire  (OVERRIDE)" == w2.dispenser_setpoint_label.text()
    )
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
):
    assert expected in logged_events, f"missing log event: {expected}"

print("SMOKE OK")
