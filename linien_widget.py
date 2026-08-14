"""
Linien spectroscopy widget for the control/master laser.

Adapted from MOT_GUI-main/Linien_Widget.py — uses the same LinienWorker
from the MOT GUI. The heavy linien_client import is deferred until start()
is called to avoid slowing down UI construction.
"""

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets
from superqt import QRangeSlider

from styles import UI_FONT_PX, plot_label_style, style_plot_axes

# This experiment's Red Pitaya. The old fallback was rp-F0C6AD,
# a different device -- an empty host field quietly connected to
# someone else's hardware.
DEFAULT_RP_HOST = "rp-f0f2b5.local"


class _ShortLineSample(pg.ItemSample):
    """Legend swatch: a short line instead of pyqtgraph's wide default."""

    def boundingRect(self):
        return QtCore.QRectF(0, 0, 16, 14)

    def paint(self, painter, *args):
        opts = self.item.opts
        pen = pg.mkPen(opts["pen"])
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(0, 7, 16, 7)


class LinienWidget(QtWidgets.QWidget):
    """Linien plot + lock controls for a Red Pitaya.

    Call start() after the window is shown to begin the Linien connection
    (defers the heavy import of linien_client).

    Parameters
    ----------
    linien_host : str
        Hostname of the Red Pitaya running linien-server.
    show_lock_buttons : bool
        If False, the Lock/Unlock overlay buttons are hidden.
    v_to_freq_ghz : float or None
        If set, the x-axis is displayed in GHz instead of Volts by
        multiplying voltages by this factor.  For Koheron 780 nm lasers:
        V → 2.5 mA/V → 0.002 nm/mA → df = -(c/λ²)·dλ ≈ -2.462 GHz/V.

    Signals:
        lock_state_changed(bool): Forwarded from LinienWorker.
        connection_status_changed(str): "connected:<host>" or "disconnected".
    """
    lock_state_changed = QtCore.pyqtSignal(bool)
    connection_status_changed = QtCore.pyqtSignal(str)
    selection_state_changed = QtCore.pyqtSignal(bool)
    fault = QtCore.pyqtSignal(str)
    # The server's heartbeat stopped/resumed -- see LinienWorker.
    server_stalled = QtCore.pyqtSignal(bool)

    def __init__(self, linien_host=None, show_lock_buttons=True,
                 show_stop_button=False, sweep_range=(-1.0, 1.0),
                 v_to_freq_ghz=None, v_to_mhz=None, event_log=None,
                 parent=None):
        super().__init__(parent)
        self._event_log = event_log
        self._linien_host = linien_host or DEFAULT_RP_HOST
        self._show_lock_buttons = show_lock_buttons
        self._show_stop_button = show_stop_button
        self._sweep_min_v, self._sweep_max_v = sweep_range
        self._v_to_freq = v_to_freq_ghz  # GHz per Volt, or None
        self._v_to_mhz = v_to_mhz        # callable(v) → MHz, or None
        self.linien_thread = None  # created lazily in start()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        pg.setConfigOptions(useOpenGL=False, enableExperimental=False, imageAxisOrder="row-major")
        self.pg_widget = pg.GraphicsLayoutWidget()
        self.pg_widget.setBackground("#0b0b0b")
        layout.addWidget(self.pg_widget)

        # --- Sweep range slider ---
        self.xrange_slider = QRangeSlider(QtCore.Qt.Horizontal)
        self.xrange_slider.setMinimum(int(self._sweep_min_v * 1000))
        self.xrange_slider.setMaximum(int(self._sweep_max_v * 1000))
        self.xrange_slider.setValue((int(self._sweep_min_v * 1000),
                                     int(self._sweep_max_v * 1000)))
        self.xrange_slider.setSingleStep(1)
        self.xrange_slider.setFixedHeight(20)
        self.xrange_slider.setStyleSheet("""
            QRangeSlider { background: transparent; }
            QSlider::groove:horizontal {
                height: 4px; background: #1d1d1d; border-radius: 0px;
            }
            QSlider::sub-page:horizontal {
                background: #383838; border-radius: 0px;
            }
            QSlider::handle:horizontal {
                background: #5e5e5e; width: 12px; border-radius: 0px; margin: -4px 0;
            }
        """)
        slider_proxy = QtWidgets.QGraphicsProxyWidget()
        slider_proxy.setWidget(self.xrange_slider)
        self.pg_widget.addItem(slider_proxy, row=0, col=0)
        self.xrange_slider.valueChanged.connect(self._update_range)

        # --- Plot ---
        self.pg_widget.nextRow()
        self.plot = self.pg_widget.addPlot(row=1, col=0)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        style_plot_axes(self.plot)

        # No left-axis label; the tick values speak for themselves. The SI
        # prefix stays off so pyqtgraph does not rescale the ticks and put
        # a "(x0.001)" multiplier where the label would have been.
        self.plot.getAxis("left").enableAutoSIPrefix(False)

        if self._v_to_mhz is not None:
            self.plot.setLabel("bottom", "VCO Frequency (MHz)",
                               **plot_label_style())
        elif self._v_to_freq is not None:
            self.plot.setLabel("bottom", "Detuning (MHz)",
                               **plot_label_style())
        else:
            self.plot.setLabel("bottom", "Sweep Voltage (V)",
                               **plot_label_style())

        # pyqtgraph's own auto-range button: a small "A" that appears over
        # the plot on hover. Autozoom replaces it.
        self.plot.hideButtons()

        self.plot.setYRange(-0.05, 0.10)
        init_xmin, init_xmax = self._v_range_to_display(
            self._sweep_min_v, self._sweep_max_v)
        # padding=0 matches _limit_xrange() so the axis doesn't jump on
        # the first slider interaction.
        self.plot.setXRange(init_xmin, init_xmax, padding=0)
        self.plot.setMouseEnabled(x=True, y=True)
        self.original_xrange = self.plot.viewRange()[0]
        self.original_yrange = self.plot.viewRange()[1]

        self.ERROR_COLOR = "#5cb8ff"
        self.MONITOR_COLOR = "#c080e0"

        # Compact legend in the corner: small type, tight spacing and a
        # short sample line, so it reads as a key rather than a panel.
        # Must be created *before* the curves -- addLegend only registers
        # named items added after it.
        self.legend = self.plot.addLegend(
            # Both negative anchors the legend to the plot's bottom-right.
            offset=(-8, -8),
            labelTextSize=f"{UI_FONT_PX - 2}px",
            labelTextColor="#9a9a9a",
            brush=pg.mkBrush(11, 11, 11, 170),
            pen=pg.mkPen(40, 40, 40, 160),
            verSpacing=-5,
            horSpacing=4,
            sampleType=_ShortLineSample,
        )

        self.error_curve = self.plot.plot(
            pen=pg.mkPen(self.ERROR_COLOR, width=1.5), name="Error signal")
        self.monitor_curve = self.plot.plot(
            pen=pg.mkPen(self.MONITOR_COLOR, width=1.5), name="Monitor signal")

        self.zero_line = pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen(color=(255, 80, 80, 60), style=QtCore.Qt.DotLine, width=1),
        )
        self.plot.addItem(self.zero_line)
        self.view_box = self.plot.getViewBox()
        self.view_box.sigRangeChanged.connect(self._limit_xrange)

        # --- Autolock line selection (linien's PlotWidget overlays) ---
        # Created once and shown/hidden. Adding items per selection is what
        # previously left marker lines on the plot forever.
        self.selection_running = False
        self.selection_boundaries = (0, 0)
        self._touch_start = None
        self._plot_paused = False
        self._last_error_signal = None

        self.overlay = pg.LinearRegionItem(
            values=(0, 0), movable=False,
            brush=pg.mkBrush(92, 184, 255, 40),
        )
        self.overlay.setVisible(False)
        self.plot.addItem(self.overlay, ignoreBounds=True)

        self.boundary_overlays = [
            pg.LinearRegionItem(
                values=(0, 0), movable=False, brush=(0, 0, 0, 200)
            )
            for _ in range(2)
        ]
        for i, overlay in enumerate(self.boundary_overlays):
            overlay.setVisible(False)
            # Hide the inner edge line so the mask reads as a shaded band.
            overlay.lines[i].setPen((0, 0, 0, 0))
            self.plot.addItem(overlay, ignoreBounds=True)

        self.plot.scene().sigMouseMoved.connect(self._selection_mouse_moved)
        self.pg_widget.viewport().installEventFilter(self)

        # Nothing sits over the traces: the lock controls and
        # the autozoom checkbox are handed to the host window.
        # Unparented: the host window places this with its other view
        # controls (see autozoom_check).
        self.autozoom_check = QtWidgets.QCheckBox("Autozoom")
        self.autozoom_check.setChecked(True)
        self.autozoom_check.setCursor(QtCore.Qt.PointingHandCursor)
        self.autozoom_check.setStyleSheet(
            "QCheckBox { color: #e6e6e6; font-size: 13px; font-weight: 700;"
            "border: none; padding: 4px 6px; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
        )
        self.autozoom_check.toggled.connect(self._set_autozoom)
        self._set_autozoom(True)

        self.lock_controls = QtWidgets.QWidget()
        lock_row = QtWidgets.QHBoxLayout(self.lock_controls)
        lock_row.setContentsMargins(0, 0, 0, 0)
        lock_row.setSpacing(6)
        if self._show_lock_buttons:
            # linien's wording: you pick a line, the lock follows.
            self.lock_button = QtWidgets.QPushButton("Select target line")
            self.lock_button.setCursor(QtCore.Qt.PointingHandCursor)
            self.lock_button.clicked.connect(self._run_lock)
            lock_row.addWidget(self.lock_button)

            # Shown only while a selection is in progress, as linien swaps
            # auto_mode_not_activated for auto_mode_activated.
            self.abort_button = QtWidgets.QPushButton("Abort")
            self.abort_button.setCursor(QtCore.Qt.PointingHandCursor)
            self.abort_button.clicked.connect(self.abort_selection)
            self.abort_button.setVisible(False)
            lock_row.addWidget(self.abort_button)

            self.unlock_button = QtWidgets.QPushButton("Unlock")
            self.unlock_button.setCursor(QtCore.Qt.PointingHandCursor)
            self.unlock_button.clicked.connect(self._run_unlock)
            lock_row.addWidget(self.unlock_button)

            self.selection_state_changed.connect(self._on_selection_state)
            self._set_locked_ui(False)

        if self._show_stop_button:
            self.stop_sweep_btn = QtWidgets.QPushButton("Stop Sweep")
            self.stop_sweep_btn.setCursor(QtCore.Qt.PointingHandCursor)
            self.stop_sweep_btn.clicked.connect(self._stop_sweep)
            lock_row.addWidget(self.stop_sweep_btn)

    # --- Voltage ↔ display coordinate helpers ---

    def _v_to_display(self, v):
        """Convert a single voltage to display x coordinate."""
        if self._v_to_mhz is not None:
            return float(self._v_to_mhz(v))
        if self._v_to_freq:
            return v * self._v_to_freq * 1000.0
        return v

    def _v_range_to_display(self, v_min, v_max):
        """Convert a voltage range to display range (sorted)."""
        a = self._v_to_display(v_min)
        b = self._v_to_display(v_max)
        return (min(a, b), max(a, b))

    # --- Deferred start ---

    def start(self):
        """Import LinienWorker (heavy) and start the acquisition thread."""
        try:
            from linien_worker import LinienWorker
        except ImportError as e:
            self.fault.emit(f"linien_worker not importable, plot is static: {e}")
            return

        # Connecting takes seconds (RPyC handshake, possibly an SSH
        # server start). Say so, or the row reads "--" and looks broken.
        self.connection_status_changed.emit(f"connecting:{self._linien_host}")
        self.linien_thread = LinienWorker(
            host=self._linien_host, event_log=self._event_log
        )
        self.linien_thread.new_data.connect(self._update_plot)
        self.linien_thread.initial.connect(self._initialize_slider)
        self.linien_thread.lock_state.connect(self._on_lock_state)
        self.linien_thread.connection_status.connect(self.connection_status_changed.emit)
        self.linien_thread.fault.connect(self.fault.emit)
        self.linien_thread.server_stalled.connect(self.server_stalled.emit)
        self.linien_thread.start()

    # --- Slots ---

    def _initialize_slider(self, data):
        center = data.get("center", 0.0)
        amplitude = data.get("amplitude", 1.0)
        x_min, x_max = center - amplitude, center + amplitude
        d_min, d_max = self._v_range_to_display(x_min, x_max)
        self.plot.setXRange(d_min, d_max, padding=0)
        self.original_xrange = (d_min, d_max)
        self.xrange_slider.blockSignals(True)
        self.xrange_slider.setValue((int(x_min * 1000), int(x_max * 1000)))
        self.xrange_slider.blockSignals(False)

    def _update_plot(self, data):
        voltages = data["voltages"]
        err = data["error_signal"]
        mon = data["monitor_signal"]
        if voltages.size == 0:
            return
        # Keep the newest spectrum for the autolock even while paused.
        self._last_error_signal = err
        if self._plot_paused and self._touch_start is None:
            # Paused with no drag in progress: the release that should
            # have resumed it never arrived (the cursor left the widget,
            # say). A frozen spectrum with no way back is worse than a
            # trace that moves slightly under the cursor.
            self._resume_plot()
        if self._plot_paused:
            # Mid-drag: hold the trace still so the line does not move
            # under the cursor while it is being picked.
            return
        if self._v_to_mhz is not None:
            x = self._v_to_mhz(voltages)
        elif self._v_to_freq is not None:
            x = voltages * self._v_to_freq * 1000.0
        else:
            x = voltages
        if err.size == voltages.size:
            self.error_curve.setData(x, err)
        if mon.size == voltages.size:
            self.monitor_curve.setData(x, mon)

    def _update_range(self, values):
        slider_min, slider_max = values
        x_min, x_max = slider_min / 1000, slider_max / 1000
        center = (x_min + x_max) / 2
        amplitude = (x_max - x_min) / 2
        if self.linien_thread is not None:
            self.linien_thread.request_sweep_change(center, amplitude)
        # Plot axis always follows the slider — both when shrinking and
        # when expanding. _limit_xrange (on sigRangeChanged) still clamps
        # mouse-driven zoom/pan to the sweep bounds.
        d_min, d_max = self._v_range_to_display(x_min, x_max)
        self.plot.setXRange(d_min, d_max, padding=0)

    def _limit_xrange(self):
        vb = self.plot.getViewBox()
        current_xrange, _ = vb.viewRange()
        slider_min, slider_max = self.xrange_slider.value()
        d_min, d_max = self._v_range_to_display(slider_min / 1000, slider_max / 1000)
        if current_xrange[0] < d_min or current_xrange[1] > d_max:
            vb.setXRange(d_min, d_max, padding=0)

    def _set_autozoom(self, on):
        """Track the signal on Y, or hold whatever range is showing.

        Switching it back on also resets X to the full sweep range, so a
        tick-off/tick-on undoes a zoom -- this is the Reset Zoom button
        the checkbox replaced. X is not auto-ranged continuously, since
        that would fight _limit_xrange.
        """
        self.plot.enableAutoRange(axis="y", enable=bool(on))
        if on:
            slider_min, slider_max = self.xrange_slider.value()
            d_min, d_max = self._v_range_to_display(
                slider_min / 1000, slider_max / 1000
            )
            self.plot.setXRange(d_min, d_max, padding=0)
        else:
            self.plot.setYRange(*self.plot.viewRange()[1], padding=0)

    def _run_lock(self):
        """Begin line selection. The lock starts when the drag completes."""
        if self.linien_thread is None:
            return
        self.start_selection()

    def _run_unlock(self):
        self.abort_selection()
        if self.linien_thread is not None:
            self.linien_thread.unlock_laser()

    # --- Line selection, ported from linien's PlotWidget ---

    def start_selection(self):
        """Enable area selection over the trace (linien: selectable 0.99).

        The trace keeps running until a drag actually starts. linien
        pauses for the whole of selection mode because it caches spectra
        to hand to the autolock; ours re-reads the spectrum in the worker
        and only needs the length, so holding the plot for longer than
        the drag buys nothing -- and a click that was too short to count
        as a selection used to leave it frozen with no way back.
        """
        if self.selection_running:
            return
        self._enable_area_selection(selectable_width=0.99)
        self.selection_state_changed.emit(True)

    def abort_selection(self):
        if not self.selection_running:
            return
        self._disable_area_selection()
        self._resume_plot()
        self.selection_state_changed.emit(False)

    def _enable_area_selection(self, selectable_width=0.99):
        self.selection_running = True
        xmin, xmax = self.plot.viewRange()[0]
        span = xmax - xmin
        # A little wider than exact: linien notes glitches otherwise.
        extra = span / 100.0
        boundary = (span * (1 - selectable_width)) / 2.0
        self.selection_boundaries = (xmin + boundary, xmax - boundary)

        self.boundary_overlays[0].setRegion(
            (xmin - extra, xmin + boundary)
        )
        self.boundary_overlays[1].setRegion(
            (xmax - boundary, xmax + extra)
        )
        for overlay in self.boundary_overlays:
            overlay.setVisible(True)
        # Panning would fight the drag-select.
        self.view_box.setMouseEnabled(x=False, y=False)

    def _disable_area_selection(self):
        self.selection_running = False
        self._touch_start = None
        self.overlay.setVisible(False)
        for overlay in self.boundary_overlays:
            overlay.setVisible(False)
        self.view_box.setMouseEnabled(x=True, y=True)

    def _pause_plot(self):
        """Hold the trace still so the line does not move while selecting."""
        self._plot_paused = True

    def _resume_plot(self):
        self._plot_paused = False

    def _within_boundaries(self, x):
        lo, hi = self.selection_boundaries
        return min(max(x, lo), hi)

    def _selection_mouse_moved(self, pos):
        if not self.selection_running or self._touch_start is None:
            return
        if not self.view_box.sceneBoundingRect().contains(pos):
            return
        x = self._within_boundaries(self.view_box.mapSceneToView(pos).x())
        x0 = self._touch_start
        self.overlay.setRegion((min(x0, x), max(x0, x)))

    def eventFilter(self, obj, event):
        """Drag-select on the plot viewport.

        The plot's mouse events reach the graphics view, not this widget,
        so press/release are filtered there rather than overridden here.
        """
        if not self.selection_running:
            return False
        kind = event.type()
        if kind == QtCore.QEvent.MouseButtonPress:
            self._on_selection_press(event)
            return True
        if kind == QtCore.QEvent.MouseButtonRelease:
            self._on_selection_release(event)
            return True
        return False

    def _event_x(self, event):
        scene_pos = self.pg_widget.mapToScene(event.pos())
        return self._within_boundaries(
            self.view_box.mapSceneToView(scene_pos).x()
        )

    def _on_selection_press(self, event):
        x = self._event_x(event)
        # Held only for the drag, so the line does not move under the
        # cursor while it is being picked.
        self._pause_plot()
        self._touch_start = x
        self.overlay.setRegion((x, x))
        self.overlay.setVisible(True)

    def _on_selection_release(self, event):
        self._resume_plot()  # every exit path below leaves the trace live
        if self._touch_start is None:
            return
        x = self._event_x(event)
        x0 = self._touch_start
        lo, hi = self.selection_boundaries
        span = hi - lo
        self.overlay.setVisible(False)
        self._touch_start = None

        # A drag under 1% of the span is a click, not a selection: this is
        # what stops a stray click starting a lock. Selection stays armed
        # so the next drag counts, as it does in linien.
        if span <= 0 or abs(x - x0) / span < 0.01:
            return

        self._disable_area_selection()
        self.selection_state_changed.emit(False)
        self._start_autolock(*sorted((x0, x)))

    def _start_autolock(self, x_from, x_to):
        """Map the selected display range to spectrum indices and lock.

        Only the indices travel to the worker: it re-reads the spectrum
        itself in raw counts, which is what the autolock needs.
        """
        spectrum = self._last_error_signal
        if self.linien_thread is None or spectrum is None:
            return
        n = int(np.asarray(spectrum).size)
        if n < 4:
            return
        xmin, xmax = self._v_range_to_display(
            self._sweep_min_v, self._sweep_max_v
        )
        span = xmax - xmin
        if span == 0:
            return
        i0 = int(round((x_from - xmin) / span * (n - 1)))
        i1 = int(round((x_to - xmin) / span * (n - 1)))
        i0 = max(0, min(n - 1, i0))
        i1 = max(0, min(n - 1, i1))
        if i1 - i0 < 2:
            return
        # The worker re-reads the spectrum in raw counts; it only
        # needs the selected indices and the length they refer to.
        self.linien_thread.lock_laser(i0, i1, n)

    def _on_lock_state(self, locked: bool):
        self._set_locked_ui(locked)
        self.lock_state_changed.emit(locked)

    def _on_selection_state(self, selecting: bool):
        """Swap Select for Abort while a drag-selection is running."""
        if not self._show_lock_buttons:
            return
        self.lock_button.setVisible(not selecting)
        self.abort_button.setVisible(selecting)

    def _set_locked_ui(self, locked: bool):
        if not self._show_lock_buttons:
            return
        if locked:
            # A lock engaging ends any selection still in progress.
            self.abort_selection()
        self.lock_button.setVisible(not locked and not self.selection_running)
        self.abort_button.setVisible(self.selection_running)
        self.unlock_button.setVisible(locked)

    def _stop_sweep(self):
        if self.linien_thread is not None:
            self.linien_thread.stop_sweep()

    def set_host(self, host):
        """Change the Linien host and reconnect."""
        self.stop()
        self._linien_host = host
        self.start()

    def stop(self):
        if self.linien_thread is not None:
            self.linien_thread.stop()
            self.linien_thread = None
