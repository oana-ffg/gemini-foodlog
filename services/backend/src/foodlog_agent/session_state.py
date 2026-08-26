from typing import Any, Protocol


class SessionStateContext(Protocol):
    state: Any


def required_state_identifier(context: SessionStateContext, key: str) -> str:
    value = context.state.get(key)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 160
    ):
        raise ValueError(f"Agent session state is missing a valid {key}")
    return value
