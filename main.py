"""
Social Media Manager API
FastAPI server with a WebSocket endpoint for the web chat UI.
"""
import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

import database as db
import scheduler
import manager_agent
from platforms import instagram, facebook, twitter, linkedin, tiktok

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

PLATFORMS = {
    "instagram": instagram,
    "facebook":  facebook,
    "twitter":   twitter,
    "linkedin":  linkedin,
    "tiktok":    tiktok,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start()
    log.info("Social Media Manager API started")
    yield
    scheduler.stop()


app = FastAPI(title="Social Media Manager API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST: status ──────────────────────────────────────────────────────────────

@app.get("/status")
def status():
    """Quick health check — shows which platforms are active."""
    return {
        "ok": True,
        "platforms": {name: mod.is_available() for name, mod in PLATFORMS.items()},
    }


# ── REST: post approval (also handled via WebSocket, but REST is easier for testing) ──

@app.post("/posts/{post_id}/approve")
async def approve_post(post_id: int):
    post = db.get_post(post_id)
    if not post:
        return {"ok": False, "error": "Post not found"}
    db.approve_post(post_id)

    # If no scheduled time, post immediately
    if not post.get("scheduled_at"):
        mod = PLATFORMS.get(post["platform"])
        if mod and mod.is_available():
            try:
                result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, mod.post_now, post["content"], post.get("image_url", "")
                    ),
                    timeout=45,
                )
            except asyncio.TimeoutError:
                return {
                    "ok": False,
                    "error": f"Posting to {post['platform']} is taking longer than expected. "
                             f"Check your {post['platform']} account directly before retrying — "
                             f"it may have actually gone through.",
                }
            if result.get("ok"):
                db.mark_posted(post_id)
                return {"ok": True, "posted": True, "media_id": result.get("media_id")}
            return {"ok": False, "error": result.get("error")}
    return {"ok": True, "posted": False, "scheduled_at": post.get("scheduled_at")}


@app.post("/posts/{post_id}/reject")
def reject_post(post_id: int):
    db.reject_post(post_id)
    return {"ok": True}


@app.get("/posts")
def list_posts(status: str = "pending_approval"):
    return db.get_posts_by_status(status)


# ── WebSocket chat ─────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    await websocket.accept()
    log.info("WebSocket connected: session=%s", session_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            msg_type = msg.get("type")

            # ── User sent a chat message ──────────────────────────────────────
            if msg_type == "message":
                user_text = msg.get("content", "").strip()
                if not user_text:
                    continue

                async def send_token(chunk: str):
                    await websocket.send_text(json.dumps({"type": "token", "content": chunk}))

                async def send_event(event: dict):
                    await websocket.send_text(json.dumps(event))

                try:
                    await manager_agent.run_turn(session_id, user_text, send_token, send_event)
                except Exception as e:
                    log.error("Agent error: %s", e, exc_info=True)
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Something went wrong. Please try again.",
                    }))

                await websocket.send_text(json.dumps({"type": "done"}))

            # ── User approved a post ──────────────────────────────────────────
            elif msg_type == "approve":
                post_id = msg.get("post_id")
                post = db.get_post(post_id)
                if not post:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Post not found"}))
                    continue

                db.approve_post(post_id)

                if not post.get("scheduled_at"):
                    mod = PLATFORMS.get(post["platform"])
                    if mod and mod.is_available():
                        try:
                            result = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(
                                    None, mod.post_now, post["content"], post.get("image_url", "")
                                ),
                                timeout=45,
                            )
                        except asyncio.TimeoutError:
                            await websocket.send_text(json.dumps({
                                "type": "post_result",
                                "post_id": post_id,
                                "success": False,
                                "message": f"Posting to {post['platform']} is taking longer than expected. "
                                           f"Check your {post['platform']} account directly before retrying — "
                                           f"it may have actually gone through.",
                            }))
                            continue
                        if result.get("ok"):
                            db.mark_posted(post_id)
                            msg = f"Posted to {post['platform']} successfully!"
                            if result.get("warning"):
                                msg += f" ({result['warning']})"
                            await websocket.send_text(json.dumps({
                                "type": "post_result",
                                "post_id": post_id,
                                "platform": post["platform"],
                                "success": True,
                                "message": msg,
                            }))
                        else:
                            await websocket.send_text(json.dumps({
                                "type": "post_result",
                                "post_id": post_id,
                                "success": False,
                                "message": f"Post failed: {result.get('error')}",
                            }))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "post_result",
                            "post_id": post_id,
                            "success": False,
                            "message": f"{post['platform']} is not configured.",
                        }))
                else:
                    await websocket.send_text(json.dumps({
                        "type": "post_result",
                        "post_id": post_id,
                        "success": True,
                        "message": f"Scheduled for {post['scheduled_at']} on {post['platform']}.",
                    }))

            # ── User rejected a post ──────────────────────────────────────────
            elif msg_type == "reject":
                post_id = msg.get("post_id")
                db.reject_post(post_id)
                await websocket.send_text(json.dumps({
                    "type": "post_result",
                    "post_id": post_id,
                    "success": False,
                    "message": "Post rejected.",
                }))

    except WebSocketDisconnect:
        log.info("WebSocket disconnected: session=%s", session_id)
    except Exception as e:
        log.error("WebSocket error: %s", e, exc_info=True)
