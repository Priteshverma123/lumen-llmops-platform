"""MinIO/S3 wrapper (async via threads). Raw OTLP batches land here BEFORE any processing."""

from __future__ import annotations

import asyncio
import io

from minio import Minio
from minio.error import S3Error

from lumen.common.settings import Settings


class ObjectStore:
    def __init__(self, s: Settings) -> None:
        self.bucket = s.s3_bucket
        self.client = Minio(s.s3_endpoint, access_key=s.s3_access_key, secret_key=s.s3_secret_key.get_secret_value(),
                            secure=s.s3_secure)

    async def ensure_bucket(self) -> None:
        def _f() -> None:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
        await asyncio.to_thread(_f)

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        await asyncio.to_thread(self.client.put_object, self.bucket, key, io.BytesIO(data), len(data),
                                content_type=content_type)

    async def get(self, key: str) -> bytes | None:
        def _f() -> bytes | None:
            try:
                r = self.client.get_object(self.bucket, key)
            except S3Error as e:
                if e.code in ("NoSuchKey", "NoSuchObject"):
                    return None
                raise
            try:
                return r.read()
            finally:
                r.close()
                r.release_conn()
        return await asyncio.to_thread(_f)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.remove_object, self.bucket, key)

    async def list_prefix(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(
            lambda: [o.object_name for o in self.client.list_objects(self.bucket, prefix=prefix, recursive=True)])
