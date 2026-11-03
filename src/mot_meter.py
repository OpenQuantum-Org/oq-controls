"""MOT brightness from the camera feed.

A box the operator drags over the cloud, and a number for how bright the
cloud is: the mean grey level inside the box minus the frame's median, so
room light and camera gain shifts mostly cancel. It turns "the MOT looks a
bit dimmer" into something two settings can be compared by.
"""

import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

# The plot's sweep-centre line uses the same amber: both mark "where you
# are looking", and neither is a trace.
ROI_COLOR = QtGui.QColor(235, 180, 70)

# Frame-to-image conversion runs on the GUI thread; 5 Hz is plenty to watch
# a MOT load or flicker and keeps the cost far inside the 16 ms budget.
METER_INTERVAL_S = 0.2
HISTORY_S = 60.0


def qimage_to_gray(image):
    """A greyscale numpy view of a QImage, downsampled 2x for speed."""
    gray = image.convertToFormat(QtGui.QImage.Format.Format_Grayscale8)
    ptr = gray.constBits()
    ptr.setsize(gray.sizeInBytes())
    rows = np.frombuffer(ptr, np.uint8).reshape(gray.height(), gray.bytesPerLine())
    return rows[::2, : gray.width() : 2].copy()


def roi_brightness(gray, roi):
    """Mean grey level inside ``roi`` minus the frame's median.

    ``roi`` is (x0, y0, x1, y1) as fractions of the frame. The median is
    the background: a MOT covers a small part of the frame, so it barely
    moves the median while room light moves both.
    """
    h, w = gray.shape
    x0, y0, x1, y1 = roi
    c0, c1 = int(x0 * w), max(int(x0 * w) + 1, int(x1 * w))
    r0, r1 = int(y0 * h), max(int(y0 * h) + 1, int(y1 * h))
    box = gray[r0:r1, c0:c1]
    if box.size == 0:
        return 0.0
    return float(box.mean()) - float(np.median(gray[::4, ::4]))


class RoiItem(QtWidgets.QGraphicsRectItem):
    """The box over the MOT: drag to move, scroll over it to resize."""

    MIN_SIZE = 12

    def __init__(self, on_change):
        super().__init__()
        self._on_change = on_change
        self._bounds = QtCore.QRectF()
        pen = QtGui.QPen(ROI_COLOR, 2)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
        self.setFlags(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        self.setToolTip(
            "Drag this box over the MOT; scroll over it to resize.\n"
            "The MOT number is the brightness inside it."
        )
        self.setZValue(1)

    def set_bounds(self, bounds):
        """The displayed video area, which the box may not leave."""
        self._bounds = QtCore.QRectF(bounds)

    def itemChange(self, change, value):
        if (
            change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange
            and not self._bounds.isEmpty()
        ):
            r = self.rect()
            x = min(max(value.x(), self._bounds.left() - r.left()),
                    self._bounds.right() - r.right())
            y = min(max(value.y(), self._bounds.top() - r.top()),
                    self._bounds.bottom() - r.bottom())
            return QtCore.QPointF(x, y)
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._on_change()
        return super().itemChange(change, value)

    def wheelEvent(self, event):
        factor = 1.1 if event.delta() > 0 else 1 / 1.1
        r = self.rect()
        w = max(self.MIN_SIZE, r.width() * factor)
        h = max(self.MIN_SIZE, r.height() * factor)
        if not self._bounds.isEmpty():
            w = min(w, self._bounds.width())
            h = min(h, self._bounds.height())
        c = r.center()
        self.setRect(QtCore.QRectF(c.x() - w / 2, c.y() - h / 2, w, h))
        self.setPos(self.pos())  # re-clamp against the bounds
        self._on_change()
        event.accept()


class VideoView(QtWidgets.QGraphicsView):
    """Camera picture with the MOT box drawn over it."""

    resized = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QtGui.QColor("#080809"))
        self.setStyleSheet("background: #080809; border: none;")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class MotMeter(QtWidgets.QWidget):
    """The MOT number and the last minute of it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        label = QtWidgets.QLabel("MOT:")
        label.setStyleSheet(
            "color: #ababab; font-size: 13px; font-weight: 600; border: none;"
        )
        label.setFixedWidth(120)
        row.addWidget(label)

        self.value_label = QtWidgets.QLabel("—")
        self.value_label.setMinimumWidth(64)
        self.value_label.setStyleSheet(
            "color: #e6e6e6; font-size: 15px; font-weight: 700; border: none;"
        )
        self.value_label.setToolTip(
            "Brightness inside the amber box, above the frame's background\n"
            "(grey levels). Higher is a bigger or denser MOT."
        )
        row.addWidget(self.value_label)

        self.spark = pg.PlotWidget()
        self.spark.setFixedHeight(34)
        self.spark.setBackground("#0b0b0b")
        self.spark.hideAxis("left")
        self.spark.hideAxis("bottom")
        self.spark.setMouseEnabled(False, False)
        self.spark.hideButtons()
        self.spark.setMenuEnabled(False)
        self.spark.setToolTip("MOT brightness over the last minute.")
        self.curve = self.spark.plot(pen=pg.mkPen(ROI_COLOR, width=1.5))
        row.addWidget(self.spark, stretch=1)

        self._history = deque()
        self._failed = False

    def add(self, value):
        now = time.monotonic()
        self._history.append((now, value))
        while self._history and now - self._history[0][0] > HISTORY_S:
            self._history.popleft()
        self.value_label.setText(f"{value:.1f}")
        t = np.fromiter((p[0] - now for p in self._history), float)
        v = np.fromiter((p[1] for p in self._history), float)
        self.curve.setData(t, v)

    def clear(self):
        self._history.clear()
        self.curve.setData([], [])
        if not self._failed:
            self.value_label.setText("—")

    def show_error(self, message):
        """Visible, not swallowed: a meter that silently stops reads as a
        MOT that silently vanished."""
        self._failed = True
        self.value_label.setText("off")
        self.value_label.setToolTip(f"MOT meter stopped: {message}")
