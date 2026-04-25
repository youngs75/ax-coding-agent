"""Critic SubAgentRole — PRD 충족도 + 코드 품질 평가 전용.

verifier 와 안 겹치는 자리:
  - verifier  : *기능* 검증 (pytest, build, run-time)
  - reviewer  : *스타일* 검토 (lint, naming, suggestions)
  - **critic**: *요구사항 충족도* + *구조 품질* (PRD 매핑, dead code,
                완료 라벨이 실제로 완료됐는지 spot check)

읽기 전용 도구만 — 직접 수정 안 함. 결정은 JSON 으로만 출력.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coding_agent.subagents.roles import (
    CodingAgentRole,
    _compose,
    _skills_for,
)

if TYPE_CHECKING:
    from coding_agent.subagents.user_decisions import UserDecisionsLog


_CRITIC_PROMPT = """\
You are a sufficiency critic. You evaluate whether a coding task's outputs
genuinely satisfy the user's original request — beyond what verifier
(functional) and reviewer (lint/style) already check. You do not modify code.

Available tools: {tools}

평가 영역:
1. PRD 충족도 — 사용자 원 요청에 명시된 항목·산출물·기능이 모두 작업
   결과물에 반영됐는가? 누락이 있다면 그 항목을 구체적으로 지적.
2. 구조 품질 — 코드 구조 / 디렉토리 레이아웃 / 명명이 ax 컨벤션을
   따르는가? 사용되지 않는 stub, 절반 구현, dead code 가 있는가?
3. ledger 일치 — 완료(completed)로 표시된 task 가 실제 산출물에서 검증
   가능한가? read_file 로 sample check.

다루지 말 것 (다른 role 영역):
- 테스트 통과 여부 / 런타임 정확성 (verifier 영역)
- lint 위반 / 코드 스타일 디테일 (reviewer 영역)

출력 규칙 (MANDATORY):
- 응답은 **JSON 한 줄** 만. 자연어 머리말, 마크다운 코드블록, 추가 설명
  모두 금지.
- 스키마:
  {{
    "verdict": "pass" | "retry_lookup" | "replan" | "escalate_hitl",
    "target_role": "coder" | "fixer" | "planner" | null,
    "reason": "<1-2 문장, 한국어>",
    "feedback_for_retry": "<retry/replan 시 다음 iteration 에 줄 구체 지시. pass/escalate 면 null>"
  }}

verdict 선택 가이드:
- pass            : PRD 모든 핵심 항목이 산출물에 반영, 구조도 정상.
- retry_lookup    : 한두 항목 누락 또는 절반 구현이 명확. target_role 에
                    누구에게 위임할지 (coder/fixer) 명시.
- replan          : 분해 자체가 사용자 요청의 *영역* 을 빠뜨린 경우.
                    target_role="planner" 로 재분해 요청.
- escalate_hitl   : 모호하거나 사용자 의도 확인 없이는 결정 불가. target_role=null.
"""


def critic_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    """Build the critic SubAgentRole.

    Read-only tools only. ``tier_default="reasoning"`` — apt-legal 도 critic
    을 별도 강한 모델(opus 급)로 라우팅. ax 의 4-tier 중 reasoning 이
    가장 자연스러운 매핑.
    """
    tool_allowlist = tools or ["read_file", "glob_files", "grep"]
    return CodingAgentRole(
        name="critic",
        system_prompt=_compose(_CRITIC_PROMPT, tool_allowlist),
        tool_allowlist=tool_allowlist,
        model_tier="reasoning",
        _user_decisions=user_decisions,
        # critic-specific skills 는 아직 없음 — skill store 에 항목이 추가되면
        # 자동 로드. 빈 튜플은 영향 없음.
        _skills=_skills_for("critic"),
    )


__all__ = ["critic_role"]
