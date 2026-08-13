"""
Linien spectroscopy widget for the control/master laser.

Adapted from MOT_GUI-main/Linien_Widget.py — uses the same LinienWorker
from the MOT GUI. The heavy linien_client import is deferred until start()
is called to avoid slowing down UI construction.
"""

import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets
from superqt import QRangeSlider


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

    def __init__(self, linien_host=None, show_lock_buttons=True,
                 show_stop_button=False, sweep_range=(-1.0, 1.0),
                 v_to_freq_ghz=None, v_to_mhz=None, parent=None):
        super().__init__(parent)
        self._linien_host = linien_host or "rp-F0C6AD.local"
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
        self.pg_widget.setBackground("#0b0b0c")
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
                height: 4px; background: #1c1c1f; border-radius: 0px;
            }
            QSlider::sub-page:horizontal {
                background: #35353d; border-radius: 0px;
            }
            QSlider::handle:horizontal {
                background: #5a5a64; width: 12px; border-radius: 0px; margin: -4px 0;
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

        if self._v_to_mhz is not None:
            self.plot.setLabel("bottom", "VCO Frequency (MHz)",
                               **{"font-size": "10pt", "color": "#93939a"})
        elif self._v_to_freq is not None:
            self.plot.setLabel("bottom", "Detuning (MHz)",
                               **{"font-size": "10pt", "color": "#93939a"})
        else:
            self.plot.setLabel("bottom", "Sweep Voltage (V)",
                               **{"font-size": "10pt", "color": "#93939a"})

        self.plot.setYRange(-0.05, 0.10)
        init_xmin, init_xmax = self._v_range_to_display(
            self._sweep_min_v, self._sweep_max_v)
        # padding=0 matches _limit_xrange() so the axis doesn't jump on
        # the first slider interaction.
        self.plot.setXRange(init_xmin, init_xmax, padding=0)
        self.plot.setMouseEnabled(x=True, y=True)
        self.original_xrange = self.plot.viewRange()[0]
        self.original_yrange = self.plot.viewRange()[1]

        self.error_curve = self.plot.plot(
            pen=pg.mkPen("#5cb8ff", width=1.5), name="Error")
        self.monitor_curve = self.plot.plot(
            pen=pg.mkPen("#c080e0", width=1.5), name="Monitor")

        self.zero_line = pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen(color=(255, 80, 80, 60), style=QtCore.Qt.DotLine, width=1),
        )
        self.plot.addItem(self.zero_line)
        self.view_box = self.plot.getViewBox()
        self.view_box.sigRangeChanged.connect(self._limit_xrange)

        # --- Overlay labels ---
        vb = self.plot.getViewBox()
        overlay_label_style = "color: #a8a8ae; background: transparent; font-size: 12px; border: none;"

        self.center_label = QtWidgets.QLabel("Center: 0.000 V")
        self.center_label.setStyleSheet(overlay_label_style)
        self.center_label.setFixedWidth(120)
        proxy = QtWidgets.QGraphicsProxyWidget(vb)
        proxy.setWidget(self.center_label)
        proxy.setZValue(1000)
        proxy.setPos(22, 10)

        self.span_label = QtWidgets.QLabel("Span: 2.000 V")
        self.span_label.setStyleSheet(overlay_label_style)
        self.span_label.setFixedWidth(140)
        proxy = QtWidgets.QGraphicsProxyWidget(vb)
        proxy.setWidget(self.span_label)
        proxy.setZValue(1000)
        proxy.setPos(150, 10)

        overlay_btn_style = """
            QPushButton {
                background-color: rgba(20,20,30,200); color: #c2c2c8;
                border: 1px solid #2c2c31; border-radius: 0px;
                font-size: 12px; padding: 3px 8px;
            }
            QPushButton:hover { background-color: rgba(40,40,60,220); color: white; }
        """

        self.reset_view_btn = QtWidgets.QPushButton("Reset Zoom")
        self.reset_view_btn.setFixedSize(100, 26)
        self.reset_view_btn.setStyleSheet(overlay_btn_style)
        self.reset_view_btn.clicked.connect(self._reset_view)
        proxy = QtWidgets.QGraphicsProxyWidget(vb)
        proxy.setWidget(self.reset_view_btn)
        proxy.setZValue(1000)
        proxy.setPos(20, 45)

        if self._show_lock_buttons:
            self.lock_button = QtWidgets.QPushButton("Lock Laser")
            self.lock_button.setFixedSize(100, 26)
            self.lock_button.setStyleSheet(overlay_btn_style)
            self.lock_button.clicked.connect(self._run_lock)
            proxy = QtWidgets.QGraphicsProxyWidget(vb)
            proxy.setWidget(self.lock_button)
            proxy.setZValue(1000)
            proxy.setPos(20, 85)

            self.unlock_button = QtWidgets.QPushButton("Unlock")
            self.unlock_button.setFixedSize(100, 26)
            self.unlock_button.setStyleSheet(overlay_btn_style)
            self.unlock_button.clicked.connect(self._run_unlock)
            proxy = QtWidgets.QGraphicsProxyWidget(vb)
            proxy.setWidget(self.unlock_button)
            proxy.setZValue(1000)
            proxy.setPos(20, 120)

            self._set_locked_ui(False)

        if self._show_stop_button:
            self.stop_sweep_btn = QtWidgets.QPushButton("Stop Sweep")
            self.stop_sweep_btn.setFixedSize(100, 26)
            self.stop_sweep_btn.setStyleSheet(overlay_btn_style)
            self.stop_sweep_btn.clicked.connect(self._stop_sweep)
            proxy = QtWidgets.QGraphicsProxyWidget(vb)
            proxy.setWidget(self.stop_sweep_btn)
            proxy.setZValue(1000)
            proxy.setPos(20, 85)

        # Populate the overlay labels with calibrated units immediately,
        # so they don't sit at "0 V / 2 V" while waiting for the Linien
        # `initial` signal (or forever if no RP is connected).
        _init_center_v = (self._sweep_min_v + self._sweep_max_v) / 2
        _init_amplitude_v = (self._sweep_max_v - self._sweep_min_v) / 2
        self._update_overlay_labels(_init_center_v, _init_amplitude_v)

    # --- Voltage ↔ display coordinate helpers ---

    def _v_to_display(self, v):
        """Convert a single voltage to display x coordinate."""
        if self._v_to_mhz is not None:
            return float(self._v_to_mhz(v))
        if self._v_to_freq:
            return v * self._v_to_freq * 1000.0
        return v

    def _display_to_v(self, x):
        """Convert a display x coordinate back to voltage (approx for v_to_mhz)."""
        if self._v_to_mhz is not None:
            # Numerical inverse: linear search between sweep bounds
            import numpy as _np
            vs = _np.linspace(self._sweep_min_v, self._sweep_max_v, 1000)
            fs = _np.array([float(self._v_to_mhz(v)) for v in vs])
            idx = int(_np.argmin(_np.abs(fs - x)))
            return float(vs[idx])
        if self._v_to_freq:
            return x / (self._v_to_freq * 1000.0)
        return x

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
        except ImportError:
            print("! linien_worker not found — plot will be static")
            return

        self.linien_thread = LinienWorker(host=self._linien_host)
        self.linien_thread.new_data.connect(self._update_plot)
        self.linien_thread.initial.connect(self._initialize_slider)
        self.linien_thread.lock_state.connect(self._on_lock_state)
        self.linien_thread.connection_status.connect(self.connection_status_changed.emit)
        self.linien_thread.start()

    # --- Slots ---

    def _initialize_slider(self, data):
        center = data.get("center", 0.0)
        amplitude = data.get("amplitude", 1.0)
        self._update_overlay_labels(center, amplitude)
        x_min, x_max = center - amplitude, center + amplitude
        d_min, d_max = self._v_range_to_display(x_min, x_max)
        self.plot.setXRange(d_min, d_max, padding=0)
        self.original_xrange = (d_min, d_max)
        self.xrange_slider.blockSignals(True)
        self.xrange_slider.setValue((int(x_min * 1000), int(x_max * 1000)))
        self.xrange_slider.blockSignals(False)

    def _update_plot(self, data):
        import numpy as np
        voltages = data["voltages"]
        err = data["error_signal"]
        mon = data["monitor_signal"]
        if voltages.size == 0:
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
        self._update_overlay_labels(center, amplitude)
        if self.linien_thread is not None:
            self.linien_thread.request_sweep_change(center, amplitude)
        # Plot axis always follows the slider — both when shrinking and
        # when expanding. _limit_xrange (on sigRangeChanged) still clamps
        # mouse-driven zoom/pan to the sweep bounds.
        d_min, d_max = self._v_range_to_display(x_min, x_max)
        self.plot.setXRange(d_min, d_max, padding=0)

    def _update_overlay_labels(self, center_v, amplitude_v):
        """Update the overlay labels with voltage and/or frequency info."""
        if self._v_to_mhz is not None:
            center_mhz = float(self._v_to_mhz(center_v))
            lo_mhz = float(self._v_to_mhz(center_v - amplitude_v))
            hi_mhz = float(self._v_to_mhz(center_v + amplitude_v))
            span_mhz = abs(hi_mhz - lo_mhz)
            self.center_label.setText(f"Center: {center_mhz:.1f} MHz")
            self.span_label.setText(f"Span: {span_mhz:.1f} MHz")
        elif self._v_to_freq is not None:
            self.center_label.setText(f"Center: {center_v:+.3f} V")
            span_mhz = 2 * amplitude_v * abs(self._v_to_freq) * 1000.0
            self.span_label.setText(f"Span: {span_mhz:.0f} MHz")
        else:
            self.center_label.setText(f"Center: {center_v:+.3f} V")
            self.span_label.setText(f"Span: {2 * amplitude_v:.3f} V")

    def _limit_xrange(self):
        vb = self.plot.getViewBox()
        current_xrange, _ = vb.viewRange()
        slider_min, slider_max = self.xrange_slider.value()
        d_min, d_max = self._v_range_to_display(slider_min / 1000, slider_max / 1000)
        if current_xrange[0] < d_min or current_xrange[1] > d_max:
            vb.setXRange(d_min, d_max, padding=0)

    def _reset_view(self):
        slider_min, slider_max = self.xrange_slider.value()
        d_min, d_max = self._v_range_to_display(slider_min / 1000, slider_max / 1000)
        self.plot.setXRange(d_min, d_max, padding=0)
        self.plot.setYRange(*self.original_yrange)

    def _run_lock(self):
        if self.linien_thread is None:
            return
        selected_display = self._pick_x_positions()
        if len(selected_display) >= 2:
            # Convert display coordinates back to voltage for the lock logic
            selected_v = [self._display_to_v(x) for x in selected_display]
            selected_v.sort()
            self.linien_thread.lock_laser(selected_v)

    def _run_unlock(self):
        if self.linien_thread is not None:
            self.linien_thread.unlock_laser()

    def _pick_x_positions(self):
        """Interactive: user clicks two x positions on the plot to define lock region."""
        vb = self.plot.getViewBox()
        orange_line = pg.InfiniteLine(
            pos=0, angle=90,
            pen=pg.mkPen(color=(255, 165, 0, 180), style=QtCore.Qt.DashLine, width=1.5),
        )
        self.plot.addItem(orange_line)
        selected = []

        def mouse_move(pos):
            if vb.sceneBoundingRect().contains(pos):
                orange_line.setPos(vb.mapSceneToView(pos).x())

        def mouse_click(evt):
            if evt.button() == QtCore.Qt.LeftButton:
                pos = evt.scenePos()
                if vb.sceneBoundingRect().contains(pos):
                    x_val = vb.mapSceneToView(pos).x()
                    selected.append(x_val)
                    marker = pg.InfiniteLine(
                        pos=x_val, angle=90,
                        pen=pg.mkPen(color=(255, 165, 0, 140), width=1),
                    )
                    self.plot.addItem(marker)
                    if len(selected) >= 2:
                        loop.quit()

        vb.scene().sigMouseMoved.connect(mouse_move)
        vb.scene().sigMouseClicked.connect(mouse_click)
        loop = QtCore.QEventLoop()
        loop.exec_()
        self.plot.removeItem(orange_line)
        selected.sort()
        return selected

    def _on_lock_state(self, locked: bool):
        self._set_locked_ui(locked)
        self.lock_state_changed.emit(locked)

    def _set_locked_ui(self, locked: bool):
        if not self._show_lock_buttons:
            return
        self.lock_button.setVisible(not locked)
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
