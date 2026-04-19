"""ax-local skill store — thin wrapper over ``minyoung_mah.skills``.

The loader classes (:class:`Skill`, :class:`SkillStore`, parser, renderer) live
in ``minyoung_mah`` so every consumer that wants procedural skill injection
shares one implementation. ax keeps only the **instance** that points at this
directory — the root path is consumer-specific and does not belong in the
library.

Exports (``Skill`` / ``render_skill_block``) are re-exported so existing
imports continue to work without churn.
"""

from __future__ import annotations

from pathlib import Path

from minyoung_mah import Skill, SkillStore, render_skill_block

_SKILLS_ROOT = Path(__file__).resolve().parent

# Module-level singleton — cheap to build (eager, ~few KB). Consumers that
# need a different root should construct their own ``SkillStore``.
SKILL_STORE = SkillStore(_SKILLS_ROOT)


__all__ = ["SKILL_STORE", "Skill", "SkillStore", "render_skill_block"]
