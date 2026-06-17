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
def _pexels()  -> str: return os.getenv("PEXELS_API_KEY", "")


def is_available() -> bool:
    return bool(_token() and _user_id())


def _not_available():
    return {"ok": False, "error": "Instagram is not configured. Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID."}


# ── Image: fetch from Pixabay + upload to imgbb ───────────────────────────────

_CARE_FALLBACKS = [
    "senior elderly caregiver",
    "home care elderly person",
    "family caregiver aging parent",
    "nurse CNA elderly patient",
    "senior living assisted care",
]

_FILLER = re.compile(
    r"^\s*(\d+\s+)?(ways?|tips?|steps?|tricks?|things?|reasons?|how\s+to|why|"
    r"what\s+is|guide\s+to|the\s+best|best|top\s+\d+|ultimate)\s+",
    re.IGNORECASE,
)

# Maps topic signal words → a targeted Pexels/Pixabay search query
_TOPIC_MAP: list[tuple[tuple[str, ...], str]] = [
    (("burnout", "stress", "tired", "exhaust", "overwhelm"),         "caregiver stress exhausted nurse support"),
    (("dementia", "alzheimer", "memory", "cognitive", "forget"),     "elderly dementia care memory support"),
    (("cna", "certified nursing", "aide", "nursing assistant"),      "CNA nurse aide healthcare worker"),
    (("job", "career", "hiring", "recruit", "opportunit"),           "nurse healthcare professional caregiver"),
    (("statistic", "research", "data", "study", "percent"),          "elderly senior health aging research"),
    (("home care", "in-home", "inhome", "home health"),              "home health aide elderly care family"),
    (("medication", "medicine", "pill", "prescription"),             "nurse senior medication assistance"),
    (("hospice", "palliative", "end of life"),                       "hospice palliative care comfort elderly"),
    (("grief", "loss", "mourn"),                                     "caregiver grief support elderly comfort"),
    (("tip", "advice", "guide", "how to"),                           "caregiver elderly practical help"),
    (("news", "policy", "law", "legislation", "update"),             "senior healthcare elderly nursing policy"),
    (("emotion", "support", "encourage", "community", "appreciat"),  "caregiver family support hug elderly"),
    (("facility", "assisted living", "nursing home", "memory care"), "assisted living nursing home elderly"),
    (("nutrition", "food", "diet", "meal"),                         "senior nutrition healthy meal elderly"),
    (("fall", "mobility", "exercise", "physical", "walk"),           "senior mobility exercise walking elderly"),
    (("family", "daughter", "son", "relative"),                      "family caregiver elderly parent home"),
]


def _build_care_query(topic: str) -> str:
    """Map topic text to a targeted caregiving search query."""
    topic_lower = topic.lower()
    for signals, query in _TOPIC_MAP:
        if any(s in topic_lower for s in signals):
            return query
    cleaned = _FILLER.sub("", topic).strip()
    words = cleaned.split()[:3]
    return " ".join(words) + " senior caregiver elderly"


def _fetch_pexels_image(topic: str) -> dict | None:
    """Search Pexels for a landscape senior care photo. Returns hit dict or None."""
    if not _pexels():
        return None
    query = _build_care_query(topic)
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": _pexels()},
            params={"query": query, "per_page": 10, "orientation": "landscape", "size": "large"},
            timeout=10,
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if photos:
            return photos[0]
    except Exception as e:
        log.warning("Pexels search failed for '%s': %s", query, e)
    return None


def _fetch_pixabay_image(topic: str) -> dict | None:
    """Search Pixabay with editors_choice filter for higher quality."""
    if not _pixabay():
        return None
    query = _build_care_query(topic)
    for q, cat, editors in [
        (query,               "people", "true"),
        (query,               "people", "false"),
        (query,               "health", "false"),
        (_CARE_FALLBACKS[0],  "people", "false"),
    ]:
        try:
            r = requests.get("https://pixabay.com/api/", params={
                "key": _pixabay(), "q": q, "image_type": "photo",
                "orientation": "horizontal", "category": cat,
                "min_width": 1080, "min_height": 1080,
                "safesearch": "true", "per_page": 10, "order": "popular",
                "editors_choice": editors,
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
    """
    Returns (public_cdn_url, description). Tries Pexels first, then Pixabay.
    Uploads via imgbb so Instagram's API can fetch a stable CDN URL.
    """
    # Try Pexels first — better senior care photo library
    pexels_hit = _fetch_pexels_image(topic)
    if pexels_hit:
        raw_url = pexels_hit["src"].get("large2x") or pexels_hit["src"].get("large", "")
        description = f"{pexels_hit.get('alt', topic)} (Photo by {pexels_hit.get('photographer', 'Pexels')})"
        if raw_url:
            try:
                cdn_url = _upload_to_imgbb(raw_url)
                return cdn_url, description
            except Exception as e:
                log.warning("imgbb upload failed for Pexels image: %s — using direct URL", e)
                return raw_url, description

    # Fallback to Pixabay
    pixabay_hit = _fetch_pixabay_image(topic)
    if not pixabay_hit:
        return "", topic

    raw_url = pixabay_hit.get("largeImageURL") or pixabay_hit.get("webformatURL", "")
    description = f"{pixabay_hit.get('tags', topic)}"
    if not raw_url:
        return "", description

    try:
        cdn_url = _upload_to_imgbb(raw_url)
        return cdn_url, description
    except Exception as e:
        log.warning("imgbb upload failed for Pixabay image: %s — using direct URL", e)
        return raw_url, description


# ── Caption generation via Claude ─────────────────────────────────────────────

def _generate_caption(topic: str, image_description: str) -> str:
    """Generate an Instagram caption using Claude."""
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
    prompt = (
        f"You are a social media manager for GrayTech Inc., a senior caregiving resource account "
        f"that supports family caregivers, CNAs, and families caring for aging loved ones.\n\n"
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

    account_data = {}
    insights_error = None

    # Try account-level insights (requires instagram_manage_insights permission)
    try:
        ins_r = requests.get(
            f"{_GRAPH_BASE}/{_user_id()}/insights",
            params={
                "metric": "reach,impressions,profile_views",
                "period": "day",
                "access_token": _token(),
            },
            timeout=10,
        )
        ins_r.raise_for_status()
        account_data = {item["name"]: item["values"][-1]["value"]
                        for item in ins_r.json().get("data", [])}
    except Exception as e:
        insights_error = str(e)
        log.warning("Instagram account insights unavailable (may need instagram_manage_insights permission): %s", e)

    # Follower count comes from the user profile, not insights
    try:
        profile_r = requests.get(
            f"{_GRAPH_BASE}/{_user_id()}",
            params={"fields": "followers_count,media_count", "access_token": _token()},
            timeout=10,
        )
        profile_r.raise_for_status()
        profile = profile_r.json()
        account_data["followers"] = profile.get("followers_count", "N/A")
        account_data["total_posts"] = profile.get("media_count", "N/A")
    except Exception as e:
        log.warning("Instagram profile fetch failed: %s", e)

    # Recent posts with engagement stats (always available)
    try:
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
    except Exception as e:
        log.error("Instagram media fetch failed: %s", e)
        return {"ok": False, "error": str(e)}

    result = {
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
    if insights_error:
        result["insights_note"] = "Reach/impressions unavailable — token may need instagram_manage_insights permission."
    return result


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
