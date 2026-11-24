# CLAUDE.md

This file provides guidance to Claude AI when working with this repository.

## Project Overview

**QFPyToolbox** is a Python data and utilities toolbox — the Python equivalent of [`MyJuliaToolbox.jl`](https://github.com/lemahdi/MyJuliaToolbox.jl).

> ⚠️ **This is NOT a financial library.** It is a general-purpose data infrastructure toolkit.

### What this library does

| Sub-package | Purpose |
|---|---|
| `qfpytoolbox.io` | DataFrame I/O, media abstractions, and async logging |
| `qfpytoolbox.utils` | Date utilities and DataFrame comparison |
| `qfpytoolbox.parameters` | JSON-backed typed configuration management |
| `qfpytoolbox.dataset` | Typed dataset persistence (Arrow/CSV + JSON) |

---

## Repository Layout

```
src/qfpytoolbox/
├── __init__.py              # Top-level re-exports
├── parameters.py            # iParameters, read/write_parameters
├── dataset.py               # iDataSet, write_dataset, read_dataset
├── io/
│   ├── __init__.py
│   ├── media.py             # iSourceMedia and all concrete backends
│   ├── dataframes.py        # read_dataframe, write_dataframe
│   ├── arrow_tables.py      # read_arrow_table, write_arrow_table (lossless pyarrow.Table I/O)
│   └── logger.py            # AsyncLogger, LogRecord, log_* helpers
└── utils/
    ├── __init__.py
    ├── dates.py             # date2int, int2date, yearfrac, add_days_convention
    └── dataframe_compare.py # compare_dataframes, DataFrameComparisonResult

tests/
├── io/                      # Tests for io sub-package
├── utils/                   # Tests for utils sub-package
├── parameters/              # Tests for parameters module
└── dataset/                 # Tests for dataset module
```

---

## Development Commands

```bash
# Install (dev + all extras)
pip install -e ".[dev,all]"

# Run tests
pytest tests/ -v

# Lint (must pass)
ruff check src/ tests/

# Format (must pass — CI checks this too)
ruff format src/ tests/

# Format check only (as CI does it)
ruff format --check src/ tests/
```

> **Important:** The CI runs both `ruff check` (lint) **and** `ruff format --check` (formatting). Always run `ruff format src/ tests/` before committing.

---

## Key Design Conventions

### Naming
- Abstract base types use a lowercase `i` prefix: `iSourceMedia`, `iParameters`, `iDataSet` — matching the Julia convention.
- These are intentionally abstract with no required abstract methods (tagged `# noqa: B024`).

### Media abstraction
All I/O functions accept an `iSourceMedia` subclass as first argument. New storage backends are added by subclassing `iSourceMedia` and adding dispatch implementations.

### Type annotations
- Use modern Python 3.10+ `X | Y` union syntax (enforced by `ruff UP` rules).
- Use lowercase built-ins (`dict`, `list`, `tuple`) rather than `typing.Dict` etc.

### `write_dataset` / `read_dataset`
- The `type=` keyword was renamed to `file_format=` to avoid shadowing Python's built-in `type()`.
- DataFrames are written as Arrow by default (`file_format="arrow"`), or CSV (`file_format="csv"`).

### Parameters
- `iParameters` subclasses should be plain Python `dataclasses`.
- `parameters_from_dict` matches dict keys to `__init__` parameters; extra or missing keys raise `ValueError`.

### Logger
- `AsyncLogger` uses a fixed-capacity ring buffer; records are dropped (not blocked) when full.
- Always call `stop_logger(logger)` or `flush_logger(logger)` before reading logged output in tests.

---

## Adding a New Media Backend

1. Subclass `iSourceMedia` in `src/qfpytoolbox/io/media.py`.
2. Add dispatch functions in `src/qfpytoolbox/io/dataframes.py` (`_read_from_*`, `_write_to_*`).
3. If the backend supports logging: add `_write_to_*` in `src/qfpytoolbox/io/logger.py`.
4. If the backend supports parameters: add `_read_parameters_impl` / `_write_parameters_impl` in `src/qfpytoolbox/parameters.py`.
5. Export the new class from `src/qfpytoolbox/io/__init__.py` and `src/qfpytoolbox/__init__.py`.
6. Add tests under `tests/io/`.

---

## Dependencies

| Package | Use |
|---|---|
| `pandas >= 1.5` | DataFrames throughout |
| `pyarrow >= 11.0` | Arrow format read/write |
| `openpyxl >= 3.0` | Excel (.xlsx) read/write |
| `boto3 >= 1.26` | Optional — MinIO / S3 support (`[minio]` extra) |

---

## Testing Notes

- Tests are in `tests/` mirroring the `src/qfpytoolbox/` layout.
- Use `tmp_path` (pytest fixture) for all file I/O in tests — never hardcode paths.
- For async logger tests: always call `stop_logger(logger)` to drain the queue before asserting output.
- `DatabaseMedia` opens an SQLite connection with `check_same_thread=False` so it can be used from the logger's background worker thread.
