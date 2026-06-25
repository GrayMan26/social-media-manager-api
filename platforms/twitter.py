"""
Twitter/X platform module.
Posts short text + image via the X API. Media upload still requires the
v1.1 endpoint (OAuth 1.0a signed); tweet creation uses v2. tweepy handles
both the OAuth1 signing and the v2 client so we don't hand-roll HMAC-SHA1.
"""
import io
import logging
import os

import anthropic
import requests
import tweepy

from platforms import instagram

log = logging.getLogger(__name__)

PLATFORM = "twitter"


# ── Credentials (from .env) ───────────────────────────────────────────────────

def _api_key()       -> str: return os.getenv("TWITTER_API_KEY", "")
def _api_secret()    -> str: return os.getenv("TWITTER_API_SECRET", "")
def _access_token()  -> str: return os.getenv("TWITTER_ACCESS_TOKEN", "")
def _access_secret() -> str: return os.getenv("TWITTER_ACCESS_TOKEN_SECRET", "")


def is_available() -> bool:
    return bool(_api_key() and _api_secret() and _access_token() and _access_secret())


def _not_available():
    return {
        "ok": False,
        "error": "Twitter/X is not configured. Set TWITTER_API_KEY, TWITTER_API_SECRET, "
                 "TWITTER_ACCESS_TOKEN, and TWITTER_ACCESS_TOKEN_SECRET.",
    }


def _client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=_api_key(), consumer_secret=_api_secret(),
        access_token=_access_token(), access_token_secret=_access_secret(),
    )


def _api_v1() -> tweepy.API:
    auth = tweepy.OAuth1UserHandler(_api_key(), _api_secret(), _access_token(), _access_secret())
    return tweepy.API(auth)


# ── Caption generation (Twitter style — 280 char hard limit) ─────────────────

def _generate_tweet_caption(topic: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return f"{topic} #SeniorCare #CaregiverLife"[:280]

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"You write Twitter/X posts for GrayTech Inc., a senior caregiving resource account "
        f"that supports family caregivers, CNAs, and families caring for aging loved ones.\n\n"
        f"Write a tweet for this topic: {topic}\n\n"
        f"Rules:\n"
        f"- Hard limit 280 characters total, including hashtags. Aim for under 250 to be safe.\n"
        f"- Punchy and direct, 1-3 short sentences\n"
        f"- Do NOT use em dashes (—). Use a comma or period instead.\n"
        f"- Include only 1-2 hashtags (more crowds out the text at this length): "
        f"#SeniorCare #CaregiverLife #ElderCare #HomeCare\n"
        f"- Return ONLY the tweet text. No extra commentary."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    caption = resp.content[0].text.strip()
    caption = caption.replace("—", ",").replace("–", "-")
    return caption[:280]


# ── Draft creation ─────────────────────────────────────────────────────────────

def create_draft(topic: str, include_image: bool = True) -> dict:
    """Generate a caption and, optionally, find a matching image for the given topic."""
    if not is_available():
        return _not_available()
    try:
        caption = _generate_tweet_caption(topic)
        image_url = ""
        if include_image:
            image_url, _desc = instagram._get_public_image_url(topic)
        return {
            "ok":        True,
            "platform":  PLATFORM,
            "content":   caption,
            "image_url": image_url,
        }
    except Exception as e:
        log.error("Twitter draft failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Posting ────────────────────────────────────────────────────────────────────

def post_now(content: str, image_url: str) -> dict:
    """
    Publish a tweet, with an attached image if one is available.
    Media upload (v1.1) is billed separately from text posting on X's API and can
    402 if the account has no upload credits — fall back to a text-only tweet rather
    than failing the whole post.
    """
    if not is_available():
        return _not_available()

    media_ids = []
    if image_url:
        try:
            img_bytes = requests.get(image_url, timeout=30).content
            media = _api_v1().media_upload(filename="post.jpg", file=io.BytesIO(img_bytes))
            media_ids = [media.media_id]
        except Exception as e:
            log.warning("Twitter media upload failed, posting text-only: %s", e)

    try:
        resp = _client().create_tweet(text=content[:280], media_ids=media_ids or None)
        result = {"ok": True, "media_id": resp.data["id"]}
        if image_url and not media_ids:
            result["warning"] = "Posted without the image — media upload failed (likely needs X API credits)."
        return result
    except Exception as e:
        log.error("Twitter tweet creation failed: %s", e, exc_info=True)
        return {"ok": False, "error": f"Tweet creation failed (not the image): {e}"}


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(period: str = "last_7_days") -> dict:
    """Basic profile counts only — tweet-level engagement metrics need a paid X API tier."""
    if not is_available():
        return _not_available()
    try:
        me = _client().get_me(user_fields=["public_metrics"])
        metrics = me.data.public_metrics
        return {
            "ok":       True,
            "platform": PLATFORM,
            "account": {
                "followers":   metrics.get("followers_count", "N/A"),
                "total_posts": metrics.get("tweet_count", "N/A"),
            },
            "insights_note": "Tweet-level engagement metrics require a paid X API tier.",
        }
    except Exception as e:
        log.error("Twitter analytics fetch failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Comments / mentions (gated to paid X API tiers) ───────────────────────────

def get_unanswered_comments() -> dict:
    return {"ok": False, "error": "Twitter/X mention replies require a paid API tier. Coming later."}


def reply_to_comment(comment_id: str, reply_text: str) -> dict:
    return {"ok": False, "error": "Twitter/X mention replies require a paid API tier. Coming later."}
