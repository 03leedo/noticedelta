import io

import pytest

from noticedelta.blobstore import (
    BlobCorrupted,
    BlobNotFound,
    LocalBlobStore,
    S3BlobStore,
    blob_key,
)


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """boto3 S3 클라이언트에서 쓰는 메서드만 흉내 낸다."""

    def __init__(self):
        self.objects = {}
        self.puts = 0

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("404")
        return {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts += 1
        self.objects[(Bucket, Key)] = (bytes(Body), kw.get("ContentType"))

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalBlobStore(tmp_path)
    return S3BlobStore(FakeS3(), "bucket")


def test_put_is_content_addressed_and_idempotent(store):
    a = store.put(b"hello", "text/plain")
    b = store.put(b"hello", "text/plain")
    assert a.key == b.key == blob_key(b"hello")
    assert (a.created, b.created) == (True, False)
    assert store.get(a.key) == b"hello"
    assert store.exists(a.key)


def test_missing_key(store):
    with pytest.raises(BlobNotFound):
        store.get("0" * 64)
    assert not store.exists("0" * 64)


def test_local_detects_tampering(tmp_path):
    store = LocalBlobStore(tmp_path)
    ref = store.put(b"original")
    path = tmp_path / ref.key[:2] / ref.key[2:4] / ref.key
    path.write_bytes(b"tampered")
    with pytest.raises(BlobCorrupted):
        store.get(ref.key)


def test_local_leaves_no_temp_files(tmp_path):
    store = LocalBlobStore(tmp_path)
    store.put(b"x")
    assert not [p for p in tmp_path.rglob(".tmp-*")]


def test_s3_skips_upload_when_present_and_sets_type():
    client = FakeS3()
    store = S3BlobStore(client, "bucket", prefix="blobs/")
    ref = store.put(b"%PDF-1.7", "application/pdf")
    store.put(b"%PDF-1.7", "application/pdf")
    assert client.puts == 1
    assert client.objects[("bucket", f"blobs/{ref.key}")][1] == "application/pdf"


def test_s3_other_errors_propagate():
    class Broken(FakeS3):
        def head_object(self, Bucket, Key):
            raise ClientError("AccessDenied")

    with pytest.raises(ClientError):
        S3BlobStore(Broken(), "bucket").put(b"x")
