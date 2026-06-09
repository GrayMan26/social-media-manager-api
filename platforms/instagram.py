"""
Instagram platform module.
Calls the Instagram Graph API, Pixabay, imgbb, and Claude directly
so there are no relative-path or import issues from the instagram-agent folder.
"""
import base64
import logging
import os
import re
import time

import anthropic
import requests

log = logging.getLogger(__name__)

PLATFORM = "instagram"

_GRAPH_BASE = "https://graph.facebook.com/v21.0"

# ── Credentials (from .env) ───────────────────────────────────────────────────

def _token()   -> str: return os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
def _user_id() -> str: return os.getenv("INSTAGRAM_USER_ID", "")
def _pixabay() -> str: return os.getenv("PIXABAY_API_KEY", "")
def _imgbb()   -> str: return os.getenv("IMGBB_API_KEY", "")


def is_available() -> bool:
    return bool(_token() and _user_id())


def _not_available():
    return {"ok": False, "error": "Instagram is not configured. Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID."}


# ── Image: fetch from Pixabay + upload to imgbb ───────────────────────────────

_TECH_FALLBACKS = [
    "computer repair technician",
    "laptop computer technology",
    "small business computer",
    "cybersecurity protection",
    "network router wifi",
]

_FILLER = re.compile(
    r"^\s*(\d+\s+)?(ways?|tips?|steps?|tricks?|things?|reasons?|how\s+to|why|"
    r"what\s+is|guide\s+to|the\s+best|best|top\s+\d+|ultimate)\s+",
    re.IGNORECASE,
)


def _fetch_pixabay_image(topic: str) -> dict | None:
    """Search Pixabay and return the best hit, or None."""
    if not _pixabay():
        return None
    cleaned = _FILLER.sub("", topic).strip()
    query = " ".join(cleaned.split()[:3])
    tech_signals = {"computer","tech","laptop","cyber","network","digital",
                    "software","hardware","server","wifi","data","repair","pc"}
    if not any(w.lower() in tech_signals for w in query.split()):
        query = f"{query} computer technology"

    for q, cat in [(query, "computer"), (query, "science"), (_TECH_FALLBACKS[0], "computer")]:
        try:
            r = requests.get("https://pixabay.com/api/", params={
                "key": _pixabay(), "q": q, "image_type": "photo",
                "orientation": "horizontal", "category": cat,
                "min_width": 1080, "min_height": 1080,
                "safesearch": "true", "per_page": 5, "order": "popular",
            }, timeout=10)
            r.raise_for_status()
            hits = r.json().get("hits", [])
            if hits:
                return hits[0]
        except Exception as e:
            log.warning("Pixabay search failed (%s, %s): %s", q, cat, e)
    return None


def _upload_to_imgbb(image_url: str) -> str:
    """Download an image from a URL and upload to imgbb. Returns public URL."""
    if not _imgbb():
        return image_url  # fall back to direct URL if no imgbb key

    # Download the image bytes
    img_bytes = requests.get(image_url, timeout=30).content
    b64 = base64.b64encode(img_bytes).decode()

    r = requests.post("https://api.imgbb.com/1/upload",
                      data={"key": _imgbb(), "image": b64}, timeout=30)
    r.raise_for_status()
    result = r.json()
    if not result.get("success"):
        raise RuntimeError(f"imgbb upload failed: {result}")
    return result["data"].get("image", {}).get("url") or result["data"]["url"]


def _get_public_image_url(topic: str) -> tuple[str, str]:
    """Returns (public_cdn_url, description). Falls back gracefully."""
    hit = _fetch_pixabay_image(topic)
    if not hit:
        return "", topic

    raw_url = hit.get("largeImageURL") or hit.get("webformatURL", "")
    description = f"{hit.get('tags', topic)}"

    if not raw_url:
        return "", description

    try:
        cdn_url = _upload_to_imgbb(raw_url)
        return cdn_url, description
    except Exception as e:
        log.warning("imgbb upload failed: %s — using direct URL", e)
        return raw_url, description


# ── Caption generation via Claude ─────────────────────────────────────────────

def _generate_caption(topic: str, image_description: str) -> str:
    """Generate an Instagram caption using Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return f"Check out our latest update on {topic}! #GrayTech #TechSupport"

    brand_voice_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "instagram-agent", "BRAND_VOICE.md"
    )
    brand_voice = ""
    try:
        with open(brand_voice_path, encoding="utf-8") as f:
            brand_voice = f.read()
    except Exception:
        brand_voice = "Professional, friendly, and helpful tone. Focus on making tech simple."

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"You are a social media manager for GrayTech Inc, a small tech support and repair business.\n\n"
        f"BRAND VOICE:\n{brand_voice}\n\n"
        f"Write an Instagram caption for this topic: {topic}\n"
        f"Image shows: {image_description}\n\n"
        f"Return ONLY the caption text with hashtags. No extra commentary."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ── Draft creation ─────────────────────────────────────────────────────────────

def create_draft(topic: str) -> dict:
    """Generate a caption and find a matching image for the given topic."""
    if not is_available():
        return _not_available()
    try:
        image_url, image_desc = _get_public_image_url(topic)
        caption = _generate_caption(topic, image_desc)
        return {
            "ok":        True,
            "platform":  PLATFORM,
            "content":   caption,
            "image_url": image_url,
        }
    except Exception as e:
        log.error("Instagram draft failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Posting ────────────────────────────────────────────────────────────────────

def post_now(content: str, image_url: str) -> dict:
    """Publish a post to Instagram immediately."""
    if not is_available():
        return _not_available()
    try:
        # Step 1: create media container
        r = requests.post(
            f"{_GRAPH_BASE}/{_user_id()}/media",
            params={"access_token": _token()},
            data={"image_url": image_url, "caption": content},
            timeout=30,
        )
        r.raise_for_status()
        container_id = r.json()["id"]

        # Step 2: wait for container to be ready (up to 60 s)
        for _ in range(30):
            time.sleep(2)
            status_r = requests.get(
                f"{_GRAPH_BASE}/{container_id}",
                params={"fields": "status_code", "access_token": _token()},
                timeout=10,
            )
            if status_r.json().get("status_code") == "FINISHED":
                break

        # Step 3: publish
        pub_r = requests.post(
            f"{_GRAPH_BASE}/{_user_id()}/media_publish",
            params={"access_token": _token()},
            data={"creation_id": container_id},
            timeout=30,
        )
        pub_r.raise_for_status()
        media_id = pub_r.json()["id"]
        return {"ok": True, "media_id": media_id}
    except Exception as e:
        log.error("Instagram post failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(period: str = "last_7_days") -> dict:
    if not is_available():
        return _not_available()
    try:
        # Account insights
        ins_r = requests.get(
            f"{_GRAPH_BASE}/{_user_id()}/insights",
            params={
                "metric": "reach,impressions,profile_views,follower_count",
                "period": "day",
                "access_token": _token(),
            },
            timeout=10,
        )
        ins_r.raise_for_status()
        account_data = {item["name"]: item["values"][-1]["value"]
                        for item in ins_r.json().get("data", [])}

        # Recent posts
        posts_r = requests.get(
            f"{_GRAPH_BASE}/{_user_id()}/media",
            params={
                "fields": "id,caption,timestamp,like_count,comments_count",
                "limit": 7,
                "access_token": _token(),
            },
            timeout=10,
        )
        posts_r.raise_for_status()
        posts = posts_r.json().get("data", [])

        return {
            "ok":      True,
            "platform": PLATFORM,
            "account":  account_data,
            "posts":   [{
                "id":       p["id"],
                "caption":  (p.get("caption") or "")[:60],
                "date":     p.get("timestamp", ""),
                "likes":    p.get("like_count", 0),
                "comments": p.get("comments_count", 0),
            } for p in posts],
        }
    except Exception as e:
        log.error("Instagram analytics failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Comments ───────────────────────────────────────────────────────────────────

def get_unanswered_comments() -> dict:
    if not is_available():
        return _not_available()
    try:
        posts_r = requests.get(
            f"{_GRAPH_BASE}/{_user_id()}/media",
            params={"fields": "id,caption", "limit": 5, "access_token": _token()},
            timeout=10,
        )
        posts_r.raise_for_status()
        posts = posts_r.json().get("data", [])

        all_comments = []
        for p in posts:
            c_r = requests.get(
                f"{_GRAPH_BASE}/{p['id']}/comments",
                params={"fields": "id,text,username,timestamp", "access_token": _token()},
                timeout=10,
            )
            c_r.raise_for_status()
            for c in c_r.json().get("data", []):
                c["post_id"] = p["id"]
                c["post_caption"] = (p.get("caption") or "")[:60]
                all_comments.append(c)

        return {"ok": True, "platform": PLATFORM, "comments": all_comments}
    except Exception as e:
        log.error("Instagram get comments failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def reply_to_comment(comment_id: str, reply_text: str) -> dict:
    if not is_available():
        return _not_available()
    try:
        time.sleep(2)  # small delay to avoid bot detection
        r = requests.post(
            f"{_GRAPH_BASE}/{comment_id}/replies",
            params={"access_token": _token()},
            data={"message": reply_text},
            timeout=15,
        )
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        log.error("Instagram reply failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}
