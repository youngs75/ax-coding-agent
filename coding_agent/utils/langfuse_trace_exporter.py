"""Langfuse 트레이스 대화 내용 추출 유틸리티.

AI Coding Agent가 수행한 전체 프로세스를 Langfuse에서 추출하여
읽기 좋은 Markdown 형태로 변환합니다.

사용법:
    # 최근 세션 목록 조회
    python -m coding_agent.utils.langfuse_trace_exporter --list-sessions 10

    # 세션 ID로 전체 대화 추출
    python -m coding_agent.utils.langfuse_trace_exporter --session <session-id>

    # 특정 trace ID로 추출
    python -m coding_agent.utils.langfuse_trace_exporter --trace <trace-id>

    # 최근 trace 목록 조회
    python -m coding_agent.utils.langfuse_trace_exporter --list-traces 10

    # 파일로 출력
    python -m coding_agent.utils.langfuse_trace_exporter --session <id> -o output.md

    # 상세 모드 (전체 tool 메시지 포함)
    python -m coding_agent.utils.langfuse_trace_exporter --session <id> -v
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langfuse import Langfuse

# 프로젝트 루트의 .env 로드
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)


# ── 데이터 모델 ──

@dataclass
class Message:
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass
class Generation:
    observation_id: str
    model: str | None
    parent_name: str | None
    start_time: datetime | None
    input_messages: list[Message]
    output_message: Message | None
    usage: dict[str, Any] | None
    latency: float | None


@dataclass
class TraceConversation:
    trace_id: str
    trace_name: str | None
    session_id: str | None
    timestamp: datetime | None
    user_input: str | None
    agent_output: str | None
    total_cost: float | None
    generations: list[Generation] = field(default_factory=list)


# ── Langfuse 클라이언트 ──

def _create_client() -> Langfuse:
    return Langfuse(
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
        host=os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
    )


# ── 파싱 헬퍼 ──

def _parse_message(msg: dict[str, Any]) -> Message:
    role = msg.get("role", "unknown")
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", json.dumps(block, ensure_ascii=False)))
            else:
                parts.append(str(block))
        content = "\n".join(parts)
    elif content is None:
        content = ""

    tool_calls = []
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        tool_calls.append({
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
        })

    return Message(
        role=role,
        content=str(content),
        tool_calls=tool_calls,
        tool_call_id=msg.get("tool_call_id"),
    )


def _extract_messages_from_input(inp: Any) -> list[Message]:
    if inp is None:
        return []
    if isinstance(inp, dict) and "messages" in inp:
        return [_parse_message(m) for m in inp["messages"]]
    if isinstance(inp, list):
        return [_parse_message(m) for m in inp if isinstance(m, dict)]
    return []


def _extract_output_message(out: Any) -> Message | None:
    if out is None:
        return None
    if isinstance(out, dict):
        return _parse_message(out)
    if isinstance(out, str):
        return Message(role="assistant", content=out)
    return None


def _extract_user_request(trace_input: Any, trace_output: Any) -> str | None:
    if isinstance(trace_output, dict) and "messages" in trace_output:
        for msg in trace_output["messages"]:
            content = msg.get("content", "")
            role = msg.get("type", msg.get("role", ""))
            if role in ("human", "user") and content:
                return str(content)
    if isinstance(trace_input, str) and trace_input:
        return trace_input
    return None


def _extract_final_output(trace_output: Any) -> str | None:
    if isinstance(trace_output, dict) and "messages" in trace_output:
        for msg in reversed(trace_output["messages"]):
            role = msg.get("type", msg.get("role", ""))
            content = msg.get("content", "")
            if role in ("ai", "assistant") and content:
                return str(content)
    if isinstance(trace_output, str) and trace_output:
        return trace_output
    return None


# ── 핵심 추출 로직 ──

def extract_trace(lf: Langfuse, trace_id: str) -> TraceConversation:
    detail = lf.api.trace.get(trace_id)
    user_input = _extract_user_request(detail.input, detail.output)
    agent_output = _extract_final_output(detail.output)

    conversation = TraceConversation(
        trace_id=trace_id,
        trace_name=detail.name,
        session_id=detail.session_id,
        timestamp=detail.timestamp,
        user_input=user_input,
        agent_output=agent_output,
        total_cost=detail.total_cost,
    )

    observations = sorted(
        detail.observations,
        key=lambda o: o.start_time or o.end_time or detail.timestamp,
    )

    id_to_name: dict[str, str] = {}
    for obs in observations:
        id_to_name[obs.id] = obs.name or obs.type or "unknown"

    for obs in observations:
        parent_name = id_to_name.get(obs.parent_observation_id or "", None)
        if obs.type == "GENERATION":
            gen = Generation(
                observation_id=obs.id,
                model=obs.model,
                parent_name=parent_name or obs.name,
                start_time=obs.start_time,
                input_messages=_extract_messages_from_input(obs.input),
                output_message=_extract_output_message(obs.output),
                usage=obs.usage_details or (
                    {"input": obs.usage.input, "output": obs.usage.output}
                    if obs.usage else None
                ),
                latency=obs.latency,
            )
            conversation.generations.append(gen)

    return conversation


def extract_session(lf: Langfuse, session_id: str) -> list[TraceConversation]:
    traces = lf.api.trace.list(session_id=session_id, limit=100)
    conversations = []
    for t in sorted(traces.data, key=lambda x: x.timestamp or datetime.min):
        conv = extract_trace(lf, t.id)
        conversations.append(conv)
    return conversations


def list_sessions(lf: Langfuse, limit: int = 20) -> list[dict[str, Any]]:
    sessions = lf.api.sessions.list(limit=limit)
    result = []
    for s in sessions.data:
        traces = lf.api.trace.list(session_id=s.id, limit=100)
        result.append({
            "session_id": s.id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "trace_count": len(traces.data),
            "trace_names": [t.name for t in traces.data],
        })
        if len(result) >= limit:
            break
    return result


def list_traces(lf: Langfuse, limit: int = 20) -> list[dict[str, Any]]:
    traces = lf.api.trace.list(limit=limit)
    result = []
    for t in traces.data:
        user_input = _extract_user_request(t.input, t.output)
        result.append({
            "trace_id": t.id,
            "name": t.name,
            "session_id": t.session_id,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            "user_input": (user_input[:100] + "...") if user_input and len(user_input) > 100 else user_input,
            "total_cost": t.total_cost,
            "tags": t.tags,
        })
    return result


# ── Markdown 포맷터 ──

def _format_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    lines = []
    for tc in tool_calls:
        name = tc.get("name", "unknown")
        args_raw = tc.get("arguments", "")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            args_str = json.dumps(args, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            args_str = str(args_raw)
        if len(args_str) > 500:
            args_str = args_str[:500] + "\n  ... (truncated)"
        lines.append(f"**`{name}`**")
        lines.append(f"```json\n{args_str}\n```")
    return "\n".join(lines)


def _format_content(content: str, max_length: int = 2000) -> str:
    if not content:
        return "(empty)"
    if len(content) > max_length:
        return content[:max_length] + f"\n\n... (truncated, total {len(content)} chars)"
    return content


def format_conversation_markdown(
    conversations: list[TraceConversation],
    *,
    verbose: bool = False,
) -> str:
    lines: list[str] = []

    if conversations:
        first = conversations[0]
        lines.append("# Langfuse Trace Export")
        if first.session_id:
            lines.append(f"**Session**: `{first.session_id}`")
        lines.append(f"**Traces**: {len(conversations)}개")
        if first.timestamp:
            lines.append(f"**시작**: {first.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        total_cost = sum(c.total_cost or 0 for c in conversations)
        if total_cost > 0:
            lines.append(f"**총 비용**: ${total_cost:.6f}")
        lines.append("")

    for conv_idx, conv in enumerate(conversations, 1):
        lines.append("---")
        lines.append(f"## Trace {conv_idx}: {conv.trace_name or 'unnamed'}")
        lines.append(f"- **ID**: `{conv.trace_id}`")
        if conv.timestamp:
            lines.append(f"- **시각**: {conv.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        if conv.total_cost:
            lines.append(f"- **비용**: ${conv.total_cost:.6f}")
        lines.append(f"- **LLM 호출**: {len(conv.generations)}회")
        lines.append("")

        if conv.user_input:
            lines.append("### 사용자 요청")
            lines.append(f"```\n{_format_content(conv.user_input, 3000)}\n```")
            lines.append("")

        for gen_idx, gen in enumerate(conv.generations, 1):
            model_tag = f"`{gen.model}`" if gen.model else "unknown"
            lines.append(f"### Step {gen_idx}: LLM Call — {model_tag}")
            if gen.usage:
                inp = gen.usage.get("input", 0)
                out = gen.usage.get("output", 0)
                if inp or out:
                    lines.append(f"tokens: in={inp} out={out}")
            if gen.latency:
                lines.append(f"latency: {gen.latency:.1f}s")
            lines.append("")

            if verbose:
                for msg in gen.input_messages:
                    lines.append(f"**[{msg.role.upper()}]**")
                    if msg.content:
                        lines.append(_format_content(msg.content))
                    if msg.tool_calls:
                        lines.append(_format_tool_calls(msg.tool_calls))
                    lines.append("")
            else:
                for msg in gen.input_messages:
                    if msg.role in ("user", "human"):
                        lines.append(f"**[USER]** {_format_content(msg.content, 300)}")
                        lines.append("")
                    elif msg.role in ("assistant", "ai") and msg.tool_calls:
                        names = [tc["name"] for tc in msg.tool_calls]
                        lines.append(f"**[ASSISTANT]** → {', '.join(names)}")
                        lines.append("")

            if gen.output_message:
                out = gen.output_message
                lines.append("**[OUTPUT]**")
                if out.content:
                    lines.append(_format_content(out.content))
                if out.tool_calls:
                    lines.append(_format_tool_calls(out.tool_calls))
                lines.append("")

        if conv.agent_output:
            lines.append("### 최종 에이전트 응답")
            lines.append(_format_content(conv.agent_output, 5000))
            lines.append("")

    return "\n".join(lines)


def format_sessions_list(sessions: list[dict[str, Any]]) -> str:
    lines = ["# Langfuse Sessions", ""]
    lines.append("| # | Session ID | Created | Traces |")
    lines.append("|---|-----------|---------|--------|")
    for i, s in enumerate(sessions, 1):
        lines.append(f"| {i} | `{s['session_id']}` | {s['created_at'] or '-'} | {s['trace_count']} |")
    return "\n".join(lines)


def format_traces_list(traces: list[dict[str, Any]]) -> str:
    lines = ["# Langfuse Traces", ""]
    lines.append("| # | Trace ID | Name | Cost | User Input |")
    lines.append("|---|----------|------|------|------------|")
    for i, t in enumerate(traces, 1):
        tid = t["trace_id"][:12] + "..."
        cost = f"${t['total_cost']:.4f}" if t["total_cost"] else "-"
        user_input = (t["user_input"] or "-")[:60]
        lines.append(f"| {i} | `{tid}` | {t['name']} | {cost} | {user_input} |")
    return "\n".join(lines)


# ── CLI ──

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Langfuse 트레이스에서 AI Coding Agent 대화 내용을 추출합니다.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", "-s", help="세션 ID로 전체 대화 추출")
    group.add_argument("--trace", "-t", help="특정 trace ID로 추출")
    group.add_argument("--list-sessions", "-ls", type=int, nargs="?", const=20, metavar="N",
                       help="최근 N개 세션 목록 (기본: 20)")
    group.add_argument("--list-traces", "-lt", type=int, nargs="?", const=20, metavar="N",
                       help="최근 N개 trace 목록 (기본: 20)")
    parser.add_argument("--output", "-o", help="출력 파일 경로")
    parser.add_argument("--verbose", "-v", action="store_true", help="상세 모드")

    args = parser.parse_args()
    lf = _create_client()

    if args.list_sessions is not None:
        sessions = list_sessions(lf, limit=args.list_sessions)
        output = format_sessions_list(sessions)
    elif args.list_traces is not None:
        traces = list_traces(lf, limit=args.list_traces)
        output = format_traces_list(traces)
    elif args.session:
        print(f"세션 ID: {args.session}", file=sys.stderr)
        conversations = extract_session(lf, args.session)
        output = format_conversation_markdown(conversations, verbose=args.verbose)
    elif args.trace:
        conversation = extract_trace(lf, args.trace)
        output = format_conversation_markdown([conversation], verbose=args.verbose)
    else:
        parser.print_help()
        return

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"출력 완료: {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
