import json

from overlay.formatting import format_caption


def test_format_caption_labels_you():
    payload = json.dumps({"speaker": "you", "text": "hello there"})
    assert format_caption(payload) == "You: hello there"


def test_format_caption_labels_other():
    payload = json.dumps({"speaker": "other", "text": "hi back"})
    assert format_caption(payload) == "Other: hi back"


def test_format_caption_handles_missing_text():
    payload = json.dumps({"speaker": "you"})
    assert format_caption(payload) == "You: "
