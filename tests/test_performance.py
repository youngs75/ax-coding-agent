"""Tests for performance optimizations: tool cache, memory cache, parallel spawn, early termination."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Tool cache tests ────────────────────────────────────────────────────────

from coding_agent.tools.file_ops import (
    _ToolCache,
    get_tool_cache,
    read_file,
    write_file,
    edit_file,
    glob_files,
    grep,
)


class TestToolCache:
    """Tests for the _ToolCache class."""

    def test_put_and_get(self):
        cache = _ToolCache(max_size=10)
        cache.put("key1", "value1")
        assert cache.get("key1") == "value1"
        assert cache.hits == 1

    def test_miss(self):
        cache = _ToolCache(max_size=10)
        assert cache.get("nonexistent") is None
        assert cache.misses == 1

    def test_eviction(self):
        cache = _ToolCache(max_size=4)
        for i in range(5):
            cache.put(f"k{i}", f"v{i}")
        # First entry should have been evicted
        assert cache.get("k0") is None

    def test_invalidate_path(self):
        cache = _ToolCache(max_size=10)
        cache.put("read:/tmp/test.py:0:200:123", "content")
        cache.put("grep:/tmp:pattern:", "matches")
        cache.invalidate_path("/tmp/test.py")
        assert cache.get("read:/tmp/test.py:0:200:123") is None
        # grep cache should remain (different path)
        assert cache.get("grep:/tmp:pattern:") is not None

    def test_clear(self):
        cache = _ToolCache(max_size=10)
        cache.put("k1", "v1")
        cache.put("k2", "v2")
        cache.clear()
        assert cache.get("k1") is None
        assert cache.get("k2") is None


class TestToolCacheIntegration:
    """Tests that read tools use cache and write tools invalidate it."""

    def setup_method(self):
        get_tool_cache().clear()

    def test_read_file_caches(self, tmp_path: Path):
        f = tmp_path / "hello.txt"
        f.write_text("line1\nline2\n")

        result1 = read_file.invoke({"path": str(f), "offset": 0, "limit": 200})
        result2 = read_file.invoke({"path": str(f), "offset": 0, "limit": 200})
        assert result1 == result2
        assert get_tool_cache().hits >= 1

    def test_write_file_invalidates(self, tmp_path: Path):
        f = tmp_path / "data.txt"
        f.write_text("old content")

        # Read to populate cache
        read_file.invoke({"path": str(f), "offset": 0, "limit": 200})
        # Write invalidates
        write_file.invoke({"path": str(f), "content": "new content"})
        # Next read should see new content
        result = read_file.invoke({"path": str(f), "offset": 0, "limit": 200})
        assert "new content" in result

    def test_edit_file_invalidates(self, tmp_path: Path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world")

        read_file.invoke({"path": str(f), "offset": 0, "limit": 200})
        edit_file.invoke({"path": str(f), "old_string": "hello", "new_string": "goodbye"})
        result = read_file.invoke({"path": str(f), "offset": 0, "limit": 200})
        assert "goodbye" in result

    def test_glob_caches(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("pass")
        (tmp_path / "b.py").write_text("pass")

        result1 = glob_files.invoke({"pattern": "*.py", "path": str(tmp_path)})
        result2 = glob_files.invoke({"pattern": "*.py", "path": str(tmp_path)})
        assert result1 == result2
        assert get_tool_cache().hits >= 1

    def test_grep_caches(self, tmp_path: Path):
        (tmp_path / "code.py").write_text("def hello():\n    pass\n")

        result1 = grep.invoke({"pattern": "def", "path": str(tmp_path), "include": "*.py"})
        result2 = grep.invoke({"pattern": "def", "path": str(tmp_path), "include": "*.py"})
        assert result1 == result2
        assert get_tool_cache().hits >= 1


# ── Memory cache tests ──────────────────────────────────────────────────────

from minyoung_mah import SqliteMemoryStore
from coding_agent.memory.middleware import MemoryMiddleware, _TOPIC_SIMILARITY_THRESHOLD
from coding_agent.memory.schema import MemoryRecord


class TestMemoryCaching:
    """Tests for MemoryMiddleware session-level caching (async)."""

    def _make_middleware(
        self, db_path: str
    ) -> tuple[MemoryMiddleware, SqliteMemoryStore]:
        store = SqliteMemoryStore(db_path, tiers=["user", "project", "domain"])
        extractor = MagicMock()
        extractor.extract.return_value = []
        mw = MemoryMiddleware(store, extractor)
        return mw, store

    @pytest.mark.asyncio
    async def test_user_cache_reuse(self, tmp_path: Path):
        db = str(tmp_path / "mem.db")
        mw, store = self._make_middleware(db)

        await store.write(
            tier="user", key="lang", value="Korean", metadata={"category": "pref"}
        )

        state = {"messages": [], "project_id": ""}
        await mw.inject(state)
        await mw.inject(state)
        # Second call should use cached user memories without hitting DB again
        assert mw._user_cache is not None
        assert len(mw._user_cache) == 1

    @pytest.mark.asyncio
    async def test_cache_invalidation_after_extract(self, tmp_path: Path):
        from langchain_core.messages import HumanMessage, AIMessage

        db = str(tmp_path / "mem.db")
        mw, _store = self._make_middleware(db)

        msgs = [HumanMessage(content="hello"), AIMessage(content="hi")]
        state = {"messages": msgs, "project_id": ""}
        await mw.inject(state)
        assert mw._user_cache is not None

        new_rec = MemoryRecord(layer="user", category="pref", key="style", content="terse")
        mw._extractor.extract.return_value = [new_rec]
        await mw.extract_and_store(state)
        assert mw._dirty is True

        await mw.inject(state)
        assert mw._dirty is False

    @pytest.mark.asyncio
    async def test_domain_cache_topic_similarity(self, tmp_path: Path):
        db = str(tmp_path / "mem.db")
        mw, store = self._make_middleware(db)

        await store.write(
            tier="domain", key="API", value="REST API pattern", metadata={"category": "term"}
        )

        from langchain_core.messages import HumanMessage

        state1 = {"messages": [HumanMessage(content="API 설계에 대해 알려줘")], "project_id": ""}
        await mw.inject(state1)
        first_query = mw._last_domain_query

        state2 = {"messages": [HumanMessage(content="API 설계에 대해서 알려줘")], "project_id": ""}
        await mw.inject(state2)
        assert mw._last_domain_query == first_query  # no re-search

        state3 = {"messages": [HumanMessage(content="Docker 배포 방법")], "project_id": ""}
        await mw.inject(state3)
        assert mw._last_domain_query != first_query  # re-searched


# Parallel spawn / parallel_tasks 테스트는 Phase 6 리팩터와 함께 삭제 —
# 로직은 이제 minyoung-mah Orchestrator 위의 task_tool.build_parallel_tasks_tool
# 이 담당한다. 새 contract 에 맞는 테스트는 Phase 8/E2E 레이어에서 재작성.


# ── Early termination tests ─────────────────────────────────────────────────


class TestEarlyTermination:
    """Tests for repeated tool call detection in SubAgent graph."""

    def test_repeated_call_detection(self):
        """Simulate the _recent_calls tracking logic."""
        _recent_calls: list[tuple[str, str]] = []
        _MAX_REPEAT = 3

        def check_repeat(tool_name: str, args: str) -> bool:
            call_sig = (tool_name, args)
            _recent_calls.append(call_sig)
            while len(_recent_calls) > _MAX_REPEAT * 2:
                _recent_calls.pop(0)
            if len(_recent_calls) >= _MAX_REPEAT:
                tail = _recent_calls[-_MAX_REPEAT:]
                if all(c == tail[0] for c in tail):
                    return True
            return False

        # Different calls — no stop
        assert not check_repeat("read_file", '{"path": "a.py"}')
        assert not check_repeat("write_file", '{"path": "b.py"}')
        assert not check_repeat("read_file", '{"path": "c.py"}')

        # Reset
        _recent_calls.clear()

        # Same call 3 times — should stop
        assert not check_repeat("read_file", '{"path": "x.py"}')
        assert not check_repeat("read_file", '{"path": "x.py"}')
        assert check_repeat("read_file", '{"path": "x.py"}')
