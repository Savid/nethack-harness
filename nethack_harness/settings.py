"""Execution and endpoint limits."""
import math
import json
from dataclasses import dataclass, asdict, field


@dataclass(frozen=True)
class Settings:
    objective: str = "Play NetHack."
    decision_timeout: float = 10.0
    quiet: float = 0.06
    max_action_steps: int = 8
    review_after_calls: int = 0
    max_action_attempts: int = 0
    caller_context: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("objective must be nonempty text")
        for key in ("decision_timeout", "quiet"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("%s must be a positive finite number" % key)
        if type(self.max_action_steps) is not int or not 1 <= self.max_action_steps <= 64:
            raise ValueError("max_action_steps must be an integer from 1 to 64")
        if type(self.review_after_calls) is not int or self.review_after_calls < 0:
            raise ValueError("review_after_calls must be a nonnegative integer (0 disables the budget)")
        if type(self.max_action_attempts) is not int or self.max_action_attempts < 0:
            raise ValueError("max_action_attempts must be a nonnegative integer (0 disables the budget)")
        if not isinstance(self.caller_context, dict):
            raise ValueError("caller_context must be a JSON object")
        try:
            json.dumps(self.caller_context, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("caller_context must contain finite JSON values") from None

    def as_dict(self):
        return asdict(self)
