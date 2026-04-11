"""SubAgent lifecycle — dynamic creation, execution, and teardown of child agents."""

from coding_agent.subagents.factory import SubAgentFactory
from coding_agent.subagents.manager import SubAgentManager
from coding_agent.subagents.models import (
    SubAgentInstance,
    SubAgentResult,
    SubAgentStatus,
)
from coding_agent.subagents.registry import SubAgentRegistry

__all__ = [
    "SubAgentFactory",
    "SubAgentInstance",
    "SubAgentManager",
    "SubAgentRegistry",
    "SubAgentResult",
    "SubAgentStatus",
]
