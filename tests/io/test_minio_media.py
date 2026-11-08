"""Tests for MinIOMedia, against an in-memory stand-in for boto3's S3 client."""

from __future__ import annotations

import gzip
import io
import json
import sys

import pandas as pd
import pyarrow as pa
import pytest

boto3 = pytest.importorskip("boto3")

from botocore.exceptions import ClientError  # noqa: E402

from qfpytoolbox.io.dataframes import read_dataframe, write_dataframe  # noqa: E402
from qfpytoolbox.io.logger import AsyncLogger, log_info, minio_flush_log_buffer, stop_logger  # noqa: E402
from qfpytoolbox.io.media import MinIOMedia, minio_create_bucket, minio_list_objects  # noqa: E402

FRAME = pd.DataFrame({"id": [1, 2, 3], "value": [10.5, 20.1, 30.0]})
CREDENTIAL_VARIABLES = ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def client_error(code: str, operation: str) -> ClientError:
    """Build the exception botocore raises for an S3 error ``code``."""
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakePaginator:
    """Serve an object listing in pages, as boto3's paginator does."""

    def __init__(self, client: FakeS3Client) -> None:
        self.client = client

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        """Yield the pages of keys under ``Prefix``; an empty listing has no ``Contents``."""
        keys = sorted(key for key in self.client.objects if key.startswith(Prefix))
        if not keys:
            yield {}
        for start in range(0, len(keys), self.client.page_size):
            yield {"Contents": [{"Key": key} for key in keys[start : start + self.client.page_size]]}


class FakeS3Client:
    """In-memory stand-in for boto3's S3 client, answering with S3's error codes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.buckets: set[str] = {"bucket"}
        self.denied_operations: set[str] = set()
        self.page_size = 1000

    def _refuse_if_denied(self, operation: str, code: str = "AccessDenied") -> None:
        if operation in self.denied_operations:
            raise client_error(code, operation)

    def head_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        self._refuse_if_denied("head_object", "403")
        if Key not in self.objects:
            raise client_error("404", "HeadObject")
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        self._refuse_if_denied("get_object")
        if Key not in self.objects:
            raise client_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict:  # noqa: N803
        self._refuse_if_denied("put_object")
        self.objects[Key] = Body
        self.content_types[Key] = ContentType
        return {}

    def head_bucket(self, Bucket: str) -> dict:  # noqa: N803
        self._refuse_if_denied("head_bucket", "403")
        if Bucket not in self.buckets:
            raise client_error("404", "HeadBucket")
        return {}

    def create_bucket(self, Bucket: str) -> dict:  # noqa: N803
        self.buckets.add(Bucket)
        return {}

    def get_paginator(self, operation: str) -> FakePaginator:
        assert operation == "list_objects_v2"
        return FakePaginator(self)


@pytest.fixture(autouse=True)
def no_credentials_in_environment(monkeypatch):
    for variable_name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)


@pytest.fixture
def client_options(monkeypatch):
    """Record the options each ``boto3.client`` call receives."""
    recorded: dict = {}

    def fake_client(service: str, **options):
        recorded.update(options, service=service)
        return FakeS3Client()

    monkeypatch.setattr(boto3, "client", fake_client)
    return recorded


@pytest.fixture
def s3_client(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(boto3, "client", lambda *args, **options: fake)
    return fake


@pytest.fixture
def media(s3_client):
    return MinIOMedia("bucket", access_key="access", secret_key="secret")


class TestConstruction:
    def test_requires_an_access_key(self, s3_client):
        with pytest.raises(ValueError, match="access_key is empty"):
            MinIOMedia("bucket", secret_key="secret")

    def test_requires_a_secret_key(self, s3_client):
        with pytest.raises(ValueError, match="secret_key is empty"):
            MinIOMedia("bucket", access_key="access")

    def test_explicit_credentials_win(self, client_options, monkeypatch):
        monkeypatch.setenv("MINIO_ACCESS_KEY", "from-environment")
        monkeypatch.setenv("MINIO_SECRET_KEY", "from-environment")
        MinIOMedia("bucket", access_key="explicit-access", secret_key="explicit-secret")
        assert client_options["aws_access_key_id"] == "explicit-access"
        assert client_options["aws_secret_access_key"] == "explicit-secret"

    def test_minio_variables_win_over_aws_variables(self, client_options, monkeypatch):
        monkeypatch.setenv("MINIO_ACCESS_KEY", "minio-access")
        monkeypatch.setenv("MINIO_SECRET_KEY", "minio-secret")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-access")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        MinIOMedia("bucket")
        assert client_options["aws_access_key_id"] == "minio-access"
        assert client_options["aws_secret_access_key"] == "minio-secret"

    def test_falls_back_on_aws_variables(self, client_options, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-access")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        MinIOMedia("bucket")
        assert client_options["aws_access_key_id"] == "aws-access"
        assert client_options["aws_secret_access_key"] == "aws-secret"

    def test_invalid_format_raises(self, s3_client):
        with pytest.raises(ValueError, match="Unsupported format"):
            MinIOMedia("bucket", access_key="access", secret_key="secret", format="parquet")

    def test_prefix_is_stripped(self, s3_client):
        assert MinIOMedia("bucket", access_key="a", secret_key="s", prefix="/one/two/").prefix == "one/two"

    def test_repr_holds_no_secret(self, s3_client):
        text = repr(MinIOMedia("bucket", access_key="the-access", secret_key="the-secret"))
        assert "MinIOMedia" in text
        assert "the-access" not in text
        assert "the-secret" not in text

    def test_client_is_path_style_on_the_endpoint(self, client_options):
        MinIOMedia("bucket", endpoint="https://store:9000/", access_key="a", secret_key="s", region="eu-west-1")
        assert client_options["service"] == "s3"
        assert client_options["endpoint_url"] == "https://store:9000"
        assert client_options["region_name"] == "eu-west-1"
        assert client_options["config"].s3 == {"addressing_style": "path"}

    def test_verification_is_left_to_botocore_by_default(self, client_options):
        MinIOMedia("bucket", access_key="a", secret_key="s")
        assert client_options["verify"] is None

    def test_tls_verify_false_applies_to_the_client(self, client_options):
        MinIOMedia("bucket", access_key="a", secret_key="s", tls_verify=False)
        assert client_options["verify"] is False

    def test_missing_boto3_names_the_extra(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "boto3", None)
        with pytest.raises(ImportError, match="qfpytoolbox\\[minio\\]"):
            MinIOMedia("bucket", access_key="a", secret_key="s")


class TestKeys:
    def test_without_prefix(self, media):
        assert media._key("a/b.arrow") == "a/b.arrow"

    def test_with_prefix(self, s3_client):
        media = MinIOMedia("bucket", access_key="a", secret_key="s", prefix="one/two")
        assert media._key("/b.arrow") == "one/two/b.arrow"

    def test_backslashes_become_forward_slashes(self, media):
        assert media._key("a\\b.arrow") == "a/b.arrow"


class TestDataFrames:
    def test_arrow_round_trip(self, media, s3_client):
        write_dataframe(media, FRAME, filename="prices.arrow")
        assert s3_client.content_types["prices.arrow"] == "application/vnd.apache.arrow.file"
        pd.testing.assert_frame_equal(read_dataframe(media, filename="prices.arrow"), FRAME)

    def test_csv_round_trip(self, media, s3_client):
        write_dataframe(media, FRAME, filename="prices.csv")
        assert s3_client.content_types["prices.csv"] == "text/csv"
        assert s3_client.objects["prices.csv"].startswith(b"id,value\n")
        pd.testing.assert_frame_equal(read_dataframe(media, filename="prices.csv"), FRAME)

    def test_gzipped_csv_is_really_gzipped(self, media, s3_client):
        write_dataframe(media, FRAME, filename="prices.csv.gz")
        assert gzip.decompress(s3_client.objects["prices.csv.gz"]).startswith(b"id,value\n")
        pd.testing.assert_frame_equal(read_dataframe(media, filename="prices.csv.gz"), FRAME)

    def test_object_is_stored_under_the_prefix(self, s3_client):
        media = MinIOMedia("bucket", access_key="a", secret_key="s", prefix="one/two")
        write_dataframe(media, FRAME, filename="prices.arrow")
        assert list(s3_client.objects) == ["one/two/prices.arrow"]

    def test_media_format_is_used_for_an_unknown_extension(self, s3_client):
        media = MinIOMedia("bucket", access_key="a", secret_key="s", format="csv")
        write_dataframe(media, FRAME, filename="prices.bin")
        assert s3_client.content_types["prices.bin"] == "text/csv"

    def test_call_format_wins_over_media_format(self, s3_client):
        media = MinIOMedia("bucket", access_key="a", secret_key="s", format="csv")
        write_dataframe(media, FRAME, filename="prices.bin", format="arrow")
        assert s3_client.content_types["prices.bin"] == "application/vnd.apache.arrow.file"

    def test_unknown_extension_raises(self, media):
        with pytest.raises(ValueError, match="Cannot use format"):
            write_dataframe(media, FRAME, filename="prices.bin")

    def test_excel_is_refused(self, media):
        with pytest.raises(ValueError, match="Cannot use format"):
            read_dataframe(media, filename="prices.xlsx")

    def test_gzipped_arrow_is_refused(self, media):
        with pytest.raises(ValueError, match="Gzipped Arrow"):
            write_dataframe(media, FRAME, filename="prices.arrow.gz")

    def test_read_requires_a_filename(self, media):
        with pytest.raises(ValueError, match="provide filename"):
            read_dataframe(media)

    def test_write_requires_a_filename(self, media):
        with pytest.raises(ValueError, match="provide filename"):
            write_dataframe(media, FRAME)

    def test_existing_object_is_not_overwritten(self, media, s3_client):
        write_dataframe(media, FRAME, filename="prices.csv")
        before = s3_client.objects["prices.csv"]
        with pytest.raises(FileExistsError, match="overwrite=True"):
            write_dataframe(media, FRAME.head(1), filename="prices.csv")
        assert s3_client.objects["prices.csv"] == before

    def test_overwrite_replaces_the_object(self, media):
        write_dataframe(media, FRAME, filename="prices.csv")
        write_dataframe(media, FRAME.head(1), filename="prices.csv", overwrite=True)
        assert len(read_dataframe(media, filename="prices.csv")) == 1

    def test_denied_existence_check_is_an_error_not_an_absent_object(self, media, s3_client):
        s3_client.denied_operations.add("head_object")
        with pytest.raises(ClientError):
            write_dataframe(media, FRAME, filename="prices.csv")
        assert s3_client.objects == {}

    def test_missing_object_raises_file_not_found(self, media):
        with pytest.raises(FileNotFoundError, match="absent.csv"):
            read_dataframe(media, filename="absent.csv")

    def test_denied_read_is_not_reported_as_missing(self, media, s3_client):
        s3_client.objects["prices.csv"] = b"id\n1\n"
        s3_client.denied_operations.add("get_object")
        with pytest.raises(ClientError):
            read_dataframe(media, filename="prices.csv")

    def test_reads_the_arrow_stream_format(self, media, s3_client):
        sink = io.BytesIO()
        table = pa.Table.from_pandas(FRAME, preserve_index=False)
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        s3_client.objects["prices.arrow"] = sink.getvalue()
        pd.testing.assert_frame_equal(read_dataframe(media, filename="prices.arrow"), FRAME)


class TestBuckets:
    def test_creates_an_absent_bucket(self, s3_client):
        minio_create_bucket(MinIOMedia("new-bucket", access_key="a", secret_key="s"))
        assert "new-bucket" in s3_client.buckets

    def test_existing_bucket_is_left_alone(self, media, s3_client):
        minio_create_bucket(media)
        assert s3_client.buckets == {"bucket"}

    def test_denied_bucket_check_is_an_error(self, s3_client):
        s3_client.denied_operations.add("head_bucket")
        with pytest.raises(ClientError):
            minio_create_bucket(MinIOMedia("new-bucket", access_key="a", secret_key="s"))
        assert "new-bucket" not in s3_client.buckets

    def test_lists_under_the_media_prefix_only(self, s3_client):
        s3_client.objects.update({"one/a.csv": b"", "one/two/b.csv": b"", "oneother/c.csv": b""})
        media = MinIOMedia("bucket", access_key="a", secret_key="s", prefix="one")
        assert minio_list_objects(media) == ["one/a.csv", "one/two/b.csv"]

    def test_lists_under_an_explicit_prefix(self, media, s3_client):
        s3_client.objects.update({"one/a.csv": b"", "two/b.csv": b""})
        assert minio_list_objects(media, prefix="two") == ["two/b.csv"]

    def test_empty_listing(self, media):
        assert minio_list_objects(media) == []

    def test_listing_follows_the_pages(self, media, s3_client):
        s3_client.page_size = 2
        s3_client.objects.update({f"{number}.csv": b"" for number in range(5)})
        assert len(minio_list_objects(media)) == 5


class TestLogSink:
    @staticmethod
    def log_keys(s3_client) -> list[str]:
        return [key for key in s3_client.objects if key.startswith("logs/")]

    def test_stop_logger_uploads_one_jsonl_object(self, media, s3_client):
        logger = AsyncLogger(media)
        log_info(logger, "first", metadata={"run": 1})
        log_info(logger, "second")
        stop_logger(logger)
        keys = self.log_keys(s3_client)
        assert len(keys) == 1
        assert s3_client.content_types[keys[0]] == "application/x-ndjson"
        records = [json.loads(line) for line in s3_client.objects[keys[0]].decode().splitlines()]
        assert [record["message"] for record in records] == ["first", "second"]
        assert records[0]["level"] == "info"
        assert records[0]["metadata"] == str({"run": 1})
        assert records[1]["metadata"] is None

    def test_records_are_held_until_the_flush(self, media, s3_client):
        logger = AsyncLogger(media)
        log_info(logger, "held")
        logger._queue.close()
        logger._worker.join()
        assert self.log_keys(s3_client) == []
        minio_flush_log_buffer(media)
        assert len(self.log_keys(s3_client)) == 1
        assert media._log_buffer == []

    def test_flush_without_records_uploads_nothing(self, media, s3_client):
        minio_flush_log_buffer(media)
        assert s3_client.objects == {}

    def test_unbatched_records_are_uploaded_one_by_one(self, media, s3_client):
        logger = AsyncLogger(media, batch_logs=False)
        log_info(logger, "first")
        log_info(logger, "second")
        stop_logger(logger)
        assert len(self.log_keys(s3_client)) == 2

    def test_failed_flush_keeps_the_records(self, media, s3_client):
        logger = AsyncLogger(media)
        log_info(logger, "kept")
        s3_client.denied_operations.add("put_object")
        with pytest.raises(ClientError):
            stop_logger(logger)
        assert [record.message for record in media._log_buffer] == ["kept"]
        s3_client.denied_operations.clear()
        minio_flush_log_buffer(media)
        assert len(self.log_keys(s3_client)) == 1
