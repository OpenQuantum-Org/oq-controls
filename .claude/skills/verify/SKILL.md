---
name: verify
description: Run the five hardware-free test suites and inspect the rendered UI screenshot. Use after any change to mot_control.py, mot_workers.py, linien_worker.py, linien_widget.py, or styles.py, and before handing work over.
---

# Verify

Hardware-free verification for oq-controls. No device is touched — safe to run
at any time, with or without instruments connected.

## Run all five suites

```powershell
.venv\Scripts\python.exe tests\test_logging.py
.venv\Scripts\python.exe tests\test_workers.py
.venv\Scripts\python.exe tests\test_dispenser.py
.venv\Scripts\python.exe tests\test_probe.py
.venv\Scripts\python.exe tests\smoke_ui.py
```

Each prints an explicit pass banner and exits 0. Expected output:

```
LOGGING OK
WORKERS OK
ALL DISPENSER + VCO CURVE + THERMISTOR TESTS PASSED
ALL PROBE LOGIC TESTS PASSED
SMOKE OK
```

Benign noise in the smoke run — **not** failures:

- `QFontDatabase: Cannot find font directory` — Qt ships no fonts of its own.
  The test separately asserts that the bundled Space Grotesk registered; if
  that assertion passes, fonts are fine.
- `could not open port '/dev/cu.fakeA'` (and `fakeB`, `fakeC`) — the test's
  deliberate fake ports.
- `psd: no_host`, `error filter read skipped: not connected` — expected
  with no hardware.
- `This plugin does not support propagateSizeHints()` — offscreen platform.

## Then read the screenshot

**This is the part that gets skipped. Do not skip it.**

`smoke_ui.py` refreshes `mot-ui-smoke.png`. Read that file with the Read tool
and actually look at it. A green test run says the UI *constructed*; only the
screenshot says it looks right.

Check for:

- Cards in the intended order, none collapsed or expanded unexpectedly
- No clipped, overlapping, or zero-height widgets
- Text legible against its background — dark-on-dark in spinboxes and combos is
  the recurring regression here
- Values and units rendering as intended, not placeholder or `--`
- Plot axes labelled with sane ranges

## Report

State plainly what passed and what did not. If a suite fails, quote the actual
output rather than summarising it. If the screenshot shows a visual regression,
say so even when all five suites are green — that combination is common and is
exactly what this skill exists to catch.

Do not claim the UI looks correct without having read the PNG in this session.
