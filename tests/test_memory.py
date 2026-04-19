"""메모리 시스템 테스트 — SqliteMemoryStore (tier/scope) 기반."""

from __future__ import annotations

import os
import tempfile

import pytest

from minyoung_mah import SqliteMemoryStore
from coding_agent.memory.schema import (
    MemoryRecord,
    entry_to_record,
    record_to_entry,
)


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = SqliteMemoryStore(path, tiers=["user", "project", "domain"])
    yield s
    s.close()
    os.unlink(path)


class TestMemoryRecord:
    def test_create_with_defaults(self):
        rec = MemoryRecord(layer="user", category="style", key="indent", content="4 spaces")
        assert rec.layer == "user"
        assert rec.id  # UUID 자동 생성
        assert rec.created_at  # 타임스탬프 자동 생성

    def test_touch_updates_timestamp(self):
        rec = MemoryRecord(layer="user", category="style", key="indent", content="4 spaces")
        old_ts = rec.updated_at
        rec.touch()
        assert rec.updated_at >= old_ts


class TestSchemaConversion:
    def test_record_to_entry_roundtrip(self):
        rec = MemoryRecord(
            layer="project",
            category="arch",
            key="framework",
            content="FastAPI",
            source="user_input",
            project_id="proj-1",
        )
        entry = record_to_entry(rec)
        assert entry.tier == "project"
        assert entry.scope == "proj-1"
        assert entry.value == "FastAPI"
        assert entry.metadata == {"category": "arch", "source": "user_input"}

        back = entry_to_record(entry)
        assert back.layer == "project"
        assert back.category == "arch"
        assert back.source == "user_input"
        assert back.project_id == "proj-1"


class TestSqliteMemoryStore:
    async def test_write_and_read(self, store: SqliteMemoryStore):
        await store.write(
            tier="user",
            key="indent",
            value="4 spaces",
            metadata={"category": "style"},
        )
        entry = await store.read(tier="user", key="indent")
        assert entry is not None
        assert entry.key == "indent"
        assert entry.value == "4 spaces"

    async def test_write_overwrites_same_key(self, store: SqliteMemoryStore):
        await store.write(tier="user", key="indent", value="4 spaces")
        await store.write(tier="user", key="indent", value="tabs")
        entry = await store.read(tier="user", key="indent")
        assert entry is not None
        assert entry.value == "tabs"

    async def test_search_fts(self, store: SqliteMemoryStore):
        await store.write(
            tier="domain",
            key="payment_api",
            value="결제 API는 POST /pay 엔드포인트를 사용한다",
            metadata={"category": "api"},
        )
        await store.write(
            tier="domain",
            key="shipping",
            value="배송비는 3만원 이상 무료이다",
            metadata={"category": "rule"},
        )

        results = await store.search(tier="domain", query="결제")
        assert len(results) >= 1
        assert any("결제" in r.value for r in results)

    async def test_scope_separates_projects(self, store: SqliteMemoryStore):
        await store.write(tier="project", key="framework", value="FastAPI", scope="proj-1")
        await store.write(tier="project", key="db", value="SQLite", scope="proj-2")

        # Same tier, different scopes → isolated
        entry_1 = await store.read(tier="project", key="framework", scope="proj-1")
        assert entry_1 is not None and entry_1.value == "FastAPI"
        # Reading proj-2 with proj-1's key should miss
        miss = await store.read(tier="project", key="framework", scope="proj-2")
        assert miss is None

    async def test_three_tier_separation(self, store: SqliteMemoryStore):
        """3-tier (user/project/domain) 키 격리 검증."""
        await store.write(tier="user", key="output_lang", value="한국어")
        await store.write(
            tier="project", key="type_hints", value="모든 함수에 타입 힌트 필수"
        )
        await store.write(
            tier="domain", key="silver_refund", value="Silver 등급 환불 수수료 0%"
        )

        tiers = await store.list_tiers()
        assert set(tiers) >= {"user", "project", "domain"}

        # 각 tier 에서 해당 key 로만 조회 가능
        assert (await store.read(tier="user", key="output_lang")) is not None
        assert (await store.read(tier="user", key="type_hints")) is None
