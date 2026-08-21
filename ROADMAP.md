# Roadmap

What is left, and what was decided against. Architectural rules live in
`CLAUDE.md`, not here.

Last updated 2026-08-14.

---

## Done

- **CLAUDE.md and the feedback loop** — rules first, then the
  edit → `smoke_ui.py` → read `mot-ui-smoke.png` cycle, wrapped as the `/verify`
  skill. Its real job is forcing the screenshot read: a green run only proves
  the UI constructed.
- **The UI** — dark flat theme, one font and one size, collapsible cards, the
  PSD window ported from linien, SIMPLE-mode locking with determine-offset, and
  physical units in place of ohms and volts. Grew well past what was planned.
- **Logging substrate** — categories, `EventLog.command()` with correlated
  `op` ids and `duration_ms`, a self-describing session header, an excepthook,
  90-day retention. Transactions are logged in the workers, where the outcome is
  known.
- **Safe shutdown** — a per-output checklist on close, defaulted to off, with
  the choice reaching each worker's teardown through `disable_on_stop` /
  `unlock_on_stop`.
- **Server liveness** — a stalled linien-server is detected from its `ping`
  heartbeat and restartable from the Devices card, so recovery is not an SSH
  session.

## Decided against

- **Linien threading fix.** Implemented and reverted the same day. The race and
  a 30 s GUI freeze on locking were both real, but the queue made setpoints land
  a poll late and put device writes on the acquisition connection, so the plot
  stuttered on every control. Tuning it did not change the verdict.
  *If revisited:* the constraint is one connection shared between writes and
  acquisition — the answer is a second connection or a server-side batch call,
  not a queue in front of the existing one.
- **Splitting `mot_control.py`.** ~3,800 lines, but navigable by card and it
  works. Pure churn with regression risk. Revisit only if panels keep being
  added.

## Open

### Bench work — none of it answerable from the desk

1. **Does the lock engage in SIMPLE mode**, and does *Determine signal offset*
   move it? Ported from linien 2.1.0, never confirmed against the RP.
2. **Do the temperature and frequency sliders land where they claim?** The
   Steinhart–Hart and VCO conversions are unverified against a real reading.
3. **Is 16–38 °C the right range**, and is 0.1 V / 0.1 A too coarse for coil
   current?
4. **Does the shutdown checklist match reality?** It reads the UI's own state
   rather than querying devices — deliberately, so the close path cannot block
   on a device that has stopped answering. That is only right if the UI and the
   hardware agree.
5. **Does the Restart Server button recover cleanly?** In particular whether the
   repump VCO and gate workers, which hold their own connections, reconnect
   rather than being left dead.

### Code

- **`/bench` skill** — power-up order, what to connect, what "working" looks
  like per subsystem. The real gap: every bug that reached the operator on
  2026-08-14 lived between "constructs correctly" and "behaves correctly against
  a device", and no test in this repo covers that.
- **Four RPyC connections to one Red Pitaya** (spectrum, repump VCO, repump
  gate, PSD) — violates CLAUDE.md's own "never point two clients at one RP".
  Prime suspect for the server wedge on 2026-08-14, but the parameter-queue
  drain added the same day may have been the whole story. Collapsing them now
  would destroy that signal. Revisit only if the wedge recurs.
- **Headless CLI** — `dps status`, `laser read`. `RidenDPSClient` and the probe
  functions are already GUI-free; the rest means pulling protocol code out of
  the QThread wrappers, with no clean seam now that the threading work is
  reverted.
