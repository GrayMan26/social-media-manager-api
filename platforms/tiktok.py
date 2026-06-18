"""
TikTok platform module.
Posts both photo carousels and short-form video to TikTok via the Content
Posting API (open.tiktokapis.com). Videos are sourced from Pexels Videos
(portrait orientation); photos reuse the same Pexels/Pixabay pipeline as
Instagram via instagram._build_care_query / instagram._get_public_image_url.
"""
import logging
import os
import time

import anthropic
import requests

from platforms import instagram

log = logging.getLogger(__name__)

PLATFORM = "tiktok"

TIKTOK_API = "https://open.tiktokapis.com/v2"

_in_memory_token: str = ""  # holds a refreshed token for this process lifetime


# ── Credentials (from .env) ───────────────────────────────────────────────────

def _client_key()    -> str: return os.getenv("TIKTOK_CLIENT_KEY", "")
def _client_secret() -> str: return os.getenv("TIKTOK_CLIENT_SECRET", "")
def _access_token()  -> str: return os.getenv("TIKTOK_ACCESS_TOKEN", "")
def _refresh_token()  -> str: return os.getenv("TIKTOK_REFRESH_TOKEN", "")
def _open_id()        -> str: return os.getenv("TIKTOK_OPEN_ID", "")
def _pexels()          -> str: return os.getenv("PEXELS_API_KEY", "")


def is_available() -> bool:
    return bool(_access_token() and _open_id())


def _not_available():
    return {"ok": False, "error": "TikTok is not configured. Set TIKTOK_ACCESS_TOKEN and TIKTOK_OPEN_ID."}


def _get_token() -> str:
    """Return a usable access token, refreshing it first if a refresh token is configured."""
    global _in_memory_token
    if _in_memory_token:
        return _in_memory_token
    if _refresh_token() and _client_key() and _client_secret():
        try:
            r = requests.post(
                f"{TIKTOK_API}/oauth/token/",
                data={
                    "client_key":    _client_key(),
                    "client_secret": _client_secret(),
                    "grant_type":    "refresh_token",
                    "refresh_token": _refresh_token(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("access_token"):
                _in_memory_token = data["access_token"]
                return _in_memory_token
        except Exception as e:
            log.warning("TikTok token refresh failed, falling back to TIKTOK_ACCESS_TOKEN: %s", e)
    return _access_token()


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}", "Content-Type": "application/json"}


# ── Caption generation (TikTok style — different from Instagram) ─────────────

def _generate_tiktok_caption(topic: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return f"{topic}. #SeniorCare #CaregiverTok #ElderCare"

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"You write TikTok captions for GrayTech Inc., a senior caregiving resource account "
        f"that supports family caregivers, CNAs, and families caring for aging loved ones.\n\n"
        f"Write a TikTok caption for this topic: {topic}\n\n"
        f"Rules:\n"
        f"- Hook in the first line, under 10 words, scroll-stopping\n"
        f"- 2-3 short sentences total, conversational TikTok tone (not formal)\n"
        f"- End with a question or CTA that invites comments\n"
        f"- Do NOT use em dashes (—). Use a comma or period instead.\n"
        f"- Include 4-6 hashtags from: #SeniorCare #CaregiverTok #ElderCare #AgingParents "
        f"#HomeCare #CNALife #CaregiverLife #SeniorTok\n"
        f"- Return ONLY the caption text. No extra commentary."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    caption = resp.content[0].text.strip()
    caption = caption.replace("—", ",").replace("–", "-")
    return caption


# ── Video sourcing from Pexels Videos ─────────────────────────────────────────

def _fetch_pexels_video(topic: str) -> dict | None:
    """Search Pexels Videos for a portrait senior-care clip. Returns dict or None."""
    if not _pexels():
        return None
    query = instagram._build_care_query(topic)
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": _pexels()},
            params={"query": query, "per_page": 5, "orientation": "portrait", "size": "medium"},
            timeout=12,
        )
        r.raise_for_status()
        videos = r.json().get("videos", [])
        if not videos:
            return None
        video = videos[0]
        files = [f for f in video.get("video_files", []) if f.get("width", 0) < f.get("height", 1)]
        if not files:
            files = video.get("video_files", [])
        if not files:
            return None
        hd = next((f for f in files if f.get("quality") == "hd"), files[0])
        return {
            "video_url":     hd["link"],
            "thumbnail_url": video.get("image", ""),
            "photographer":  video.get("user", {}).get("name", "Pexels"),
        }
    except Exception as e:
        log.warning("Pexels video search failed for '%s': %s", query, e)
        return None


# ── Draft creation ─────────────────────────────────────────────────────────────

def create_draft(topic: str) -> dict:
    """Generate a caption and find video (preferred) or photo content for TikTok."""
    if not is_available():
        return _not_available()
    try:
        caption = _generate_tiktok_caption(topic)

        video = _fetch_pexels_video(topic)
        if video:
            return {
                "ok":            True,
                "platform":      PLATFORM,
                "content":       caption,
                "image_url":     video["video_url"],
                "thumbnail_url": video["thumbnail_url"],
            }

        image_url, _desc = instagram._get_public_image_url(topic)
        return {
            "ok":        True,
            "platform":  PLATFORM,
            "content":   caption,
            "image_url": image_url,
        }
    except Exception as e:
        log.error("TikTok draft failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Posting ────────────────────────────────────────────────────────────────────

def _poll_publish_status(publish_id: str, max_wait: int) -> dict:
    """Poll TikTok's publish status endpoint until terminal state or timeout."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        try:
            r = requests.post(
                f"{TIKTOK_API}/post/publish/status/fetch/",
                headers=_headers(),
                json={"publish_id": publish_id},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json().get("data", {})
            status = data.get("status")
            if status == "PUBLISH_COMPLETE":
                return {"ok": True, "media_id": publish_id}
            if status == "FAILED":
                return {"ok": False, "error": data.get("fail_reason", "TikTok publish failed")}
        except Exception as e:
            log.warning("TikTok status poll failed: %s", e)
    return {"ok": False, "error": "Timed out waiting for TikTok to finish publishing"}


def _post_photo(content: str, image_url: str) -> dict:
    try:
        r = requests.post(
            f"{TIKTOK_API}/post/publish/content/init/",
            headers=_headers(),
            json={
                "post_info": {"title": content[:2200], "privacy_level": "PUBLIC_TO_EVERYONE"},
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "photo_cover_index": 0,
                    "photo_images": [image_url],
                },
                "post_mode": "DIRECT_POST",
                "media_type": "PHOTO",
            },
            timeout=20,
        )
        r.raise_for_status()
        publish_id = r.json().get("data", {}).get("publish_id")
        if not publish_id:
            return {"ok": False, "error": f"TikTok did not return a publish_id: {r.text[:200]}"}
        return _poll_publish_status(publish_id, max_wait=60)
    except Exception as e:
        log.error("TikTok photo post failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def _post_video(content: str, video_url: str) -> dict:
    try:
        r = requests.post(
            f"{TIKTOK_API}/post/publish/video/init/",
            headers=_headers(),
            json={
                "post_info": {"title": content[:2200], "privacy_level": "PUBLIC_TO_EVERYONE"},
                "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
                "post_mode": "DIRECT_POST",
            },
            timeout=20,
        )
        r.raise_for_status()
        publish_id = r.json().get("data", {}).get("publish_id")
        if not publish_id:
            return {"ok": False, "error": f"TikTok did not return a publish_id: {r.text[:200]}"}
        return _poll_publish_status(publish_id, max_wait=90)
    except Exception as e:
        log.error("TikTok video post failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def post_now(content: str, image_url: str) -> dict:
    """Publish to TikTok. Detects video vs photo from the media URL."""
    if not is_available():
        return _not_available()
    if image_url.lower().endswith((".mp4", ".mov", ".webm")) or "pexels-video" in image_url.lower() or "/video-files/" in image_url.lower():
        return _post_video(content, image_url)
    return _post_photo(content, image_url)


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(period: str = "last_7_days") -> dict:
    """Basic creator info only — detailed analytics needs business account verification."""
    if not is_available():
        return _not_available()
    try:
        r = requests.get(
            f"{TIKTOK_API}/user/info/",
            headers=_headers(),
            params={"fields": "display_name,follower_count,video_count,likes_count"},
            timeout=10,
        )
        r.raise_for_status()
        user = r.json().get("data", {}).get("user", {})
        return {
            "ok":       True,
            "platform": PLATFORM,
            "account": {
                "followers":   user.get("follower_count", "N/A"),
                "total_posts": user.get("video_count", "N/A"),
                "total_likes": user.get("likes_count", "N/A"),
            },
            "insights_note": "Detailed video-level analytics requires TikTok business account verification.",
        }
    except Exception as e:
        log.error("TikTok analytics fetch failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Comments (not yet supported by this integration) ──────────────────────────

def get_unanswered_comments() -> dict:
    return {"ok": False, "error": "TikTok comment management coming soon."}


def reply_to_comment(comment_id: str, reply_text: str) -> dict:
    return {"ok": False, "error": "TikTok comment replies coming soon."}
