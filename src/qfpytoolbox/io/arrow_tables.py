"""Lossless Arrow table I/O: ``pyarrow.Table`` objects read and written without a DataFrame round-trip.

Converting a table to pandas and back loses information: nullable integer columns become floats, and field
metadata is dropped.  :func:`read_arrow_table` and :func:`write_arrow_table` keep the exact schema, including
schema-level and field-level metadata, which is what an exchange between Python and Julia programs relies on.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from qfpytoolbox.io.media import FileSystemMedia

__all__ = ["read_arrow_table", "write_arrow_table"]


def _path_of(location: Any) -> Path:
    """Return the file path a ``FileSystemMedia``, a ``str`` or an ``os.PathLike`` names."""
    if isinstance(location, FileSystemMedia):
        return Path(location.path)
    if isinstance(location, (str, os.PathLike)):
        return Path(location)
    raise TypeError(
        f"Arrow tables are read from and written to a FileSystemMedia or a path. Got: {type(location).__name__!r}"
    )


def read_arrow_table(source: Any) -> Any:
    """Read an Arrow IPC file (Feather v2) or an Arrow IPC stream as a ``pyarrow.Table``.

    Parameters
    ----------
    source:
        A :class:`~qfpytoolbox.io.media.FileSystemMedia` naming the file, or its path.

    Returns
    -------
    pyarrow.Table
        The table, with its schema and metadata exactly as stored.
    """
    import pyarrow as pa  # noqa: PLC0415

    path = _path_of(source)
    with pa.memory_map(str(path), "r") as stream:
        try:
            return pa.ipc.open_file(stream).read_all()
        except pa.ArrowInvalid:
            stream.seek(0)
            return pa.ipc.open_stream(stream).read_all()


def write_arrow_table(target: Any, table: Any, *, overwrite: bool = False) -> Path:
    """Write ``table`` as an Arrow IPC file (Feather v2), atomically, keeping its schema and metadata.

    The table is written to a temporary file in the target's folder, flushed to disk, then renamed onto the
    target, so a reader never sees a partial file.  Missing parent folders are created.

    Parameters
    ----------
    target:
        A :class:`~qfpytoolbox.io.media.FileSystemMedia` naming the file, or its path.
    table:
        The ``pyarrow.Table`` to write.
    overwrite:
        Replace an existing file (default ``False``: an existing file raises ``FileExistsError``).

    Returns
    -------
    pathlib.Path
        The path written.
    """
    import pyarrow as pa  # noqa: PLC0415

    path = _path_of(target)
    if path.exists() and not overwrite:
        raise FileExistsError(f"File already exists: {str(path)!r}. Pass overwrite=True to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".arrow-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with pa.ipc.new_file(stream, table.schema) as writer:
                writer.write_table(table)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return path
