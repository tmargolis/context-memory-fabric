"""Shared error types for Context Memory Fabric providers.

Provider-specific errors (e.g. Graphiti's MissingGraphConfigurationError)
should subclass ProviderConfigurationError rather than raising bare
RuntimeError, so calling code (server.mcp, server.context) can catch
configuration problems generically across providers.
"""


class CMFError(Exception):
    """Base class for Context Memory Fabric errors raised by core/provider code."""


class ProviderConfigurationError(CMFError):
    """A provider was invoked without the configuration it requires.

    Distinct from a runtime failure (e.g. a network error talking to
    FalkorDB): this means the deployment itself is missing required
    configuration (an API key, a database name, a corpus path), and the
    fix is a configuration change, not a retry.
    """


class ProviderNotConfiguredError(ProviderConfigurationError):
    """A capability was requested from a provider that is not configured at all.

    Distinguishes "not configured" (e.g. no LLM_WIKI_PATH set, knowledge
    provider absent by design) from "configured but invalid" (e.g.
    LLM_WIKI_PATH set to a nonexistent directory), which remains a plain
    ProviderConfigurationError. Callers that want to degrade gracefully
    (skip a section, omit a tool) should catch this specific subclass;
    callers that want to fail loudly on a misconfiguration should let the
    broader ProviderConfigurationError propagate.
    """
