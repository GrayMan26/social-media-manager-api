"""
Scheduler — checks for approved posts that are due and executes them.
APScheduler runs a job every 60 seconds on server startup.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
from platforms import instagram, facebook, twitter, linkedin, tiktok

log = logging.getLogger(__name__)

PLATFORMS = {
    "instagram": instagram,
    "facebook":  facebook,
    "twitter":   twitter,
    "linkedin":  linkedin,
    "tiktok":    tiktok,
}

_scheduler = AsyncIOScheduler()


def post_due_items():
    """Called every minute. Finds approved posts whose time has come and posts them."""
    due = db.get_due_posts()
    if not due:
        return

    for post in due:
        platform_name = post["platform"]
        mod = PLATFORMS.get(platform_name)
        if not mod or not mod.is_available():
            log.warning("Scheduler: platform %s not available for post %s", platform_name, post["id"])
            continue

        log.info("Scheduler: posting id=%s to %s", post["id"], platform_name)
        result = mod.post_now(post["content"], post.get("image_url", ""))

        if result.get("ok"):
            db.mark_posted(post["id"])
            log.info("Scheduler: post %s published successfully", post["id"])
        else:
            log.error("Scheduler: post %s failed — %s", post["id"], result.get("error"))


def start():
    _scheduler.add_job(post_due_items, "interval", seconds=60, id="post_due_items", replace_existing=True)
    _scheduler.start()
    log.info("Scheduler started — checking for due posts every 60 seconds")


def stop():
    _scheduler.shutdown(wait=False)
