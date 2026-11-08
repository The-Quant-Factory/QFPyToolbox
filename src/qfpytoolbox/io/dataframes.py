"""DataFrame I/O helpers — mirrors ``MyJuliaToolbox.jl/src/io/dataframes.jl``
and ``src/io/media_dataframes.jl``.

Supported formats: CSV, Arrow, gzipped CSV, Excel (``.xlsx``).
Supports plain file paths, ``io.IOBase`` streams, and ``iSourceMedia`` objects.
"""

from __future__ import annotations

import gzip
import io
import os
import sqlite3
import tempfile
from typing import Any

__all__ = [
    "read_csv_to_df",
    "read_arrow_to_df",
    "read_dataframe",
    "write_dataframe",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_format(path: str) -> str:
    """Return ``'csv'``, ``'arrow'``, or ``'excel'`` from ``path``'s extension."""
    lp = path.lower()
    # strip .gz wrapper
    if lp.endswith(".gz"):
        lp = lp[:-3]
    if lp.endswith((".csv", ".tsv", ".txt")):
        return "csv"
    if lp.endswith((".arrow", ".feather")):
        return "arrow"
    if lp.endswith(".xlsx"):
        return "excel"
    return "auto"


def _import_pandas():
    try:
        import pandas as pd  # noqa: PLC0415

        return pd
    except ImportError as exc:
        raise ImportError("pandas is required for DataFrame I/O. Install it with: pip install pandas") from exc


def _import_pyarrow():
    try:
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.feather as feather  # noqa: PLC0415

        return pa, feather
    except ImportError as exc:
        raise ImportError("pyarrow is required for Arrow format. Install it with: pip install pyarrow") from exc


# ---------------------------------------------------------------------------
# read_csv_to_df / read_arrow_to_df — low-level helpers
# ---------------------------------------------------------------------------


def read_csv_to_df(path: str, **kwargs: Any):
    """Read a CSV file and return a ``pandas.DataFrame``."""
    pd = _import_pandas()
    return pd.read_csv(path, **kwargs)


def read_arrow_to_df(path: str, **kwargs: Any):
    """Read an Arrow/Feather file and return a ``pandas.DataFrame``."""
    _, feather = _import_pyarrow()
    return feather.read_feather(path, **kwargs)


# ---------------------------------------------------------------------------
# read_dataframe — factory
# ---------------------------------------------------------------------------


def read_dataframe(src: Any, *, format: str = "auto", **kwargs: Any):
    """Read various inputs into a ``pandas.DataFrame``.

    Parameters
    ----------
    src:
        - ``str`` path — format inferred from extension (``.csv``, ``.arrow``,
          ``.xlsx``, ``.gz`` wrapper accepted).
        - ``io.IOBase`` — pass ``format='csv'`` or ``format='arrow'``.
        - ``pandas.DataFrame`` — returned as-is.
        - ``iSourceMedia`` subclass — dispatched to the matching implementation.
        - Any object with a ``to_frame()`` method or a sequence of dicts.
    format:
        Override automatic format detection.
    **kwargs:
        Forwarded to the underlying reader.
    """
    pd = _import_pandas()

    # Already a DataFrame — pass through
    if isinstance(src, pd.DataFrame):
        return src

    # File path
    if isinstance(src, (str, os.PathLike)):
        return _read_from_path(str(src), format=format, **kwargs)

    # IO stream
    if isinstance(src, (io.IOBase, io.RawIOBase, io.BufferedIOBase, io.TextIOBase)):
        return _read_from_io(src, format=format, **kwargs)

    # Media objects — lazy import to avoid circular deps
    from qfpytoolbox.io.media import (  # noqa: PLC0415
        ConsoleMedia,
        DatabaseMedia,
        FileSystemMedia,
        MinIOMedia,
        SQLDumpMedia,
        iSourceMedia,
    )

    if isinstance(src, FileSystemMedia):
        return _read_from_filesystem_media(src, format=format, **kwargs)
    if isinstance(src, MinIOMedia):
        return _read_from_minio_media(src, format=format, **kwargs)
    if isinstance(src, DatabaseMedia):
        return _read_from_database_media(src, **kwargs)
    if isinstance(src, SQLDumpMedia):
        return _read_from_sqldump_media(src, **kwargs)
    if isinstance(src, ConsoleMedia):
        raise TypeError("ConsoleMedia is write-only; cannot read a DataFrame from it.")
    if isinstance(src, iSourceMedia):
        raise TypeError(f"read_dataframe is not implemented for media type {type(src).__name__!r}")

    # Tables-protocol fallback: anything with __iter__ of dicts or similar
    try:
        return pd.DataFrame(src)
    except Exception as exc:
        raise TypeError(f"Unsupported input type for read_dataframe: {type(src).__name__!r}") from exc


def _read_from_path(path: str, *, format: str = "auto", **kwargs: Any):
    pd = _import_pandas()
    is_gz = path.lower().endswith(".gz")
    base = path[:-3] if is_gz else path
    detected = _infer_format(base)
    fmt = format if format != "auto" else detected

    if fmt == "csv" or (fmt == "auto" and detected == "auto"):
        if is_gz:
            with gzip.open(path, "rt") as f:
                return pd.read_csv(f, **kwargs)
        # default auto-attempt: try arrow first, then csv
        if fmt == "auto":
            try:
                _, feather = _import_pyarrow()
                return feather.read_feather(path, **kwargs)
            except Exception:
                pass
        return pd.read_csv(path, **kwargs)

    if fmt == "arrow":
        if is_gz:
            raise ValueError("Gzipped Arrow files are not supported; provide an uncompressed .arrow file.")
        _, feather = _import_pyarrow()
        return feather.read_feather(path, **kwargs)

    if fmt == "excel":
        if is_gz:
            raise ValueError("Gzipped Excel files are not supported.")
        sheet = kwargs.pop("sheet", 0)
        return pd.read_excel(path, sheet_name=sheet, **kwargs)

    if path.lower().endswith(".xls"):
        raise ValueError("Legacy .xls format is not supported; please convert to .xlsx.")

    # unknown extension — try arrow then csv
    try:
        _, feather = _import_pyarrow()
        return feather.read_feather(path, **kwargs)
    except Exception:
        pass
    return pd.read_csv(path, **kwargs)


def _read_from_io(stream: Any, *, format: str = "auto", **kwargs: Any):
    pd = _import_pandas()
    if format == "arrow":
        _, feather = _import_pyarrow()
        return feather.read_feather(stream, **kwargs)
    if format == "csv":
        return pd.read_csv(stream, **kwargs)
    # auto: try arrow first
    data = stream.read()
    buf = io.BytesIO(data) if isinstance(data, bytes) else io.StringIO(data)
    try:
        _, feather = _import_pyarrow()
        return feather.read_feather(buf, **kwargs)
    except Exception:
        buf.seek(0)
        return pd.read_csv(buf, **kwargs)


def _read_from_filesystem_media(media: Any, *, format: str = "auto", **kwargs: Any):
    filename: str | None = kwargs.pop("filename", None)
    fmt = format if format != "auto" else media.format
    if filename is not None:
        path = os.path.join(media.path, filename)
    else:
        path = media.path
    return _read_from_path(path, format=fmt, **kwargs)


def _pop_minio_filename(options: dict[str, Any]) -> str:
    """Remove and return the required ``filename`` option of a MinIO read or write."""
    filename: str | None = options.pop("filename", None)
    if filename is None:
        raise ValueError("For MinIOMedia, provide filename=<object name>")
    return filename


def _resolve_minio_format(media: Any, filename: str, format: str) -> str:
    """Return ``'csv'`` or ``'arrow'``: the call's format, else the media's, else the extension's."""
    requested = format if format != "auto" else media.format
    resolved = requested if requested != "auto" else _infer_format(filename)
    if resolved not in ("csv", "arrow"):
        raise ValueError(
            f"Cannot use format {resolved!r} for object {filename!r}. "
            "Pass format='arrow' or format='csv', or name the object .arrow or .csv."
        )
    if resolved == "arrow" and filename.lower().endswith(".gz"):
        raise ValueError("Gzipped Arrow objects are not supported.")
    return resolved


def _read_arrow_bytes(data: bytes, **kwargs: Any):
    """Read Arrow bytes in the IPC file format (Feather) or the IPC stream format, which MyJuliaToolbox writes."""
    pa, feather = _import_pyarrow()
    if data[:4] == b"\xff\xff\xff\xff":
        return pa.ipc.open_stream(data).read_pandas(**kwargs)
    return feather.read_feather(io.BytesIO(data), **kwargs)


def _read_from_minio_media(media: Any, *, format: str = "auto", **kwargs: Any):
    """Download one object and return it as a ``pandas.DataFrame``."""
    filename = _pop_minio_filename(kwargs)
    resolved_format = _resolve_minio_format(media, filename, format)
    data = media._get_bytes(media._key(filename))
    if resolved_format == "arrow":
        return _read_arrow_bytes(data, **kwargs)
    compression = "gzip" if filename.lower().endswith(".gz") else None
    return _import_pandas().read_csv(io.BytesIO(data), compression=compression, **kwargs)


def _read_from_database_media(media: Any, **kwargs: Any):
    table: str | None = kwargs.pop("table", None)
    query: str | None = kwargs.pop("query", None)
    if table is None and query is None:
        raise ValueError("For DatabaseMedia, provide either query=<sql> or table=<table_name>")
    if table is not None and query is not None:
        raise ValueError("Provide only one of query or table for DatabaseMedia reads")

    from qfpytoolbox.io.media import _validate_sql_identifier  # noqa: PLC0415

    pd = _import_pandas()
    if query is None:
        safe = _validate_sql_identifier(table, field="table name")  # type: ignore[arg-type]
        query = f"SELECT * FROM {safe}"
    conn = media.connection
    return pd.read_sql_query(query, conn)


def _read_from_sqldump_media(media: Any, **kwargs: Any):
    """Load a SQL dump into an in-memory SQLite DB and query it."""
    table: str | None = kwargs.pop("table", None)
    query: str | None = kwargs.pop("query", None)
    if table is not None and query is not None:
        raise ValueError("Cannot provide both 'table' and 'query'. Choose one.")

    db_path = ":memory:" if media.in_memory else tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db_path)
    try:
        _load_sql_dump(conn, media.path, media.db_type)
        pd = _import_pandas()
        if table is None and query is None:
            return pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence' ORDER BY name",
                conn,
            )
        from qfpytoolbox.io.media import _validate_sql_identifier  # noqa: PLC0415

        if query is None:
            safe = _validate_sql_identifier(table, field="table name")  # type: ignore[arg-type]
            query = f"SELECT * FROM {safe}"
        return pd.read_sql_query(query, conn)
    finally:
        conn.close()
        if not media.in_memory and os.path.isfile(db_path):
            os.remove(db_path)


def _load_sql_dump(conn: sqlite3.Connection, path: str, db_type: str) -> None:
    """Read a SQL dump file and execute it in ``conn``, converting syntax as needed."""
    with open(path, encoding="utf-8", errors="replace") as f:
        sql = f.read()
    if db_type == "mysql":
        sql = _convert_mysql_to_sqlite(sql)
    elif db_type == "postgresql":
        sql = _convert_postgresql_to_sqlite(sql)
    conn.executescript(sql)


def _convert_mysql_to_sqlite(sql: str) -> str:
    import re

    # Remove MySQL-specific keywords
    sql = re.sub(r"\bAUTO_INCREMENT\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bUNSIGNED\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bTINYINT\b", "INTEGER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bMEDIUMINT\b", "INTEGER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bBIGINT\b", "INTEGER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDOUBLE\b", "REAL", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bFLOAT\b", "REAL", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDATETIME\b", "TEXT", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bTIMESTAMP\b", "TEXT", sql, flags=re.IGNORECASE)
    # Remove KEY lines inside CREATE TABLE
    sql = re.sub(r",\s*\bKEY\b[^,)]*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r",\s*\bINDEX\b[^,)]*", "", sql, flags=re.IGNORECASE)
    # Remove CHARACTER SET / COLLATE / ENGINE / CHARSET lines
    sql = re.sub(r"\bCHARACTER SET\b\s+\S+", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bCOLLATE\b\s+\S+", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bENGINE\s*=\s*\S+", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDEFAULT CHARSET\s*=\s*\S+", "", sql, flags=re.IGNORECASE)
    return sql


def _convert_postgresql_to_sqlite(sql: str) -> str:
    import re

    sql = re.sub(r"\bSERIAL\b", "INTEGER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bBIGSERIAL\b", "INTEGER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bBOOLEAN\b", "INTEGER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bBYTEA\b", "BLOB", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDOUBLE PRECISION\b", "REAL", sql, flags=re.IGNORECASE)
    return sql


# ---------------------------------------------------------------------------
# write_dataframe
# ---------------------------------------------------------------------------


def write_dataframe(
    dest: Any,
    df: Any,
    *,
    format: str = "auto",
    overwrite: bool = False,
    atomic: bool = True,
    **kwargs: Any,
) -> None:
    """Write ``df`` (a ``pandas.DataFrame``) to ``dest``.

    Parameters
    ----------
    dest:
        - ``str`` path — format inferred from extension.
        - ``io.IOBase`` stream — pass ``format='csv'`` or ``format='arrow'``.
        - ``iSourceMedia`` — dispatched to the matching implementation.
    df:
        DataFrame to write.
    format:
        Override automatic format detection.
    overwrite:
        Allow overwriting an existing file (default ``False``).
    atomic:
        Write to a temporary file then rename (default ``True``).  Ignored for
        stream and media destinations.
    **kwargs:
        Forwarded to the underlying writer.
    """
    pd = _import_pandas()
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"df must be a pandas.DataFrame, got {type(df).__name__!r}")

    if isinstance(dest, (str, os.PathLike)):
        return _write_to_path(str(dest), df, format=format, overwrite=overwrite, atomic=atomic, **kwargs)

    if isinstance(dest, (io.IOBase, io.RawIOBase, io.BufferedIOBase, io.TextIOBase)):
        return _write_to_io(dest, df, format=format, **kwargs)

    from qfpytoolbox.io.media import (  # noqa: PLC0415
        ConsoleMedia,
        DatabaseMedia,
        FileSystemMedia,
        MinIOMedia,
        SQLDumpMedia,
        iSourceMedia,
    )

    if isinstance(dest, FileSystemMedia):
        return _write_to_filesystem_media(dest, df, format=format, overwrite=overwrite, atomic=atomic, **kwargs)
    if isinstance(dest, MinIOMedia):
        return _write_to_minio_media(dest, df, format=format, overwrite=overwrite, **kwargs)
    if isinstance(dest, DatabaseMedia):
        return _write_to_database_media(dest, df, **kwargs)
    if isinstance(dest, ConsoleMedia):
        return _write_to_io(dest.stream, df, format=format if format != "auto" else "csv", **kwargs)
    if isinstance(dest, SQLDumpMedia):
        raise TypeError("SQLDumpMedia is read-only; cannot write a DataFrame to it.")
    if isinstance(dest, iSourceMedia):
        raise TypeError(f"write_dataframe is not implemented for media type {type(dest).__name__!r}")

    raise TypeError(f"Unsupported destination type for write_dataframe: {type(dest).__name__!r}")


def _write_to_path(
    path: str,
    df: Any,
    *,
    format: str,
    overwrite: bool,
    atomic: bool,
    **kwargs: Any,
) -> None:
    if os.path.isfile(path) and not overwrite:
        raise FileExistsError(f"File already exists: {path!r}. Pass overwrite=True to overwrite.")

    is_gz = path.lower().endswith(".gz")
    base = path[:-3] if is_gz else path
    detected = _infer_format(base)
    fmt = format if format != "auto" else detected

    if fmt in ("csv", "auto") or (fmt == "auto" and detected == "auto"):
        _do_write_csv(path, df, is_gz=is_gz, atomic=atomic, overwrite=overwrite, **kwargs)
    elif fmt == "arrow":
        if is_gz:
            raise ValueError("Gzipped Arrow writing is not supported.")
        _do_write_arrow(path, df, atomic=atomic, overwrite=overwrite, **kwargs)
    elif fmt == "excel":
        if is_gz:
            raise ValueError("Gzipped Excel writing is not supported.")
        _do_write_excel(path, df, atomic=atomic, overwrite=overwrite, **kwargs)
    elif path.lower().endswith(".xls"):
        raise ValueError("Legacy .xls format is not supported for writing; use .xlsx instead.")
    else:
        raise ValueError(
            f"Unsupported file extension for writing: {os.path.splitext(path)[1]!r}. "
            "Specify format='csv', 'arrow', or 'excel'."
        )


def _do_write_csv(path: str, df: Any, *, is_gz: bool, atomic: bool, overwrite: bool, **kwargs: Any) -> None:
    if atomic:
        dir_ = os.path.dirname(path) or "."
        suffix = ".csv.gz" if is_gz else ".csv"
        fd, tmp = tempfile.mkstemp(suffix=suffix, dir=dir_)
        os.close(fd)
        try:
            if is_gz:
                with gzip.open(tmp, "wt", newline="") as f:
                    df.to_csv(f, index=False, **kwargs)
            else:
                df.to_csv(tmp, index=False, **kwargs)
            os.replace(tmp, path)
        except Exception:
            if os.path.isfile(tmp):
                os.remove(tmp)
            raise
    else:
        if is_gz:
            with gzip.open(path, "wt", newline="") as f:
                df.to_csv(f, index=False, **kwargs)
        else:
            df.to_csv(path, index=False, **kwargs)


def _do_write_arrow(path: str, df: Any, *, atomic: bool, overwrite: bool, **kwargs: Any) -> None:
    _, feather = _import_pyarrow()
    if atomic:
        dir_ = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(suffix=".arrow", dir=dir_)
        os.close(fd)
        try:
            feather.write_feather(df, tmp, **kwargs)
            os.replace(tmp, path)
        except Exception:
            if os.path.isfile(tmp):
                os.remove(tmp)
            raise
    else:
        feather.write_feather(df, path, **kwargs)


def _do_write_excel(path: str, df: Any, *, atomic: bool, overwrite: bool, **kwargs: Any) -> None:
    sheet = kwargs.pop("sheet", "Sheet1")
    if atomic:
        dir_ = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=dir_)
        os.close(fd)
        try:
            df.to_excel(tmp, sheet_name=sheet, index=False, **kwargs)
            os.replace(tmp, path)
        except Exception:
            if os.path.isfile(tmp):
                os.remove(tmp)
            raise
    else:
        df.to_excel(path, sheet_name=sheet, index=False, **kwargs)


def _write_to_io(stream: Any, df: Any, *, format: str = "auto", **kwargs: Any) -> None:
    if format == "arrow":
        _, feather = _import_pyarrow()
        feather.write_feather(df, stream, **kwargs)
    else:
        df.to_csv(stream, index=False, **kwargs)


def _write_to_filesystem_media(
    media: Any,
    df: Any,
    *,
    format: str,
    overwrite: bool,
    atomic: bool,
    **kwargs: Any,
) -> None:
    filename: str | None = kwargs.pop("filename", None)
    fmt = format if format != "auto" else media.format
    os.makedirs(media.path, exist_ok=True)
    if filename is not None:
        path = os.path.join(media.path, filename)
    else:
        path = media.path
    _write_to_path(path, df, format=fmt, overwrite=overwrite, atomic=atomic, **kwargs)


def _write_to_minio_media(media: Any, df: Any, *, format: str, overwrite: bool, **kwargs: Any) -> None:
    """Serialise ``df`` in memory and upload it as one object."""
    filename = _pop_minio_filename(kwargs)
    resolved_format = _resolve_minio_format(media, filename, format)
    key = media._key(filename)
    if not overwrite and media._exists(key):
        raise FileExistsError(f"Object already exists: {key!r}. Pass overwrite=True to overwrite.")
    if resolved_format == "arrow":
        _, feather = _import_pyarrow()
        buffer = io.BytesIO()
        feather.write_feather(df, buffer, **kwargs)
        media._put_bytes(key, buffer.getvalue(), content_type="application/vnd.apache.arrow.file")
        return
    data = df.to_csv(index=False, **kwargs).encode("utf-8")
    if filename.lower().endswith(".gz"):
        data = gzip.compress(data)
    media._put_bytes(key, data, content_type="text/csv")


def _write_to_database_media(media: Any, df: Any, **kwargs: Any) -> None:
    table: str | None = kwargs.pop("table", None)
    if table is None:
        raise ValueError(
            "For DatabaseMedia writes, provide table=<table_name> or use write_dataframe(media, df, table='name')"
        )
    from qfpytoolbox.io.media import _validate_sql_identifier  # noqa: PLC0415

    safe_table = _validate_sql_identifier(table, field="table name")
    conn = media.connection
    df.to_sql(safe_table, conn, if_exists="replace", index=False)
