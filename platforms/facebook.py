"""
Facebook Page platform module.
Posts a photo or text-only update to a Facebook Page via the Graph API.
Unlike Instagram (container + poll) and TikTok (async publish status), Facebook
Page posts publish in a single call. Comment reading/replying works on standard
Page permissions too — no paid-tier gating like Twitter/X.
"""
import logging
import os

import anthropic
import requests

from platforms import instagram

log = logging.getLogger(__name__)

PLATFORM = "facebook"

_GRAPH_BASE = "https://graph.facebook.com/v21.0"


# ── Credentials (from .env) ───────────────────────────────────────────────────

def _page_id() -> str: return os.getenv("FACEBOOK_PAGE_ID", "")
def _token()   -> str: return os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")


def is_available() -> bool:
    return bool(_page_id() and _token())


def _not_available():
    return {
        "ok": False,
        "error": "Facebook is not configured. Set FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN.",
    }


# ── Caption generation (Facebook style — same brand voice as Instagram) ──────

def _generate_fb_caption(topic: str, image_description: str = "") -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return f"Supporting seniors and caregivers every step of the way. {topic} #SeniorCare #CaregiverLife"

    brand_voice_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "instagram-agent", "BRAND_VOICE.md"
    )
    brand_voice = ""
    try:
        with open(brand_voice_path, encoding="utf-8") as f:
            brand_voice = f.read()
    except Exception:
        brand_voice = (
            "Warm, informative, and emotionally resonant. "
            "Speak to family caregivers and CNAs. "
            "Lead with a fact or emotional hook. End with a CTA to follow for more."
        )

    client = anthropic.Anthropic(api_key=api_key)
    image_note = f"Image shows: {image_description}\n" if image_description else ""
    prompt = (
        f"You are a social media manager for GrayTech Inc., a senior caregiving resource account "
        f"that supports family caregivers, CNAs, and families caring for aging loved ones.\n\n"
        f"BRAND VOICE:\n{brand_voice}\n\n"
        f"Write a Facebook post for this topic: {topic}\n"
        f"{image_note}\n"
        f"Rules:\n"
        f"- Facebook tone: warm and conversational, can run a bit longer than an Instagram caption,\n"
        f"  written to be easily shareable\n"
        f"- Do NOT use em dashes (—). Use a comma or period instead.\n"
        f"- Return ONLY the post text with hashtags. No extra commentary."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    caption = resp.content[0].text.strip()
    caption = caption.replace("—", ",").replace("–", "-")
    return caption


# ── Draft creation ─────────────────────────────────────────────────────────────

def create_draft(topic: str, include_image: bool = True) -> dict:
    """Generate a caption and, optionally, find a matching image for the given topic."""
    if not is_available():
        return _not_available()
    try:
        image_url, image_desc = ("", "")
        if include_image:
            image_url, image_desc = instagram._get_public_image_url(topic)
        caption = _generate_fb_caption(topic, image_desc)
        return {
            "ok":        True,
            "platform":  PLATFORM,
            "content":   caption,
            "image_url": image_url,
        }
    except Exception as e:
        log.error("Facebook draft failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Posting ────────────────────────────────────────────────────────────────────

def post_now(content: str, image_url: str) -> dict:
    """Publish to the Facebook Page — a photo post if image_url is set, else a text post."""
    if not is_available():
        return _not_available()
    try:
        if image_url:
            r = requests.post(
                f"{_GRAPH_BASE}/{_page_id()}/photos",
                params={"access_token": _token()},
                data={"url": image_url, "caption": content},
                timeout=30,
            )
        else:
            r = requests.post(
                f"{_GRAPH_BASE}/{_page_id()}/feed",
                params={"access_token": _token()},
                data={"message": content},
                timeout=30,
            )
        r.raise_for_status()
        data = r.json()
        return {"ok": True, "media_id": data.get("post_id") or data.get("id")}
    except Exception as e:
        log.error("Facebook post failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(period: str = "last_7_days") -> dict:
    if not is_available():
        return _not_available()
    try:
        page_r = requests.get(
            f"{_GRAPH_BASE}/{_page_id()}",
            params={"fields": "fan_count,followers_count", "access_token": _token()},
            timeout=10,
        )
        page_r.raise_for_status()
        page = page_r.json()

        posts_r = requests.get(
            f"{_GRAPH_BASE}/{_page_id()}/posts",
            params={
                "fields": "message,created_time,likes.summary(true),comments.summary(true)",
                "limit": 7,
                "access_token": _token(),
            },
            timeout=10,
        )
        posts_r.raise_for_status()
        posts = posts_r.json().get("data", [])

        return {
            "ok":       True,
            "platform": PLATFORM,
            "account": {
                "followers":   page.get("followers_count", page.get("fan_count", "N/A")),
                "total_posts": len(posts),
            },
            "posts": [{
                "id":       p["id"],
                "caption":  (p.get("message") or "")[:60],
                "date":     p.get("created_time", ""),
                "likes":    p.get("likes", {}).get("summary", {}).get("total_count", 0),
                "comments": p.get("comments", {}).get("summary", {}).get("total_count", 0),
            } for p in posts],
        }
    except Exception as e:
        log.error("Facebook analytics fetch failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Comments ───────────────────────────────────────────────────────────────────

def get_unanswered_comments() -> dict:
    if not is_available():
        return _not_available()
    try:
        posts_r = requests.get(
            f"{_GRAPH_BASE}/{_page_id()}/posts",
            params={"fields": "id,message", "limit": 5, "access_token": _token()},
            timeout=10,
        )
        posts_r.raise_for_status()
        posts = posts_r.json().get("data", [])

        all_comments = []
        for p in posts:
            c_r = requests.get(
                f"{_GRAPH_BASE}/{p['id']}/comments",
                params={"fields": "id,message,from,created_time", "access_token": _token()},
                timeout=10,
            )
            c_r.raise_for_status()
            for c in c_r.json().get("data", []):
                all_comments.append({
                    "id":           c["id"],
                    "text":         c.get("message", ""),
                    "username":     c.get("from", {}).get("name", "Unknown"),
                    "timestamp":    c.get("created_time", ""),
                    "post_id":      p["id"],
                    "post_caption": (p.get("message") or "")[:60],
                })

        return {"ok": True, "platform": PLATFORM, "comments": all_comments}
    except Exception as e:
        log.error("Facebook get comments failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def reply_to_comment(comment_id: str, reply_text: str) -> dict:
    if not is_available():
        return _not_available()
    try:
        r = requests.post(
            f"{_GRAPH_BASE}/{comment_id}/comments",
            params={"access_token": _token()},
            data={"message": reply_text},
            timeout=15,
        )
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        log.error("Facebook reply failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}
