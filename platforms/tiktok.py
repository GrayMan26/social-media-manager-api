"""TikTok platform — stub. Fill in when the TikTok agent is built."""

PLATFORM = "tiktok"
_MSG = "TikTok is not set up yet. It will be available after the TikTok agent is built."


def is_available() -> bool:
    return False

def create_draft(topic: str) -> dict:
    return {"ok": False, "platform": PLATFORM, "error": _MSG}

def post_now(content: str, image_url: str) -> dict:
    return {"ok": False, "platform": PLATFORM, "error": _MSG}

def get_analytics(period: str = "last_7_days") -> dict:
    return {"ok": False, "platform": PLATFORM, "error": _MSG}

def get_unanswered_comments() -> dict:
    return {"ok": False, "platform": PLATFORM, "error": _MSG}

def reply_to_comment(comment_id: str, reply_text: str) -> dict:
    return {"ok": False, "platform": PLATFORM, "error": _MSG}
