import json
import logging

logger = logging.getLogger(__name__)


def format_caption(payload: str) -> str:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.exception("Received malformed WebSocket payload: %r", payload)
        return ""
    speaker_label = "You" if data.get("speaker") == "you" else "Other"
    return f"{speaker_label}: {data.get('text', '')}"
