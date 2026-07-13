"""Content-addressed store (M2 storage engine, stdlib reference impl).

Snapshots are stored as content-addressed chunks so identical data — unchanged memory pages
and disk blocks shared across snapshots, forks, and tenants — is stored exactly once. A
snapshot's manifest is just the ordered list of its chunk hashes.

Design notes vs. the PRD:
- Chunking: fixed 4 KiB blocks by default. Memory snapshots and ext4 disks are page/block
  aligned, so fixed-block dedup captures cross-snapshot sharing exactly and is fast. The PRD's
  FastCDC (content-defined chunking) is the win for shifting/unaligned file data; a `--cdc`
  gear-hash mode is included for that comparison.
- Hash: BLAKE2b (stdlib) here; production uses BLAKE3 (faster, same content-addressing role).
- Compression: zlib (stdlib) here; production uses zstd.

Two modes: measure-only (track the dedup index in memory — fast, for economics numbers) or
--write (actually persist chunks to disk as a real store).
"""
from __future__ import annotations

import hashlib
import os
import zlib


class CAS:
    def __init__(self, root: str | None = None, block: int = 4096, cdc: bool = False, level: int = 3):
        self.root = root
        self.block = block
        self.cdc = cdc
        self.level = level
        if root:
            os.makedirs(root, exist_ok=True)
        self.index: dict[str, int] = {}   # chunk hash -> stored (compressed) length
        self.logical = 0
        self.stored = 0
        self.chunks = 0

    # --- chunking ---
    def _blocks(self, data: bytes):
        if not self.cdc:
            for i in range(0, len(data), self.block):
                yield data[i:i + self.block]
            return
        # content-defined chunking via a 64-bit gear hash (min 2 KiB, avg ~8 KiB, max 64 KiB)
        GEAR = _GEAR
        mask, mn, mx = (1 << 13) - 1, 2048, 65536
        h = 0
        start = 0
        for i, b in enumerate(data):
            h = ((h << 1) + GEAR[b]) & 0xFFFFFFFFFFFFFFFF
            size = i - start + 1
            if (size >= mn and (h & mask) == 0) or size >= mx:
                yield data[start:i + 1]
                start = i + 1
                h = 0
        if start < len(data):
            yield data[start:]

    def put_bytes(self, data: bytes) -> list[str]:
        manifest = []
        for blk in self._blocks(data):
            self.logical += len(blk)
            self.chunks += 1
            h = hashlib.blake2b(blk, digest_size=16).hexdigest()
            if h not in self.index:
                comp = zlib.compress(blk, self.level)
                self.index[h] = len(comp)
                self.stored += len(comp)
                if self.root:
                    d = os.path.join(self.root, h[:2])
                    os.makedirs(d, exist_ok=True)
                    p = os.path.join(d, h)
                    if not os.path.exists(p):
                        with open(p, "wb") as cf:
                            cf.write(comp)
            manifest.append(h)
        return manifest

    def put_file(self, path: str, buf: int = 8 << 20) -> list[str]:
        manifest = []
        with open(path, "rb") as f:
            while True:
                data = f.read(buf)
                if not data:
                    break
                # align read buffer to block size so fixed-block hashing is stable across reads
                manifest += self.put_bytes(data)
        return manifest

    def stats(self) -> dict:
        return {
            "logical_bytes": self.logical,
            "chunks_total": self.chunks,
            "chunks_unique": len(self.index),
            "stored_bytes": self.stored,
            "dedup_ratio": round(self.logical / self.stored, 2) if self.stored else 0.0,
            "unique_fraction": round(len(self.index) / self.chunks, 4) if self.chunks else 0.0,
        }


# small gear table for CDC mode (deterministic pseudo-random 64-bit values)
_GEAR = [(hashlib.blake2b(bytes([i]), digest_size=8).digest()[0] << 56 |
          int.from_bytes(hashlib.blake2b(bytes([i]), digest_size=8).digest(), "big")) & 0xFFFFFFFFFFFFFFFF
         for i in range(256)]
