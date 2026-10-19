"""원문 저장소 (설계 7.2). 내용의 sha256을 키로 쓰므로 같은 파일은 한 번만 저장된다.

- LocalBlobStore: 개발·테스트용 파일시스템 저장소.
- S3BlobStore: S3 호환 저장소(로컬 SeaweedFS, 클라우드 Cloudflare R2). boto3 클라이언트를 주입받는다.

읽을 때 해시를 다시 계산해 저장소가 내용을 바꿔 돌려주면 BlobCorrupted를 낸다.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class BlobNotFound(KeyError):
    pass


class BlobCorrupted(Exception):
    """저장된 내용의 해시가 키와 다르다."""


@dataclass(frozen=True)
class BlobRef:
    key: str  # sha256 hex
    size: int
    created: bool  # 이번 호출에서 새로 저장했는가


def blob_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check(key: str, data: bytes) -> bytes:
    if blob_key(data) != key:
        raise BlobCorrupted(key)
    return data


class BlobStore(Protocol):
    def put(self, data: bytes, content_type: str | None = None) -> BlobRef: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalBlobStore:
    """root/ab/cd/<sha256> 에 저장한다. 임시 파일에 쓰고 이름을 바꿔 반쯤 쓴 파일이 남지 않게 한다."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / key[2:4] / key

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def put(self, data: bytes, content_type: str | None = None) -> BlobRef:
        key = blob_key(data)
        path = self._path(key)
        if path.is_file():
            return BlobRef(key, len(data), created=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return BlobRef(key, len(data), created=True)

    def get(self, key: str) -> bytes:
        try:
            return _check(key, self._path(key).read_bytes())
        except FileNotFoundError:
            raise BlobNotFound(key) from None


class S3BlobStore:
    """S3 호환 저장소. `client`는 boto3 S3 클라이언트(또는 같은 메서드를 가진 객체)."""

    def __init__(self, client: Any, bucket: str, prefix: str = "blobs/"):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    @classmethod
    def from_env(cls) -> S3BlobStore:
        import boto3  # 선택 의존성: uv sync --extra s3

        client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY") or None,
            region_name=os.environ.get("S3_REGION") or "auto",
        )
        return cls(client, os.environ.get("S3_BUCKET", "noticedelta"))

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    @staticmethod
    def _is_missing(e: Exception) -> bool:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        return code in {"404", "NoSuchKey", "NotFound"}

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as e:
            if self._is_missing(e):
                return False
            raise

    def put(self, data: bytes, content_type: str | None = None) -> BlobRef:
        key = blob_key(data)
        if self.exists(key):
            return BlobRef(key, len(data), created=False)
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, **extra)
        return BlobRef(key, len(data), created=True)

    def get(self, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if self._is_missing(e):
                raise BlobNotFound(key) from None
            raise
        return _check(key, obj["Body"].read())


def from_env() -> BlobStore:
    """NOTICEDELTA_BLOB_BACKEND=local(기본) | s3."""
    if os.environ.get("NOTICEDELTA_BLOB_BACKEND", "local") == "s3":
        return S3BlobStore.from_env()
    return LocalBlobStore(os.environ.get("NOTICEDELTA_BLOB_DIR", "data/blobs"))
