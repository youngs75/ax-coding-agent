"""Skill loader — static injection of procedural knowledge into SubAgent runs.

Skills are kept separate from role system prompts so that (1) the role
prompt stays as an identity contract and (2) procedures can be edited
by changing a file instead of redeploying prompt strings. Phase 1 does
static injection: when a role is built, the loader reads the relevant
SKILL.md bodies and stores them on the role; ``build_user_message``
appends them to the per-invocation user message.

Later phases may add progressive disclosure (description-only in prompt,
body loaded on demand via a ``load_skill`` tool) — the loader already
distinguishes ``summary`` and ``body`` to keep that path open.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_SKILLS_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Skill:
    name: str
    summary: str
    applies_to: tuple[str, ...]
    body: str
    path: Path


def _parse_frontmatter(raw: str) -> dict[str, object]:
    """Minimal key:value parser for the subset of YAML we use in SKILL.md.

    Supported:
      key: scalar
      key: [a, b, c]   (flow-style list on one line)
    We intentionally avoid a full YAML dep since the frontmatter schema is
    tiny and fixed.
    """
    meta: dict[str, object] = {}
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            items = [p.strip() for p in inner.split(",") if p.strip()]
            meta[key] = items
        else:
            meta[key] = value
    return meta


def _parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"Skill {path} missing YAML frontmatter")
    _, frontmatter_raw, body = text.split("---", 2)
    meta = _parse_frontmatter(frontmatter_raw)
    name = str(meta.get("name") or path.stem)
    summary = str(meta.get("summary") or "")
    applies_raw = meta.get("applies_to") or []
    if isinstance(applies_raw, str):
        applies_raw = [applies_raw]
    applies_to = tuple(str(r) for r in applies_raw)
    return Skill(
        name=name,
        summary=summary,
        applies_to=applies_to,
        body=body.lstrip("\n"),
        path=path,
    )


class SkillStore:
    """Loads every SKILL (``*.md``) under ``coding_agent/skills/``.

    Files are read eagerly at construction; bodies are kept in memory so
    subsequent lookups avoid disk I/O inside the hot SubAgent invocation
    path. Total size is tiny (a few KB), so eager load is fine.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _SKILLS_ROOT
        self._by_name: dict[str, Skill] = {}
        self._by_role: dict[str, list[Skill]] = {}
        self._load()

    def _load(self) -> None:
        for md_path in sorted(self._root.rglob("*.md")):
            skill = _parse_skill(md_path)
            self._by_name[skill.name] = skill
            for role in skill.applies_to:
                self._by_role.setdefault(role, []).append(skill)

    def get(self, name: str) -> Skill | None:
        return self._by_name.get(name)

    def for_role(self, role_name: str) -> list[Skill]:
        return list(self._by_role.get(role_name, []))

    def all(self) -> list[Skill]:
        return list(self._by_name.values())


# Module-level singleton — cheap to build (eager, ~few KB).
SKILL_STORE = SkillStore()


def render_skill_block(skills: list[Skill]) -> str:
    """Format skill bodies for inclusion in a SubAgent user message."""
    if not skills:
        return ""
    parts = ["## Skills (procedural playbooks)"]
    for s in skills:
        parts.append(f"### Skill: {s.name}")
        if s.summary:
            parts.append(f"_Summary: {s.summary}_")
        parts.append(s.body.rstrip())
    return "\n\n".join(parts)


__all__ = ["Skill", "SkillStore", "SKILL_STORE", "render_skill_block"]
