"""History Cleanup stage shared by Router and standalone adapters."""

from shared.request_runtime import RequestRuntime

is_tool_image_message = RequestRuntime.is_tool_image_message


def last_user_index(messages: list[dict]) -> int:
    return next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
            and not is_tool_image_message(messages[index])
        ),
        -1,
    )


def cleanup_history(body: dict, __request__=None, *, debug) -> dict:
    metadata = body.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("History Cleanup metadata must be an object")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise TypeError("History Cleanup messages must be a list")

    RequestRuntime(__request__, metadata).before_filter("history_cleanup")

    current_user = last_user_index(messages)
    current_start = max(current_user, 0)
    historical_text = [
        {"role": message["role"], "content": message.get("content", "")}
        for message in messages[:current_start]
        if message.get("role") in {"system", "user", "assistant"}
        and message.get("content")
        and not is_tool_image_message(message)
    ]
    body["messages"] = [*historical_text, *messages[current_start:]]
    RequestRuntime(__request__, metadata).finish_filter("history_cleanup")
    debug(
        "messages before=%s after=%s current_user=%s",
        len(messages),
        len(body["messages"]),
        current_user,
    )
    return body
