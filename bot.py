
#!/usr/bin/env python3
"""Fast Telegram join-request approval bot.

The bot intentionally keeps the public surface small:
  /start   - private welcome + configured start post
  /help    - setup instructions
  /approve - the same setup instructions
  /raj     - admin panel

Secrets are read only from environment variables. Never put BOT_TOKEN or
MONGO_URI in this file or commit a real .env file.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gridfs
import telebot
from pymongo import ASCENDING, MongoClient
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def parse_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                logging.warning("Ignoring invalid admin ID: %s", part)
    return result


BOT_TOKEN = required_env("BOT_TOKEN")
MONGO_URI = required_env("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "auto_request_approve_bot")
OWNER_ID = int_env("OWNER_ID", 7981894574)
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "rajfflive").lstrip("@")
EXTRA_ADMIN_IDS = parse_ids(os.getenv("ADMIN_IDS", ""))
INFO_BOT_URL = os.getenv("INFO_BOT_URL", "https://t.me/rajfflivebot")
JOIN_CHANNEL_URL = os.getenv(
    "JOIN_CHANNEL_URL",
    "https://t.me/+A6klPh9Ms-MwYjBl",
)
REQUEST_TTL_SECONDS = max(60, int_env("REQUEST_TTL_SECONDS", 290))
RETRY_DELAY_SECONDS = max(0.15, float(os.getenv("RETRY_DELAY_SECONDS", "0.35")))
MAX_APPROVE_ATTEMPTS = max(5, int_env("MAX_APPROVE_ATTEMPTS", 240))
BRAND_ASSET = ASSETS / "raj-bots.png"
HELP_ASSET = ASSETS / "how-to-add-bot.jpg"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logging.getLogger("telebot").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=16)
mongo = MongoClient(
    MONGO_URI,
    maxPoolSize=50,
    serverSelectionTimeoutMS=7000,
    connectTimeoutMS=7000,
)
db = mongo[MONGO_DB]
settings_col = db["settings"]
chats_col = db["chats"]
users_col = db["users"]
events_col = db["join_events"]
admins_col = db["admins"]
media_col = db["media"]
stats_col = db["approval_stats"]
media_bucket = gridfs.GridFSBucket(db)


# ---------------------------------------------------------------------------
# Text and keyboards
# ---------------------------------------------------------------------------

WELCOME_TEXT = (
    "Ye Hey 𝐑𝐀𝐉 !!!!\n\n"
    "I'm Auto Request Approve Bot🤖. i can also APPROVE TG Pending Join Requests.\n\n"
    "if any help use use /help"
)

DEFAULT_REQUEST_TEXT = (
    "<b>👋 Hello {first}!</b>\n\n"
    "Your join request for <b>{chat_title}</b> has been received.\n"
    "The bot will approve it automatically in a moment.\n\n"
    "<i>Please keep this chat open until the request is accepted.</i>"
)

HELP_TEXT = (
    "<b>How to use Auto Request Approve Bot</b>\n\n"
    "<b>1. Add the bot</b>\n"
    "Open the bot profile and tap <b>Add to Group or Channel</b>.\n"
    "Choose your group or channel, then confirm.\n\n"
    "<b>2. Give the required admin permission</b>\n"
    "Promote the bot as an administrator. It needs permission to "
    "<b>Invite Users via Link</b> / manage join requests.\n"
    "For a channel, add it as an administrator with the permission to "
    "invite subscribers.\n\n"
    "<b>3. Turn on auto mode</b>\n"
    "Send <code>/raj</code> to this bot in private chat, open <b>My Chats</b>, "
    "select your chat, and switch <b>Auto Mode</b> on.\n\n"
    "<b>4. Use an approval invite link</b>\n"
    "Create or edit an invite link and enable <b>Approve New Members</b>. "
    "Requests received through that link are sent a message first and then "
    "approved with a fast retry loop before Telegram expires the request.\n\n"
    "<b>What /approve means</b>\n"
    "<code>/approve</code> opens these same setup steps. The bot does not "
    "need a separate approve command in your group; approval is automatic "
    "when Auto Mode is enabled.\n\n"
    "<b>Important</b>\n"
    "The bot must remain an administrator and Telegram must still allow "
    "the user to be contacted through the join request."
)


class StyledButton(InlineKeyboardButton):
    """Preserve the primary style used by the original bot when supported."""

    def __init__(self, text: str, style: str = "primary", **kwargs: Any) -> None:
        super().__init__(text=text, **kwargs)
        self._button_style = style

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["style"] = self._button_style
        return data


def button(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str = "primary",
) -> InlineKeyboardButton:
    if callback_data is not None:
        return StyledButton(text, style=style, callback_data=callback_data)
    return StyledButton(text, style=style, url=url)


def add_bot_buttons() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        button(
            "➕ Add to Group",
            url=bot_add_url("group"),
            style="primary",
        ),
        button(
            "📢 Add to Channel",
            url=bot_add_url("channel"),
            style="primary",
        ),
        button("📖 How to use /approve", callback_data="show_help", style="primary"),
        button("ℹ️ Info Bot", url=INFO_BOT_URL, style="primary"),
    )
    return keyboard


def bot_add_url(kind: str = "group") -> str:
    username = BOT_USERNAME or os.getenv("BOT_USERNAME", "autorequestapprovebot")
    suffix = "startchannel=true" if kind == "channel" else "startgroup=true"
    return f"https://t.me/{username}?{suffix}"


def help_buttons() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        button("➕ Add to Group", url=bot_add_url("group"), style="primary"),
        button("📢 Add to Channel", url=bot_add_url("channel"), style="primary"),
        button("ℹ️ Info Bot", url=INFO_BOT_URL, style="primary"),
        button("◀️ Back", callback_data="back_home", style="primary"),
    )
    return keyboard


def request_buttons() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        button("ℹ️ Info Bot", url=INFO_BOT_URL, style="primary"),
        button("🔗 ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ", url=JOIN_CHANNEL_URL, style="primary"),
        button("📖 Help: how to add the bot", callback_data="show_help", style="primary"),
    )
    return keyboard


def admin_keyboard(is_owner: bool) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        button("📋 My Chats", callback_data="my_chats", style="primary"),
        button("📊 Global Stats", callback_data="global_stats", style="success"),
        button("📝 Request Message", callback_data="edit_request_text", style="primary"),
        button("🖼 Preview Request", callback_data="preview_request", style="success"),
        button("🔗 Start Post", callback_data="set_start_post", style="primary"),
        button("📚 Help Preview", callback_data="show_help", style="primary"),
    )
    if is_owner:
        keyboard.add(
            button("➕ Add Bot Admin", callback_data="add_bot_admin", style="primary"),
            button("➖ Remove Bot Admin", callback_data="remove_bot_admin", style="danger"),
            button("🌐 All Chats", callback_data="all_chats", style="primary"),
        )
    keyboard.add(button("◀️ Back", callback_data="back_home", style="danger"))
    return keyboard


def chat_keyboard(chat_id: int, auto_mode: bool) -> InlineKeyboardMarkup:
    state = "✅ ON" if auto_mode else "❌ OFF"
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        button(f"⚙️ Auto Mode {state}", callback_data=f"toggle_auto:{chat_id}", style="primary"),
        button("📊 Chat Stats", callback_data=f"chat_stats:{chat_id}", style="success"),
        button("🔗 Set Start Post", callback_data=f"set_start_post:{chat_id}", style="primary"),
        button("📝 Set Req. Text", callback_data=f"set_req_text:{chat_id}", style="primary"),
        button("👁 Preview", callback_data=f"preview_chat:{chat_id}", style="success"),
        button("◀️ My Chats", callback_data="my_chats", style="danger"),
    )
    return keyboard


# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def initialize_database() -> None:
    settings_col.create_index([("key", ASCENDING)], unique=True)
    chats_col.create_index([("chat_id", ASCENDING)], unique=True)
    users_col.create_index([("user_id", ASCENDING)], unique=True)
    events_col.create_index([("chat_id", ASCENDING), ("created_at", ASCENDING)])
    admins_col.create_index([("user_id", ASCENDING)], unique=True)
    stats_col.create_index([("chat_id", ASCENDING), ("date", ASCENDING)], unique=True)
    seed_media("brand_image", BRAND_ASSET, "raj-bots.png")
    seed_media("help_image", HELP_ASSET, "how-to-add-bot.jpg")


def seed_media(key: str, path: Path, filename: str) -> None:
    if not path.exists():
        logging.warning("Asset not found, using text fallback: %s", path)
        return
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    existing = media_col.find_one({"_id": key})
    if existing and existing.get("sha256") == digest:
        return
    if existing and existing.get("file_id"):
        try:
            media_bucket.delete(existing["file_id"])
        except Exception:
            pass
    file_id = media_bucket.upload_from_stream(
        filename,
        io.BytesIO(payload),
        metadata={"asset_key": key, "sha256": digest, "content_type": "image"},
    )
    media_col.replace_one(
        {"_id": key},
        {
            "_id": key,
            "file_id": file_id,
            "filename": filename,
            "sha256": digest,
            "updated_at": utc_now(),
        },
        upsert=True,
    )
    logging.info("Saved %s to MongoDB GridFS", filename)


def load_media(key: str, fallback: Path) -> io.BytesIO | None:
    try:
        doc = media_col.find_one({"_id": key})
        if doc and doc.get("file_id"):
            stream = io.BytesIO()
            media_bucket.download_to_stream(doc["file_id"], stream)
            stream.seek(0)
            stream.name = doc.get("filename", fallback.name)
            return stream
    except Exception as exc:
        logging.warning("Could not load %s from GridFS: %s", key, exc)
    if fallback.exists():
        stream = io.BytesIO(fallback.read_bytes())
        stream.name = fallback.name
        return stream
    return None


def setting(key: str, default: Any = None) -> Any:
    doc = settings_col.find_one({"_id": key})
    return doc.get("value", default) if doc else default


def set_setting(key: str, value: Any) -> None:
    settings_col.update_one(
        {"_id": key},
        {"$set": {"value": value, "updated_at": utc_now()}},
        upsert=True,
    )


def request_text_for(chat_id: int | None = None) -> str:
    if chat_id is not None:
        doc = chats_col.find_one({"chat_id": chat_id})
        if doc and doc.get("request_text"):
            return str(doc["request_text"])
    return str(setting("request_text", DEFAULT_REQUEST_TEXT))


def upsert_user(user_id: int, first_name: str, username: str | None = None) -> None:
    users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "first_name": first_name,
                "username": username or "",
                "updated_at": utc_now(),
            },
            "$setOnInsert": {"created_at": utc_now(), "started": False},
        },
        upsert=True,
    )


def mark_started(user_id: int, first_name: str, username: str | None) -> None:
    upsert_user(user_id, first_name, username)
    users_col.update_one(
        {"user_id": user_id},
        {"$set": {"started": True, "started_at": utc_now()}},
    )


def chat_doc(chat_id: int) -> dict[str, Any] | None:
    return chats_col.find_one({"chat_id": chat_id})


def save_chat(chat: Any, active: bool = True) -> None:
    chats_col.update_one(
        {"chat_id": chat.id},
        {
            "$set": {
                "title": getattr(chat, "title", None) or str(chat.id),
                "username": getattr(chat, "username", None) or "",
                "chat_type": getattr(chat, "type", "unknown"),
                "active": active,
                "updated_at": utc_now(),
            },
            "$setOnInsert": {"auto_mode": True, "created_at": utc_now()},
        },
        upsert=True,
    )


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_bot_admin(user_id: int) -> bool:
    return is_owner(user_id) or user_id in EXTRA_ADMIN_IDS or bool(
        admins_col.find_one({"user_id": user_id})
    )


def chat_admin(user_id: int, chat_id: int) -> bool:
    if is_bot_admin(user_id):
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in {"administrator", "creator"}
    except Exception:
        return False


def visible_chats(user_id: int) -> list[dict[str, Any]]:
    rows = list(chats_col.find({"active": {"$ne": False}}).sort("title", ASCENDING))
    if is_bot_admin(user_id):
        return rows
    return [row for row in rows if chat_admin(user_id, int(row["chat_id"]))]


def record_event(
    *,
    chat_id: int,
    user_id: int,
    status: str,
    message_status: str,
    attempts: int = 0,
) -> None:
    events_col.insert_one(
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "status": status,
            "message_status": message_status,
            "attempts": attempts,
            "created_at": utc_now(),
        }
    )


def increment_approval(chat_id: int) -> None:
    day = utc_now().strftime("%Y-%m-%d")
    stats_col.update_one(
        {"chat_id": chat_id, "date": day},
        {"$inc": {"approved": 1}, "$set": {"updated_at": utc_now()}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Telegram send helpers
# ---------------------------------------------------------------------------

BOT_USERNAME = ""


def safe_send_photo(
    chat_id: int,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    asset_key: str = "brand_image",
    fallback_path: Path = BRAND_ASSET,
) -> Any:
    photo = load_media(asset_key, fallback_path)
    if photo is None:
        return bot.send_message(
            chat_id,
            caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    try:
        return bot.send_photo(
            chat_id,
            photo,
            caption=caption[:1024],
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except ApiTelegramException as exc:
        # Some older Telegram Bot API/library combinations do not accept the
        # optional button style field. Retry once with ordinary buttons.
        if "style" in str(exc).lower() and reply_markup is not None:
            plain = plain_keyboard(reply_markup)
            photo.seek(0)
            return bot.send_photo(
                chat_id,
                photo,
                caption=caption[:1024],
                parse_mode="HTML",
                reply_markup=plain,
            )
        raise


def plain_keyboard(keyboard: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    plain = InlineKeyboardMarkup(row_width=1)
    for row in keyboard.keyboard:
        buttons: list[InlineKeyboardButton] = []
        for item in row:
            buttons.append(
                InlineKeyboardButton(
                    text=item.text,
                    url=getattr(item, "url", None),
                    callback_data=getattr(item, "callback_data", None),
                )
            )
        plain.row(*buttons)
    return plain


def send_request_message(user_chat_id: int, chat_id: int, first_name: str) -> str:
    chat = chat_doc(chat_id) or {}
    title = html.escape(str(chat.get("title", chat_id)))
    first = html.escape(first_name or "User")
    template = request_text_for(chat_id)
    try:
        caption = template.format(first=first, chat_title=title)
    except (KeyError, ValueError):
        caption = DEFAULT_REQUEST_TEXT.format(first=first, chat_title=title)

    for attempt in range(1, 4):
        try:
            safe_send_photo(user_chat_id, caption, request_buttons())
            return "sent"
        except ApiTelegramException as exc:
            error = str(exc).lower()
            if "user is deactivated" in error or "chat not found" in error:
                return "unavailable"
            if attempt == 3:
                logging.info("Request message could not be sent: %s", exc)
                return "failed"
            time.sleep(0.2 * attempt)
        except Exception as exc:
            if attempt == 3:
                logging.info("Request message could not be sent: %s", exc)
                return "failed"
            time.sleep(0.2 * attempt)
    return "failed"


def error_is_processed(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in (
            "already approved",
            "already declined",
            "join request not found",
            "request is no longer pending",
            "user is already a participant",
            "chat not found",
            "bot is not an administrator",
            "not enough rights",
        )
    )


def retry_after_seconds(exc: ApiTelegramException) -> float | None:
    text = str(exc)
    match = re.search(r"retry after[ :]+(\d+)", text, flags=re.IGNORECASE)
    if match:
        return float(match.group(1))
    try:
        payload = getattr(exc, "result_json", None) or {}
        value = payload.get("parameters", {}).get("retry_after")
        return float(value) if value else None
    except Exception:
        return None


def approve_until_success(chat_id: int, user_id: int) -> tuple[str, int]:
    deadline = time.monotonic() + REQUEST_TTL_SECONDS
    attempts = 0
    while time.monotonic() < deadline and attempts < MAX_APPROVE_ATTEMPTS:
        attempts += 1
        try:
            bot.approve_chat_join_request(chat_id, user_id)
            increment_approval(chat_id)
            return "approved", attempts
        except ApiTelegramException as exc:
            text = str(exc)
            if error_is_processed(text):
                return "processed", attempts
            wait = retry_after_seconds(exc) or RETRY_DELAY_SECONDS
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(wait, RETRY_DELAY_SECONDS), remaining))
        except Exception as exc:
            logging.warning("Approval attempt %s failed for %s/%s: %s", attempts, chat_id, user_id, exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(RETRY_DELAY_SECONDS, remaining))
    return "expired_or_failed", attempts


# ---------------------------------------------------------------------------
# Join-request processing
# ---------------------------------------------------------------------------

executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="join-request")
inflight: set[tuple[int, int]] = set()
inflight_lock = threading.Lock()


def process_join_request(request: Any) -> None:
    chat_id = int(request.chat.id)
    user_id = int(request.from_user.id)
    key = (chat_id, user_id)
    with inflight_lock:
        if key in inflight:
            return
        inflight.add(key)

    try:
        first_name = request.from_user.first_name or "User"
        username = request.from_user.username or ""
        upsert_user(user_id, first_name, username)
        save_chat(request.chat)
        auto_mode = bool((chat_doc(chat_id) or {}).get("auto_mode", True))

        # This call happens before approval so Telegram's temporary
        # user_chat_id window is still available for the request message.
        message_status = send_request_message(
            int(request.user_chat_id),
            chat_id,
            first_name,
        )

        if auto_mode:
            status, attempts = approve_until_success(chat_id, user_id)
        else:
            status, attempts = "manual_mode", 0

        record_event(
            chat_id=chat_id,
            user_id=user_id,
            status=status,
            message_status=message_status,
            attempts=attempts,
        )
        logging.info(
            "Join request chat=%s user=%s message=%s status=%s attempts=%s",
            chat_id,
            user_id,
            message_status,
            status,
            attempts,
        )
    except Exception:
        logging.exception("Unhandled join-request error for chat=%s user=%s", chat_id, user_id)
    finally:
        with inflight_lock:
            inflight.discard(key)


@bot.chat_join_request_handler()
def handle_join_request(request: Any) -> None:
    executor.submit(process_join_request, request)


@bot.my_chat_member_handler()
def handle_bot_membership(update: Any) -> None:
    try:
        chat = update.chat
        new_status = getattr(update.new_chat_member, "status", "")
        active = new_status in {"administrator", "creator"}
        save_chat(chat, active=active)
        logging.info("Bot membership changed in %s: %s", chat.id, new_status)
    except Exception:
        logging.exception("Could not save bot membership update")


# ---------------------------------------------------------------------------
# Public screens
# ---------------------------------------------------------------------------

def send_help(chat_id: int) -> None:
    safe_send_photo(
        chat_id,
        "<b>How to add the bot</b>\nOpen the bot profile and use the buttons below.",
        help_buttons(),
        asset_key="help_image",
        fallback_path=HELP_ASSET,
    )
    bot.send_message(
        chat_id,
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=help_buttons(),
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["start"])
def start_command(message: Any) -> None:
    if message.chat.type != "private":
        return
    user = message.from_user
    mark_started(user.id, user.first_name or "User", user.username)
    start_post = setting("start_post")
    if start_post and start_post.get("chat_id") and start_post.get("message_id"):
        try:
            bot.copy_message(
                message.chat.id,
                int(start_post["chat_id"]),
                int(start_post["message_id"]),
            )
        except Exception as exc:
            logging.warning("Configured start post could not be copied: %s", exc)
    safe_send_photo(message.chat.id, WELCOME_TEXT, add_bot_buttons())


@bot.message_handler(commands=["help", "approve"])
def help_command(message: Any) -> None:
    if message.chat.type == "private":
        send_help(message.chat.id)


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def admin_text(user_id: int) -> str:
    role = "OWNER" if is_owner(user_id) else "BOT ADMIN"
    return (
        f"<blockquote>🛠️ <b>{role} PANEL</b></blockquote>\n"
        "<i>Only the auto-request controls are kept here.</i>"
    )


def edit_or_send(call: Any, text: str, **kwargs: Any) -> None:
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            **kwargs,
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode="HTML", **kwargs)


def render_admin(call: Any) -> None:
    edit_or_send(
        call,
        admin_text(call.from_user.id),
        reply_markup=admin_keyboard(is_owner(call.from_user.id)),
    )


@bot.message_handler(commands=["raj"])
def admin_command(message: Any) -> None:
    if not is_bot_admin(message.from_user.id):
        bot.reply_to(message, "❌ You are not authorized to open the admin panel.")
        return
    bot.send_message(
        message.chat.id,
        admin_text(message.from_user.id),
        parse_mode="HTML",
        reply_markup=admin_keyboard(is_owner(message.from_user.id)),
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_home")
def back_home(call: Any) -> None:
    bot.answer_callback_query(call.id)
    if call.message.chat.type == "private":
        safe_send_photo(call.message.chat.id, WELCOME_TEXT, add_bot_buttons())


@bot.callback_query_handler(func=lambda call: call.data == "show_help")
def help_callback(call: Any) -> None:
    bot.answer_callback_query(call.id)
    send_help(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "my_chats")
def my_chats_callback(call: Any) -> None:
    if not is_bot_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    rows = visible_chats(call.from_user.id)
    keyboard = InlineKeyboardMarkup(row_width=1)
    for row in rows:
        chat_id = int(row["chat_id"])
        icon = "📢" if row.get("chat_type") == "channel" else "👥"
        keyboard.add(
            button(
                f"{icon} {str(row.get('title', chat_id))[:40]}",
                callback_data=f"open_chat:{chat_id}",
                style="primary",
            )
        )
    keyboard.add(button("◀️ Back", callback_data="back_admin", style="danger"))
    text = "<b>MY CHATS</b>\nSelect a group or channel:"
    if not rows:
        text = (
            "<b>MY CHATS</b>\n\n"
            "No active chats found. Add the bot as an administrator first."
        )
    edit_or_send(call, text, reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_admin")
def back_admin_callback(call: Any) -> None:
    if is_bot_admin(call.from_user.id):
        render_admin(call)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("open_chat:"))
def open_chat_callback(call: Any) -> None:
    try:
        chat_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid chat", show_alert=True)
        return
    if not chat_admin(call.from_user.id, chat_id):
        bot.answer_callback_query(call.id, "You are not a chat admin", show_alert=True)
        return
    row = chat_doc(chat_id) or {}
    title = html.escape(str(row.get("title", chat_id)))
    auto = bool(row.get("auto_mode", True))
    start_post = row.get("start_post") or setting("start_post")
    post_state = "✅ Set" if start_post else "❌ Not set"
    text = (
        f"<b>{title}</b>\n\n"
        f"Auto Mode: <b>{'ON ✅' if auto else 'OFF ❌'}</b>\n"
        f"Start post: <b>{post_state}</b>\n\n"
        "Auto mode sends the request message first, then retries approval "
        "until Telegram accepts it or the request expires."
    )
    edit_or_send(call, text, reply_markup=chat_keyboard(chat_id, auto))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_auto:"))
def toggle_auto_callback(call: Any) -> None:
    try:
        chat_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid chat", show_alert=True)
        return
    if not chat_admin(call.from_user.id, chat_id):
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    row = chat_doc(chat_id) or {}
    new_state = not bool(row.get("auto_mode", True))
    chats_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"auto_mode": new_state, "updated_at": utc_now()}},
        upsert=True,
    )
    bot.answer_callback_query(call.id, f"Auto mode {'ON' if new_state else 'OFF'}")
    row["auto_mode"] = new_state
    title = html.escape(str(row.get("title", chat_id)))
    edit_or_send(
        call,
        f"<b>{title}</b>\n\nAuto Mode: <b>{'ON ✅' if new_state else 'OFF ❌'}</b>",
        reply_markup=chat_keyboard(chat_id, new_state),
    )


def register_next(call: Any, prompt: str, handler: Any) -> None:
    bot.send_message(call.message.chat.id, prompt, parse_mode="HTML")
    bot.register_next_step_handler(call.message, handler)


@bot.callback_query_handler(func=lambda call: call.data == "set_start_post")
def global_start_post_prompt(call: Any) -> None:
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    register_next(
        call,
        "Forward the channel post you want to use after <code>/start</code>.\n"
        "The forwarded post will be copied to every user in private chat.",
        save_global_start_post,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("set_start_post:"))
def chat_start_post_prompt(call: Any) -> None:
    try:
        chat_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid chat", show_alert=True)
        return
    if not chat_admin(call.from_user.id, chat_id):
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    register_next(
        call,
        "Forward the channel post now. It will become the start message for "
        "this chat.",
        lambda message, cid=chat_id: save_chat_start_post(message, cid),
    )
    bot.answer_callback_query(call.id)


def forwarded_source(message: Any) -> tuple[int, int] | None:
    forward_chat = getattr(message, "forward_from_chat", None)
    forward_message_id = getattr(message, "forward_from_message_id", None)
    if forward_chat and forward_message_id:
        return int(forward_chat.id), int(forward_message_id)

    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None)
    origin_message_id = getattr(origin, "message_id", None)
    if origin_chat and origin_message_id:
        return int(origin_chat.id), int(origin_message_id)
    return None


def save_global_start_post(message: Any) -> None:
    source = forwarded_source(message)
    if source is None:
        bot.reply_to(message, "❌ Please forward a channel post, not a normal message.")
        return
    set_setting(
        "start_post",
        {"chat_id": source[0], "message_id": source[1], "saved_at": utc_now()},
    )
    bot.reply_to(message, "✅ This forwarded channel post is now the global /start message.")


def save_chat_start_post(message: Any, chat_id: int) -> None:
    source = forwarded_source(message)
    if source is None:
        bot.reply_to(message, "❌ Please forward a channel post, not a normal message.")
        return
    chats_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"start_post": {"chat_id": source[0], "message_id": source[1]}}},
        upsert=True,
    )
    # /start is a private-chat command, so keep the selected post global too.
    # The per-chat copy above preserves which chat it was configured from.
    set_setting(
        "start_post",
        {"chat_id": source[0], "message_id": source[1], "saved_at": utc_now()},
    )
    bot.reply_to(message, "✅ This chat's /start post was saved.")


@bot.callback_query_handler(func=lambda call: call.data == "edit_request_text")
def global_request_text_prompt(call: Any) -> None:
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    register_next(
        call,
        "Send the default request message text.\n"
        "Available placeholders: <code>{first}</code> and <code>{chat_title}</code>.\n"
        "HTML formatting is supported.",
        save_global_request_text,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("set_req_text:"))
def chat_request_text_prompt(call: Any) -> None:
    try:
        chat_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid chat", show_alert=True)
        return
    if not chat_admin(call.from_user.id, chat_id):
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    register_next(
        call,
        "Send the request message for this chat.\n"
        "Placeholders: <code>{first}</code>, <code>{chat_title}</code>.",
        lambda message, cid=chat_id: save_chat_request_text(message, cid),
    )
    bot.answer_callback_query(call.id)


def save_global_request_text(message: Any) -> None:
    text = (message.text or message.caption or "").strip()
    if not text:
        bot.reply_to(message, "❌ Text was empty; no change made.")
        return
    set_setting("request_text", text[:4000])
    bot.reply_to(message, "✅ Default request message saved.")


def save_chat_request_text(message: Any, chat_id: int) -> None:
    text = (message.text or message.caption or "").strip()
    if not text:
        bot.reply_to(message, "❌ Text was empty; no change made.")
        return
    chats_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"request_text": text[:4000]}},
        upsert=True,
    )
    bot.reply_to(message, "✅ Request message saved for this chat.")


@bot.callback_query_handler(
    func=lambda call: call.data == "preview_request"
    or call.data.startswith("preview_chat:")
)
def preview_request_callback(call: Any) -> None:
    chat_id: int | None = None
    if call.data.startswith("preview_chat:"):
        try:
            chat_id = int(call.data.split(":", 1)[1])
        except ValueError:
            pass
    text = request_text_for(chat_id).format(first="RAJ", chat_title="Your Chat")
    safe_send_photo(call.message.chat.id, text, request_buttons())
    bot.answer_callback_query(call.id, "Preview sent")


@bot.callback_query_handler(func=lambda call: call.data == "global_stats")
def global_stats_callback(call: Any) -> None:
    if not is_bot_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    users = users_col.count_documents({})
    started = users_col.count_documents({"started": True})
    chats = chats_col.count_documents({"active": {"$ne": False}})
    approvals = sum(int(row.get("approved", 0)) for row in stats_col.find({}))
    text = (
        "<b>GLOBAL STATS</b>\n\n"
        f"👥 Users: <b>{users}</b>\n"
        f"▶️ Started: <b>{started}</b>\n"
        f"💬 Active chats: <b>{chats}</b>\n"
        f"✅ Approved: <b>{approvals}</b>"
    )
    edit_or_send(call, text, reply_markup=admin_keyboard(is_owner(call.from_user.id)))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("chat_stats:"))
def chat_stats_callback(call: Any) -> None:
    try:
        chat_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid chat", show_alert=True)
        return
    if not chat_admin(call.from_user.id, chat_id):
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    row = chat_doc(chat_id) or {}
    approved = sum(int(item.get("approved", 0)) for item in stats_col.find({"chat_id": chat_id}))
    events = events_col.count_documents({"chat_id": chat_id})
    edit_or_send(
        call,
        f"<b>{html.escape(str(row.get('title', chat_id)))} STATS</b>\n\n"
        f"📥 Requests seen: <b>{events}</b>\n"
        f"✅ Approved: <b>{approved}</b>\n"
        f"⚙️ Auto Mode: <b>{'ON' if row.get('auto_mode', True) else 'OFF'}</b>",
        reply_markup=chat_keyboard(chat_id, bool(row.get("auto_mode", True))),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "all_chats")
def all_chats_callback(call: Any) -> None:
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    rows = list(chats_col.find({"active": {"$ne": False}}).sort("title", ASCENDING))
    if not rows:
        text = "<b>ALL CHATS</b>\n\nNo chats have added the bot yet."
    else:
        lines = ["<b>ALL CHATS</b>", ""]
        for row in rows[:100]:
            icon = "📢" if row.get("chat_type") == "channel" else "👥"
            state = "ON ✅" if row.get("auto_mode", True) else "OFF ❌"
            lines.append(f"{icon} {html.escape(str(row.get('title', row['chat_id'])))} — {state}")
        text = "\n".join(lines)
    edit_or_send(call, text, reply_markup=admin_keyboard(True))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "add_bot_admin")
def add_admin_prompt(call: Any) -> None:
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    register_next(
        call,
        "Send the Telegram user ID or forward a message from the user.",
        save_bot_admin,
    )
    bot.answer_callback_query(call.id)


def save_bot_admin(message: Any) -> None:
    target = getattr(getattr(message, "forward_from", None), "id", None)
    if target is None:
        try:
            target = int((message.text or "").strip())
        except ValueError:
            bot.reply_to(message, "❌ Invalid user ID.")
            return
    admins_col.update_one(
        {"user_id": target},
        {"$set": {"added_by": message.from_user.id, "added_at": utc_now()}},
        upsert=True,
    )
    bot.reply_to(message, f"✅ Bot admin added: <code>{target}</code>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "remove_bot_admin")
def remove_admin_prompt(call: Any) -> None:
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    rows = list(admins_col.find({}).sort("user_id", ASCENDING))
    keyboard = InlineKeyboardMarkup(row_width=1)
    for row in rows:
        keyboard.add(
            button(
                f"➖ Remove {row['user_id']}",
                callback_data=f"remove_admin:{row['user_id']}",
                style="danger",
            )
        )
    keyboard.add(button("◀️ Back", callback_data="back_admin", style="primary"))
    edit_or_send(call, "<b>BOT ADMINS</b>\nSelect an admin to remove:", reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_admin:"))
def remove_admin_callback(call: Any) -> None:
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Owner only", show_alert=True)
        return
    try:
        target = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid user", show_alert=True)
        return
    admins_col.delete_one({"user_id": target})
    bot.answer_callback_query(call.id, "Admin removed")
    render_admin(call)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def main() -> None:
    global BOT_USERNAME
    initialize_database()
    me = bot.get_me()
    BOT_USERNAME = me.username or os.getenv("BOT_USERNAME", "autorequestapprovebot")
    logging.info("Auto Request Approve Bot started as @%s", BOT_USERNAME)
    logging.info("Approval retry window: %ss", REQUEST_TTL_SECONDS)

    while True:
        try:
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=30,
                allowed_updates=["message", "callback_query", "chat_join_request", "my_chat_member"],
            )
        except KeyboardInterrupt:
            logging.info("Bot stopped")
            return
        except Exception:
            logging.exception("Polling stopped unexpectedly; retrying in 3 seconds")
            time.sleep(3)


if __name__ == "__main__":
    main()
