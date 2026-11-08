"""QFPyToolbox — a Pythonic data and utilities toolbox.

Python equivalent of `MyJuliaToolbox.jl <https://github.com/lemahdi/MyJuliaToolbox.jl>`_.

Sub-packages
------------
- ``qfpytoolbox.io`` — DataFrame I/O, media abstractions, and async logging.
- ``qfpytoolbox.utils`` — Date utilities and DataFrame comparison.
- ``qfpytoolbox.parameters`` — JSON-backed configuration management.
- ``qfpytoolbox.dataset`` — Typed dataset persistence (Arrow/CSV + JSON).
"""

from __future__ import annotations

__version__ = "0.1.0"

# -- IO helpers re-exported at top level for convenience --------------------
# -- Parameters & Dataset ---------------------------------------------------
from qfpytoolbox.dataset import (
    LoadedDataSet,
    iDataSet,
    nonpersisted_fields,
    read_dataset,
    write_dataset,
)
from qfpytoolbox.io import (
    ArchiveMedia,
    AsyncLogger,
    ConsoleMedia,
    DatabaseMedia,
    FileSystemMedia,
    LogRecord,
    MinIOMedia,
    SQLDumpMedia,
    dropped_logs,
    flush_logger,
    iSourceMedia,
    log_debug,
    log_error,
    log_info,
    log_warn,
    minio_create_bucket,
    minio_flush_log_buffer,
    minio_list_objects,
    read_arrow_to_df,
    read_csv_to_df,
    read_dataframe,
    stop_logger,
    write_dataframe,
)
from qfpytoolbox.parameters import (
    iParameters,
    parameters_from_dict,
    parameters_from_json,
    read_parameters,
    write_parameters,
)

# -- Utils re-exported at top level -----------------------------------------
from qfpytoolbox.utils import (
    DataFrameComparisonResult,
    add_days_convention,
    compare_dataframes,
    date2int,
    days_between,
    int2date,
    yearfrac,
)

__all__ = [
    "__version__",
    # io — media
    "iSourceMedia",
    "FileSystemMedia",
    "DatabaseMedia",
    "SQLDumpMedia",
    "ConsoleMedia",
    "ArchiveMedia",
    "MinIOMedia",
    "minio_create_bucket",
    "minio_list_objects",
    "minio_flush_log_buffer",
    # io — dataframes
    "read_dataframe",
    "write_dataframe",
    "read_csv_to_df",
    "read_arrow_to_df",
    # io — logger
    "LogRecord",
    "AsyncLogger",
    "log_debug",
    "log_info",
    "log_warn",
    "log_error",
    "flush_logger",
    "stop_logger",
    "dropped_logs",
    # utils — dates
    "date2int",
    "int2date",
    "days_between",
    "yearfrac",
    "add_days_convention",
    # utils — compare
    "DataFrameComparisonResult",
    "compare_dataframes",
    # parameters
    "iParameters",
    "parameters_from_dict",
    "parameters_from_json",
    "read_parameters",
    "write_parameters",
    # dataset
    "iDataSet",
    "LoadedDataSet",
    "nonpersisted_fields",
    "write_dataset",
    "read_dataset",
]
