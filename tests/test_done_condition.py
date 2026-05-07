"""DONE_CONDITION.md forbidden pattern 추출 — 결정론 영역 단위 테스트.

이 함수는 *결정론* 영역만 다룬다: bullet 의 첫 토큰 (glob) 추출. bullet
이 *의미적으로* 올바른 forbidden pattern 인지의 판단은 planner skill
(``done-condition.md``) 가 LLM 에게 위임 + critic LLM 이 의심 시 catch.
정규식 기반 자연어 거부는 R-003 위반 (format coercion 으로 robustness
누락 메우기 금지) — 도입했다가 폐기.
"""

from __future__ import annotations

from coding_agent.sufficiency.signals import _extract_forbidden_patterns


_HEADER = "## Forbidden Patterns\n"


def _section(*lines: str) -> str:
    return _HEADER + "\n".join(lines) + "\n"


def test_pure_glob_passes():
    text = _section("- *.vue", "- *.svelte", "- **/Cargo.toml")
    assert _extract_forbidden_patterns(text) == ["*.vue", "*.svelte", "**/Cargo.toml"]


def test_paren_note_kept_as_glob_only():
    """bullet 끝 괄호 메모는 자유 텍스트 — 첫 토큰 (glob) 만 추출."""
    text = _section(
        "- *.vue (React was chosen)",
        "- **/requirements.txt (Node.js was chosen, not Python)",
    )
    assert _extract_forbidden_patterns(text) == ["*.vue", "**/requirements.txt"]


def test_asterisk_bullet_also_supported():
    text = _HEADER + "* *.vue\n* *.svelte\n"
    assert _extract_forbidden_patterns(text) == ["*.vue", "*.svelte"]


def test_no_forbidden_header_returns_empty():
    text = "## Framework Choice\n- React 18\n"
    assert _extract_forbidden_patterns(text) == []


def test_section_bounded_by_next_h2():
    text = (
        "## Forbidden Patterns\n"
        "- *.vue\n"
        "\n"
        "## Required Tests\n"
        "- *.notapattern\n"
    )
    # 다음 ## 헤더 이후 bullet 은 무시
    assert _extract_forbidden_patterns(text) == ["*.vue"]
