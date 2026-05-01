"""DONE_CONDITION.md forbidden pattern 추출 — v22.4 자연어 거부 안전망.

planner skill (``done-condition.md``) 의 가이드가 ``Forbidden Patterns``
bullet 에 자연어 조건을 섞지 못하게 명시하지만, planner LLM 이 그래도
``- **/requirements.txt must exist`` 같은 *반대 의미* bullet 을 작성한
v25 회귀를 차단하는 deterministic 안전망. ``_extract_forbidden_patterns``
가 그 안전망의 단위.
"""

from __future__ import annotations

from coding_agent.sufficiency.signals import _extract_forbidden_patterns


_HEADER = "## Forbidden Patterns\n"


def _section(*lines: str) -> str:
    return _HEADER + "\n".join(lines) + "\n"


# ── 정상 패턴 통과 ──────────────────────────────────────────────────────────


def test_pure_glob_passes():
    text = _section("- *.vue", "- *.svelte", "- **/Cargo.toml")
    assert _extract_forbidden_patterns(text) == ["*.vue", "*.svelte", "**/Cargo.toml"]


def test_paren_note_allowed():
    """괄호 안 자연어 메모는 허용 — `(React was chosen)` 등 자연스런 영어."""
    text = _section(
        "- *.vue (React was chosen)",
        "- **/requirements.txt (Node.js was chosen, not Python)",
    )
    assert _extract_forbidden_patterns(text) == ["*.vue", "**/requirements.txt"]


def test_asterisk_bullet_also_supported():
    text = _HEADER + "* *.vue\n* *.svelte\n"
    assert _extract_forbidden_patterns(text) == ["*.vue", "*.svelte"]


# ── 자연어 조건 거부 (v22.4) ────────────────────────────────────────────────


def test_must_outside_paren_rejected():
    """v25 회귀의 직접 케이스 — `must exist` 가 반대 의미로 들어감."""
    text = _section(
        "- **/requirements.txt must exist (Python backend)",
        "- *.vue",
    )
    # must 가 들어간 줄은 거부, 정상 패턴만 통과
    assert _extract_forbidden_patterns(text) == ["*.vue"]


def test_should_rejected():
    text = _section("- *.py should not appear when Node was chosen", "- *.svelte")
    assert _extract_forbidden_patterns(text) == ["*.svelte"]


def test_if_rejected():
    """`if` 조건은 harness 가 평가 못 함 — 거부."""
    text = _section(
        "- *.py if backend is Python",
        "- *.vue",
    )
    assert _extract_forbidden_patterns(text) == ["*.vue"]


def test_korean_natural_language_rejected():
    text = _section(
        "- *.py 가 있어야 함",
        "- *.ts 는 없어야 함",
        "- *.vue",
    )
    assert _extract_forbidden_patterns(text) == ["*.vue"]


def test_when_rejected():
    text = _section("- *.tsx when React was chosen", "- *.vue")
    assert _extract_forbidden_patterns(text) == ["*.vue"]


def test_keyword_inside_paren_only_is_allowed():
    """괄호 *안* 의 자연어는 메모로 허용 — false negative 보다 false positive 가 더 위험.

    (planner skill 가이드가 이런 bullet 도 ❌ 로 명시하지만, 안전망은
    *괄호 밖* 자연어만 거부 — 괄호 안에 ``must`` 가 들어간 평범한 메모를
    잘못 거부하지 않음.)
    """
    text = _section("- *.vue (must replace with React component)")
    # 괄호 안 must 만 있고 밖은 깨끗 → 통과
    assert _extract_forbidden_patterns(text) == ["*.vue"]


def test_mixed_clean_and_dirty_lines():
    text = _section(
        "- *.vue",
        "- **/requirements.txt must exist",
        "- *.svelte",
        "- *.py if Python",
        "- **/Cargo.toml",
    )
    assert _extract_forbidden_patterns(text) == ["*.vue", "*.svelte", "**/Cargo.toml"]


# ── 헤더 / 섹션 경계 ────────────────────────────────────────────────────────


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
