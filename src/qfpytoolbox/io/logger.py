"""Async logger — mirrors ``MyJuliaToolbox.jl/src/io/logger.jl``.

Queues ``LogRecord`` objects in a ring buffer and writes them from a background
thread so the calling code is never blocked by sink I/O.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "LogRecord",
    "AsyncLogger",
    "log_debug",
    "log_info",
    "log_warn",
    "log_error",
    "flush_logger",
    "stop_logger",
    "dropped_logs",
    "minio_flush_log_buffer",
]

# ---------------------------------------------------------------------------
# Level priority
# ---------------------------------------------------------------------------

_LEVEL_PRIORITY: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "error": 40,
}

_LEVEL_ANSI: dict[str, str] = {
    "debug": "\033[90m",  # bright-black / grey
    "info": "\033[36m",  # cyan
    "warn": "\033[33m",  # yellow
    "error": "\033[31m",  # red
}
_TS_ANSI = "\033[32m"  # green for timestamp
_RESET = "\033[0m"

# ---------------------------------------------------------------------------
# LogRecord
# ---------------------------------------------------------------------------


@dataclass
class LogRecord:
    """Lightweight log payload used by :class:`AsyncLogger`."""

    timestamp: datetime
    level: str
    message: str
    metadata: Any = field(default=None)


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


class _RingBuffer:
    """Fixed-capacity circular queue with drop-on-full behaviour."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be > 0")
        self._buf: list[LogRecord | None] = [None] * capacity
        self._head = 0
        self._tail = 0
        self._count = 0
        self._dropped = 0
        self._closed = False
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    # -- producer side -------------------------------------------------------

    def enqueue(self, record: LogRecord) -> bool:
        with self._not_empty:
            if self._closed:
                return False
            if self._count == len(self._buf):
                self._dropped += 1
                return False
            self._buf[self._tail] = record
            self._tail = (self._tail + 1) % len(self._buf)
            self._count += 1
            self._not_empty.notify()
            return True

    # -- consumer side -------------------------------------------------------

    def dequeue(self) -> LogRecord | None:
        with self._not_empty:
            while self._count == 0 and not self._closed:
                self._not_empty.wait()
            if self._count == 0 and self._closed:
                return None
            record = self._buf[self._head]
            self._buf[self._head] = None
            self._head = (self._head + 1) % len(self._buf)
            self._count -= 1
            return record

    def close(self) -> None:
        with self._not_empty:
            self._closed = True
            self._not_empty.notify_all()

    # -- stats ---------------------------------------------------------------

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped


# ---------------------------------------------------------------------------
# AsyncLogger
# ---------------------------------------------------------------------------


class AsyncLogger:
    """Asynchronous logger that queues records and writes them from a background thread.

    Parameters
    ----------
    source:
        Destination sink — any :class:`~qfpytoolbox.io.media.iSourceMedia` instance.
    min_level:
        Minimum accepted level (``'debug'``, ``'info'``, ``'warn'``, ``'error'``).
    capacity:
        Ring-buffer size.  When full, new records are silently dropped.
    **sink_kwargs:
        Extra options forwarded to the sink's ``write_log_record`` function
        (e.g. ``table='logs'`` for a :class:`~qfpytoolbox.io.media.DatabaseMedia`).
    """

    def __init__(
        self,
        source: Any,
        *,
        min_level: str = "info",
        capacity: int = 1024,
        **sink_kwargs: Any,
    ) -> None:
        _validate_level(min_level)
        self.source = source
        self.min_level = min_level
        self.sink_kwargs = sink_kwargs
        self._queue = _RingBuffer(capacity)
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # -- worker loop ---------------------------------------------------------

    def _run(self) -> None:
        while True:
            record = self._queue.dequeue()
            if record is None:
                break
            try:
                _write_log_record(self.source, record, **self.sink_kwargs)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[AsyncLogger] Failed to write record: {exc}",
                    file=sys.stderr,
                )

    # -- logging API ---------------------------------------------------------

    def log(self, level: str, message: str, *, metadata: Any = None) -> bool:
        """Queue a record.  Returns ``True`` if accepted, ``False`` if dropped."""
        if not _should_log(self.min_level, level):
            return True
        record = LogRecord(
            timestamp=datetime.now(tz=timezone.utc),
            level=level,
            message=message,
            metadata=metadata,
        )
        return self._queue.enqueue(record)


# ---------------------------------------------------------------------------
# Module-level log helpers (mirror Julia's free functions)
# ---------------------------------------------------------------------------


def log_debug(logger: AsyncLogger, message: str, *, metadata: Any = None) -> bool:
    """Queue a ``debug`` record."""
    return logger.log("debug", message, metadata=metadata)


def log_info(logger: AsyncLogger, message: str, *, metadata: Any = None) -> bool:
    """Queue an ``info`` record."""
    return logger.log("info", message, metadata=metadata)


def log_warn(logger: AsyncLogger, message: str, *, metadata: Any = None) -> bool:
    """Queue a ``warn`` record."""
    return logger.log("warn", message, metadata=metadata)


def log_error(logger: AsyncLogger, message: str, *, metadata: Any = None) -> bool:
    """Queue an ``error`` record."""
    return logger.log("error", message, metadata=metadata)


def flush_logger(logger: AsyncLogger) -> None:
    """Block until all pending records in ``logger``'s queue are written."""
    import time

    max_iters = 10_000
    _max_stable = 5  # consecutive iterations with unchanged count → worker is stuck
    sleep = 0.001
    last_count = -1
    stable = 0

    for _ in range(max_iters):
        current = logger._queue.count
        if current == 0:
            return
        if current == last_count:
            stable += 1
            if stable >= _max_stable:
                raise RuntimeError("Logger queue did not drain; worker may have failed.")
        else:
            last_count = current
            stable = 0
        time.sleep(min(sleep * 2, 0.1))
        sleep = min(sleep * 2, 0.1)

    raise RuntimeError("Logger queue did not drain within the maximum iteration limit.")


def stop_logger(logger: AsyncLogger) -> None:
    """Close the queue, wait for the background worker to finish, and upload what a MinIO sink still holds."""
    logger._queue.close()
    logger._worker.join()

    from qfpytoolbox.io.media import MinIOMedia  # noqa: PLC0415

    if isinstance(logger.source, MinIOMedia):
        minio_flush_log_buffer(logger.source)


def dropped_logs(logger: AsyncLogger) -> int:
    """Return the number of records dropped due to a full queue."""
    return logger._queue.dropped


# ---------------------------------------------------------------------------
# Level validation helpers
# ---------------------------------------------------------------------------


def _validate_level(level: str) -> None:
    if level not in _LEVEL_PRIORITY:
        raise ValueError(f"Unsupported log level: {level!r}. Supported: {sorted(_LEVEL_PRIORITY)}")


def _should_log(min_level: str, level: str) -> bool:
    _validate_level(min_level)
    _validate_level(level)
    return _LEVEL_PRIORITY[level] >= _LEVEL_PRIORITY[min_level]


# ---------------------------------------------------------------------------
# Record formatting
# ---------------------------------------------------------------------------


def _format_record(record: LogRecord, *, color: bool = False) -> str:
    ts = record.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # milliseconds
    ts_txt = f"[{ts}]"
    level_txt = f"[{record.level.upper()}]"
    if color:
        ts_txt = f"{_TS_ANSI}{ts_txt}{_RESET}"
        code = _LEVEL_ANSI.get(record.level, "")
        level_txt = f"{code}{level_txt}{_RESET}"
    meta = "" if record.metadata is None else f" | metadata={record.metadata!r}"
    return f"{ts_txt} {level_txt} {record.message}{meta}"


# ---------------------------------------------------------------------------
# Sink implementations (write_log_record per media type)
# ---------------------------------------------------------------------------


def _write_log_record(source: Any, record: LogRecord, **kwargs: Any) -> None:
    from qfpytoolbox.io.media import (  # noqa: PLC0415
        ConsoleMedia,
        DatabaseMedia,
        FileSystemMedia,
        MinIOMedia,
        SQLDumpMedia,
    )

    if isinstance(source, FileSystemMedia):
        _write_to_filesystem(source, record, **kwargs)
    elif isinstance(source, ConsoleMedia):
        _write_to_console(source, record, **kwargs)
    elif isinstance(source, DatabaseMedia):
        _write_to_database(source, record, **kwargs)
    elif isinstance(source, MinIOMedia):
        _write_to_minio(source, record, **kwargs)
    elif isinstance(source, SQLDumpMedia):
        raise TypeError("SQLDumpMedia is read-only and cannot be used as a logging sink.")
    else:
        raise TypeError(f"write_log_record not implemented for {type(source).__name__!r}")


def _write_to_filesystem(source: Any, record: LogRecord, **_: Any) -> None:
    path = source.path
    dir_ = os.path.dirname(path)
    if dir_:
        os.makedirs(dir_, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_format_record(record) + "\n")


def _write_to_console(source: Any, record: LogRecord, **kwargs: Any) -> None:
    color: bool = kwargs.get("color", True)
    print(_format_record(record, color=color), file=source.stream)
    if hasattr(source.stream, "flush"):
        source.stream.flush()


def _write_to_database(source: Any, record: LogRecord, *, table: str = "logs", **_: Any) -> None:
    from qfpytoolbox.io.media import _validate_sql_identifier  # noqa: PLC0415

    safe_table = _validate_sql_identifier(table, field="table name")
    conn = source.connection
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {safe_table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata TEXT
        )"""
    )
    conn.execute(
        f"INSERT INTO {safe_table} (timestamp, level, message, metadata) VALUES (?, ?, ?, ?)",
        (
            record.timestamp.isoformat(),
            record.level,
            record.message,
            str(record.metadata) if record.metadata is not None else None,
        ),
    )
    conn.commit()


def _write_to_minio(source: Any, record: LogRecord, *, batch_logs: bool = True, **_: Any) -> None:
    """Hold ``record`` for one later upload (S3 cannot append), or upload it now when ``batch_logs`` is false."""
    if batch_logs:
        with source._log_lock:
            source._log_buffer.append(record)
    else:
        _upload_minio_log_records(source, [record])


def minio_flush_log_buffer(media: Any) -> None:
    """Upload the log records ``media`` holds as one JSONL object.

    The object's key is ``{prefix}/logs/{yyyy-mm-dd}/{timestamp_ns}-{id}.jsonl``.  :func:`stop_logger`
    calls this for a MinIO sink; call it yourself for periodic uploads.  If the upload fails, the
    records stay held for the next attempt.
    """
    with media._log_lock:
        records = list(media._log_buffer)
        media._log_buffer.clear()
    if not records:
        return
    try:
        _upload_minio_log_records(media, records)
    except Exception:
        with media._log_lock:
            media._log_buffer[:0] = records
        raise


def _upload_minio_log_records(media: Any, records: list[LogRecord]) -> None:
    """Store ``records`` as one new JSONL object."""
    lines = "".join(_log_record_to_jsonl(record) + "\n" for record in records)
    date_text = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    key = media._key(f"logs/{date_text}/{time.time_ns()}-{uuid.uuid4().hex[:8]}.jsonl")
    media._put_bytes(key, lines.encode("utf-8"), content_type="application/x-ndjson")


def _log_record_to_jsonl(record: LogRecord) -> str:
    """Return ``record`` as one line of JSON."""
    return json.dumps(
        {
            "timestamp": record.timestamp.isoformat(),
            "level": record.level,
            "message": record.message,
            "metadata": None if record.metadata is None else str(record.metadata),
        }
    )
