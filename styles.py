from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

# One family and one body size for the whole UI, plots included.
UI_FONT_FAMILY = "Space Grotesk"
UI_FONT_PX = 13
PLOT_AXIS_COLOR = "#969696"

# ---------------------------------------------------------------------------
# Global application stylesheet — applied once at startup
# ---------------------------------------------------------------------------
APP_STYLESHEET = """
    * {
        font-family: "Space Grotesk", "Segoe UI", sans-serif;
    }

    QMainWindow, QWidget#central {
        background-color: #0b0b0b;
    }

    /* Dialogs are top-level windows, so they miss the rule above and
       fall back to the platform palette -- a light panel in a dark app. */
    QDialog {
        background-color: #0b0b0b;
        color: #e6e6e6;
    }
    /* Form labels inside a dialog have no class of their own, so without
       this they render in the platform's near-black on the dark panel --
       the PID dialog's P/I/D captions were invisible. Scoped to QDialog
       so the main window's classed labels keep their own colours. */
    QDialog QLabel {
        color: #e6e6e6;
        font-size: 13px;
        border: none;
    }

    QScrollArea {
        border: none;
        background: transparent;
    }

    /* --- Card panels --- */
    QFrame.card {
        background-color: #151515;
        border: 1px solid #282828;
        border-radius: 0px;
    }

    /* --- Section titles --- */
    QLabel.section-title {
        color: #ababab;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 1px;
        text-transform: uppercase;
        padding: 6px 12px 4px 12px;
        border: none;
    }

    /* --- Buttons --- */
    QPushButton {
        background-color: #1d1d1d;
        color: #e6e6e6;
        border: 1px solid #2e2e2e;
        border-radius: 0px;
        font-weight: 600;
        font-size: 13px;
        padding: 6px 16px;
    }
    QPushButton:hover {
        background-color: #313131;
        border-color: #404040;
    }
    QPushButton:pressed {
        background-color: #181818;
    }
    /* Checked means the thing is ON, so it reads brighter than the idle
       state. Dimming it here made every active output look disabled. */
    QPushButton:checked {
        background-color: #3a3a3a;
        color: #ffffff;
        border-color: #6f6f6f;
    }
    QPushButton:checked:hover {
        background-color: #454545;
        border-color: #8a8a8a;
    }
    QPushButton:disabled {
        color: #6a6a6a;
        border-color: #262626;
    }

    /* --- Checkboxes --- */
    QCheckBox {
        color: #e6e6e6;
        font-size: 13px;
        border: none;
        padding: 4px 2px;
        spacing: 8px;          /* gap between the box and its label */
    }
    QCheckBox::indicator {
        width: 14px;
        height: 14px;
    }

    /* --- Sliders --- */
    QSlider::groove:horizontal {
        height: 4px;
        background: #1d1d1d;
        border-radius: 0px;
    }
    QSlider::handle:horizontal {
        background: #5e5e5e;
        width: 12px;
        height: 12px;
        border-radius: 0px;
        margin: -4px 0;
    }
    QSlider::handle:horizontal:hover {
        background: #707070;
    }
    QSlider::sub-page:horizontal {
        background: #383838;
        border-radius: 0px;
    }

    /* --- Line edits --- */
    QLineEdit {
        background: #171717;
        color: #e6e6e6;
        border: 1px solid #1d1d1d;
        border-radius: 0px;
        /* Without this an editable value inherits the app default and
           renders larger than the read-only value beside it. */
        font-size: 13px;
        padding: 4px 6px;
        selection-background-color: #383838;
    }
    QLineEdit:focus {
        color: white;
        border-color: #5e5e5e;
    }

    /* --- Readout labels --- */
    QLabel.readout-label {
        color: #969696;
        font-weight: 600;
        font-size: 13px;
        border: none;
    }
    QLabel.readout-value {
        color: #e6e6e6;
        font-size: 13px;
        border: none;
    }

    /* --- Scroll bar --- */
    QScrollBar:vertical {
        background: #0b0b0b;
        width: 6px;
        border: none;
    }
    QScrollBar::handle:vertical {
        background: #2e2e2e;
        border-radius: 0px;
        min-height: 30px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
"""

# ---------------------------------------------------------------------------
# Lock indicator
# ---------------------------------------------------------------------------
LOCK_INDICATOR_LOCKED = """
    QFrame {
        background-color: rgba(30, 200, 100, 15);
        border: 1px solid rgba(30, 200, 100, 60);
        border-radius: 0px;
    }
"""
LOCK_INDICATOR_UNLOCKED = """
    QFrame {
        background-color: rgba(200, 60, 60, 12);
        border: 1px solid rgba(200, 60, 60, 40);
        border-radius: 0px;
    }
"""
LOCK_LABEL_LOCKED = "color: #6f9e7c; font-size: 13px; font-weight: 700; border: none; letter-spacing: 1px;"
LOCK_LABEL_UNLOCKED = "color: #b56a6a; font-size: 13px; font-weight: 700; border: none; letter-spacing: 1px;"

PD_READOUT_STYLE = "color: white; font-size: 20px; font-weight: 700; border: none;"

# ---------------------------------------------------------------------------
# Big editable number fields
# ---------------------------------------------------------------------------
BIG_READOUT_STYLE = """
    QLineEdit {
        background: #111111;
        color: white;
        border: 1px solid #2e2e2e;
        border-radius: 0px;
        font-size: 20px;
        font-weight: 700;
        font-family: "Space Grotesk";
        padding: 4px 8px;
    }
    QLineEdit:focus {
        border-color: #5e5e5e;
        background: #141414;
    }
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_card() -> QtWidgets.QFrame:
    """Create a styled card frame for grouping controls."""
    frame = QtWidgets.QFrame()
    frame.setProperty("class", "card")
    frame.setStyleSheet("""
        QFrame {
            background-color: #151515;
            border: 1px solid #282828;
            border-radius: 0px;
        }
    """)
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(6)
    return frame


# Accents are deliberately desaturated. On a near-black panel a saturated
# hue reads as decoration; a muted one reads as an instrument, and status
# colours still separate cleanly at a glance.
ACCENT = "#c4c4c4"  # section titles, headings, neutral emphasis
STATUS_OK = "#6f9e7c"
STATUS_WARN = "#b9924f"
STATUS_BAD = "#b56a6a"

# Retained so older imports keep working; prefer ACCENT in new code.
NEON_BLUE = ACCENT


def make_section_title(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text.upper())
    label.setAlignment(QtCore.Qt.AlignLeft)
    label.setStyleSheet(
        f"color: {ACCENT}; font-size: 13px; font-weight: 600;"
        "letter-spacing: 1px; padding: 6px 4px 3px 4px; border: none;"
    )
    return label


def add_slider_row(parent_layout, label_text, min_val, max_val, init_val,
                   single=1, page=1, suffix="", decimals=0, scale=1.0):
    """Create a labeled slider row. Returns (slider, value_edit)."""
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 3, 0, 3)
    row.setSpacing(8)

    label = QtWidgets.QLabel(label_text)
    label.setStyleSheet("color: #ababab; font-size: 13px; font-weight: 600; border: none;")
    label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    label.setFixedWidth(120)
    row.addWidget(label)

    slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
    slider.setRange(min_val, max_val)
    slider.setSingleStep(single)
    slider.setPageStep(page)
    slider.setValue(init_val)
    slider.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    row.addWidget(slider, stretch=1)

    if decimals > 0:
        display_val = init_val / scale
        value_edit = QtWidgets.QLineEdit(f"{display_val:.{decimals}f}{suffix}")
    else:
        value_edit = QtWidgets.QLineEdit(f"{init_val}{suffix}")
    value_edit.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    value_edit.setFixedWidth(72)
    row.addWidget(value_edit)

    parent_layout.addLayout(row)

    value_edit.min_val = min_val
    value_edit.max_val = max_val
    value_edit.suffix = suffix
    value_edit.slider = slider
    value_edit.decimals = decimals
    value_edit.scale = scale

    return slider, value_edit


def add_readout_row(parent_layout, label_text, initial_text="--", unit=""):
    """Read-only readout: label on left, value on right. Returns value QLabel."""
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 3, 0, 3)
    row.setSpacing(8)

    label = QtWidgets.QLabel(label_text)
    label.setStyleSheet("color: #969696; font-size: 13px; font-weight: 600; border: none;")
    label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    label.setFixedWidth(120)
    row.addWidget(label)

    value_label = QtWidgets.QLabel(f"{initial_text} {unit}".strip())
    value_label.setStyleSheet("color: #e6e6e6; font-size: 13px; border: none;")
    value_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    row.addWidget(value_label, stretch=1)

    parent_layout.addLayout(row)
    value_label.unit = unit
    return value_label


def plot_label_style():
    """Axis-label kwargs for setLabel(), matching the UI's type."""
    return {
        "font-size": f"{UI_FONT_PX}px",
        "font-family": UI_FONT_FAMILY,
        "color": PLOT_AXIS_COLOR,
    }


def style_plot_axes(plot):
    """Put a pyqtgraph plot's tick labels on the UI's font and size.

    pyqtgraph draws ticks with its own default font, which is neither the
    app family nor the app size, so plots otherwise read as a foreign
    element pasted into the window.
    """
    font = QtGui.QFont(UI_FONT_FAMILY)
    font.setPixelSize(UI_FONT_PX)
    for side in ("left", "bottom"):
        axis = plot.getAxis(side)
        axis.setTickFont(font)
        axis.setTextPen(PLOT_AXIS_COLOR)
    return plot
