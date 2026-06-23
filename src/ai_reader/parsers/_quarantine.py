"""Quarantine sink for bad JSONL records.

A parser that hits an unparseable line, an unknown record type, or a
schema mismatch must keep reading — dropping the whole session on a
single bad row is worse than the row itself.  This module is the
side-channel that swallows such records on disk so the operator can
post-mortem them without breaking the read pipeline.

The sink is intentionally write-only and best-effort.  Failures here
must never bubble up to the caller: if the quarantine file cannot be
written we log to stderr and move on, because the alternative
(silently dropping the row) is the behaviour we are trying to fix.

Output layout::

    <AI_READER_HOME>/quarantine/<agent>/<uuid>.badlines.jsonl

Each line is a JSON object with the keys ``detected_at``, ``line_no``,
``reason`` and ``raw`` — mirroring the Kairo ``FileQuarantineSink``
contract that inspired this module.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


QUARANTINE_REASON_INVALID_JSON = "invalid_json"
QUARANTINE_REASON_SCHEMA_MISMATCH = "schema_mismatch"
QUARANTINE_REASON_UNSUPPORTED_TYPE = "unsupported_record_type"


def _resolve_base(base_dir: Optional[str]) -> Path:
    if base_dir:
        return Path(base_dir).expanduser()
    env_home = os.environ.get("AI_READER_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path("~/.ai-reader").expanduser()


class QuarantineSink:
    """Append-only sink for malformed JSONL records."""

    def __init__(
        self, agent: str, uuid: str, base_dir: Optional[str] = None
    ) -> None:
        self._agent = agent
        self._uuid = uuid
        self._path = self.quarantine_path(agent, uuid, base_dir)
        self._count = 0
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"ai_reader: quarantine dir create failed: {exc}",
                file=sys.stderr,
            )

    @classmethod
    def quarantine_path(
        cls, agent: str, uuid: str, base_dir: Optional[str] = None
    ) -> Path:
        """Return the on-disk path without touching the filesystem."""
        return _resolve_base(base_dir) / "quarantine" / agent / f"{uuid}.badlines.jsonl"

    def quarantine(self, line_no: int, raw: str, reason: str) -> None:
        """Persist one bad record.  Never raises."""
        record = {
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "line_no": int(line_no),
            "reason": reason,
            "raw": raw,
        }
        try:
            payload = json.dumps(record, ensure_ascii=False) + "\n"
            self._atomic_append(payload)
            self._count += 1
        except OSError as exc:
            print(
                f"ai_reader: quarantine write failed: {exc}",
                file=sys.stderr,
            )

    def flush(self) -> int:
        """Finalise the sink and return the number of records written."""
        return self._count

    def _atomic_append(self, payload: str) -> None:
        existing = self._path.read_bytes() if self._path.exists() else b""
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("wb") as fh:
            fh.write(existing)
            fh.write(payload.encode("utf-8"))
        tmp_path.replace(self._path)


__all__ = [
    "QUARANTINE_REASON_INVALID_JSON",
    "QUARANTINE_REASON_SCHEMA_MISMATCH",
    "QUARANTINE_REASON_UNSUPPORTED_TYPE",
    "QuarantineSink",
]
