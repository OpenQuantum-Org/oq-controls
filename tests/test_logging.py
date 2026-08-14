"""Event-log substrate: correlation, durations, faults, thread safety.

Run: python tests/test_logging.py

No Qt and no hardware -- this is the one part of the app that must keep
working when everything else is broken, so it is tested on its own.
"""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mot_logging import NULL_LOG, EventLog  # noqa: E402


def read(log):
    with open(log.path_for_today(), encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def main():
    tmp = tempfile.mkdtemp(prefix="mot-log-")
    log = EventLog(tmp)

    # --- categories -----------------------------------------------------
    log.log("state_change", where="here")
    log.user("clicked", button="fire")
    log.readback("dps", i_out=1.5)
    log.fault("boom", error="nope")
    kinds = [r["kind"] for r in read(log)]
    assert kinds == ["state", "user", "readback", "fault"], kinds

    # --- commands are correlated and timed -------------------------------
    with log.command("laser.current", ma=80) as op:
        time.sleep(0.01)
        op.result(readback_ma=79)
    start, end = read(log)[-2:]
    assert start["kind"] == end["kind"] == "command"
    assert start["phase"] == "start" and end["phase"] == "end"
    # Correlated by id, not by adjacency: several threads interleave.
    assert start["op"] == end["op"] == op.id
    assert start["ma"] == 80
    assert end["ok"] is True and end["readback_ma"] == 79
    assert end["duration_ms"] >= 10, end["duration_ms"]

    # --- a failing command is both a command end and a fault -------------
    try:
        with log.command("dps.output", value=True):
            raise RuntimeError("serial timeout")
    except RuntimeError:
        pass
    else:
        raise AssertionError("command swallowed the exception")
    cmd_end, fault = read(log)[-2:]
    assert cmd_end["kind"] == "command" and cmd_end["ok"] is False
    assert "duration_ms" in cmd_end  # timing survives the failure
    assert fault["kind"] == "fault"
    assert fault["op"] == cmd_end["op"]  # the fault points back at it
    assert "serial timeout" in fault["error"]
    assert "RuntimeError" in fault["traceback"]

    # --- reserved keys cannot be clobbered -------------------------------
    log.log("collide", kind="OCP", event="other", t="fake")
    r = read(log)[-1]
    assert r["kind"] == "state" and r["event"] == "collide"
    assert r["kind_"] == "OCP" and r["event_"] == "other" and r["t_"] == "fake"
    assert r["t"] != "fake"

    # --- unserialisable payloads still produce a record ------------------
    log.log("weird", obj=object())
    assert read(log)[-1]["event"] == "weird"

    # --- a broken UI hook must not stop the write ------------------------
    log.on_record = lambda record: 1 / 0
    log.log("survives_bad_hook")
    assert read(log)[-1]["event"] == "survives_bad_hook"
    log.on_record = None

    # --- concurrent writers: one line each, none interleaved -------------
    before = len(read(log))
    threads = [
        threading.Thread(target=lambda i=i: [
            log.command("t", n=i).__enter__().__exit__(None, None, None)
            for _ in range(20)
        ])
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    after = read(log)
    assert len(after) - before == 8 * 20 * 2, len(after) - before
    ops = [r["op"] for r in after[before:]]
    assert len(set(ops)) == 8 * 20, "op ids collided across threads"

    # --- session header --------------------------------------------------
    log.session_start(rp_host="rp-test.local")
    header = read(log)[-1]
    assert header["kind"] == "session"
    assert header["rp_host"] == "rp-test.local"
    for field in ("schema", "python", "platform", "machine", "pid", "frozen"):
        assert field in header, field

    # --- retention -------------------------------------------------------
    stale = os.path.join(tmp, "mot-19700101.jsonl")
    with open(stale, "w") as f:
        f.write("{}\n")
    os.utime(stale, (0, 0))
    log.prune(days=30)
    assert not os.path.exists(stale)
    assert os.path.exists(log.path_for_today())  # today's is kept

    # --- unhandled exceptions reach the log ------------------------------
    saved = sys.excepthook
    try:
        # The hook chains to whatever was there; stub it so the expected
        # traceback does not get printed into this test's output.
        sys.excepthook = lambda *exc: None
        log.install_excepthook()
        try:
            raise ValueError("unhandled")
        except ValueError:
            sys.excepthook(*sys.exc_info())
    finally:
        sys.excepthook = saved
    r = read(log)[-1]
    assert r["event"] == "unhandled_exception" and r["kind"] == "fault"
    assert "unhandled" in r["error"]

    # --- the null log accepts everything and records nothing -------------
    NULL_LOG.log("x", a=1)
    NULL_LOG.fault("y")
    with NULL_LOG.command("z", v=1) as op:
        op.result(ok=True)
    assert op.id is None

    print("LOGGING OK")


if __name__ == "__main__":
    main()
