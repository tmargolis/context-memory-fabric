"""Concrete provider implementations of server.core.protocols.

Deliberately the only place outside tests/fakes that names a concrete
provider class. server.context (and, in later milestones, anything else
that needs a default provider instance) should import
get_default_memory_provider/get_default_knowledge_provider from here
rather than importing GraphitiMemoryProvider/FileKnowledgeProvider
directly — keeping provider-specific names out of server.context.py by
one layer of indirection, so the day a second provider exists, only this
file's factory functions need to change, not every caller.
"""

from server.providers.wiki.provider import FileKnowledgeProvider
from server.providers.memory_graphiti import GraphitiMemoryProvider


def get_default_memory_provider():
    """The memory provider used when no explicit override is given.

    Today there is exactly one memory provider, so "default" and "only"
    coincide. This function is the seam a future provider-selection policy
    (e.g. reading a MEMORY_PROVIDER env var) would change, without every
    caller needing to know that selection happened.
    """
    return GraphitiMemoryProvider()


def get_default_knowledge_provider():
    """The knowledge provider used when no explicit override is given.

    See get_default_memory_provider's docstring — same rationale.
    """
    return FileKnowledgeProvider()
