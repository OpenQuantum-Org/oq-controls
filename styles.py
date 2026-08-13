from pyqtgraph.Qt import QtCore, QtWidgets

# ---------------------------------------------------------------------------
# Global application stylesheet — applied once at startup
# ---------------------------------------------------------------------------
APP_STYLESHEET = """
    * {
        font-family: "Share Tech Mono", "Menlo", "Segoe UI", "Helvetica Neue", sans-serif;
    }

    QMainWindow, QWidget#central {
        background-color: #0a0a0f;
    }

    QScrollArea {
        border: none;
        background: transparent;
    }

    /* --- Card panels --- */
    QFrame.card {
        background-color: #14141c;
        border: 1px solid #1e1e2a;
        border-radius: 10px;
    }

    /* --- Section titles --- */
    QLabel.section-title {
        color: #c0c0d4;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 1px;
        text-transform: uppercase;
        padding: 6px 12px 4px 12px;
        border: none;
    }

    /* --- Buttons --- */
    QPushButton {
        background-color: #1e1e2e;
        color: #ececf4;
        border: 1px solid #2a2a3a;
        border-radius: 6px;
        font-weight: 600;
        font-size: 13px;
        padding: 6px 16px;
    }
    QPushButton:hover {
        background-color: #282840;
        border-color: #3a3a55;
    }
    QPushButton:pressed {
        background-color: #1a1a28;
    }
    QPushButton:checked {
        background-color: #1a3a6a;
        color: #70b0ff;
        border-color: #2855a0;
    }

    /* --- Sliders --- */
    QSlider::groove:horizontal {
        height: 4px;
        background: #1e1e2e;
        border-radius: 2px;
    }
    QSlider::handle:horizontal {
        background: #5080cc;
        width: 12px;
        height: 12px;
        border-radius: 6px;
        margin: -4px 0;
    }
    QSlider::handle:horizontal:hover {
        background: #6090dd;
    }
    QSlider::sub-page:horizontal {
        background: #2a4a80;
        border-radius: 2px;
    }

    /* --- Line edits --- */
    QLineEdit {
        background: #12121a;
        color: #ececf4;
        border: 1px solid #1e1e2e;
        border-radius: 4px;
        padding: 2px 6px;
        selection-background-color: #2a4a80;
    }
    QLineEdit:focus {
        color: white;
        border-color: #4070b0;
    }

    /* --- Readout labels --- */
    QLabel.readout-label {
        color: #a0a0b4;
        font-weight: 600;
        font-size: 12px;
        border: none;
    }
    QLabel.readout-value {
        color: #ececf4;
        font-size: 13px;
        border: none;
    }

    /* --- Scroll bar --- */
    QScrollBar:vertical {
        background: #0a0a0f;
        width: 6px;
        border: none;
    }
    QScrollBar::handle:vertical {
        background: #2a2a3a;
        border-radius: 3px;
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
        border-radius: 10px;
    }
"""
LOCK_INDICATOR_UNLOCKED = """
    QFrame {
        background-color: rgba(200, 60, 60, 12);
        border: 1px solid rgba(200, 60, 60, 40);
        border-radius: 10px;
    }
"""
LOCK_LABEL_LOCKED = "color: #40dd80; font-size: 15px; font-weight: 700; border: none; letter-spacing: 1px;"
LOCK_LABEL_UNLOCKED = "color: #dd5050; font-size: 15px; font-weight: 700; border: none; letter-spacing: 1px;"

PD_READOUT_STYLE = "color: white; font-size: 22px; font-weight: 700; border: none;"

# ---------------------------------------------------------------------------
# Big editable number fields
# ---------------------------------------------------------------------------
BIG_READOUT_STYLE = """
    QLineEdit {
        background: #0e0e16;
        color: white;
        border: 1px solid #2a2a3a;
        border-radius: 8px;
        font-size: 20px;
        font-weight: 700;
        font-family: "Share Tech Mono", "Menlo", "Consolas", monospace;
        padding: 4px 8px;
    }
    QLineEdit:focus {
        border-color: #4070b0;
        background: #111119;
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
            background-color: #14141c;
            border: 1px solid #1e1e2a;
            border-radius: 10px;
        }
    """)
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(6)
    return frame


NEON_BLUE = "#00d4ff"

def make_section_title(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text.upper())
    label.setAlignment(QtCore.Qt.AlignLeft)
    label.setStyleSheet(
        f"color: {NEON_BLUE}; font-size: 11px; font-weight: 600;"
        "letter-spacing: 1px; padding: 2px 4px; border: none;"
    )
    return label


def add_slider_row(parent_layout, label_text, min_val, max_val, init_val,
                   single=1, page=1, suffix="", decimals=0, scale=1.0):
    """Create a labeled slider row. Returns (slider, value_edit)."""
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(8)

    label = QtWidgets.QLabel(label_text)
    label.setStyleSheet("color: #c0c0d4; font-size: 12px; font-weight: 600; border: none;")
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
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(8)

    label = QtWidgets.QLabel(label_text)
    label.setStyleSheet("color: #a0a0b4; font-size: 12px; font-weight: 600; border: none;")
    label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    label.setFixedWidth(120)
    row.addWidget(label)

    value_label = QtWidgets.QLabel(f"{initial_text} {unit}".strip())
    value_label.setStyleSheet("color: #ececf4; font-size: 13px; border: none;")
    value_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    row.addWidget(value_label, stretch=1)

    parent_layout.addLayout(row)
    value_label.unit = unit
    return value_label
