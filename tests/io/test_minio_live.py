"""MinIOMedia against a real server.

Runs only when ``QFPYTOOLBOX_MINIO_ENDPOINT`` is set, with credentials in ``MINIO_ACCESS_KEY`` and
``MINIO_SECRET_KEY``.  Only the missing variable skips these tests: an unreachable server fails them.
"""

from __future__ import annotations

import json
import os
import uuid

import pandas as pd
import pytest

from qfpytoolbox.io.dataframes import read_dataframe, write_dataframe
from qfpytoolbox.io.logger import AsyncLogger, log_info, stop_logger
from qfpytoolbox.io.media import MinIOMedia, minio_create_bucket, minio_list_objects

ENDPOINT = os.environ.get("QFPYTOOLBOX_MINIO_ENDPOINT", "")
FRAME = pd.DataFrame({"id": [1, 2, 3], "value": [10.5, 20.1, 30.0]})

pytestmark = pytest.mark.skipif(not ENDPOINT, reason="QFPYTOOLBOX_MINIO_ENDPOINT is not set")


@pytest.fixture
def media():
    """Yield a media on a new bucket, and remove the bucket afterwards."""
    store = MinIOMedia(f"qfpy-test-{uuid.uuid4().hex[:12]}", endpoint=ENDPOINT, prefix="run/one")
    minio_create_bucket(store)
    yield store
    for key in minio_list_objects(store, prefix=""):
        store._client.delete_object(Bucket=store.bucket, Key=key)
    store._client.delete_bucket(Bucket=store.bucket)


@pytest.mark.parametrize("filename", ["prices.arrow", "prices.csv", "prices.csv.gz"])
def test_round_trip(media, filename):
    write_dataframe(media, FRAME, filename=filename)
    assert minio_list_objects(media) == [f"run/one/{filename}"]
    pd.testing.assert_frame_equal(read_dataframe(media, filename=filename), FRAME)


def test_create_bucket_twice_is_harmless(media):
    minio_create_bucket(media)


def test_overwrite_is_refused_then_allowed(media):
    write_dataframe(media, FRAME, filename="prices.arrow")
    with pytest.raises(FileExistsError):
        write_dataframe(media, FRAME.head(1), filename="prices.arrow")
    write_dataframe(media, FRAME.head(1), filename="prices.arrow", overwrite=True)
    assert len(read_dataframe(media, filename="prices.arrow")) == 1


def test_missing_object_raises_file_not_found(media):
    with pytest.raises(FileNotFoundError):
        read_dataframe(media, filename="absent.arrow")


def test_listing_keeps_to_the_prefix(media):
    other = MinIOMedia(media.bucket, endpoint=ENDPOINT, prefix="run/oneother")
    write_dataframe(media, FRAME, filename="mine.csv")
    write_dataframe(other, FRAME, filename="theirs.csv")
    assert minio_list_objects(media) == ["run/one/mine.csv"]


def test_wrong_secret_is_an_error_not_an_absent_object(media):
    client_error_type = pytest.importorskip("botocore.exceptions").ClientError
    wrong = MinIOMedia(media.bucket, endpoint=ENDPOINT, secret_key="not-the-secret", prefix=media.prefix)
    with pytest.raises(client_error_type):
        write_dataframe(wrong, FRAME, filename="denied.arrow")
    assert minio_list_objects(media) == []


def test_stop_logger_uploads_the_records(media):
    logger = AsyncLogger(media)
    log_info(logger, "stored")
    stop_logger(logger)
    keys = minio_list_objects(media)
    assert len(keys) == 1
    assert keys[0].startswith("run/one/logs/")
    body = media._get_bytes(keys[0]).decode()
    assert json.loads(body)["message"] == "stored"
