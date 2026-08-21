"""Append-only JSONL event log — the experimental record for the MOT app.

This is not an ops convenience. Months after a measurement it is the only
evidence of what the hardware was actually told to do and what it did, so
it is built to stand alone: a reader with the file and nothing else should
be able to reconstruct the session.

One file per day (``mot-YYYYMMDD.jsonl``), one JSON object per line, always
carrying ``t``, ``event`` and ``kind``::

    {"t": "2026-08-14T17:04:11.532-07:00", "kind": "command",
     "event": "linien.set_pid", "op": 41, "phase": "end", "ok": true,
     "duration_ms": 12.4, "p": 300, "i": 2000, "d": 0}

``kind`` is the filter axis:

``session``   one per run, at the top: version, git SHA, platform, devices
``user``      a person did something (clicked, typed, dragged)
``command``   something was sent to hardware; paired start/end share ``op``
``readback``  hardware reported a value
``state``     an app-level state change (connected, phase, lock)
``fault``     anything that went wrong, including unhandled exceptions

Commands are correlated by ``op``, not by adjacency — with several worker
threads writing, adjacent lines are not causally related.

Files are opened per write so records survive a crash, and a lock guards
the handle because worker threads log directly (they must: a fault that
has to reach the GUI thread to be recorded is lost when the GUI is the
thing that broke).
"""

import glob
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

# Bump when the record shape changes in a way a reader must know about.
SCHEMA_VERSION = 2

# Older logs are evidence, but not forever.
RETENTION_DAYS = 90


def _git_sha():
    """Short SHA of the working tree, or None outside a checkout.

    Frozen builds have no git, so this is best-effort by design; the
    session header records what it could find rather than failing.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0:
            sha = out.stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", root, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                sha += "-dirty"
            return sha
    except Exception:
        pass
    return None


class Operation:
    """One hardware transaction, open between its start and end records.

    Returned by :meth:`EventLog.command`. Call :meth:`result` to attach
    what the device reported; the end record is written on exit with the
    elapsed time either way.
    """

    __slots__ = ("_log", "_event", "_id", "_fields", "_result", "_t0")

    def __init__(self, log, event, op_id, fields):
        self._log = log
        self._event = event
        self._id = op_id
        self._fields = fields
        self._result = {}
        self._t0 = time.perf_counter()

    @property
    def id(self):
        return self._id

    def result(self, **fields):
        """Record what came back, to be written with the end record."""
        self._result.update(fields)
        return self

    def __enter__(self):
        self._log._write(
            "command", self._event,
            dict(self._fields, op=self._id, phase="start"),
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = round((time.perf_counter() - self._t0) * 1000, 3)
        fields = dict(self._result, op=self._id, phase="end",
                      duration_ms=elapsed_ms)
        if exc is None:
            self._log._write(
                "command", self._event, dict(fields, ok=True)
            )
        else:
            # A failed command is a fault as well as a command: the end
            # record keeps the timing with its op, and the fault record
            # is what a reader filtering on kind="fault" will find.
            self._log._write(
                "command", self._event,
                dict(fields, ok=False,
                     error=f"{exc_type.__name__}: {exc}"),
            )
            self._log.fault(
                self._event, op=self._id,
                error=f"{exc_type.__name__}: {exc}",
                traceback="".join(
                    traceback.format_exception(exc_type, exc, tb)
                ),
            )
        return False  # never swallow


class _NullOperation:
    id = None

    def result(self, **fields):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NullLog:
    """Drop-in EventLog that records nothing.

    Workers take a log so they can record their own transactions where
    they happen. This is the default, so a worker constructed without one
    (tests, the probe path) needs no branching at every call site.
    """

    def log(self, event, /, **fields):
        pass

    user = readback = fault = log

    def command(self, event, /, **fields):
        return _NullOperation()


NULL_LOG = NullLog()


class EventLog:
    def __init__(self, directory, on_record=None):
        self.directory = directory
        # Optional UI hook: called with each record dict. Worker threads
        # log too, so this may fire off the GUI thread -- the hook is
        # responsible for getting itself back there.
        self.on_record = on_record
        self._lock = threading.Lock()
        self._op_seq = 0
        # Repeat-collapse state for fault(). Its own lock: the file lock is
        # held during the write, and this has to be decided before it.
        self._fault_lock = threading.Lock()
        # signature -> [suppressed_count, run_started_monotonic]
        self._fault_runs = {}

    def path_for_today(self):
        return os.path.join(
            self.directory, f"mot-{datetime.now():%Y%m%d}.jsonl"
        )

    # -- public API ------------------------------------------------------

    def log(self, event, /, **fields):
        """Generic app-state record. Kept for the many existing callers."""
        self._write("state", event, fields)

    def user(self, event, /, **fields):
        """A person did something."""
        self._write("user", event, fields)

    def readback(self, event, /, **fields):
        """Hardware reported a value."""
        self._write("readback", event, fields)

    # A failure inside a poll loop repeats at the poll rate. Unplugging a
    # supply used to write the same line 2x a second -- 400 identical
    # records in one session, which buries everything that happened around
    # them. Repeats are counted instead of written, and the count is
    # written, so nothing is lost: "this failed 148 times over 30 s" is
    # better evidence than 148 copies of it.
    FAULT_REPEAT_WINDOW_S = 30.0

    def fault(self, event, /, **fields):
        """Something went wrong. This path must never be lossy."""
        # Every field, not just the message: two faults of the same event
        # can be different things (one startup report naming the coils,
        # another naming the dither), and keying on the event alone
        # collapsed the second into the first.  "traceback" is left out --
        # it is identical for a repeat and huge for a key.
        signature = (event,) + tuple(
            (key, str(value)[:200])
            for key, value in sorted(fields.items())
            if key != "traceback"
        )
        now = time.monotonic()
        with self._fault_lock:
            run = self._fault_runs.get(signature)
            suppress = run is not None and now - run[1] < self.FAULT_REPEAT_WINDOW_S
            if suppress:
                run[0] += 1
                summaries = []
            else:
                # Its own count first: a failure that outlasts the window
                # starts a new run, and the old one's tally is due now.
                summaries = (
                    [self._summary_record(signature, run[0], now - run[1])]
                    if run is not None and run[0]
                    else []
                )
                self._fault_runs[signature] = [0, now]
            # Runs are per signature, not one "last fault": two devices
            # failing at once alternate, and a single slot would collapse
            # neither of them.
            summaries += self._expired_summaries(now, keep=signature)
        for record in summaries:
            self._emit(record)
        if not suppress:
            self._write("fault", event, fields)

    def _expired_summaries(self, now, keep=None):
        """Records for runs whose window has closed. Caller holds the lock.

        Returns records rather than writing them, so the file write happens
        outside the lock.
        """
        records = []
        for signature, (count, t0) in list(self._fault_runs.items()):
            if signature == keep or now - t0 < self.FAULT_REPEAT_WINDOW_S:
                continue
            del self._fault_runs[signature]
            if not count:
                continue  # nothing repeated: the one record already stands
            records.append(self._summary_record(signature, count, now - t0))
        return records

    def _summary_record(self, signature, count, over_s):
        """One record standing for a run of identical faults."""
        fields = dict(signature[1:])
        return self._compose("fault", "fault_repeated", {
            "of": signature[0],
            "device": fields.get("device", ""),
            "message": fields.get("message") or fields.get("error") or "",
            "count": count,
            "over_s": round(over_s, 1),
        })

    def command(self, event, /, **fields):
        """Open a correlated hardware transaction::

            with log.command("laser.set_current", ma=80) as op:
                dev.write(...)
                op.result(readback_ma=dev.read())

        The end record carries ``duration_ms`` and ``ok``, which is the
        only profiling this app needs.
        """
        with self._lock:
            self._op_seq += 1
            op_id = self._op_seq
        return Operation(self, event, op_id, fields)

    def flush_repeats(self):
        """Write the counts for every open run of suppressed faults.

        Called on the way out: a run whose window has not closed yet still
        has to reach the file, or a failure that ended just before shutdown
        would take its own count with it.
        """
        with self._fault_lock:
            records = self._expired_summaries(
                time.monotonic() + self.FAULT_REPEAT_WINDOW_S
            )
        for record in records:
            self._emit(record)

    def session_start(self, **context):
        """Open the log with everything needed to read it standalone."""
        self.prune()
        self._write(
            "session",
            "session_start",
            dict(
            schema=SCHEMA_VERSION,
            app="oq-controls",
            git_sha=_git_sha(),
            python=sys.version.split()[0],
            platform=f"{platform.system()} {platform.release()}",
            machine=platform.node(),
            frozen=bool(getattr(sys, "frozen", False)),
            pid=os.getpid(),
            **context,
            ),
        )

    def prune(self, days=RETENTION_DAYS):
        """Delete logs older than the retention window."""
        cutoff = time.time() - days * 86400
        try:
            for path in glob.glob(os.path.join(self.directory, "mot-*.jsonl")):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
        except Exception:
            pass

    def install_excepthook(self):
        """Log unhandled exceptions before the process dies.

        Without this, the one failure worth having a record of is the one
        that never reaches the log.
        """
        previous = sys.excepthook

        def hook(exc_type, exc, tb):
            try:
                self.fault(
                    "unhandled_exception",
                    error=f"{exc_type.__name__}: {exc}",
                    traceback="".join(
                        traceback.format_exception(exc_type, exc, tb)
                    ),
                )
            finally:
                previous(exc_type, exc, tb)

        sys.excepthook = hook

        # Qt runs slots on threads Python's excepthook does not cover.
        if hasattr(threading, "excepthook"):
            def thread_hook(args):
                self.fault(
                    "unhandled_exception",
                    thread=args.thread.name if args.thread else None,
                    error=f"{args.exc_type.__name__}: {args.exc_value}",
                    traceback="".join(
                        traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback
                        )
                    ),
                )

            threading.excepthook = thread_hook

    # -- internals -------------------------------------------------------

    def _compose(self, kind, event, fields):
        # fields arrives as a dict, not **kwargs: callers legitimately log
        # a field called "kind" (DPS protection kind), which as a keyword
        # would collide with this record's own key.
        record = {
            "t": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": kind,
            "event": event,
        }
        for key, value in fields.items():
            # A field must never overwrite the record's own keys -- doing
            # so silently destroys the category a reader filters on. Keep
            # the value under a suffixed name rather than dropping it.
            record[f"{key}_" if key in record else key] = value
        return record

    def _write(self, kind, event, fields):
        if kind != "fault":
            # Closed runs are flushed on the way past, so a count lands in
            # the timeline near the failures it stands for rather than
            # waiting for the same failure to happen again.
            with self._fault_lock:
                summaries = self._expired_summaries(time.monotonic())
            for record in summaries:
                self._emit(record)
        self._emit(self._compose(kind, event, fields))

    def _emit(self, record):
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception:
                pass  # a broken UI hook must not stop the write below
        try:
            line = json.dumps(record, default=str) + "\n"
        except Exception as e:
            line = json.dumps({
                "t": record["t"], "kind": "fault", "event": "log_encode_failed",
                "of": record.get("event"), "error": str(e),
            }) + "\n"
        try:
            with self._lock:
                os.makedirs(self.directory, exist_ok=True)
                with open(self.path_for_today(), "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            # Nowhere left to report this: stdout is not a fault path
            # (CLAUDE.md invariant 4) and raising would take down the
            # caller over a logging problem.
            pass


# -- third-party logging ------------------------------------------------

# Libraries whose connection chatter belongs in the record, and the level
# to take each at.
_LIBRARY_LOGGERS = {
    # The whole connection story -- the attempt, the host that could not be
    # resolved, the decision to start the server -- is DEBUG and INFO here,
    # and it is a handful of lines per attempt.
    "linien_client": logging.DEBUG,
    "linien_common": logging.DEBUG,
    # The SSH and RPyC stack underneath, at INFO rather than DEBUG on
    # purpose: paramiko debug-logs every SSH packet, which would bury the
    # experimental record under thousands of lines per connect.
    "rpyc": logging.INFO,
    "paramiko": logging.INFO,
    "fabric": logging.INFO,
    "invoke": logging.INFO,
}


class _EventLogHandler(logging.Handler):
    """A logging handler that writes into an :class:`EventLog`."""

    def __init__(self, event_log):
        super().__init__(level=logging.DEBUG)
        self.event_log = event_log

    def emit(self, record):
        try:
            fields = {
                "logger": record.name,
                "level": record.levelname,
                "message": record.getMessage(),
            }
            if record.levelno >= logging.WARNING:
                self.event_log.fault("library", **fields)
            else:
                self.event_log.log("library", **fields)
        except Exception:
            # A bridge that raises inside a worker thread is worse than the
            # console noise it was installed to remove.
            pass


_bridge = None


def capture_library_logging(event_log=None):
    """Route third-party logging into ``event_log`` instead of the console.

    The sweep must run *after* the libraries are imported, and it has to be
    re-run at every import site. ``linien_client`` attaches its own console
    handler when the package first loads, and four different workers import
    it -- whichever gets there first is not fixed, so no single call site
    can be the one that cleans up. Anything done *before* the import is
    doubly useless: each linien module sets its own logger to DEBUG (a
    child's record is filtered by the child's level, then handed to every
    ancestor's handlers regardless of the ancestor's level), and the import
    overwrites the level anyway.

    ``event_log`` is optional so an import site can sweep without having a
    log to hand; the window installs the real one at startup. Idempotent:
    called again it re-points the one handler rather than stacking another.
    """
    global _bridge
    if event_log is not None:
        if _bridge is None:
            _bridge = _EventLogHandler(event_log)
        else:
            _bridge.event_log = event_log
    elif _bridge is None:
        return None  # nothing to route into yet; the window installs it
    for name, level in _LIBRARY_LOGGERS.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            # type(), not isinstance(): RotatingFileHandler subclasses
            # StreamHandler, and linien's own debug file should survive.
            # It is only the console copy that competes with tracebacks.
            if type(handler) is logging.StreamHandler:
                logger.removeHandler(handler)
        if _bridge not in logger.handlers:
            logger.addHandler(_bridge)
        logger.setLevel(level)
        # Nothing above these loggers may print: if anything ever calls
        # logging.basicConfig(), root would grow a console handler and the
        # noise would be back without this.
        logger.propagate = False
    return _bridge
