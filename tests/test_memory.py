"""메모리 시스템 테스트 — store CRUD + FTS5 검색."""

from __future__ import annotations

import os
import tempfile

import pytest

from coding_agent.memory.schema import MemoryRecord
from coding_agent.memory.store import MemoryStore


@pytest.fixture
def store():
    """임시 SQLite DB로 MemoryStore 생성."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = MemoryStore(path)
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


class TestMemoryStore:
    def test_upsert_and_get(self, store: MemoryStore):
        rec = MemoryRecord(layer="user", category="style", key="indent", content="4 spaces")
        store.upsert(rec)

        results = store.get_by_layer("user")
        assert len(results) == 1
        assert results[0].key == "indent"
        assert results[0].content == "4 spaces"

    def test_upsert_overwrites_same_key(self, store: MemoryStore):
        rec1 = MemoryRecord(layer="user", category="style", key="indent", content="4 spaces")
        store.upsert(rec1)

        rec2 = MemoryRecord(layer="user", category="style", key="indent", content="tabs")
        store.upsert(rec2)

        results = store.get_by_layer("user")
        assert len(results) == 1
        assert results[0].content == "tabs"

    def test_delete(self, store: MemoryStore):
        rec = MemoryRecord(layer="domain", category="rule", key="refund", content="no refund")
        store.upsert(rec)

        assert store.delete(rec.id)
        assert len(store.get_by_layer("domain")) == 0

    def test_list_all(self, store: MemoryStore):
        store.upsert(MemoryRecord(layer="user", category="pref", key="lang", content="python"))
        store.upsert(MemoryRecord(layer="project", category="arch", key="db", content="postgres"))
        store.upsert(MemoryRecord(layer="domain", category="rule", key="tax", content="10%"))

        all_records = store.list_all()
        assert len(all_records) == 3

    def test_search_fts(self, store: MemoryStore):
        store.upsert(
            MemoryRecord(
                layer="domain", category="api", key="payment_api",
                content="결제 API는 POST /pay 엔드포인트를 사용한다",
            )
        )
        store.upsert(
            MemoryRecord(
                layer="domain", category="rule", key="shipping",
                content="배송비는 3만원 이상 무료이다",
            )
        )

        results = store.search("결제", layer="domain")
        assert len(results) >= 1
        assert any("결제" in r.content for r in results)

    def test_get_by_layer_with_project_id(self, store: MemoryStore):
        store.upsert(
            MemoryRecord(
                layer="project", category="arch", key="framework",
                content="FastAPI", project_id="proj-1",
            )
        )
        store.upsert(
            MemoryRecord(
                layer="project", category="arch", key="db",
                content="SQLite", project_id="proj-2",
            )
        )

        results = store.get_by_layer("project", project_id="proj-1")
        assert len(results) == 1
        assert results[0].content == "FastAPI"

    def test_get_existing_keys(self, store: MemoryStore):
        store.upsert(MemoryRecord(layer="user", category="pref", key="k1", content="v1"))
        store.upsert(MemoryRecord(layer="domain", category="rule", key="k2", content="v2"))

        keys = store.get_existing_keys()
        assert "k1" in keys
        assert "k2" in keys

    def test_three_layer_separation(self, store: MemoryStore):
        """3계층 메모리가 올바르게 분리되어 저장/조회되는지 검증."""
        store.upsert(MemoryRecord(layer="user", category="pref", key="output_lang", content="한국어"))
        store.upsert(MemoryRecord(layer="project", category="rule", key="type_hints", content="모든 함수에 타입 힌트 필수"))
        store.upsert(MemoryRecord(layer="domain", category="biz", key="silver_refund", content="Silver 등급 환불 수수료 0%"))

        assert len(store.get_by_layer("user")) == 1
        assert len(store.get_by_layer("project")) == 1
        assert len(store.get_by_layer("domain")) == 1

        # 다른 계층과 혼합되지 않음
        user_records = store.get_by_layer("user")
        assert user_records[0].key == "output_lang"
        assert user_records[0].layer == "user"
