"""
Social Media Manager Agent.

Uses LangGraph with Claude tool calling. The agent receives natural-language
instructions, decides which platforms and actions to use, drafts content,
and pauses for human approval before posting anything.

The graph exposes a single entry point: run_turn(session_id, user_message, websocket)
It streams tokens back through the websocket and emits an "approval_needed"
event when a post is ready for review.
"""
import json
import logging
import os

import anthropic

import database as db
from platforms import instagram, facebook, twitter, linkedin, tiktok

log = logging.getLogger(__name__)

_client       = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
_async_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

# Registry of platform modules — add new ones here when they're built
PLATFORMS = {
    "instagram": instagram,
    "facebook":  facebook,
    "twitter":   twitter,
    "linkedin":  linkedin,
    "tiktok":    tiktok,
}


# ── Tool definitions (given to Claude) ────────────────────────────────────────

TOOLS = [
    {
        "name": "draft_post",
        "description": (
            "Generate a social media post draft for one or more platforms on a given topic. "
            "Instagram posts a photo with a caption. TikTok posts either a short-form video "
            "(preferred, sourced automatically) or a photo carousel, with a TikTok-style caption. "
            "Twitter/X posts a short text caption (280 character limit), normally with an image, "
            "but the image is optional for Twitter/X specifically — use include_image=false if the "
            "user asks for a text-only tweet or if image posting isn't working. "
            "The draft will be shown to the user for approval before anything is posted. "
            "Use this whenever the user asks to create, write, or post content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The topic or message for the post (e.g. 'tips for preventing caregiver burnout', 'CNA job openings', 'senior home care statistics')",
                },
                "platforms": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["instagram", "facebook", "twitter", "linkedin", "tiktok"]},
                    "description": "Which platforms to create posts for. Use all available ones if not specified.",
                },
                "include_image": {
                    "type": "boolean",
                    "description": "Only applies to Twitter/X. Whether to attach an image. Instagram and TikTok always include media regardless of this flag. Default true.",
                },
                "scheduled_at": {
                    "type": "string",
                    "description": "ISO 8601 datetime to post (e.g. '2026-06-10T10:00:00'). Leave empty to post immediately after approval.",
                },
            },
            "required": ["topic", "platforms"],
        },
    },
    {
        "name": "get_analytics",
        "description": "Get performance metrics (reach, impressions, likes, engagement) for one or all platforms.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": ["instagram", "facebook", "twitter", "linkedin", "tiktok", "all"],
                    "description": "Which platform to get analytics for. Use 'all' for a summary across all platforms.",
                },
                "period": {
                    "type": "string",
                    "description": "Time period, e.g. 'last_7_days', 'last_30_days'.",
                    "default": "last_7_days",
                },
            },
            "required": ["platform"],
        },
    },
    {
        "name": "generate_content_plan",
        "description": (
            "Create a weekly social media content calendar. "
            "Returns a day-by-day plan with post topics and suggested platforms."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "Main theme for the week (e.g. 'caregiver burnout awareness', 'CNA recruitment', 'home care vs assisted living').",
                },
                "platforms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Platforms to include in the plan.",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days to plan for (default 7).",
                    "default": 7,
                },
            },
            "required": ["focus", "platforms"],
        },
    },
    {
        "name": "get_pending_replies",
        "description": "Get unanswered comments or DMs on a platform that need a reply.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": ["instagram", "facebook", "twitter", "linkedin", "tiktok"],
                },
            },
            "required": ["platform"],
        },
    },
    {
        "name": "send_reply",
        "description": "Reply to a comment on a social media post.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": ["instagram", "facebook", "twitter", "linkedin", "tiktok"],
                },
                "comment_id": {"type": "string", "description": "The ID of the comment to reply to."},
                "reply_text": {"type": "string", "description": "The reply message to send."},
            },
            "required": ["platform", "comment_id", "reply_text"],
        },
    },
]


# ── Tool execution ─────────────────────────────────────────────────────────────

def _execute_tool(name: str, inputs: dict, pending_approval_callback) -> str:
    """
    Execute a tool and return a text result for Claude.
    pending_approval_callback(post_dict) is called when a draft needs approval.
    """
    if name == "draft_post":
        return _tool_draft_post(inputs, pending_approval_callback)
    elif name == "get_analytics":
        return _tool_get_analytics(inputs)
    elif name == "generate_content_plan":
        return _tool_generate_content_plan(inputs)
    elif name == "get_pending_replies":
        return _tool_get_pending_replies(inputs)
    elif name == "send_reply":
        return _tool_send_reply(inputs)
    return f"Unknown tool: {name}"


def _tool_draft_post(inputs: dict, pending_approval_callback) -> str:
    topic = inputs["topic"]
    platforms = inputs.get("platforms", ["instagram"])
    scheduled_at = inputs.get("scheduled_at")
    include_image = inputs.get("include_image", True)

    results = []
    for platform_name in platforms:
        mod = PLATFORMS.get(platform_name)
        if not mod:
            results.append(f"{platform_name}: not recognized")
            continue
        if not mod.is_available():
            results.append(f"{platform_name}: not configured yet")
            continue

        draft = (
            mod.create_draft(topic, include_image=include_image)
            if platform_name == "twitter"
            else mod.create_draft(topic)
        )
        if not draft.get("ok"):
            results.append(f"{platform_name}: could not generate draft — {draft.get('error')}")
            continue

        post_id = db.create_post(
            platform=platform_name,
            content=draft["content"],
            image_url=draft.get("image_url", ""),
            scheduled_at=scheduled_at,
        )

        post_preview = {
            "post_id":       post_id,
            "platform":      platform_name,
            "content":       draft["content"],
            "image_url":     draft.get("image_url", ""),
            "thumbnail_url": draft.get("thumbnail_url", ""),
            "scheduled_at":  scheduled_at or "immediately after approval",
        }
        pending_approval_callback(post_preview)
        results.append(f"{platform_name}: draft ready (post_id={post_id})")

    return "\n".join(results)


def _tool_get_analytics(inputs: dict) -> str:
    platform_name = inputs["platform"]
    period = inputs.get("period", "last_7_days")

    if platform_name == "all":
        parts = []
        for name, mod in PLATFORMS.items():
            if mod.is_available():
                data = mod.get_analytics(period)
                parts.append(f"=== {name.upper()} ===\n{json.dumps(data, indent=2)}")
            else:
                parts.append(f"=== {name.upper()} ===\nNot configured yet.")
        return "\n\n".join(parts) if parts else "No platforms are configured yet."

    mod = PLATFORMS.get(platform_name)
    if not mod:
        return f"Unknown platform: {platform_name}"
    if not mod.is_available():
        return f"{platform_name} is not configured yet."
    data = mod.get_analytics(period)
    return json.dumps(data, indent=2)


def _tool_generate_content_plan(inputs: dict) -> str:
    focus = inputs["focus"]
    platforms = inputs.get("platforms", ["instagram"])
    days = inputs.get("days", 7)

    available = [p for p in platforms if PLATFORMS.get(p) and PLATFORMS[p].is_available()]
    unavailable = [p for p in platforms if p not in available]

    prompt = (
        f"Create a {days}-day social media content calendar for GrayTech Inc., "
        f"a senior caregiving resource account that supports family caregivers, CNAs, "
        f"and families caring for aging loved ones.\n\n"
        f"Weekly focus: {focus}\n"
        f"Platforms: {', '.join(available) if available else 'Instagram'}\n\n"
        f"For each day, provide:\n"
        f"- Day and date offset (Day 1, Day 2, etc.)\n"
        f"- Platform(s)\n"
        f"- Post type (news/awareness, caregiver tip, or community/recruitment)\n"
        f"- Post topic/angle\n"
        f"- Brief content idea (1-2 sentences)\n"
        f"- Best time to post\n\n"
        f"Vary the post types across the week. Keep the tone warm and informative. "
        f"Focus on senior care news, practical caregiving tips, and caregiver appreciation. "
        f"Format as a clear table or list."
    )

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    plan_text = response.content[0].text
    if unavailable:
        plan_text += f"\n\nNote: {', '.join(unavailable)} not yet configured and excluded from this plan."
    return plan_text


def _tool_get_pending_replies(inputs: dict) -> str:
    platform_name = inputs["platform"]
    mod = PLATFORMS.get(platform_name)
    if not mod:
        return f"Unknown platform: {platform_name}"
    if not mod.is_available():
        return f"{platform_name} is not configured yet."
    data = mod.get_unanswered_comments()
    if not data.get("ok"):
        return f"Error: {data.get('error')}"
    comments = data.get("comments", [])
    if not comments:
        return "No unanswered comments found."
    lines = [f"Found {len(comments)} comment(s) that may need a reply:\n"]
    for c in comments[:10]:
        lines.append(
            f"- ID: {c.get('id')}  |  @{c.get('username')}  |  \"{c.get('text','')[:80]}\""
            f"\n  (on post: {c.get('post_caption','')[:50]})"
        )
    return "\n".join(lines)


def _tool_send_reply(inputs: dict) -> str:
    platform_name = inputs["platform"]
    comment_id    = inputs["comment_id"]
    reply_text    = inputs["reply_text"]

    mod = PLATFORMS.get(platform_name)
    if not mod:
        return f"Unknown platform: {platform_name}"
    if not mod.is_available():
        return f"{platform_name} is not configured yet."

    result = mod.reply_to_comment(comment_id, reply_text)
    if result.get("ok"):
        return f"Reply sent successfully on {platform_name}."
    return f"Reply failed: {result.get('error')}"


# ── Main agent runner ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Social Media Manager for GrayTech Inc., a senior caregiving
resource account that supports family caregivers, professional CNAs, home care workers,
and families caring for aging loved ones.

Your job is to help manage social media presence across Instagram, Facebook, Twitter/X,
LinkedIn, and TikTok. You post three types of content:
1. Senior care news and awareness — policy changes, statistics, healthcare developments
2. Daily tips for family caregivers — practical advice on home care, dementia, burnout
3. Caregiver community posts — job opportunities for CNAs, caregiver appreciation

You:
- Create posts when asked, always showing a preview for approval before posting
- Check analytics and summarize performance in plain English
- Generate weekly content plans focused on senior caregiving
- Help draft replies to comments from caregivers and families

Currently available platforms: Instagram, TikTok, Twitter/X (Facebook, LinkedIn coming soon).
TikTok posts are short-form video when a suitable clip is found, otherwise a photo carousel.
Twitter/X posts are short text (280 character limit), normally with an image, but the
image is optional — if the user asks for a text-only tweet, or if image posting is failing,
use draft_post with include_image=false rather than refusing.

Always be warm, informative, and clear. Speak directly to caregivers and families.
When a platform is not yet configured, let the user know and focus on what IS available.

When you call draft_post, the user will see a preview card and can approve or reject it
before anything is posted — so always go ahead and create the draft."""


async def run_turn(
    session_id: str,
    user_message: str,
    send_token,       # async callable(str) — streams text chunks
    send_event,       # async callable(dict) — sends structured events
) -> str:
    """
    Run one conversation turn. Streams Claude's response token-by-token.
    Returns the full assistant text.

    send_token(chunk: str)   — called for each streamed text chunk
    send_event(event: dict)  — called for structured events (approval_needed, etc.)
    """
    # Load conversation history
    history = db.get_history(session_id, limit=20)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_message})

    # Collect approval previews triggered during tool execution
    approval_events = []

    def on_approval_needed(post_preview: dict):
        approval_events.append(post_preview)

    full_response = ""

    # Agentic loop — Claude may call multiple tools before giving a final answer
    while True:
        # Stream Claude's response
        tool_calls = []
        current_text = ""

        async with _async_client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        chunk = event.delta.text
                        current_text += chunk
                        full_response += chunk
                        await send_token(chunk)

            # Get the complete message after streaming
            final_message = await stream.get_final_message()

        # Check if Claude wants to call tools
        tool_use_blocks = [b for b in final_message.content if b.type == "tool_use"]

        if not tool_use_blocks:
            # No tool calls — this is the final answer
            break

        # Add assistant message to conversation
        messages.append({"role": "assistant", "content": final_message.content})

        # Execute each tool
        tool_results = []
        for tool_block in tool_use_blocks:
            result_text = _execute_tool(tool_block.name, tool_block.input, on_approval_needed)

            # Send any queued approval events to the frontend
            for preview in approval_events:
                await send_event({"type": "approval_needed", "post": preview})
            approval_events.clear()

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": result_text,
            })

        # Add tool results back to conversation
        messages.append({"role": "user", "content": tool_results})

    # Save to history
    db.save_message(session_id, "user", user_message)
    db.save_message(session_id, "assistant", full_response)

    return full_response
