"""MemoryExtractor — LLM-driven extraction of durable facts from conversation."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import structlog
from langchain_core.messages import SystemMessage

from coding_agent.memory.schema import MemoryRecord

# R-003 (2026-04-27) — prompt 가 "no markdown fences, no explanation" 를
# 강제하던 형식 강제. LLM 이 머리말/설명을 섞으면 직접 ``json.loads`` 가
# 실패해 빈 list silently 반환하던 회피. raw 안 어딘가의 첫 JSON array 를
# 추출해 흡수 (앞뒤 자연어가 있어도 작동).
_JSON_ARRAY_RE = re.compile(r"\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]", re.DOTALL)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage

log = structlog.get_logger(__name__)

_EXTRACTION_PROMPT = """\
You are a memory extraction module for an AI coding agent.

Analyze the conversation below and extract **durable facts** worth remembering
for future interactions.  Classify each fact into exactly one layer:

  - **user**    — personal preferences, coding style, habits, tool choices
  - **project** — architecture decisions, project rules, directory structure, conventions
  - **domain**  — business rules, domain terminology, invariants, acronyms

Return a JSON array (no markdown fences, no explanation) of objects:
[
  {{"layer": "<layer>", "category": "<short category>", "key": "<unique snake_case key>", "content": "<concise fact>"}}
]

Rules:
1. Only extract facts that are **durable** — likely to remain true across sessions.
2. Do NOT store passwords, tokens, secrets, or PII.
3. Do NOT duplicate any of these existing keys: {existing_keys}
4. If there is nothing worth remembering, return an empty array: []
5. Keep each *content* value concise (one or two sentences).
6. The *key* must be globally unique and descriptive (e.g. "preferred_test_framework").
"""


class MemoryExtractor:
    """Extracts memorable facts from conversation using an LLM.

    Parameters
    ----------
    llm : BaseChatModel
        A LangChain chat model, typically the FAST-tier model for low latency.
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    def extract(
        self,
        messages: list[BaseMessage],
        existing_keys: set[str] | None = None,
    ) -> list[MemoryRecord]:
        """Analyse the last few messages and return newly extracted MemoryRecords.

        Parameters
        ----------
        messages:
            Full message list from the agent state.  Only the tail (last 2-4
            messages) is sent to the LLM to keep costs low.
        existing_keys:
            Keys that already exist in the store — the LLM is instructed not
            to duplicate them.

        Returns
        -------
        list[MemoryRecord]
            Zero or more new records ready for upserting.
        """
        if not messages:
            log.debug("memory_extractor.skip_empty_messages")
            return []

        existing_keys = existing_keys or set()

        # Take the last 2-4 messages to keep the context window small.
        window = messages[-4:]

        system = SystemMessage(
            content=_EXTRACTION_PROMPT.format(existing_keys=existing_keys)
        )
        extraction_messages: list[BaseMessage] = [system, *window]

        try:
            response = self._llm.invoke(extraction_messages)
            raw_text: str = (
                response.content if isinstance(response.content, str) else str(response.content)
            )
            records = self._parse_response(raw_text)
            log.info("memory_extractor.extracted", count=len(records))
            return records
        except Exception:
            log.exception("memory_extractor.extraction_failed")
            return []

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _parse_response(raw: str) -> list[MemoryRecord]:
        """Parse the LLM JSON response into MemoryRecord instances."""
        # Strip markdown code fences if the LLM wraps the response.
        text = raw.strip()
        if text.startswith("```"):
            # Remove opening fence (```json or ```)
            first_newline = text.index("\n")
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()

        if not text:
            return []

        parsed: Any = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Fallback — raw 안에서 첫 JSON array 패턴 추출 시도.
            for m in _JSON_ARRAY_RE.finditer(text):
                try:
                    candidate = json.loads(m.group(0))
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, list):
                    parsed = candidate
                    break
            if parsed is None:
                log.warning("memory_extractor.json_parse_error", raw_text=text[:200])
                return []

        if not isinstance(parsed, list):
            log.warning("memory_extractor.unexpected_type", type=type(parsed).__name__)
            return []

        records: list[MemoryRecord] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            layer = item.get("layer", "")
            if layer not in ("user", "project", "domain"):
                log.warning("memory_extractor.invalid_layer", layer=layer)
                continue
            key = item.get("key", "")
            content = item.get("content", "")
            if not key or not content:
                continue
            records.append(
                MemoryRecord(
                    layer=layer,
                    category=item.get("category", "general"),
                    key=key,
                    content=content,
                    source="auto_extract",
                )
            )
        return records
