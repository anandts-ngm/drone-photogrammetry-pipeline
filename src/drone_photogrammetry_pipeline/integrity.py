"""Checksums and canonical hashing.

Every hash in this project is SHA-256 and is produced here, so that two values computed in
different modules are always comparable. A hash produced by a private reimplementation
elsewhere would look identical in the manifest while meaning something different.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_CHUNK_BYTES = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_sha256(document: Any) -> str:
    """Hash a document by its content rather than by its text.

    Keys are sorted and whitespace is removed first, so that reformatting a profile,
    reordering its keys or editing its comments does not change the hash. Only a change in
    meaning does. This is what makes `profile_hash` in a run manifest a usable claim about
    which processing was applied.
    """
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return sha256_bytes(canonical.encode("utf-8"))


def hash_outputs(paths: dict[str, Path]) -> dict[str, str]:
    return {name: sha256_file(path) for name, path in paths.items()}
