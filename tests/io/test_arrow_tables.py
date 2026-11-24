"""Tests for lossless Arrow table I/O."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from qfpytoolbox.io.arrow_tables import read_arrow_table, write_arrow_table
from qfpytoolbox.io.media import FileSystemMedia


def sample_table() -> pa.Table:
    """A table a pandas round-trip would change: a nullable integer column and schema and field metadata."""
    schema = pa.schema(
        [
            pa.field("code", pa.string()),
            pa.field("count", pa.int64(), metadata={b"unit": b"items"}),
            pa.field("amount", pa.decimal128(25, 8)),
        ],
        metadata={b"origin": b"generic"},
    )
    return pa.Table.from_arrays(
        [
            pa.array(["", None, "001"]),
            pa.array([1, None, 3], type=pa.int64()),
            pa.array([None, 1, 2], type=pa.decimal128(25, 8)),
        ],
        schema=schema,
    )


class TestRoundTrip:
    @pytest.mark.parametrize("as_media", [True, False], ids=["media", "path"])
    def test_schema_and_metadata_are_kept(self, tmp_path, as_media):
        path = tmp_path / "table.arrow"
        location = FileSystemMedia(str(path), format="arrow") if as_media else path
        assert write_arrow_table(location, sample_table()) == path
        result = read_arrow_table(location)
        assert result.equals(sample_table(), check_metadata=True)
        assert result.schema.field("count").type == pa.int64()

    def test_a_str_path_is_accepted(self, tmp_path):
        path = str(tmp_path / "table.arrow")
        write_arrow_table(path, sample_table())
        assert read_arrow_table(path).equals(sample_table(), check_metadata=True)

    def test_the_file_is_in_the_ipc_file_format(self, tmp_path):
        path = tmp_path / "table.arrow"
        write_arrow_table(path, sample_table())
        assert path.read_bytes().startswith(b"ARROW1")

    def test_the_ipc_stream_format_is_read_too(self, tmp_path):
        path = tmp_path / "table.arrows"
        table = sample_table()
        with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        assert read_arrow_table(path).equals(table, check_metadata=True)


class TestWrite:
    def test_missing_parent_folders_are_created(self, tmp_path):
        path = tmp_path / "one" / "two" / "table.arrow"
        write_arrow_table(path, sample_table())
        assert path.is_file()

    def test_an_existing_file_is_not_overwritten(self, tmp_path):
        path = tmp_path / "table.arrow"
        write_arrow_table(path, sample_table())
        before = path.read_bytes()
        with pytest.raises(FileExistsError, match="overwrite=True"):
            write_arrow_table(path, sample_table().slice(0, 1))
        assert path.read_bytes() == before

    def test_overwrite_replaces_the_file(self, tmp_path):
        path = tmp_path / "table.arrow"
        write_arrow_table(path, sample_table())
        write_arrow_table(path, sample_table().slice(0, 1), overwrite=True)
        assert read_arrow_table(path).num_rows == 1

    def test_no_temporary_file_is_left(self, tmp_path):
        write_arrow_table(tmp_path / "table.arrow", sample_table())
        assert sorted(item.name for item in tmp_path.iterdir()) == ["table.arrow"]

    def test_a_failed_write_leaves_neither_target_nor_temporary_file(self, tmp_path):
        class Broken:
            schema = sample_table().schema

        with pytest.raises(TypeError):
            write_arrow_table(tmp_path / "table.arrow", Broken())
        assert list(tmp_path.iterdir()) == []


class TestArguments:
    @pytest.mark.parametrize("location", [42, None, b"table.arrow"])
    def test_other_locations_are_refused(self, location):
        with pytest.raises(TypeError, match="FileSystemMedia or a path"):
            read_arrow_table(location)

    def test_write_refuses_other_locations(self):
        with pytest.raises(TypeError, match="FileSystemMedia or a path"):
            write_arrow_table(Path, sample_table())
