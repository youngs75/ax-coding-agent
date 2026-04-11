"""3-layer long-term memory system.

Layers:
    user    — preferences, habits, coding style
    project — architecture decisions, project rules, conventions
    domain  — business rules, domain terminology, invariants
"""

from coding_agent.memory.extractor import MemoryExtractor
from coding_agent.memory.middleware import MemoryMiddleware
from coding_agent.memory.schema import MemoryRecord
from coding_agent.memory.store import MemoryStore

__all__ = [
    "MemoryExtractor",
    "MemoryMiddleware",
    "MemoryRecord",
    "MemoryStore",
]
