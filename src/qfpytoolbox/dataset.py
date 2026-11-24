"""Dataset interface — mirrors ``MyJuliaToolbox.jl/src/dataset.jl``.

Provides an abstract base class ``iDataSet`` for collections that combine
multiple ``pandas.DataFrame`` fields with arbitrary JSON-serialisable metadata,
plus typed parameter sub-objects.

``write_dataset`` / ``read_dataset`` persist datasets to / from a directory
(each DataFrame as a separate Arrow or CSV file, metadata in ``data.json``,
type info in ``data_info.json``).  Archive (``.zip`` / ``.tar.gz``) I/O is
supported through :class:`~qfpytoolbox.io.media.ArchiveMedia`, and object-store
I/O through :class:`~qfpytoolbox.io.media.MinIOMedia` (the dataset's files become
objects under the media's prefix).
"""

from __future__ import annotations

import dataclasses
import json
import os
import tarfile
import tempfile
import zipfile
from abc import ABC
from typing import Any, TypeVar

__all__ = [
    "iDataSet",
    "LoadedDataSet",
    "write_dataset",
    "read_dataset",
    "nonpersisted_fields",
]

T = TypeVar("T", bound="iDataSet")

_DATASET_PARAMETERS_DIR = "parameters"
_DATASET_INFO_FILE = "data_info.json"
_DATASET_PARAMETER_REF_KIND = "mjt_iParameters_ref"
_DATASET_INFO_KIND = "mjt_dataset_info"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class iDataSet(ABC):  # noqa: B024
    """Abstract base type for datasets.

    Concrete subclasses typically contain:

    - ``pandas.DataFrame`` fields — persisted as Arrow or CSV files.
    - :class:`~qfpytoolbox.parameters.iParameters` fields — persisted as JSON
      in the ``parameters/`` sub-directory.
    - Other JSON-serialisable fields — persisted in ``data.json``.
    """


# ---------------------------------------------------------------------------
# nonpersisted_fields
# ---------------------------------------------------------------------------


def nonpersisted_fields(cls: type) -> tuple[str, ...]:
    """Return field names that ``write_dataset`` should skip.

    Override per ``iDataSet`` subclass to exclude computed/cached fields from
    persistence.  The default returns an empty tuple.
    """
    return ()


# ---------------------------------------------------------------------------
# LoadedDataSet
# ---------------------------------------------------------------------------


class LoadedDataSet(iDataSet):
    """Concrete ``iDataSet`` returned by untyped ``read_dataset`` calls.

    Attributes are stored in a plain dictionary accessible via ``.attributes``.
    """

    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes = attributes


# ---------------------------------------------------------------------------
# write_dataset
# ---------------------------------------------------------------------------


def write_dataset(
    source: Any,
    dataset: iDataSet,
    *,
    file_format: str = "arrow",
    overwrite: bool = False,
    atomic: bool = True,
    pretty: bool = True,
) -> None:
    """Write ``dataset`` to ``source``.

    Parameters
    ----------
    source:
        :class:`~qfpytoolbox.io.media.FileSystemMedia`,
        :class:`~qfpytoolbox.io.media.ArchiveMedia`,
        :class:`~qfpytoolbox.io.media.ConsoleMedia`,
        :class:`~qfpytoolbox.io.media.MinIOMedia`, or a plain ``str`` path.
    dataset:
        The ``iDataSet`` instance to persist.
    file_format:
        File format for DataFrame fields — ``"arrow"`` (default) or ``"csv"``.
    overwrite:
        Allow overwriting existing files (or objects).
    atomic:
        Use atomic writes for DataFrame files (default ``True``).  For a ``MinIOMedia``, which has no
        multi-object commit, a failed upload deletes the objects already uploaded by this call.
    pretty:
        Pretty-print JSON output (default ``True``).
    """
    from qfpytoolbox.io.media import ArchiveMedia, ConsoleMedia, FileSystemMedia, MinIOMedia  # noqa: PLC0415

    if isinstance(source, str):
        if _is_archive_path(source):
            source = ArchiveMedia(source)
        else:
            source = FileSystemMedia(source)

    if isinstance(source, FileSystemMedia):
        _write_to_filesystem(
            source.path, dataset, file_format=file_format, overwrite=overwrite, atomic=atomic, pretty=pretty
        )
    elif isinstance(source, ArchiveMedia):
        _write_to_archive(source, dataset, file_format=file_format, overwrite=overwrite, pretty=pretty)
    elif isinstance(source, ConsoleMedia):
        _write_to_console(source, dataset, pretty=pretty)
    elif isinstance(source, MinIOMedia):
        _write_to_minio(source, dataset, file_format=file_format, overwrite=overwrite, atomic=atomic, pretty=pretty)
    else:
        raise TypeError(
            f"write_dataset is only implemented for FileSystemMedia, ArchiveMedia, ConsoleMedia, and MinIOMedia. "
            f"Got: {type(source).__name__!r}"
        )


def _write_to_filesystem(
    base_path: str,
    dataset: iDataSet,
    *,
    file_format: str,
    overwrite: bool,
    atomic: bool,
    pretty: bool,
) -> None:
    import pandas as pd  # noqa: PLC0415

    from qfpytoolbox.io.dataframes import write_dataframe  # noqa: PLC0415
    from qfpytoolbox.parameters import iParameters, write_parameters  # noqa: PLC0415

    if os.path.isfile(base_path):
        raise ValueError(f"Expected a directory path for FileSystemMedia, got a file: {base_path!r}")
    os.makedirs(base_path, exist_ok=True)

    if file_format not in ("arrow", "csv"):
        raise ValueError(f"Unsupported file_format: {file_format!r}. Supported: 'arrow', 'csv'")

    ext = "arrow" if file_format == "arrow" else "csv"
    fmt = file_format

    payload: dict[str, Any] = {}
    np_fields = set(nonpersisted_fields(dataset.__class__))
    dataset_type = type(dataset)

    for field_name, value in _iter_dataset_fields(dataset):
        if field_name in np_fields:
            continue
        if isinstance(value, pd.DataFrame):
            if value.empty:
                continue
            file_name = f"{field_name}.{ext}"
            full = os.path.join(base_path, file_name)
            write_dataframe(full, value, format=fmt, overwrite=overwrite, atomic=atomic)
        elif isinstance(value, iParameters):
            params_dir = os.path.join(base_path, _DATASET_PARAMETERS_DIR)
            os.makedirs(params_dir, exist_ok=True)
            file_name = f"{field_name}.json"
            write_parameters(
                params_dir,
                value,
                file_name,
                overwrite=overwrite,
                pretty=pretty,
            )
            value_type = type(value)
            payload[field_name] = {
                "__kind__": _DATASET_PARAMETER_REF_KIND,
                "type": f"{value_type.__module__}.{value_type.__qualname__}",
                "file": file_name,
                "relative_path": _DATASET_PARAMETERS_DIR,
            }
        else:
            payload[field_name] = _to_json(value)

    json_path = os.path.join(base_path, "data.json")
    if os.path.isfile(json_path) and not overwrite:
        raise FileExistsError(f"File already exists: {json_path!r}. Pass overwrite=True to overwrite.")
    with open(json_path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(payload, f, indent=2)
        else:
            json.dump(payload, f)

    _write_info(base_path, dataset_type, overwrite=overwrite, pretty=pretty)


def _write_to_archive(
    media: Any,
    dataset: iDataSet,
    *,
    file_format: str,
    overwrite: bool,
    pretty: bool,
) -> None:
    """Write dataset to a temporary directory then pack it into an archive."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        _write_to_filesystem(tmp_dir, dataset, file_format=file_format, overwrite=True, atomic=False, pretty=pretty)
        _pack_directory_to_archive(tmp_dir, media.path, media.format, overwrite=overwrite)


def _pack_directory_to_archive(src_dir: str, archive_path: str, fmt: str, *, overwrite: bool) -> None:
    if os.path.isfile(archive_path) and not overwrite:
        raise FileExistsError(f"Archive already exists: {archive_path!r}. Pass overwrite=True.")
    if fmt == "zip":
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(src_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arcname = os.path.relpath(abs_path, src_dir)
                    zf.write(abs_path, arcname)
    elif fmt == "tar_gz":
        with tarfile.open(archive_path, "w:gz") as tf:
            tf.add(src_dir, arcname=".")
    else:
        raise ValueError(f"Unsupported archive format: {fmt!r}")


def _write_to_minio(
    media: Any,
    dataset: iDataSet,
    *,
    file_format: str,
    overwrite: bool,
    atomic: bool,
    pretty: bool,
) -> None:
    """Write ``dataset`` to a temporary folder as a ``FileSystemMedia``, then upload each file under the prefix.

    Without ``overwrite``, every key is checked before the first upload, so a refusal uploads nothing.  With
    ``atomic``, a failed upload deletes the objects this call uploaded (S3 has no multi-object commit, so this
    is a clean-up, not a transaction).  Mirrors ``_write_dataset_impl(::MinIOMedia)`` in ``MyJuliaToolbox.jl``.
    """
    from qfpytoolbox.io.media import _minio_content_type  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp_dir:
        _write_to_filesystem(tmp_dir, dataset, file_format=file_format, overwrite=True, atomic=False, pretty=pretty)
        uploads: list[tuple[str, str]] = []
        for root, _, files in os.walk(tmp_dir):
            for name in sorted(files):
                local_path = os.path.join(root, name)
                relative = os.path.relpath(local_path, tmp_dir).replace(os.sep, "/")
                uploads.append((media._key(relative), local_path))
        if not overwrite:
            existing = [key for key, _ in uploads if media._exists(key)]
            if existing:
                raise FileExistsError(f"Objects already exist: {existing!r}. Pass overwrite=True to overwrite.")
        uploaded: list[str] = []
        try:
            for key, local_path in uploads:
                with open(local_path, "rb") as stream:
                    media._put_bytes(key, stream.read(), content_type=_minio_content_type(key))
                uploaded.append(key)
        except Exception:
            if atomic:
                for key in uploaded:
                    # Best-effort clean-up: the upload error is the one raised.
                    try:
                        media._delete(key)
                    except Exception:
                        pass
            raise


def _write_to_console(source: Any, dataset: iDataSet, *, pretty: bool) -> None:
    import pandas as pd  # noqa: PLC0415

    payload: dict[str, Any] = {}
    for field_name, value in _iter_dataset_fields(dataset):
        if isinstance(value, pd.DataFrame):
            print(f"### {field_name}", file=source.stream)
            print(value.to_string(), file=source.stream)
            print(file=source.stream)
        else:
            payload[field_name] = _to_json(value)

    if payload:
        print("### data.json", file=source.stream)
        if pretty:
            print(json.dumps(payload, indent=2), file=source.stream)
        else:
            print(json.dumps(payload), file=source.stream)
        print(file=source.stream)


def _write_info(base_path: str, cls: type, *, overwrite: bool, pretty: bool) -> None:
    info_path = os.path.join(base_path, _DATASET_INFO_FILE)
    if os.path.isfile(info_path) and not overwrite:
        raise FileExistsError(f"File already exists: {info_path!r}. Pass overwrite=True to overwrite.")
    payload = {
        "__kind__": _DATASET_INFO_KIND,
        "dataset_type": f"{cls.__module__}.{cls.__qualname__}",
    }
    with open(info_path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(payload, f, indent=2)
        else:
            json.dump(payload, f)


# ---------------------------------------------------------------------------
# read_dataset
# ---------------------------------------------------------------------------


def read_dataset(
    source_or_type: Any,
    source: Any | None = None,
    *,
    file_format: str = "arrow",
) -> Any:
    """Read a dataset from ``source``.

    Signatures
    ----------
    ``read_dataset(source, file_format='arrow')``
        Untyped read — returns a :class:`LoadedDataSet`.

    ``read_dataset(MyDataSet, source, file_format='arrow')``
        Typed read — attempts to reconstruct ``MyDataSet`` from the loaded
        attributes.

    ``source`` may be:

    - A ``str`` directory path or archive path (``.zip``, ``.tar.gz``).
    - A :class:`~qfpytoolbox.io.media.FileSystemMedia`.
    - A :class:`~qfpytoolbox.io.media.ArchiveMedia`.
    - A :class:`~qfpytoolbox.io.media.MinIOMedia`: the objects under its prefix are downloaded to a temporary
      folder, then read as a ``FileSystemMedia`` (so the type comes from ``data_info.json``, as for a folder).
    """
    from qfpytoolbox.io.media import ArchiveMedia, FileSystemMedia, MinIOMedia  # noqa: PLC0415

    # typed call: read_dataset(MyClass, source, ...)
    cls: type | None
    if isinstance(source_or_type, type) and issubclass(source_or_type, iDataSet):
        cls = source_or_type
        src = source
    else:
        cls = None
        src = source_or_type

    if isinstance(src, str):
        if _is_archive_path(src):
            src = ArchiveMedia(src)
        else:
            src = FileSystemMedia(src)

    if isinstance(src, FileSystemMedia):
        loaded = _read_from_filesystem(src.path, file_format=file_format)
        if cls is None:
            # try to auto-resolve from data_info.json
            declared = _read_declared_type(src.path)
            if declared is not None:
                return _build_typed(declared, loaded, src.path)
        else:
            return _build_typed(cls, loaded, src.path)
        return loaded
    elif isinstance(src, ArchiveMedia):
        return _read_from_archive(src, cls=cls, file_format=file_format)
    elif isinstance(src, MinIOMedia):
        return _read_from_minio(src, cls=cls, file_format=file_format)
    else:
        raise TypeError(
            "read_dataset is only implemented for FileSystemMedia, ArchiveMedia, and MinIOMedia. "
            f"Got: {type(src).__name__!r}"
        )


def _read_from_filesystem(base_path: str, *, file_format: str) -> LoadedDataSet:
    from qfpytoolbox.io.dataframes import read_dataframe  # noqa: PLC0415

    if not os.path.isdir(base_path):
        raise ValueError(f"Directory not found for dataset read: {base_path!r}")

    ext = ".arrow" if file_format == "arrow" else ".csv"
    attributes: dict[str, Any] = {}

    for entry in os.listdir(base_path):
        full = os.path.join(base_path, entry)
        if os.path.isfile(full) and entry.lower().endswith(ext):
            key = os.path.splitext(entry)[0]
            attributes[key] = read_dataframe(full, format=file_format)

    json_path = os.path.join(base_path, "data.json")
    if os.path.isfile(json_path):
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            attributes[k] = _decode_json_value(v, base_path)

    return LoadedDataSet(attributes)


def _read_from_archive(media: Any, *, cls: type | None, file_format: str) -> Any:
    from qfpytoolbox.io.media import FileSystemMedia  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp_dir:
        if media.format == "zip":
            with zipfile.ZipFile(media.path, "r") as zf:
                zf.extractall(tmp_dir)
        elif media.format == "tar_gz":
            with tarfile.open(media.path, "r:gz") as tf:
                tf.extractall(tmp_dir)
        else:
            raise ValueError(f"Unsupported archive format: {media.format!r}")

        fs_media = FileSystemMedia(tmp_dir)
        if cls is not None:
            return read_dataset(cls, fs_media, file_format=file_format)
        return read_dataset(fs_media, file_format=file_format)


def _read_from_minio(media: Any, *, cls: type | None, file_format: str) -> Any:
    """Download every object under the media's prefix into a temporary folder and read it as a ``FileSystemMedia``.

    Mirrors ``read_dataset(::MinIOMedia)`` in ``MyJuliaToolbox.jl``.  A prefix holding no object raises
    ``ValueError``; a key that would leave the temporary folder (a ``..`` part) is refused.
    """
    from qfpytoolbox.io.media import FileSystemMedia, minio_list_objects  # noqa: PLC0415

    keys = minio_list_objects(media)
    if not keys:
        raise ValueError(
            f"No objects found at bucket={media.bucket!r}, prefix={media.prefix!r}. Has the dataset been written yet?"
        )
    search_prefix = f"{media.prefix}/" if media.prefix else ""
    with tempfile.TemporaryDirectory() as tmp_dir:
        for key in keys:
            parts = key[len(search_prefix) :].split("/")
            if any(part in ("", ".", "..") for part in parts):
                raise ValueError(f"Refusing object key {key!r}: it does not name a file under the prefix.")
            destination = os.path.join(tmp_dir, *parts)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as stream:
                stream.write(media._get_bytes(key))
        fs_media = FileSystemMedia(tmp_dir)
        if cls is not None:
            return read_dataset(cls, fs_media, file_format=file_format)
        return read_dataset(fs_media, file_format=file_format)


def _read_declared_type(base_path: str) -> type | None:
    info_path = os.path.join(base_path, _DATASET_INFO_FILE)
    if not os.path.isfile(info_path):
        return None
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    if not isinstance(info, dict) or "dataset_type" not in info:
        return None
    kind = info.get("__kind__", _DATASET_INFO_KIND)
    if kind != _DATASET_INFO_KIND:
        return None
    return _resolve_type(info["dataset_type"])


def _build_typed(cls: type, loaded: LoadedDataSet, base_path: str) -> Any:
    """Construct a concrete ``iDataSet`` from loaded attributes."""
    import typing  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415

    hints: dict[str, Any] = {}
    try:
        hints = typing.get_type_hints(cls)
    except Exception:  # noqa: BLE001
        pass

    np_fields = set(nonpersisted_fields(cls))

    kwargs: dict[str, Any] = {}
    for field_name, hint in hints.items():
        if field_name in np_fields:
            kwargs[field_name] = _default_for(hint)
            continue
        if field_name in loaded.attributes:
            kwargs[field_name] = loaded.attributes[field_name]
        elif isinstance(hint, type) and issubclass(hint, pd.DataFrame):
            kwargs[field_name] = pd.DataFrame()
        else:
            # check for Optional
            origin = getattr(hint, "__origin__", None)
            import typing  # noqa: PLC0415, F811

            if origin is typing.Union:
                args = hint.__args__
                if type(None) in args:
                    kwargs[field_name] = None
                    continue

    # only pass kwargs for fields that exist in hints
    filtered = {k: v for k, v in kwargs.items() if k in hints}
    return cls(**filtered)


def _default_for(hint: Any) -> Any:
    import typing  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415

    if isinstance(hint, type) and issubclass(hint, pd.DataFrame):
        return pd.DataFrame()
    origin = getattr(hint, "__origin__", None)
    if origin is typing.Union:
        if type(None) in hint.__args__:
            return None
    return None


def _decode_json_value(value: Any, base_path: str) -> Any:
    if isinstance(value, dict) and value.get("__kind__") == _DATASET_PARAMETER_REF_KIND:
        file_name = value["file"]
        rel_path = value.get("relative_path", "")
        full = os.path.join(base_path, rel_path, file_name) if rel_path else os.path.join(base_path, file_name)
        type_name = value.get("type", "")
        resolved_cls = _resolve_type(type_name)
        if resolved_cls is not None:
            from qfpytoolbox.parameters import parameters_from_json  # noqa: PLC0415

            return parameters_from_json(resolved_cls, full)
        # fallback: return raw dict
        with open(full, encoding="utf-8") as f:
            return json.load(f)
    return value


def _resolve_type(type_name: str) -> type | None:
    """Try to resolve a dotted type name to a Python type."""
    import importlib  # noqa: PLC0415

    if not type_name:
        return None
    parts = type_name.rsplit(".", 1)
    if len(parts) == 2:
        module_name, class_name = parts
        try:
            mod = importlib.import_module(module_name)
            return getattr(mod, class_name, None)
        except (ImportError, AttributeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Field iteration helpers
# ---------------------------------------------------------------------------


def _iter_dataset_fields(dataset: iDataSet):
    """Yield (name, value) pairs from a dataset object."""
    if dataclasses.is_dataclass(dataset):
        for f in dataclasses.fields(dataset):
            yield f.name, getattr(dataset, f.name)
    else:
        for name in vars(dataset):
            if not name.startswith("_"):
                yield name, getattr(dataset, name)


def _to_json(value: Any) -> Any:
    """Recursively convert a value to a JSON-compatible primitive."""
    if isinstance(value, dict):
        return {str(k): _to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def _is_archive_path(path: str) -> bool:
    lp = path.lower()
    return lp.endswith(".zip") or lp.endswith(".tar.gz") or lp.endswith(".tgz")
