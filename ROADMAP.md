# Roadmap

Working sequence for the current improvement effort. One line per step and why
it sits where it does. This is the *order*; implementation detail belongs in a
plan made at the start of each session, against the code as it exists that day.

Architectural rules live in `CLAUDE.md`, not here. This file is expected to
shrink and change — edit it directly as priorities move.

Status: `[ ]` not started · `[~]` in progress · `[x]` done

Last updated 2026-08-14.

---

## [x] 0. CLAUDE.md

Environment facts, hardware-safety rules, architecture invariants, state
ownership, performance principles, linien hazards.

*Why first:* every step below inherits it. Rules written before the work get
enforced; rules written after are just documentation.

## [x] 1. Feedback loop

Confirmed working 2026-08-13: all three suites pass, and the
edit → `smoke_ui.py` → read `mot-ui-smoke.png` loop is verified end to end.
Wrapped as the `/verify` skill, whose real job is forcing the screenshot read —
a green run only proves the UI constructed, not that it looks right.

`/bench` (step 8) is the only other skill worth making, and not yet.

## [x] 2. Colours and card order

Done 2026-08-14, and it grew well past "colours and card order": the dark grey
flat theme, one font and one body size, the collapsible-card restructure, the
PSD window port, the linien locking rewrite (SIMPLE mode, determine-offset,
selection-bar cleanup), unit fixes for modulation amplitude and frequency,
temperature and MHz readouts in place of ohms and volts, and a spacing pass.

Mod Strength is now **Mod Amplitude** (`mot_control.py:1612`).

## [x] 3. Logging substrate — the experimental record

Done 2026-08-14. `mot_logging.py` carries categories (`session`/`user`/
`command`/`readback`/`state`/`fault`), `EventLog.command()` for correlated
transactions with `duration_ms`, a self-describing session header, an
excepthook on both `sys` and `threading`, and 90-day retention. Transactions
are logged in the workers, where the duration and outcome are known.
`tests/test_logging.py` covers it.

## [-] 4. Linien threading fix — done, then reverted

Implemented 2026-08-14 and reverted the same day at the bench. The queue was
correct (the race and a 30 s GUI freeze on locking were both real), but in use
it made the app feel worse: setpoints landed a poll late and device writes
shared the acquisition connection, so the plot stuttered on every control.
Tuning it — waking the loop on queue, one `to_plot` fetch per poll, debounced
setpoints — did not change the verdict.

`LinienWorker` and `LaserSetpointWorker` are back to direct GUI-thread calls.
The logging, the shutdown flags and the unit fixes survived the revert.

*If revisited:* the honest fix is not a queue in front of one connection, it is
a second connection or a server-side batch call, so writes stop competing with
acquisition. That is a bigger job than it looked.

## [x] 5. Safe shutdown

Done 2026-08-14. Closing with anything energised shows a per-item checklist —
laser, lock, dither, coils, dispenser, repump gate — everything defaulted to
off, so the safe path is the default path. Unchecking an item really leaves it
energised: the choice reaches each worker's teardown through `disable_on_stop`
/ `unlock_on_stop`. The dispenser is not offered as a choice. The
worker-level teardown is the half that holds without a clean exit.

*Still open:* the answer to "is everything off always right?" turned out to be
no, which is why this is a checklist rather than a blanket kill.

## [ ] 6. Headless hardware entry point

`python -m oq.cli dps status`, `laser read`, `linien connect`. `RidenDPSClient`
and the probe functions are already GUI-free; the rest is pulling protocol code
out of the QThread wrappers.

*Note:* this used to say "after step 4, which gives the CLI a clean seam". Step
4 is reverted, so there is no seam — the CLI would have to pull the protocol out
of the QThread wrappers itself.

## [~] 7. Structural UI, done as the module split

The **UI half is done** (step 2): filters and lock are one *Error Signal & Lock*
card, `DeviceConfigDialog` is gone entirely and replaced by the always-visible
Devices panel. Both open questions below are answered by what shipped.

What remains is only the **module split**. `mot_control.py` is still 3,400 lines
with `OpenQuantumControlsWindow` most of it, so the plan to extract each panel
"on the way out" did not happen — the UI work went in place instead. It is now
its own job rather than a side effect, which makes it less attractive: it is
pure churn against a file that currently works.

*Reconsider before doing:* the god class is a real cost only if you keep adding
panels. If the UI is settled, the split buys less than steps 3–5.

## [ ] 8. `/bench` skill

Hardware-in-the-loop checklist: power-up order, what to connect, what "working"
looks like per subsystem.

*Why last:* only writable honestly after doing steps 4–7 at the bench.

---

## Open questions

Both original questions were answered by shipping step 2: device config is one
always-visible Devices panel with a nested collapsed *Manual Setup*, and filters
and lock are a single collapsed *Error Signal & Lock* card.

What is open now is all bench work, none of it answerable from the desk:

1. **Does the lock actually engage in SIMPLE mode**, and does *Determine signal
   offset* move it? Ported from linien 2.1.0, never run against the RP.
2. **Do the temperature and frequency sliders land where they claim?** The
   Steinhart-Hart and VCO conversions are unverified against a real reading.
3. **Is the 16-38 °C range right**, and is 0.1 V / 0.1 A too coarse for setting
   coil current?
4. **Does the shutdown checklist match reality?** It reads the UI's own state
   rather than querying devices, deliberately — the close path must not block
   on a device that has stopped answering. That is only right if the UI and the
   hardware actually agree.
5. **Is the linien lock/unlock path actually working?** Reported broken on
   2026-08-14 and not reproducible from the desk — the logic tests clean against
   a fake client, and the detection matches linien's own signal naming. The
   event log records `linien.lock` with its outcome, which is where the answer
   will be.

## Ordering rationale, in one line

Rules before code, feedback loop before changes, observability before the fix
it verifies, correctness before the refactor that spreads it, and the module
split as a side effect of UI work rather than a project needing its own
justification.
