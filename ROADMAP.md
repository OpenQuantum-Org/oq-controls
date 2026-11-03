# Roadmap

What future work must keep, what is done, what was decided against, and what
is still open. The hard rules (safety, threading, linien) live in `CLAUDE.md`;
this file is the design brief. Read both before adding anything.

Last updated 2026-10-21.

---

## Keep this: the design the operator likes

New work should look and behave like what is already here. When in doubt,
copy the nearest existing card rather than inventing a pattern.

### Look

- **Dark, flat, muted.** Near-black background (`#0b0b0b`), cards a step
  lighter (`#151515`, 1 px `#282828` border), **square corners everywhere**
  (`border-radius: 0`). No gradients, shadows or bright accents. Every colour
  is in `styles.py`; take it from there, never write a new hex in a widget.
- **Colour means state, nothing else.** Neutral grey (`ACCENT`) for titles and
  emphasis; green / amber / red (`STATUS_OK/WARN/BAD`) only for whether a
  thing is OK. A checked button reads *brighter*, because checked means on.
- **One font, one size.** Space Grotesk, 13 px, plots included
  (`UI_FONT_FAMILY`, `UI_FONT_PX`). Weight 600 for buttons and titles, 700
  for status. Uppercase, letter-spaced section titles.
- **Shared edges.** Things stacked in the plot column share
  `PLOT_COLUMN_MARGINS`; boxes on one row share a height. Small alignment
  drift is noticed.

### Layout

- **Plot on the left, a column of collapsible cards on the right**, one card
  per subsystem (`_add_*_card` in `mot_control.py`). Cards start collapsed.
- **Every card header carries a status chip** (DEVICES 1/4, LASER OFF,
  LOCKED, COILS TRIP…), so the collapsed column reads from across the lab.
- **Physical units, never raw ones.** mA, °C, MHz, V — not ohms, DAC counts or
  tune volts. Conversions live in one place and show enough decimals that one
  arrow press moves the last digit shown.
- Fewer controls is better. A control that is not used on the bench gets
  removed (see *Decided against*), not hidden behind a checkbox.

### Behaviour

- **Every slider has a typed value box**: type, Enter, clamped and applied.
  Up/Down step one displayed digit, PageUp/PageDown ten.
- **Nothing actuates by surprise.** The wheel never changes an unfocused
  control; restored settings set the UI only; actuation asks first
  (`CLAUDE.md`, *Hardware safety*).
- **Hardware-paced input.** A drag is coalesced into one write; a held key on
  the repump is capped at 0.2 MHz/s so the VCO and lock can follow.
- **Say what the hardware is actually doing.** Desired and actual are kept
  separate and divergence is shown, not papered over. Faults go to the log
  pane, once per occurrence, never to the console.
- **Tooltips on every control**, saying what it does to the hardware and the
  one thing that commonly goes wrong.
- **The UI must feel immediate.** The operator's hands-on verdict beats an
  architecturally cleaner design (the reverted threading fix below).

### Code

- Small, plain, matching what is around it. No abstraction for one caller.
- Comments say *why* a value is what it is, with `file:line` into linien or
  the datasheet when that is the reason.
- New UI goes in a subsystem module or card method, styles in `styles.py`.
- Verification is `/verify` (five suites + read the screenshot), then the
  operator on the bench. Don't pile up tests.

---

## Done

- **CLAUDE.md and the feedback loop** — edit → `smoke_ui.py` → read
  `mot-ui-smoke.png`, wrapped as `/verify`.
- **The UI** — the theme above, collapsible cards with status chips, the PSD
  window ported from linien, SIMPLE-mode locking with determine-offset,
  physical units throughout.
- **Logging** — categorised JSONL event log, `EventLog.command()` with
  `duration_ms`, collapsed repeat faults, linien/RPyC logging captured.
- **Safe shutdown** — per-output checklist on close.
- **Server liveness** — stalled linien-server detected from its heartbeat,
  restartable from the Devices card.
- **First MOT, 2026-10-19/20.** Lock at ~2912 MHz on the repump, tune slowly
  to ~2920. Cause of the week-on-week difference not isolated; see
  `CLAUDE.md`.
- **Bench changes, 2026-10-16..21** — laser setpoints restored after a power
  cycle, dispenser lockout removed, Sweep Centre box, frozen lock-point trace,
  repump slider 2850–2950 MHz with a held-key rate limit, lock selections
  logged.

## Decided against

Each was built, used and removed. Don't rebuild without being asked.

- **Linien threading fix** (2026-08-14). Correct, but setpoints landed a poll
  late and the plot stuttered. *If revisited:* a second connection or a
  server-side batch call, not a queue in front of the existing one.
- **Splitting `mot_control.py`.** Navigable by card, and the churn buys
  nothing. Revisit only if panels keep being added.
- **Track repump** (2026-10-16..19). Rewrote Mod Phase and the signal offset
  while locked as the repump moved. Never helped get a MOT; the lock broke
  more easily.
- **Record Repump Scan** (2026-10-19..21). Diagnostic I/Q scan across the
  repump range. Traces kept in `logs/repump-*.npz`.
- **Lock here + Slope** (2026-10-16..21). Locking at the sweep centre with no
  line search. Select target line is all that was used.
- **Signal Offset slider** (2026-10-16..21). Hand-set `combined_offset`.
- **Dispenser 15-min re-fire lockout** (removed 2026-10-16).

## Open

### Bench

1. **Why the MOT appeared this week and not last.** Candidates: repump sweep
   speed, lock feature. Test: same lock point, tune 2912→2925 fast
   (~1.5 MHz/s) vs slow (~0.2 MHz/s).
2. **Is the repump MHz scale right?** It is a nominal linear map of 0–5 V onto
   2800–2970 MHz, never measured against a counter or spectrum analyser.
3. **Thermistor setpoint** — default 6215 Ω (~36 °C) vs target ~27 °C. Unresolved.
4. **Does Restart Server recover the repump VCO and gate workers**, which hold
   their own connections?
5. **Aluminium lid on the optics box** makes the signals bigger but rippled.
   Suspected RF pickup; check with the beam blocked first.

### Code

- **Four RPyC connections to one Red Pitaya** (spectrum, repump VCO, repump
  gate, PSD). Consolidating has to reconcile `use_parameter_cache` (True for
  acquisition, False for writers). Revisit if the server wedges again.
- **`/bench` skill** — power-up order and what "working" looks like per
  subsystem; the gap between "constructs" and "works" is where bugs hide.
- **Headless CLI** (`dps status`, `laser read`) — no clean seam yet.
- **Unused fonts.** IBM Plex Sans (4 files, ~800 KB) is bundled and
  registered but never rendered: body and title families are both Space
  Grotesk (`mot_control.py:135-136`; the comment above them is stale). Only
  `smoke_ui.py:95` checks it is registered. Space Grotesk Medium also looks
  unreferenced (no weight 500 in any style).
