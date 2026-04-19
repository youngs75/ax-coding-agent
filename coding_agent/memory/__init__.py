"""3-layer long-term memory system.

Layers:
    user    — preferences, habits, coding style
    project — architecture decisions, project rules, conventions
    domain  — business rules, domain terminology, invariants

Storage is provided by ``minyoung_mah.SqliteMemoryStore`` (exported here as
``MemoryStore`` for import compat). The extractor and middleware are
domain-specific and remain in this package.
"""

from minyoung_mah import SqliteMemoryStore as MemoryStore

from coding_agent.memory.extractor import MemoryExtractor
from coding_agent.memory.middleware import MemoryMiddleware
from coding_agent.memory.schema import MemoryRecord

__all__ = [
    "MemoryExtractor",
    "MemoryMiddleware",
    "MemoryRecord",
    "MemoryStore",
]
