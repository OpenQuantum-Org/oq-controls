"""Lock-noise PSD window.

A port of linien 2.1.0's noise-analysis window
(``linien-gui/linien_gui/ui/psd_window.py``, ``psd_plot_widget.py`` and
``psd_table_widget.py``, reference tree ``C:\\Users\\kostu\\Desktop\\linien``).
The plotting, stitching and table logic are linien's; only the app plumbing
differs -- linien reads RPyC parameters directly through its own app
singleton, we drive the same server through ``PsdWorker`` signals, and the
layout is built by hand rather than loaded from a ``.ui`` file.

Do not re-derive this against ``klab-linien-relocking_stable``: that tree is
2.2.0.dev0 and renames both the RPCs and the counts-to-volts constant.
"""

import pickle
from datetime import datetime, timezone
from time import time

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from styles import (
    UI_FONT_FAMILY,
    UI_FONT_PX,
    make_card,
    make_section_title,
)

# Same explicit combo styling the main window uses; unstyled combos fall
# back to the platform palette and render white on this dark theme.
COMBO_STYLE = """
    QComboBox {
        background: #161618;
        color: #e4e4e7;
        border: 1px solid #404040;
        border-radius: 0px;
        padding: 2px 6px;
        font-size: 13px;
    }
    QComboBox:focus { color: white; border-color: #949494; }
    QComboBox::drop-down { border: none; width: 18px; }
    QComboBox QAbstractItemView {
        background: #161618;
        color: #e4e4e7;
        selection-background-color: #35353d;
        selection-color: white;
        outline: none;
    }
"""

# linien reports PSD amplitudes in ADC counts; its GUI divides by this to
# plot volts (linien_gui.ui.plot_widget.V in 2.1.0).
V = 8192

# linien_common.common.PSDAlgorithm order: WELCH = 0, LPSD = 1. The combo is
# indexed by enum position, so these two must stay in that order.
PSD_ALGORITHMS = [
    ("Scipy (faster but prone to glitches)", 0),
    ("LPSD", 1),
]

# linien's psd_window.ui maxMeasurementTimeComboBox: index i selects
# psd_acquisition_max_decimation = 12 + i.
MEASUREMENT_TIMES = [
    "0.5 s", "1.1 s", "2.1 s", "4.3 s", "8.6 s", "17.2 s", "34.4 s",
    "68.7 s", "137 s", "275 s", "9 min", "18 min", "36 min", "73 min",
    "147 min", "293 min", "586 min", "20 h", "40 h", "78 h", "7 days",
]
MIN_DECIMATION = 12

# linien_gui.utils.RandomColorChoser's LUT, in order.
CURVE_COLORS = [
    "#2e8b57", "#0000ff", "#87cefa", "#ff1493", "#adff2f", "#b03060",
    "#6495ed", "#90ee90", "#dc143c", "#ffff00", "#483d8b", "#f08080",
    "#8b4513", "#00ff00", "#da70d6", "#f4a460", "#008000", "#00ff7f",
    "#808000", "#7b68ee", "#a9a9a9", "#ff8c00", "#00008b", "#f0e68c",
    "#ff0000", "#800080", "#2f4f4f", "#ff00ff", "#00ffff", "#8a2be2",
]


def _superscript_minus():
    """U+207B if the UI font has it, else a fallback that renders.

    Space Grotesk has every superscript digit but not the superscript
    minus, which would otherwise draw as a missing-glyph box on every
    negative exponent.
    """
    for candidate in ("⁻", "−", "-"):
        try:
            raw = QtGui.QRawFont.fromFont(QtGui.QFont(UI_FONT_FAMILY))
            if raw.supportsCharacter(candidate):
                return candidate
        except Exception:
            break
    return "-"


class CustomLogAxis(pg.AxisItem):
    """Log axis whose negative exponents are also in scientific notation.

    linien's CustomLogAxis, with the superscript minus swapped for one
    the bundled font can actually draw.
    """

    def logTickStrings(self, values, scale, spacing):
        estrings = [
            f"{x:0.1e}"
            for x in 10 ** np.array(values).astype(float) * np.array(scale)
        ]
        convdict = {
            "0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3",
            "4": "\u2074", "5": "\u2075", "6": "\u2076", "7": "\u2077",
            "8": "\u2078", "9": "\u2079",
        }
        dstrings = []
        for e in estrings:
            if e.count("e"):
                v, p = e.split("e")
                sign = _superscript_minus() if p[0] == "-" else ""
                if p[1:].count("0") == len(p[1:]):
                    pot = convdict["0"]
                else:
                    pot = "".join([convdict[pp] for pp in p[1:].lstrip("0")])
                v = v.rstrip(".0")
                v = "" if v == "1" else v + "\u00b7"
                dstrings.append(v + "10" + sign + pot)
            else:
                dstrings.append(e)
        return dstrings


class PsdPlotWidget(pg.PlotWidget):
    """linien's PSDPlotWidget: stitched log-log curves plus a crosshair."""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            background="#0b0b0b",
            axisItems={
                "bottom": CustomLogAxis(orientation="bottom"),
                "left": CustomLogAxis(orientation="left"),
            },
        )
        self.curves = {}

        self.setLogMode(x=True, y=True)
        self.setLabel("left", "PSD", units="V / Sqrt[Hz]")
        self.setLabel("bottom", "Frequency", units="Hz")
        self.getAxis("left").enableAutoSIPrefix(False)
        self.getAxis("bottom").enableAutoSIPrefix(False)
        self.showGrid(x=True, y=True, alpha=0.15)

        font = QtGui.QFont()
        font.setPixelSize(UI_FONT_PX)
        for side in ("left", "bottom"):
            self.getAxis(side).setTickFont(font)
            self.getAxis(side).setTextPen("#969696")

        self.vertical_line = pg.InfiniteLine(angle=90, movable=False)
        self.vertical_line.hide()
        self.horizontal_line = pg.InfiniteLine(angle=0, movable=False)
        self.horizontal_line.hide()
        self.cursor_label = pg.TextItem()
        self.scene().sigMouseMoved.connect(self.mouseMoved)

        for element in (
            self.vertical_line, self.horizontal_line, self.cursor_label
        ):
            # ignoreBounds: these must not affect automatic axis scaling.
            self.addItem(element, ignoreBounds=True)
            element.setZValue(10000)

        self.recalculate_min_max()

    def plot_curve(self, uuid, psds, color):
        """Plot one run, stitching its decimation stages. linien's algorithm.

        The stages cover overlapping frequency bands, so they are walked
        from the highest decimation (lowest frequencies) upward and each
        contributes only the points above what is already drawn; the
        previous segment's last point is prepended to keep the joins
        continuous.
        """
        self.curves[uuid] = self.curves.get(uuid, [])
        for idx in range(len(psds)):
            if len(self.curves[uuid]) <= idx:
                curve = pg.PlotCurveItem()
                self.curves[uuid].append(curve)
                self.addItem(curve)

        # sort such that high decimations are first
        psds_sorted = sorted(psds.items(), key=lambda v: -1 * v[0])
        highest_plotted_frequency = 0
        highest_plotted_frequency_psd = None

        for idx, (_decimation, (f, psd)) in enumerate(psds_sorted):
            curve = self.curves[uuid][idx]
            f = np.asarray(f, dtype=float)
            psd = np.asarray(psd, dtype=float)

            psd = psd[f > highest_plotted_frequency]
            f = f[f > highest_plotted_frequency]
            if f.size == 0:
                continue

            if highest_plotted_frequency_psd is not None:
                # this connects first and last points of adjacent segments
                f = np.array([highest_plotted_frequency] + list(f))
                psd = np.array([highest_plotted_frequency_psd] + list(psd))

            highest_plotted_frequency = f[-1]
            highest_plotted_frequency_psd = psd[-1]

            curve.setData(np.log10(f), np.log10(psd / V))
            curve.setPen(pg.mkPen(QtGui.QColor(color)))

        self.recalculate_min_max()

    def recalculate_min_max(self):
        self._x_min = np.inf
        self._x_max = -np.inf
        self._y_min = np.inf
        self._y_max = -np.inf
        for curves in self.curves.values():
            for curve in curves:
                x, y = curve.getData()
                if x is None or len(x) == 0:
                    continue
                self._x_min = min(self._x_min, np.min(x))
                self._x_max = max(self._x_max, np.max(x))
                self._y_min = min(self._y_min, np.min(y))
                self._y_max = max(self._y_max, np.max(y))

    def show_or_hide_curve(self, uuid, show):
        for curve in self.curves.get(uuid, []):
            curve.setVisible(show)

    def delete_curve(self, uuid):
        for curve in self.curves.get(uuid, []):
            self.removeItem(curve)
        self.curves.pop(uuid, None)
        self.recalculate_min_max()

    def clear_curves(self):
        for uuid in list(self.curves):
            self.delete_curve(uuid)

    def mouseMoved(self, pos):
        if self.sceneBoundingRect().contains(pos):
            point = self.plotItem.vb.mapSceneToView(pos)
            x, y = point.x(), point.y()
            self.cursor_label.setHtml(
                f"<span style='font-size: {UI_FONT_PX}px; color: #e6e6e6'>"
                f"({10**x:.1e}, {10**y:.1e})</span>"
            )
            # Put the label on whichever side of the crosshair has room.
            self.cursor_label.setAnchor((1 if x > self._x_max / 2 else 0, 1))
            self.cursor_label.setPos(x, y)
            self.vertical_line.setPos(x)
            self.horizontal_line.setPos(y)
            self.show_cursor_position(True)
        else:
            self.show_cursor_position(False)

    def show_cursor_position(self, show):
        self.vertical_line.setVisible(show)
        self.horizontal_line.setVisible(show)
        self.cursor_label.setVisible(show)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.show_cursor_position(False)


class PsdTableWidget(QtWidgets.QTableWidget):
    """One row per run: show/hide, colour, time, P, I, D, integrated noise.

    linien's PSDTableWidget.
    """

    show_or_hide_curve = QtCore.pyqtSignal(str, bool)

    COLUMNS = ("Display", "Color", "Time", "P", "I", "D", "\u222b Noise")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(self.COLUMNS))
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.verticalHeader().setVisible(False)
        self.setStyleSheet(
            "QTableWidget { background: #0b0b0b; color: #e6e6e6;"
            " border: 1px solid #282828; font-size: %(px)dpx;"
            " gridline-color: #282828; }"
            "QHeaderView { background: #171717; border: 0px; }"
            "QHeaderView::section { background: #171717; color: #ababab;"
            " border: 0px; padding: 4px; font-size: %(px)dpx; }"
            "QTableWidget::item:selected { background: #383838; }"
            "QTableCornerButton::section { background: #171717;"
            " border: 0px; }"
            % {"px": UI_FONT_PX}
        )
        self.uuids = []

    def add_curve(self, uuid, data, color):
        if uuid not in self.uuids:
            row = self.rowCount()
            self.setRowCount(row + 1)
            self.uuids.append(uuid)
        else:
            row = self.uuids.index(uuid)

        checkbox = QtWidgets.QCheckBox()
        checkbox.setChecked(True)
        checkbox.setStyleSheet("margin-left:auto; margin-right:auto;")
        checkbox.stateChanged.connect(
            lambda status, uuid=uuid: self.show_or_hide_curve.emit(
                uuid, status > 0
            )
        )
        self.setCellWidget(row, 0, checkbox)

        swatch = QtWidgets.QLabel()
        swatch.setStyleSheet(f"background-color: {color};")
        self.setCellWidget(row, 1, swatch)

        def create_item(text):
            item = QtWidgets.QTableWidgetItem(str(text))
            item.setFlags(item.flags() ^ QtCore.Qt.ItemIsEditable)
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            return item

        stamp = datetime.fromtimestamp(
            data["time"], tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        self.setItem(row, 2, create_item(stamp))
        self.setItem(row, 3, create_item(data["p"]))
        self.setItem(row, 4, create_item(data["i"]))
        self.setItem(row, 5, create_item(data["d"]))
        self.setItem(row, 6, create_item(f"{data['fitness']:.4f}"))
        self.resizeColumnsToContents()

    def delete_selected_curve(self):
        current = self.currentRow()
        if not self.uuids or current == -1:
            return None
        try:
            uuid = self.uuids.pop(current)
        except IndexError:
            return None
        self.removeRow(current)
        row_count = len(self.uuids)
        if row_count:
            self.setCurrentCell(
                current if row_count > current else current - 1, 0
            )
        return uuid

    def clear_curves(self):
        self.setRowCount(0)
        self.uuids = []


class PsdWindow(QtWidgets.QMainWindow):
    """linien's noise-analysis window, driven by our PsdWorker.

    ``worker`` may be None until a measurement is started; the host sets
    it with ``set_worker`` so the window can outlive one connection.
    """

    def __init__(self, parent=None, settings=None, event_log=None):
        super().__init__(parent)
        self.setWindowTitle("Lock Noise Analysis")
        self.resize(1100, 780)
        self._settings = settings
        self._event_log = event_log
        self._worker = None

        self._colors = {}
        self._color_index = 0
        self._data = {}
        self._complete_uids = []
        self._locked = False

        central = QtWidgets.QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        root.addLayout(self._build_settings_row())
        root.addWidget(self._build_run_controls())

        self.plot = PsdPlotWidget()
        root.addWidget(self.plot, stretch=1)

        self.table = PsdTableWidget()
        self.table.setFixedHeight(160)
        self.table.show_or_hide_curve.connect(self.plot.show_or_hide_curve)
        root.addWidget(self.table)

        root.addLayout(self._build_trace_buttons())

    # --- construction helpers ---

    def _build_settings_row(self):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)

        row.addWidget(self._label("Maximum measurement time:"))
        self.time_combo = QtWidgets.QComboBox()
        self.time_combo.setStyleSheet(COMBO_STYLE)
        for text in MEASUREMENT_TIMES:
            self.time_combo.addItem(text)
        saved = MIN_DECIMATION
        if self._settings is not None:
            saved = int(
                self._settings.value("psd/max_decimation", 18) or 18
            )
        self.time_combo.setCurrentIndex(
            max(0, min(len(MEASUREMENT_TIMES) - 1, saved - MIN_DECIMATION))
        )
        self.time_combo.currentIndexChanged.connect(self._on_time_changed)
        row.addWidget(self.time_combo, stretch=1)

        row.addWidget(self._label("Algorithm:"))
        self.algo_combo = QtWidgets.QComboBox()
        self.algo_combo.setStyleSheet(COMBO_STYLE)
        for text, _value in PSD_ALGORITHMS:
            self.algo_combo.addItem(text)
        saved_algo = 1
        if self._settings is not None:
            saved_algo = int(self._settings.value("psd/algorithm", 1) or 1)
        self.algo_combo.setCurrentIndex(
            next(
                (i for i, (_t, v) in enumerate(PSD_ALGORITHMS)
                 if v == saved_algo),
                1,
            )
        )
        self.algo_combo.currentIndexChanged.connect(self._on_algorithm_changed)
        row.addWidget(self.algo_combo, stretch=1)
        return row

    def _build_run_controls(self):
        card = make_card()
        layout = card.layout()

        self.not_running_container = QtWidgets.QWidget()
        idle = QtWidgets.QVBoxLayout(self.not_running_container)
        idle.setContentsMargins(0, 0, 0, 0)
        idle.setSpacing(6)
        idle.addWidget(make_section_title("Single PSD measurement"))
        self.start_btn = QtWidgets.QPushButton("Start single PSD measurement")
        self.start_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self.start_psd)
        idle.addWidget(self.start_btn)
        layout.addWidget(self.not_running_container)

        self.running_container = QtWidgets.QWidget()
        busy = QtWidgets.QVBoxLayout(self.running_container)
        busy.setContentsMargins(0, 0, 0, 0)
        busy.setSpacing(6)
        self.running_label = QtWidgets.QLabel("PSD measurement running!")
        self.running_label.setStyleSheet(
            f"color: #b9924f; font-size: {UI_FONT_PX}px; font-weight: 700;"
            " border: none;"
        )
        busy.addWidget(self.running_label)
        self.stop_btn = QtWidgets.QPushButton("Stop PSD measurement")
        self.stop_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self.stop_psd)
        busy.addWidget(self.stop_btn)
        layout.addWidget(self.running_container)
        self.running_container.hide()
        return card

    def _build_trace_buttons(self):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.delete_btn = QtWidgets.QPushButton("Delete selected")
        self.delete_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.delete_btn.clicked.connect(self.delete_curve)
        row.addWidget(self.delete_btn)
        self.import_btn = QtWidgets.QPushButton("Import traces")
        self.import_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.import_btn.clicked.connect(self.import_psd)
        row.addWidget(self.import_btn)
        self.export_btn = QtWidgets.QPushButton("Export traces")
        self.export_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.export_btn.clicked.connect(self.export_psd)
        row.addWidget(self.export_btn)
        row.addStretch()
        return row

    @staticmethod
    def _label(text):
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(
            f"color: #ababab; font-size: {UI_FONT_PX}px; font-weight: 600;"
            " border: none;"
        )
        return label

    # --- host interface ---

    def set_worker(self, worker):
        self._worker = worker

    def on_status(self, status):
        """Swap the run controls, as linien does on psd_acquisition_running."""
        self._locked = bool(status.get("locked"))
        busy = bool(status.get("acquiring") or status.get("optimizing"))
        self.running_container.setVisible(busy)
        self.not_running_container.setVisible(not busy)

    # --- linien's PSDWindow methods ---

    def psd_data_received(self, data):
        curve_uuid = data["uuid"]
        if curve_uuid in self._complete_uids:
            # in networks with high latency it may happen that psd data is
            # not received in order. We do not want to update a complete
            # plot with a partial one --> stop here
            return
        if data["complete"]:
            self._complete_uids.append(curve_uuid)

        # reuse the colour of this curve's previous partial plot, or take
        # the next one if it has not been plotted yet
        if curve_uuid not in self._colors:
            self._colors[curve_uuid] = CURVE_COLORS[
                self._color_index % len(CURVE_COLORS)
            ]
            self._color_index += 1
        color = self._colors[curve_uuid]

        self.plot.plot_curve(curve_uuid, data["psds"], color)
        self.table.add_curve(curve_uuid, data, color)
        self._data[curve_uuid] = data

    def start_psd(self):
        if not self._locked:
            QtWidgets.QMessageBox.critical(
                self,
                "Not locked",
                "Laser has to be locked for PSD measurement!",
            )
            return
        if self._worker is not None:
            self._worker.start_psd()
        if self._event_log is not None:
            self._event_log.log("psd_start")

    def stop_psd(self):
        if self._worker is not None:
            self._worker.stop_psd()
        if self._event_log is not None:
            self._event_log.log("psd_stop")

    def delete_curve(self):
        uuid = self.table.delete_selected_curve()
        if uuid is not None:
            self.plot.delete_curve(uuid)
            self._data.pop(uuid, None)
            self._colors.pop(uuid, None)
            if uuid in self._complete_uids:
                self._complete_uids.remove(uuid)

    def clear_curves(self):
        self.plot.clear_curves()
        self.table.clear_curves()
        self._data = {}
        self._colors = {}
        self._complete_uids = []

    def export_psd(self):
        default_ext = ".pickle"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export PSD traces", "", f"PICKLE (*{default_ext})"
        )
        if not path:
            return
        if not path.endswith(default_ext):
            path += default_ext
        with open(path, "wb") as fh:
            pickle.dump(
                {
                    "linien-version": "2.1.0",
                    "time": time(),
                    "psd-data": self._data,
                },
                fh,
            )
        if self._event_log is not None:
            self._event_log.log("psd_export", path=path, runs=len(self._data))

    def import_psd(self):
        default_ext = ".pickle"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import PSD traces", "", f"PICKLE (*{default_ext})"
        )
        if not path:
            return
        with open(path, "rb") as fh:
            data = pickle.load(fh)
        if "linien-version" not in data:
            QtWidgets.QMessageBox.critical(
                self, "Import failed", "Not a linien PSD trace file."
            )
            return
        for psd_data in data["psd-data"].values():
            self.psd_data_received(psd_data)
        if self._event_log is not None:
            self._event_log.log(
                "psd_import", path=path, runs=len(data["psd-data"])
            )

    # --- settings ---

    def apply_settings(self):
        """Re-read the persisted settings (after a config load)."""
        if self._settings is None:
            return
        decimation = int(self._settings.value("psd/max_decimation", 18) or 18)
        self.time_combo.setCurrentIndex(
            max(0, min(len(MEASUREMENT_TIMES) - 1, decimation - MIN_DECIMATION))
        )
        algorithm = int(self._settings.value("psd/algorithm", 1) or 1)
        self.algo_combo.setCurrentIndex(
            next(
                (i for i, (_t, v) in enumerate(PSD_ALGORITHMS)
                 if v == algorithm),
                1,
            )
        )

    def _on_time_changed(self, index):
        decimation = MIN_DECIMATION + index
        if self._settings is not None:
            self._settings.setValue("psd/max_decimation", decimation)
        if self._worker is not None:
            self._worker.set_max_decimation(decimation)

    def _on_algorithm_changed(self, index):
        value = PSD_ALGORITHMS[index][1]
        if self._settings is not None:
            self._settings.setValue("psd/algorithm", value)
        if self._worker is not None:
            self._worker.set_algorithm(value)

    def closeEvent(self, event):
        # Hide rather than destroy, so traces survive reopening (linien
        # does the same).
        event.ignore()
        self.hide()
