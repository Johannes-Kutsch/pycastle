import json
from typing import Optional


class StreamParser:
    def feed(self, line: str) -> Optional[str]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        if obj.get("type") != "assistant":
            return None
        content = (obj.get("message") or {}).get("content") or []
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts) if parts else None
