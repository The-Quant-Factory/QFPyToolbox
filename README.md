# QFPyToolbox

[![CI](https://github.com/lemahdi/QFPyToolbox/actions/workflows/ci.yml/badge.svg)](https://github.com/lemahdi/QFPyToolbox/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Pythonic data and utilities toolbox — Python equivalent of [`MyJuliaToolbox.jl`](https://github.com/lemahdi/MyJuliaToolbox.jl).

---

## Features

- **DataFrame I/O** — Read/write DataFrames from CSV, Arrow, gzipped CSV, and Excel (`.xlsx`), with atomic writes
- **Media interface** — Unified read/write abstraction over file system, SQLite database, SQL dump files, console, archives (`.zip` / `.tar.gz`), and MinIO / S3-compatible object stores
- **Async logger** — Non-blocking structured logging to files, databases, or the console via a background thread
- **Date utilities** — Integer `yyyymmdd` date helpers: `date2int`, `int2date`, `days_between`, `yearfrac`, `add_days_convention` (following/modified-following/previous conventions, holidays, custom weekends)
- **DataFrame comparison** — Cell-level comparison with numeric tolerances, type checking, missing-value handling, and per-column precision
- **Parameters** — JSON-backed configuration with `iParameters`, typed round-trip serialisation, and media-aware `read_parameters`/`write_parameters`
- **Datasets** — Typed dataset persistence with `iDataSet`: DataFrames as Arrow/CSV, metadata as JSON, archive I/O

---

## Installation

```bash
# Core (pandas + pyarrow + openpyxl)
pip install qfpytoolbox

# With MinIO / S3-compatible object store support
pip install "qfpytoolbox[minio]"

# Development
pip install "qfpytoolbox[dev]"
```

---

## Quick Start

### DataFrame I/O

```python
import pandas as pd
from qfpytoolbox import read_dataframe, write_dataframe, FileSystemMedia

df = pd.DataFrame({"id": [1, 2, 3], "value": [10.5, 20.1, 30.0]})

# Write / read CSV
write_dataframe("data.csv", df)
df2 = read_dataframe("data.csv")

# Write / read Arrow
write_dataframe("data.arrow", df)
df3 = read_dataframe("data.arrow")

# Write / read gzipped CSV
write_dataframe("data.csv.gz", df)
df4 = read_dataframe("data.csv.gz")

# Via FileSystemMedia (directory-based)
media = FileSystemMedia("/tmp/mydata")
write_dataframe(media, df, filename="prices.arrow", format="arrow", overwrite=True)
df5 = read_dataframe(media, filename="prices.arrow")
```

### SQLite Database

```python
from qfpytoolbox import DatabaseMedia, read_dataframe, write_dataframe

with DatabaseMedia("analytics.db") as db:
    write_dataframe(db, df, table="prices")
    result = read_dataframe(db, table="prices")
    custom = read_dataframe(db, query="SELECT id FROM prices WHERE value > 15")
```

### MinIO / S3-Compatible Object Store

```python
from qfpytoolbox import MinIOMedia, minio_create_bucket, read_dataframe, write_dataframe

# Credentials: the arguments, then MINIO_ACCESS_KEY / MINIO_SECRET_KEY, then AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
media = MinIOMedia("analytics", endpoint="https://minio.example.com", prefix="datasets/2025/q1")
minio_create_bucket(media)

write_dataframe(media, df, filename="prices.arrow")
prices = read_dataframe(media, filename="prices.arrow")
```

To trust a private CA, set `AWS_CA_BUNDLE` to its certificate file. As a logging sink, records are held in memory and uploaded as one JSONL object by `stop_logger` or `minio_flush_log_buffer`.

### Async Logger

```python
from qfpytoolbox import (
    AsyncLogger, ConsoleMedia, FileSystemMedia,
    log_info, log_warn, log_error, stop_logger,
)

# Log to console
logger = AsyncLogger(ConsoleMedia(), min_level="info")
log_info(logger, "Job started", metadata={"job_id": 42})
log_warn(logger, "Slow query detected")
stop_logger(logger)

# Log to file
file_logger = AsyncLogger(FileSystemMedia("app.log"), min_level="debug")
log_info(file_logger, "Processing complete")
stop_logger(file_logger)
```

### Date Utilities

```python
from datetime import date
from qfpytoolbox import date2int, int2date, days_between, yearfrac, add_days_convention

# Integer date helpers
d = date(2025, 6, 30)
print(date2int(d))          # 20250630
print(int2date(20250630))   # date(2025, 6, 30)

# Day count / year fraction
print(days_between(20250101, 20251231))              # 364
print(yearfrac(20250101, 20251231, basis="actual365")) # 0.9972...

# Business day adjustment
result = add_days_convention(
    date(2026, 1, 30), 1,
    convention="modified_following",
    holidays=[date(2026, 2, 2)],
)
```

### DataFrame Comparison

```python
from qfpytoolbox import compare_dataframes

left  = pd.DataFrame({"id": [1, 2, 3], "amount": [100.0, 200.0, 300.0]})
right = pd.DataFrame({"id": [1, 2, 4], "amount": [100.0, 200.5, 400.0]})

result = compare_dataframes(
    left, right,
    left_key="id",
    target_columns=["amount"],
    precision=0.1,
    title="reconcile",
)
print(result.equal)              # False
print(result.left_only_rows)     # 1  (id=3)
print(result.right_only_rows)    # 1  (id=4)
print(result.numeric_mismatches) # 1  (id=2, diff=0.5)
print(result.differences)        # DataFrame with full details
```

### Parameters (JSON Configuration)

```python
import dataclasses
from qfpytoolbox import iParameters, write_parameters, read_parameters, FileSystemMedia

@dataclasses.dataclass
class ModelParams(iParameters):
    learning_rate: float
    epochs: int
    name: str

params = ModelParams(learning_rate=0.001, epochs=20, name="experiment-1")

# Write / read directly
write_parameters("config.json", params)
loaded = read_parameters(ModelParams, FileSystemMedia("."), "config.json")
print(loaded.learning_rate)  # 0.001
```

### Datasets

```python
import dataclasses
from qfpytoolbox import iDataSet, write_dataset, read_dataset, FileSystemMedia

@dataclasses.dataclass
class MyDataSet(iDataSet):
    prices: pd.DataFrame
    label: str
    version: int

ds = MyDataSet(prices=df, label="v1", version=1)

# Write (DataFrames → Arrow, scalars → data.json)
write_dataset(FileSystemMedia("/tmp/myds"), ds)

# Read back (auto-typed from data_info.json when class is importable)
loaded = read_dataset("/tmp/myds")

# Archive I/O
from qfpytoolbox import ArchiveMedia
write_dataset(ArchiveMedia("myds.zip"), ds, overwrite=True)
```

---

## Module Overview

| Module | Description |
|---|---|
| `qfpytoolbox.io.media` | `iSourceMedia`, `FileSystemMedia`, `DatabaseMedia`, `SQLDumpMedia`, `ConsoleMedia`, `ArchiveMedia`, `MinIOMedia`, `minio_create_bucket`, `minio_list_objects` |
| `qfpytoolbox.io.dataframes` | `read_dataframe`, `write_dataframe`, `read_csv_to_df`, `read_arrow_to_df` |
| `qfpytoolbox.io.logger` | `AsyncLogger`, `LogRecord`, `log_info`, `log_warn`, `log_error`, `log_debug`, `stop_logger`, `flush_logger`, `dropped_logs`, `minio_flush_log_buffer` |
| `qfpytoolbox.utils.dates` | `date2int`, `int2date`, `days_between`, `yearfrac`, `add_days_convention` |
| `qfpytoolbox.utils.dataframe_compare` | `compare_dataframes`, `DataFrameComparisonResult` |
| `qfpytoolbox.parameters` | `iParameters`, `parameters_from_dict`, `parameters_from_json`, `read_parameters`, `write_parameters` |
| `qfpytoolbox.dataset` | `iDataSet`, `LoadedDataSet`, `write_dataset`, `read_dataset`, `nonpersisted_fields` |

All public symbols are also re-exported from the top-level `qfpytoolbox` package.

---

## Python Version Support

| Python | Status |
|---|---|
| 3.9 | ✅ Supported |
| 3.10 | ✅ Supported |
| 3.11 | ✅ Supported |
| 3.12 | ✅ Supported |
| 3.13 | ✅ Supported |

---

## Development

```bash
git clone https://github.com/lemahdi/QFPyToolbox.git
cd QFPyToolbox
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.

