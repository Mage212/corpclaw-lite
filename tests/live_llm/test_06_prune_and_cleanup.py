from __future__ import annotations

import time
from pathlib import Path

import pytest

from corpclaw_lite.llm.cache import LLMCacheManager, LLMCacheMetadata, PersistentCacheConfig

pytestmark = [pytest.mark.live_llm]


@pytest.mark.asyncio
async def test_prune_deletes_only_old_inactive_managed_files(tmp_path: Path) -> None:
    from corpclaw_lite.llm.cache import LLMCacheScope

    root = tmp_path / "slot-cache"
    root.mkdir()
    old_scope = LLMCacheScope("u", "default", "main", "llamacpp", "model", None, "s", "t")
    active_scope = LLMCacheScope("u", "default", "data-agent", "llamacpp", "model", None, "s", "t")
    old_file = root / old_scope.filename
    active_file = root / active_scope.filename
    unmanaged = root / "foreign_cache.bin"
    old_file.write_bytes(b"old")
    active_file.write_bytes(b"active")
    unmanaged.write_bytes(b"foreign")

    manager = LLMCacheManager(
        PersistentCacheConfig(
            enabled=True,
            root_dir=root,
            index_path=tmp_path / "index.sqlite",
            max_age_days=1,
            max_total_bytes=10_000,
        ),
        provider_base_urls={"llamacpp": "http://example.invalid"},
    )
    now = time.time()
    await manager._store.upsert(
        LLMCacheMetadata(
            old_scope,
            old_scope.filename,
            10,
            3,
            10,
            0,
            10,
            now - 200000,
            now - 200000,
            now - 200000,
            1,
            0,
        )
    )
    await manager._store.upsert(
        LLMCacheMetadata(
            active_scope,
            active_scope.filename,
            10,
            6,
            10,
            0,
            10,
            now,
            now,
            now,
            1,
            0,
        )
    )
    manager._slot_scopes[0] = active_scope

    await manager.prune()

    assert not old_file.exists()
    assert active_file.exists()
    assert unmanaged.exists()
