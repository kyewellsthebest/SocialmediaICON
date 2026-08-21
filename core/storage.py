"""Object storage.

R2 when configured (S3 API via boto3 — zero egress fees, which is the whole
reason it is the choice for video). Otherwise a local directory, so Phase 1
runs on a laptop with no cloud account.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from core.config import settings


class Storage(Protocol):
    kind: str

    def put_file(self, local_path: Path | str, key: str) -> str: ...

    def get_file(self, key: str, local_path: Path | str) -> Path: ...

    def url_for(self, key: str, expires_s: int = 3600) -> str: ...


class LocalStorage:
    """Filesystem stand-in for R2. Keys map to paths under `root`."""

    kind = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.local_storage_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put_file(self, local_path: Path | str, key: str) -> str:
        dest = self._path(key)
        src = Path(local_path)
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        return key

    def get_file(self, key: str, local_path: Path | str) -> Path:
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(key), dest)
        return dest

    def url_for(self, key: str, expires_s: int = 3600) -> str:
        return self._path(key).as_uri()


class R2Storage:
    kind = "r2"

    def __init__(self) -> None:
        import boto3  # imported lazily so tests don't need boto3 installed

        self.bucket = settings.r2_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )

    def put_file(self, local_path: Path | str, key: str) -> str:
        self.client.upload_file(str(local_path), self.bucket, key)
        return key

    def get_file(self, key: str, local_path: Path | str) -> Path:
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(dest))
        return dest

    def url_for(self, key: str, expires_s: int = 3600) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_s
        )


def get_storage() -> Storage:
    if settings.has_r2:
        return R2Storage()
    return LocalStorage()
