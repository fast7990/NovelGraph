"""存储抽象：DB 只存 key，不存路径。

今天 `local` 写磁盘，明天 `s3` 写对象存储 —— 换实现不改调用方，
这是需求里"预留对象存储扩展接口"的落地方式。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

from app.config import settings


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes) -> str: ...

    async def get(self, key: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


class LocalStorage:
    name = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.storage_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"非法存储 key：{key}")  # 防目录穿越
        return p

    async def put(self, key: str, data: bytes) -> str:
        def _do() -> str:
            p = self._path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)  # 原子落盘，避免上传中断留下半截文件
            return key

        return await asyncio.to_thread(_do)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(lambda: self._path(key).read_bytes())

    async def delete_prefix(self, prefix: str) -> None:
        def _do() -> None:
            p = self._path(prefix)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()

        await asyncio.to_thread(_do)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(lambda: self._path(key).exists())


class S3Storage:
    """P1 接入（boto3 / 阿里云 OSS）。接口已对齐，不改调用方。

    实现要点：分片上传 + presigned URL 直传（大文件不经过 API 进程），
    key 规则与 Local 保持一致：novels/{novel_id}/source.txt
    """

    name = "s3"

    def __init__(self) -> None:
        raise NotImplementedError("S3 存储在 P1 阶段接入，当前请使用 STORAGE_BACKEND=local")


_storage: ObjectStorage | None = None


def get_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        if settings.storage_backend == "local":
            _storage = LocalStorage()
        else:
            _storage = S3Storage()
    return _storage
