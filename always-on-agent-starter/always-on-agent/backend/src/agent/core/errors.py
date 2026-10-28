"""Agent error hierarchy (F01)."""


class AgentError(Exception):
    """Base class for every error raised by the agent."""


class ConfigError(AgentError):
    """Configuration is missing or invalid."""


class AdapterError(AgentError):
    """An adapter call failed. ``retryable`` says whether the call may be retried.

    ``status_code`` carries the HTTP status when there is one (429 throttled, 410 delta expired).
    """

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class PolicyViolation(AgentError):
    """A tool call was attempted above its mode."""


class IllegalTransition(AgentError):
    """A task state change that the lifecycle (architecture 4.2) does not allow."""


class LinkConflict(AgentError):
    """An external id is already linked to a different task."""


class DataRuleViolation(AgentError):
    """Content would leave the laptop beyond the allowed data-rule level."""


class ApprovalConflict(AgentError):
    """An approval does not match the content it is meant to cover."""


class ModelUnavailable(AgentError):
    """The model gateway could not get a response after retries."""


class ModelOutputError(AgentError):
    """The model's output did not match the schema after one repair retry."""


class NeedsSignIn(AgentError):
    """No cached token could be refreshed silently; the user must sign in again."""
