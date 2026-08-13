"""Append-only JSONL event log for the MOT control app.

One file per day (mot-YYYYMMDD.jsonl), one JSON object per line:

    {"t": "2026-08-12T17:04:11.532-07:00", "event": "dispenser_fire_end",
     "elapsed_s": 30.0, "planned_s": 30.0, "current_a": 7.5, "aborted": false}

Files are opened per write, so records survive a crash and the directory
can be tailed or parsed (e.g. pandas.read_json(..., lines=True)) while the
app runs. All logging happens on the GUI thread (worker signals arrive as
queued slots), so no locking is needed.
"""

import json
import os
from datetime import datetime


class EventLog:
    def __init__(self, directory):
        self.directory = directory

    def path_for_today(self):
        return os.path.join(
            self.directory, f"mot-{datetime.now():%Y%m%d}.jsonl"
        )

    def log(self, event, **fields):
        record = {
            "t": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
        }
        record.update(fields)
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(self.path_for_today(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            print(f"! event log write failed: {e}")
