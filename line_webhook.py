import json
from datetime import datetime
from pathlib import Path

from flask import Flask, request


INBOX_PATH = Path("runtime/line_inbox.jsonl")
INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def append_message(text: str) -> None:
    payload = {
        "text": text,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with INBOX_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


@app.post("/callback")
def callback():
    body = request.get_json(silent=True) or {}
    events = body.get("events", [])

    for event in events:
        message = event.get("message", {})
        if message.get("type") == "text":
            text = str(message.get("text", "")).strip()
            if text:
                append_message(text)

    return "OK", 200


@app.get("/health")
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787)
