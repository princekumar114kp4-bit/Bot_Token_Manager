#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Manager Pro (Telegram)

Manage bots that YOU own, using each bot's token (from @BotFather):
name, photo, description, about text, per-language texts, commands, menu
button, bot info, status, webhook, default admin rights and broadcast.

Setup:
  1. Create a NEW bot in @BotFather (this is the manager bot) and copy its token.
  2. Set it as env var BOT_TOKEN (never share it).
  3. pip install -r requirements.txt  and run: python3 bot.py

Design notes:
  * Reply keyboards only (no inline buttons). The keyboard changes on every step.
  * Coloured buttons use KeyboardButton.style (Bot API 9.4+: primary/success/danger).
    If Telegram rejects the field the bot retries without it and falls back to
    colour-square emoji (blue/green/red) in front of the button text.
  * Every button is built by Btn (emoji + style always set) and every keyboard cell
    passes through decorate(), so no button can be shown without colour.
  * Only standard emoji are used (no custom/premium emoji ids).
  * Target-bot tokens live only in memory, the message with the token is
    deleted right away, and tokens are redacted from logs.
  * No files are written to disk (photos are handled in memory).
"""

import html
import io
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import telebot

# >>> hyv-injected:web (added by the implementer bot: starts one background web server)
try:
    import threading as _hyv_threading
    import os as _hyv_os
    from http.server import BaseHTTPRequestHandler as _hyv_Base, ThreadingHTTPServer as _hyv_Server

    class _hyv_WebHandler(_hyv_Base):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health"):
                code, body = 200, b"ok"
            else:
                code, body = 404, b"not found"
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    def _hyv_serve_web():
        try:
            _hyv_port = int(_hyv_os.environ.get("PORT", "10000"))
            _hyv_Server(("0.0.0.0", _hyv_port), _hyv_WebHandler).serve_forever()
        except Exception:
            print("web server could not start", flush=True)

    _hyv_threading.Thread(target=_hyv_serve_web, daemon=True).start()
except Exception:
    print("web server could not start", flush=True)
# <<< hyv-injected:web
# >>> hyv-injected:pinger (added by the implementer bot: pings RENDER_EXTERNAL_URL in a background thread)
try:
    import threading as _hyv_threading2
    import os as _hyv_os2
    import time as _hyv_time
    import urllib.request as _hyv_urlreq

    def _hyv_ping_minutes():
        try:
            _hyv_m = int(_hyv_os2.environ.get("PING_MINUTES", "5").strip())
        except ValueError:
            return 5
        return _hyv_m if 1 <= _hyv_m <= 60 else 5

    def _hyv_ping_loop():
        _hyv_time.sleep(30)
        while True:
            try:
                _hyv_url = _hyv_os2.environ.get("RENDER_EXTERNAL_URL", "").strip()
                if _hyv_url.lower().startswith(("http://", "https://")):
                    try:
                        with _hyv_urlreq.urlopen(_hyv_url, timeout=30) as _hyv_resp:
                            _hyv_code = str(_hyv_resp.status)
                    except _hyv_urlreq.HTTPError as _hyv_err:
                        _hyv_code = str(_hyv_err.code)
                    except Exception:
                        _hyv_code = "down"
                    print("keep-alive ping:", _hyv_code, flush=True)
            except Exception:
                pass
            _hyv_time.sleep(_hyv_ping_minutes() * 60)

    _hyv_threading2.Thread(target=_hyv_ping_loop, daemon=True).start()
except Exception:
    print("pinger could not start", flush=True)
# <<< hyv-injected:pinger


try:  # Optional: used to make photos square / valid JPEGs.
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

PLACEHOLDER = "9049781241:AAG0fEMiKUJr6TvfX0SfKvxLX1LBlps-_WM"
BOT_TOKEN = os.environ.get("BOT_TOKEN", PLACEHOLDER).strip()

API_TIMEOUT = 30                      # seconds, every request to Telegram
SESSION_TTL = 30 * 60                 # forget idle sessions (and tokens) after 30 min
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_BC_PHOTO_BYTES = 10 * 1024 * 1024
BC_RATE_PER_SEC = 25                  # broadcast speed limit (Telegram allows ~30/s)
BC_MAX_WAIT = 300                     # longest 429 pause we will honour in a broadcast
BC_MAX_TARGETS = 10000
MSG_LIMIT = 3900                      # keep messages under Telegram's 4096 limit
TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
TOKEN_SHAPE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")

_sleep = time.sleep                   # replaced in tests


# ---------------------------------------------------------------- logging
class _RedactFilter(logging.Filter):
    """Removes anything that looks like a bot token from every log line."""

    def filter(self, record):
        try:
            record.msg = TOKEN_RE.sub("<token>", record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactFilter())
log = logging.getLogger("botmanagerpro")


def redact(text):
    return TOKEN_RE.sub("<token>", str(text))


def esc(x):
    return html.escape(str(x), quote=False)


def clip(text, limit=MSG_LIMIT):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 20].rstrip() + "\n... (kata gaya)"


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ------------------------------------------------- reply keyboard (colours)
STYLE = {"on": os.environ.get("BUTTON_STYLES", "1").strip() != "0"}
SQUARES = {"primary": "\U0001F7E6", "success": "\U0001F7E9", "danger": "\U0001F7E5"}


def norm(text):
    """Button key: text without leading emoji/symbols, lower-case."""
    t = str(text or "").strip()
    i = 0
    while i < len(t) and not t[i].isalnum():
        i += 1
    return re.sub(r"\s+", " ", t[i:]).lower()


STYLES = ("primary", "success", "danger")

# Single central colour table. EVERY button goes through Btn, and every Kb cell goes
# through decorate(), so a button can never reach the member without an emoji + style.
# Palette of coloured emoji used when a plain text label is passed without one.
AUTO_EMOJI = ["\U0001F7E2", "\U0001F535", "\U0001F7E1", "\U0001F7E3", "\U0001F7E0",
              "\U0001F525", "\U0001F48E", "\u26A1", "\u2728", "\U0001F680",
              "\U0001F3A8", "\U0001F31F", "\U0001F308", "\U0001F3AF", "\U0001F9E9"]
# words that decide the colour of a plain label (red / green), everything else is blue
_DANGER_WORDS = ("cancel", "delete", "remove", "clear", "reset", "stop", "forget", "drop", "no")
_SUCCESS_WORDS = ("yes", "apply", "save", "send", "confirm", "ok", "done", "add", "keep",
                  "review", "refresh", "start")


def _has_emoji(text):
    """True if the text starts with a pictograph (not a letter, digit or plain punctuation)."""
    t = str(text or "").lstrip()
    return bool(t) and not t[0].isalnum() and ord(t[0]) > 0x2000


def auto_style(label):
    words = set(re.findall(r"[a-z]+", str(label).lower()))
    if words & set(_DANGER_WORDS):
        return "danger"
    if words & set(_SUCCESS_WORDS):
        return "success"
    return "primary"


def auto_emoji(label):
    """Stable coloured emoji for a label (same label -> same emoji)."""
    return AUTO_EMOJI[sum(ord(c) for c in str(label)) % len(AUTO_EMOJI)]


class Btn:
    """One reply-keyboard button: emoji + label + colour style. Both are ALWAYS set:
    a missing/plain emoji or an unknown style is filled in automatically."""
    __slots__ = ("emoji", "label", "style", "key")

    def __init__(self, emoji, label, style=None):
        label = str(label).strip()
        emoji = str(emoji or "").strip()
        if not _has_emoji(emoji):
            emoji = auto_emoji(label)
        if style not in STYLES:
            style = auto_style(label)
        self.emoji, self.label, self.style = emoji, label, style
        self.key = norm(label)

    def text(self, styled=True):
        base = "%s %s" % (self.emoji, self.label)
        if not styled:   # older clients/libs: colour square in front of the text
            return "%s %s" % (SQUARES[self.style], base)
        return base

    def entry(self, styled=True):
        d = {"text": self.text(styled)}
        if styled:
            d["style"] = self.style
        return d


def decorate(x):
    """Guard: turn anything a keyboard builder gets into a coloured Btn.
    Btn stays as is; a plain string (with or without its own emoji) is split into
    emoji + label and gets an automatic coloured emoji and style."""
    if isinstance(x, Btn):
        return x
    if isinstance(x, (tuple, list)) and len(x) >= 2:
        return Btn(*x[:3])
    text = str(x).strip()
    i = 0
    while i < len(text) and not text[i].isalnum():
        i += 1
    lead, label = text[:i].strip(), text[i:].strip()
    return Btn(lead if _has_emoji(lead) else "", label or text)


class Kb:
    """ReplyKeyboardMarkup built as raw JSON (works with any telebot version)."""

    def __init__(self, rows, placeholder):
        built = []
        for row in rows or []:
            cells = [decorate(b) for b in (row or []) if b is not None]
            if cells:
                built.append(cells)
        self.rows = built
        self.placeholder = placeholder[:64]

    def payload(self, styled=True):
        return json.dumps({
            "keyboard": [[b.entry(styled) for b in row] for row in self.rows],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": self.placeholder,
        }, ensure_ascii=False)

    def text_map(self):
        """Exact button text (both looks) -> key."""
        out = {}
        for row in self.rows:
            for b in row:
                out[b.text(True)] = b.key
                out[b.text(False)] = b.key
        return out

    def keys(self):
        return {b.key for row in self.rows for b in row}


P, S, D = "primary", "success", "danger"

# navigation
B_BACK = Btn("\U0001F519", "Back", P)
B_HOME = Btn("\U0001F3E0", "Main Menu", S)
B_CANCEL = Btn("❌", "Cancel", D)
B_SKIP = Btn("⏩", "Skip", P)
B_APPLY = Btn("✅", "Yes, Apply", S)
B_EDIT_AGAIN = Btn("✏️", "Edit Again", P)
B_KEEP = Btn("\U0001F6DF", "No, Keep It", S)
B_YES_DELETE = Btn("\U0001F5D1️", "Yes, Delete", D)
B_YES_REMOVE = Btn("\U0001F6AE", "Yes, Remove", D)
B_REFRESH = Btn("♻️", "Refresh", S)
# main
B_CONNECT = Btn("\U0001F511", "Connect Bot", P)
B_HOWTO = Btn("\U0001F4D6", "How It Works", S)
B_PRIVACY = Btn("\U0001F512", "Privacy & Safety", P)
B_PROFILE = Btn("\U0001F3A8", "Profile & Texts", P)
B_CMDS = Btn("\U0001F9E9", "Commands & Menu", S)
B_INFO = Btn("\U0001F4E1", "Info & Status", P)
B_RIGHTS = Btn("\U0001F6E1️", "Admin Rights", S)
B_BC = Btn("\U0001F4E2", "Broadcast", P)
B_SWITCH = Btn("\U0001F504", "Switch Bot", P)
B_FORGET = Btn("\U0001F6AA", "Forget Token", D)
# profile
B_NAME = Btn("\U0001F3F7️", "Change Name", P)
B_PHOTO = Btn("\U0001F5BC️", "Profile Photo", S)
B_DESC = Btn("\U0001F4DD", "Description", P)
B_ABOUT = Btn("\U0001F4AC", "About Text", S)
B_LANGS = Btn("\U0001F30D", "Language Overview", P)
B_LANG_DEFAULT = Btn("\U0001F310", "All Users (Default)", P)
B_LANG_SPECIFIC = Btn("\U0001F5E3️", "Specific Language", S)
B_OTHER_CODE = Btn("\U0001F520", "Other Code", P)
B_REMOVE_TEXT = Btn("\U0001F6AB", "Remove This Text", D)
B_SET_PHOTO = Btn("\U0001F4F8", "Set New Photo", P)
B_RM_PHOTO = Btn("\U0001F648", "Remove Photo", D)
B_PHOTO_AGAIN = Btn("\U0001F501", "Send Another Photo", P)
# commands & menu
B_CV = Btn("\U0001F4CB", "View Commands", P)
B_CS = Btn("\U0001F195", "Set Commands", S)
B_CD = Btn("\U0001F525", "Delete Commands", D)
B_MB = Btn("\U0001F518", "Menu Button", P)
B_SC_ALL = Btn("\U0001F30E", "Default (Everyone)", P)
B_SC_PRIV = Btn("\U0001F464", "Private Chats", S)
B_SC_GRP = Btn("\U0001F465", "Group Chats", P)
B_SC_ADM = Btn("\U0001F451", "Group Admins", S)
B_MB_VIEW = Btn("\U0001F440", "View Current", S)
B_MB_DEFAULT = Btn("\U0001F3AF", "Default Button", P)
B_MB_COMMANDS = Btn("\U0001F39B️", "Commands Button", S)
B_MB_WEB = Btn("\U0001F9ED", "Web App Button", P)
# info & status
B_BOTINFO = Btn("\U0001F916", "Bot Info", P)
B_STATUS = Btn("\U0001FA7A", "Status Check", S)
B_WH = Btn("\U0001F517", "Webhook", P)
B_WH_CHECK = Btn("\U0001F50D", "Check Webhook", P)
B_WH_RESET = Btn("\U0001F9F9", "Reset & Drop Pending", D)
B_WH_DEL = Btn("⛔", "Delete (Keep Pending)", D)
# admin rights
B_R_GROUPS = Btn("\U0001F3D8️", "Groups & Supergroups", P)
B_R_CHANNELS = Btn("\U0001F4E3", "Channels", S)
B_R_VIEW = Btn("\U0001F441️", "View Current", S)
B_R_EDIT = Btn("\U0001F6E0️", "Edit Rights", P)
B_R_CLEAR = Btn("\U0001F9FD", "Clear Defaults", D)
B_R_ALL = Btn("✨", "Select All", S)
B_R_NONE = Btn("\U0001F9CA", "Unselect All", P)
B_R_SAVE = Btn("\U0001F4BE", "Save Rights", S)
B_KEEP_EDIT = Btn("\U0001F58D️", "Keep Editing", P)
# broadcast
B_BC_TARGETS = Btn("\U0001F4CC", "Add Targets", P)
B_BC_COLLECT = Btn("\U0001F4E5", "Collect From Updates", S)
B_BC_TEXT = Btn("\U0001F48C", "Write Text", P)
B_BC_PHOTO = Btn("\U0001F304", "Photo + Caption", S)
B_BC_REVIEW = Btn("\U0001F680", "Review & Send", S)
B_BC_CLEAR = Btn("\U0001F9FC", "Clear Targets", D)
B_BC_SEND = Btn("\U0001F4E4", "Send Now", S)
B_BC_TEST = Btn("\U0001F9EA", "Test To Me", P)
B_BC_EDIT_MSG = Btn("✍️", "Edit Message", P)
B_BC_EDIT_T = Btn("\U0001F9F2", "Edit Targets", P)
B_BC_STOP = Btn("\U0001F6D1", "Stop Broadcast", D)
B_ADD_THESE = Btn("\U0001F7E2", "Add These Chats", S)
B_FETCH_AGAIN = Btn("\U0001F4EC", "Fetch Again", P)
B_HTML_OFF = Btn("\U0001F524", "HTML: Off", P)
B_HTML_ON = Btn("\U0001F524", "HTML: On", S)
# buttons made on the fly (result screens): kept here so the table stays the single source
B_EDIT_MORE = Btn("\U0001FA84", "Edit More", P)
B_CHANGE_AGAIN = Btn("\U0001F39E️", "Change Again", P)
B_VIEW_ANOTHER = Btn("\U0001F50E", "View Another", P)
# new main-menu entries
B_CHATS = Btn("\U0001F6F0️", "Chat Tools", S)
B_BACKUP = Btn("\U0001F4BC", "Backup & Clone", S)
B_EXTRAS = Btn("\U0001F381", "Stars & Links", P)
# info: health + pending updates
B_HEALTH = Btn("\U0001F3E5", "Health Report", S)
B_PENDING = Btn("\U0001F4EE", "Pending Updates", P)
B_PEEK = Btn("\U0001F52D", "Peek Updates", P)
B_PEND_CLEAR = Btn("\U0001F6BF", "Clear Pending", D)
B_YES_CLEAR = Btn("\U0001F4A5", "Yes, Clear", D)
# webhook: set a new one
B_WH_SET = Btn("\U0001F50C", "Set Webhook", S)
B_DROP_OFF = Btn("\U0001F4A4", "Drop Pending: Off", S)
B_DROP_ON = Btn("\U0001F4A4", "Drop Pending: On", D)
# chat tools
B_CT_INFO = Btn("\U0001F4C7", "Chat Info", P)
B_CT_TEST = Btn("\U0001F4E9", "Send Test Message", S)
B_CT_LEAVE = Btn("\U0001F6B6", "Leave Chat", D)
B_YES_SEND = Btn("\U0001F4EF", "Yes, Send It", S)
B_YES_LEAVE = Btn("\U0001F44B", "Yes, Leave", D)
# backup & clone
B_EXPORT = Btn("\U0001F4E6", "Export Settings", P)
B_IMPORT = Btn("\U0001F4C2", "Import Settings", S)
B_TPL = Btn("\U0001F5C2️", "Templates", P)
B_BULK = Btn("\U0001F4DA", "Bulk Apply", S)
B_TPL_SAVE = Btn("\U0001F516", "Save As Template", S)
B_TPL_USE = Btn("\U0001F58C️", "Apply Template", P)
B_TPL_DEL = Btn("\U0001F4A3", "Delete Template", D)
B_YES_BULK = Btn("\U0001F4AB", "Yes, Apply To All", S)
B_BULK_STOP = Btn("\U0001F6A7", "Stop Bulk", D)
# stars & links
B_STARS = Btn("⭐", "Star Balance", S)
B_TXNS = Btn("\U0001F4B3", "Star History", P)
B_OLDER = Btn("⏪", "Older 10", P)
B_LINKS = Btn("\U0001F4CE", "Share Links", P)
B_LINK_PAYLOAD = Btn("\U0001F9F7", "Custom Start Link", S)
B_NO = Btn("\U0001F645", "No, Don't", P)
# template buttons (one per saved template, distinct emoji + colour each)
TPL_EMOJI = ["\U0001F4D8", "\U0001F4D7", "\U0001F4D9", "\U0001F4D5", "\U0001F4D3"]

LANGS = [
    ("en", "English", "\U0001F1EC\U0001F1E7"), ("hi", "Hindi", "\U0001F1EE\U0001F1F3"),
    ("es", "Spanish", "\U0001F1EA\U0001F1F8"), ("pt", "Portuguese", "\U0001F1E7\U0001F1F7"),
    ("ru", "Russian", "\U0001F1F7\U0001F1FA"), ("ar", "Arabic", "\U0001F1F8\U0001F1E6"),
    ("id", "Indonesian", "\U0001F1EE\U0001F1E9"), ("tr", "Turkish", "\U0001F1F9\U0001F1F7"),
    ("fr", "French", "\U0001F1EB\U0001F1F7"), ("de", "German", "\U0001F1E9\U0001F1EA"),
    ("it", "Italian", "\U0001F1EE\U0001F1F9"), ("fa", "Persian", "\U0001F1EE\U0001F1F7"),
    ("uz", "Uzbek", "\U0001F1FA\U0001F1FF"), ("bn", "Bengali", "\U0001F1E7\U0001F1E9"),
    ("ur", "Urdu", "\U0001F1F5\U0001F1F0"), ("uk", "Ukrainian", "\U0001F1FA\U0001F1E6"),
]
LANG_BTNS = {code: Btn(flag, name, P) for code, name, flag in LANGS}
LANG_BY_KEY = {b.key: code for code, b in LANG_BTNS.items()}

# editable texts: kind -> Bot API details
FIELDS = {
    "name": dict(title="Name", set="setMyName", get="getMyName", param="name",
                 key="name", limit=64),
    "description": dict(title="Description", set="setMyDescription", get="getMyDescription",
                        param="description", key="description", limit=512),
    "about": dict(title="About text", set="setMyShortDescription", get="getMyShortDescription",
                  param="short_description", key="short_description", limit=120),
}

CMD_SCOPES = {
    B_SC_ALL.key: ("default", "Default (everyone)"),
    B_SC_PRIV.key: ("all_private_chats", "Private chats"),
    B_SC_GRP.key: ("all_group_chats", "Group chats"),
    B_SC_ADM.key: ("all_chat_administrators", "Group admins"),
}

# (api key, label, emoji) for the admin-rights toggles
RIGHTS_GROUP = [
    ("is_anonymous", "Anonymous Admin", "\U0001F575️"),
    ("can_manage_chat", "Manage Chat", "\U0001F527"),
    ("can_delete_messages", "Delete Messages", "\U0001F9FA"),
    ("can_restrict_members", "Ban Users", "\U0001F6AB"),
    ("can_invite_users", "Invite Users", "✉️"),
    ("can_change_info", "Change Info", "\U0001F4DB"),
    ("can_pin_messages", "Pin Messages", "\U0001F4CC"),
    ("can_manage_topics", "Manage Topics", "\U0001F9F5"),
    ("can_promote_members", "Add Admins", "\U0001F451"),
    ("can_manage_video_chats", "Video Chats", "\U0001F3A5"),
    ("can_post_stories", "Post Stories", "\U0001F4F8"),
    ("can_edit_stories", "Edit Stories", "\U0001F58A️"),
    ("can_delete_stories", "Delete Stories", "\U0001F5D1️"),
    ("can_manage_tags", "Member Tags", "\U0001F3F7️"),
    ("can_send_welcome_messages", "Welcome Messages", "\U0001F44B"),
]
RIGHTS_CHANNEL = [
    ("can_manage_chat", "Manage Channel", "\U0001F527"),
    ("can_post_messages", "Post Messages", "\U0001F4E3"),
    ("can_edit_messages", "Edit Messages", "\U0001F4DD"),
    ("can_delete_messages", "Delete Messages", "\U0001F9FA"),
    ("can_invite_users", "Invite Users", "✉️"),
    ("can_change_info", "Change Info", "\U0001F4DB"),
    ("can_restrict_members", "Ban Subscribers", "\U0001F6AB"),
    ("can_promote_members", "Add Admins", "\U0001F451"),
    ("can_manage_video_chats", "Video Chats", "\U0001F3A5"),
    ("can_post_stories", "Post Stories", "\U0001F4F8"),
    ("can_edit_stories", "Edit Stories", "\U0001F58A️"),
    ("can_delete_stories", "Delete Stories", "\U0001F5D1️"),
    ("can_manage_direct_messages", "Direct Messages", "\U0001F4AC"),
]
RIGHTS_REQUIRED = ["is_anonymous", "can_manage_chat", "can_delete_messages",
                   "can_manage_video_chats", "can_restrict_members", "can_promote_members",
                   "can_change_info", "can_invite_users", "can_post_stories",
                   "can_edit_stories", "can_delete_stories"]


# ------------------------------------------------------------ Telegram API
def _post(token, method, data, files):
    """One HTTP call to the TARGET bot's API. Never raises."""
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    try:
        if files:
            r = requests.post(url, data=data, files=files, timeout=API_TIMEOUT)
        elif data:
            r = requests.post(url, json=data, timeout=API_TIMEOUT)
        else:
            r = requests.get(url, timeout=API_TIMEOUT)
        try:
            j = r.json()
        except Exception:
            return {"ok": False, "description": "Telegram ne samajhne layak jawab nahi diya"}
        return j if isinstance(j, dict) else {"ok": False, "description": "Unexpected reply"}
    except Exception as e:
        return {"ok": False, "network": True, "description": redact(e)}


def call_api(token, method, data=None, files=None, max_wait=10, retries=2, stats=None):
    """Call a Bot API method of the TARGET bot. Never raises.

    Handles flood limits: on 429 it waits `retry_after` (if <= max_wait) and
    retries. Read/set style calls are retried once on network errors."""
    safe_retry = method.startswith(("get", "set", "delete", "remove"))
    r = {}
    for attempt in range(retries + 1):
        r = _post(token, method, data, files)
        if r.get("ok"):
            return r
        if r.get("error_code") == 429:
            wait = float((r.get("parameters") or {}).get("retry_after") or 1)
            if stats is not None:
                stats["throttled"] = stats.get("throttled", 0) + 1
            if attempt < retries and wait <= max_wait:
                _sleep(wait + 0.3)
                continue
            return r
        if r.get("network") and safe_retry and attempt < retries:
            _sleep(1)
            continue
        return r
    return r


def plain_error(r):
    """Turn an API error into a plain-words line with a fix hint."""
    r = r or {}
    desc = str(r.get("description") or "Unknown error")
    low = desc.lower()
    code = r.get("error_code")
    if code == 401 or "unauthorized" in low:
        return ("Token galat hai ya revoke ho chuka hai. BotFather se naya token lekar "
                "'Switch Bot' dabao. (%s)" % desc)
    if code == 429 or "too many requests" in low:
        return ("Telegram ne thodi der ruk jaane ko kaha hai (bahut zyada requests). "
                "Kuch der baad dobara try karo. (%s)" % desc)
    if "conflict" in low or code == 409:
        return ("Ye bot abhi kisi aur jagah se updates le raha hai (webhook ya doosra "
                "polling). Pehle webhook reset karo ya wo program band karo. (%s)" % desc)
    if r.get("network") or "timed out" in low or "timeout" in low or "connection" in low:
        return "Telegram se connect nahi ho paya, thodi der baad try karo. (%s)" % desc
    if "blocked" in low or "deactivated" in low or "can't initiate" in low or code == 403:
        return ("Is chat ne bot ko start nahi kiya ya block kar diya hai, isliye message "
                "nahi ja sakta. (%s)" % desc)
    if "chat not found" in low:
        return "Ye chat nahi mili. ID sahi hai? User ne bot ko /start kiya hona chahiye. (%s)" % desc
    return desc


PHOTO_HINTS = (
    "\U0001F4A1 <b>Fix ke tareeke</b>\n"
    "1. JPG image use karo (PNG ho to JPG me convert karo)\n"
    "2. Image chhoti rakho (500KB ke andar)\n"
    "3. Square image use karo (1:1 ratio)\n"
    "4. Pillow install ho to main khud square JPG bana deta hoon"
)


# -------------------------------------------------------------- image tools
def prepare_image(data, square=True, max_side=640):
    """Return JPEG bytes via Pillow, or None if Pillow is missing/fails."""
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            if square:
                w, h = im.size
                side = min(w, h)
                left, top = (w - side) // 2, (h - side) // 2
                im = im.crop((left, top, left + side, top + side))
            if max(im.size) > max_side:
                scale = max_side / float(max(im.size))
                im = im.resize((max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=90)
            return out.getvalue()
    except Exception as e:
        log.info("image convert skipped: %s", redact(e))
        return None


def set_profile_photo(token, jpeg_bytes):
    """setMyProfilePhoto with a multipart upload (InputProfilePhotoStatic)."""
    return call_api(
        token, "setMyProfilePhoto",
        data={"photo": json.dumps({"type": "static", "photo": "attach://photo_file"})},
        files={"photo_file": ("photo.jpg", jpeg_bytes, "image/jpeg")})


# --------------------------------------------------------------- the bot
# A placeholder token is accepted here so the file can be imported/checked;
# main() refuses to start polling until a real BOT_TOKEN is set.
bot = telebot.TeleBot(BOT_TOKEN if ":" in BOT_TOKEN else "0:" + "x" * 35,
                      parse_mode=None, threaded=True)


def _err_info(e):
    code = getattr(e, "error_code", None)
    desc = str(getattr(e, "description", None) or e)
    retry = None
    try:
        retry = (getattr(e, "result_json", None) or {}).get("parameters", {}).get("retry_after")
    except Exception:
        retry = None
    return code, redact(desc), retry


def send(chat_id, text, kb=None):
    """Send an HTML message (+ reply keyboard) to the member. Never raises.

    * 429: waits retry_after and retries
    * keyboard rejected (style field etc.): retries without `style`, with
      colour-square emoji instead, and remembers it
    * HTML rejected: retries as plain text"""
    text = clip(text)
    styled = STYLE["on"]
    parse_mode = "HTML"
    fell_back = False
    net_retried = False
    for _ in range(5):
        markup = kb.payload(styled) if kb else None
        try:
            res = bot.send_message(chat_id, text, parse_mode=parse_mode,
                                   reply_markup=markup, disable_web_page_preview=True)
            if fell_back and STYLE["on"]:
                STYLE["on"] = False
                log.warning("Button styles rejected by Telegram: using colour squares instead.")
            return res
        except Exception as e:
            code, desc, retry = _err_info(e)
            low = desc.lower()
            if code == 429:
                _sleep(min(float(retry or 1), 30) + 0.3)
                continue
            if code == 403:
                log.info("member blocked the bot")
                return None
            if "parse entities" in low and parse_mode:
                parse_mode, text = None, strip_tags(text)
                continue
            if code is None and not net_retried:
                net_retried = True
                _sleep(1)
                continue
            if kb and styled and code is not None and code < 500:
                styled, fell_back = False, True
                continue
            log.warning("send_message failed: %s", desc)
            return None
    return None


def is_private(m):
    return getattr(m.chat, "type", "private") == "private"


# ---------------------------------------------------------------- sessions
SESSIONS = {}
_lock = threading.Lock()
_last_purge = [0.0]


def _new_session(uid, chat_id):
    return {"uid": uid, "chat_id": chat_id, "screen": "main", "token": None, "bot": None,
            "flow": {}, "res": None, "ts": time.time(), "busy": False, "stop": False,
            "kbmap": {}, "kbkeys": set(), "lock": threading.RLock(), "bc": _new_bc(), "tpls": []}


def _new_bc():
    return {"targets": [], "kind": None, "text": "", "photo": None, "caption": "",
            "html": False, "found": []}


def get_session(uid, chat_id):
    """Return (session, expired_flag). Idle sessions (and their tokens) are dropped."""
    now = time.time()
    with _lock:
        if now - _last_purge[0] > 60:
            _last_purge[0] = now
            for k in [k for k, v in SESSIONS.items()
                      if now - v["ts"] > SESSION_TTL and not v["busy"]]:
                SESSIONS.pop(k, None)
        st = SESSIONS.get(uid)
        expired = False
        if st and now - st["ts"] > SESSION_TTL and not st["busy"]:
            expired = bool(st.get("token"))
            st = None
        if st is None:
            st = _new_session(uid, chat_id)
            SESSIONS[uid] = st
        st["ts"] = now
        st["chat_id"] = chat_id
        return st, expired


def drop_session(uid):
    with _lock:
        SESSIONS.pop(uid, None)


def forget_target(st):
    st["token"] = None
    st["bot"] = None
    st["flow"] = {}
    st["res"] = None
    st["bc"] = _new_bc()


def api(st, method, data=None, **kw):
    st["ts"] = time.time()
    return call_api(st["token"], method, data, **kw)


# ----------------------------------------------------------- screen engine
RENDER = {}
HANDLE = {}
MEDIA = {}
BACK_OF = {}
# screens where the member types free text (button matching is exact there)
FREE_TEXT = {"token", "lang_code", "field_text", "cmd_text", "mb_text", "mb_url",
             "bc_targets", "bc_text", "bc_caption", "wh_url", "ct_id", "ct_text", "tpl_name",
             "bulk_tokens", "links_payload"}
MENU_SCREENS = {"main", "profile", "cmdmenu", "infomenu", "webhook", "rights_scope", "bc_menu",
                "pending", "ct_menu", "backup", "extras"}
BC_SCREENS = {"bc_menu", "bc_targets", "bc_collect", "bc_text", "bc_photo", "bc_caption",
              "bc_confirm", "bc_sending"}


def render(name):
    def deco(f):
        RENDER[name] = f
        return f
    return deco


def handle(name):
    def deco(f):
        HANDLE[name] = f
        return f
    return deco


def goto(st, name, notice=""):
    st["screen"] = name
    if name in MENU_SCREENS:
        st["flow"] = {}
    text, kb = RENDER[name](st)
    if notice:
        text = notice + "\n\n" + text
    st["kbmap"] = kb.text_map()
    st["kbkeys"] = kb.keys()
    send(st["chat_id"], text, kb)


def reshow(st, notice=""):
    goto(st, st["screen"], notice)


def head(title):
    return "<b>%s</b>" % title


def me_line(st):
    b = st.get("bot") or {}
    return "\U0001F916 Bot: <b>@%s</b>" % esc(b.get("username", "?"))


def finish(st, text, back, again=None, again_fn=None):
    """Show a results screen with its own keyboard."""
    st["res"] = {"text": text, "back": back, "again": again, "again_fn": again_fn}
    goto(st, "result")


def flow_of(st):
    return st.setdefault("flow", {})


# ------------------------------------------------------------------ main
@render("main")
def r_main(st):
    if not st.get("token"):
        return (head("\U0001F451 Bot Manager Pro") + "\n\n"
                "Apne <b>khud ke bots</b> ko yahin se manage karo: naam, photo, description, "
                "commands, webhook, admin rights aur broadcast — sab ek jagah.\n\n"
                "\U0001F511 Shuru karne ke liye <b>Connect Bot</b> dabao aur bot ka token "
                "(BotFather wala) bhejo.",
                Kb([[B_CONNECT], [B_HOWTO, B_PRIVACY]], "Tap Connect Bot to begin"))
    return (head("\U0001F451 Main Menu") + "\n\n" + me_line(st) + "\n\nKya karna hai? Neeche se chuno.",
            Kb([[B_PROFILE, B_CMDS], [B_INFO, B_RIGHTS], [B_BC],
                [B_CHATS, B_BACKUP], [B_EXTRAS], [B_SWITCH, B_FORGET]], "Choose a feature"))


HOWTO_TEXT = (
    head("\U0001F4D6 How It Works") + "\n\n"
    "1. <b>Connect Bot</b> dabao aur apne bot ka token bhejo (sirf wo bot jo aapka apna hai).\n"
    "2. Token wala message main turant delete kar deta hoon aur token sirf memory me rakhta hoon.\n"
    "3. Ab menu se feature chuno. Har step par neeche ke buttons badalte rahenge.\n"
    "4. Kabhi bhi <b>Cancel</b> ya <b>Main Menu</b> dabao. /start se bhi menu khulta hai.\n\n"
    "<b>Naye features</b>\n"
    "\U0001F3E5 Health Report: bot ki poori sehat ek screen par (Info & Status me)\n"
    "\U0001F4EE Pending Updates: dekho ya webhook chhue bina clear karo (Info & Status me)\n"
    "\U0001F50C Set Webhook: naya webhook URL lagao (Webhook me)\n"
    "\U0001F6F0️ Chat Tools: chat info, test message, chat chhodna (sirf un chats me jahan bot pehle se hai)\n"
    "\U0001F4E6 Export / Import: settings JSON file me nikalo aur wapas lagao (photo shamil nahi)\n"
    "\U0001F5C2️ Templates: settings session me save karke baad me lagao\n"
    "\U0001F4DA Bulk Apply: ek bot ki settings (naam chhod kar) apne kai bots par ek saath\n"
    "⭐ Star Balance aur \U0001F4B3 Star History: bot ke Telegram Stars\n"
    "\U0001F4CE Share Links: bot ke t.me links aur custom start link\n"
    "\U0001F30D Language Overview me ab description bhi dikhta hai.")
PRIVACY_TEXT = (
    head("\U0001F512 Privacy & Safety") + "\n\n"
    "• Token sirf memory me rehta hai, file ya log me nahi. 30 minute idle rehne par ya "
    "<b>Forget Token</b> dabane par hat jata hai.\n"
    "• Main kisi ke private messages nahi padhta. Ye tool sirf aapke apne bot ko manage karta hai.\n"
    "• Token group me kabhi share mat karo, sirf is private chat me.")


@handle("main")
def h_main(m, st, key):
    if key == B_CONNECT.key or (key == B_SWITCH.key and st.get("token")):
        st["flow"] = {"back": "main", "switching": bool(st.get("token"))}
        goto(st, "token")
    elif key == B_HOWTO.key and not st.get("token"):
        reshow(st, HOWTO_TEXT)
    elif key == B_PRIVACY.key and not st.get("token"):
        reshow(st, PRIVACY_TEXT)
    elif not st.get("token"):
        reshow(st, "\U0001F511 Pehle <b>Connect Bot</b> dabao.")
    elif key == B_PROFILE.key:
        goto(st, "profile")
    elif key == B_CMDS.key:
        goto(st, "cmdmenu")
    elif key == B_INFO.key:
        goto(st, "infomenu")
    elif key == B_RIGHTS.key:
        goto(st, "rights_scope")
    elif key == B_BC.key:
        goto(st, "bc_menu")
    elif key == B_CHATS.key:
        goto(st, "ct_menu")
    elif key == B_BACKUP.key:
        goto(st, "backup")
    elif key == B_EXTRAS.key:
        goto(st, "extras")
    elif key == B_FORGET.key:
        forget_target(st)
        goto(st, "main", "\U0001F6AA Token memory se hata diya gaya hai. Dobara use karne ke liye "
                         "Connect Bot dabao.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# ----------------------------------------------------------------- token
@render("token")
def r_token(st):
    switching = flow_of(st).get("switching")
    return (head("\U0001F511 Connect Bot") + "\n\n"
            + ("Naya bot connect karne ke liye uska token bhejo. Abhi wala bot tab tak connected "
               "rahega jab tak naya verify na ho.\n\n" if switching else "")
            + "\U0001F4E4 Us bot ka token bhejo jise manage karna hai (BotFather wala).\n"
            "Token bhejte hi main wo message <b>delete</b> kar dunga aur token sirf memory me rakhunga.",
            Kb([[B_CANCEL]], "Paste the bot token here"))


BACK_OF["token"] = "main"


def handle_token(m, st, token):
    chat_id = st["chat_id"]
    deleted = True
    try:
        bot.delete_message(chat_id, m.message_id)
    except Exception:
        deleted = False
    notice = ""
    if not deleted:
        notice = ("⚠️ Main token wala message delete nahi kar paya. Please usse khud "
                  "delete kar do.")
    if st["screen"] != "token":
        st["flow"] = {"back": st["screen"] if st.get("token") else "main",
                      "switching": bool(st.get("token"))}
    st["screen"] = "token"
    send(chat_id, notice + ("\n\n" if notice else "") + "\U0001F50D Bot check kar raha hoon...")
    me = call_api(token, "getMe")
    if not me.get("ok"):
        goto(st, "token", "❌ Token sahi nahi hai: %s\nDobara bhejo ya Cancel dabao."
             % esc(plain_error(me)))
        return
    result = me.get("result") or {}
    st["token"] = token
    st["bot"] = result
    st["bc"] = _new_bc()
    st["flow"] = {}
    st["res"] = None
    goto(st, "main", "✅ Connected: <b>@%s</b> (%s)\nYe bot is session ke liye yaad rahega."
         % (esc(result.get("username", "?")), esc(result.get("first_name", ""))))


@handle("token")
def h_token(m, st, key):
    # Buttons are handled by the global nav; any other text is just a wrong token.
    reshow(st, "❌ Ye token jaisa nahi lag raha. Format aisa hota hai <code>123456:ABC...</code>")


# ---------------------------------------------------------- profile menu
@render("profile")
def r_profile(st):
    return (head("\U0001F3A8 Profile & Texts") + "\n\n" + me_line(st) + "\n\n"
            "Naam, photo, description aur about text badlo. Naam/description/about "
            "<b>language-wise</b> bhi set ho sakte hain.",
            Kb([[B_NAME, B_PHOTO], [B_DESC, B_ABOUT], [B_LANGS], [B_BACK, B_HOME]],
               "Pick what to edit"))


BACK_OF["profile"] = "main"


@handle("profile")
def h_profile(m, st, key):
    if key == B_NAME.key:
        start_field(st, "name")
    elif key == B_DESC.key:
        start_field(st, "description")
    elif key == B_ABOUT.key:
        start_field(st, "about")
    elif key == B_PHOTO.key:
        goto(st, "photo_menu")
    elif key == B_LANGS.key:
        do_language_overview(st)
    else:
        reshow(st, "Neeche ke buttons use karo.")


# --------------------------------------------------- language scope / pick
def start_field(st, kind):
    st["flow"] = {"kind": kind, "back": "profile", "lang": ""}
    goto(st, "lang_scope")


def lang_label(code):
    return "default (sabke liye)" if not code else "<b>%s</b>" % esc(code)


@render("lang_scope")
def r_lang_scope(st):
    f = flow_of(st)
    what = FIELDS[f["kind"]]["title"] if f["kind"] in FIELDS else {
        "cmd_view": "Commands (view)", "cmd_set": "Commands (set)", "cmd_del": "Commands (delete)"}[f["kind"]]
    return (head("\U0001F310 Language Choose Karo") + "\n\n"
            "<b>%s</b> kis ke liye?\n\n"
            "\U0001F310 <b>All Users</b> = jin users ki language ka alag text nahi hai unhe dikhega.\n"
            "\U0001F5E3️ <b>Specific Language</b> = sirf ek language ke users ko dikhega." % esc(what),
            Kb([[B_LANG_DEFAULT], [B_LANG_SPECIFIC], [B_BACK, B_CANCEL]], "All users or one language?"))


def lang_scope_back(st):
    return "cmd_scope" if flow_of(st).get("kind", "").startswith("cmd_") else flow_of(st).get("back", "main")


BACK_OF["lang_scope"] = lang_scope_back


@handle("lang_scope")
def h_lang_scope(m, st, key):
    if key == B_LANG_DEFAULT.key:
        flow_of(st)["lang"] = ""
        proceed_after_lang(st)
    elif key == B_LANG_SPECIFIC.key:
        goto(st, "lang_pick")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("lang_pick")
def r_lang_pick(st):
    btns = [LANG_BTNS[c] for c, _n, _f in LANGS]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([B_OTHER_CODE])
    rows.append([B_BACK, B_CANCEL])
    return (head("\U0001F5E3️ Language Pick Karo") + "\n\nJis language ke liye set karna hai wo chuno. "
            "List me nahi hai to <b>Other Code</b> dabao (2-letter code, jaise <code>nl</code>).",
            Kb(rows, "Pick a language"))


BACK_OF["lang_pick"] = "lang_scope"


@handle("lang_pick")
def h_lang_pick(m, st, key):
    if key in LANG_BY_KEY:
        flow_of(st)["lang"] = LANG_BY_KEY[key]
        proceed_after_lang(st)
    elif key == B_OTHER_CODE.key:
        goto(st, "lang_code")
    else:
        reshow(st, "Neeche se language chuno.")


@render("lang_code")
def r_lang_code(st):
    return (head("✍️ Language Code") + "\n\n2-letter ISO 639-1 code bhejo, jaise <code>hi</code>, "
            "<code>es</code>, <code>nl</code>.", Kb([[B_BACK, B_CANCEL]], "Type a code like hi or es"))


BACK_OF["lang_code"] = "lang_pick"


@handle("lang_code")
def h_lang_code(m, st, key):
    code = (m.text or "").strip().lower()
    if not re.match(r"^[a-z]{2}$", code):
        reshow(st, "❌ Code 2 letters ka hona chahiye (a-z), jaise <code>hi</code>.")
        return
    flow_of(st)["lang"] = code
    proceed_after_lang(st)


def proceed_after_lang(st):
    f = flow_of(st)
    kind = f["kind"]
    if kind in FIELDS:
        enter_field_text(st)
    elif kind == "cmd_view":
        do_cmd_view(st)
    elif kind == "cmd_set":
        goto(st, "cmd_text")
    elif kind == "cmd_del":
        goto(st, "cmd_del_confirm")


def lang_data(f, extra=None):
    d = dict(extra or {})
    if f.get("lang"):
        d["language_code"] = f["lang"]
    return d


# ----------------------------------------------- name / description / about
def enter_field_text(st):
    f = flow_of(st)
    spec = FIELDS[f["kind"]]
    r = api(st, spec["get"], lang_data(f) or None)
    if not r.get("ok"):
        goto(st, "lang_scope", "❌ Abhi ka text nahi mil paya: %s" % esc(plain_error(r)))
        return
    f["current"] = (r.get("result") or {}).get(spec["key"], "") or ""
    f.pop("new", None)
    goto(st, "field_text")


@render("field_text")
def r_field_text(st):
    f = flow_of(st)
    spec = FIELDS[f["kind"]]
    cur = f.get("current", "")
    can_remove = bool(f.get("lang")) or f["kind"] != "name"
    rows = [[B_REMOVE_TEXT] if can_remove else None, [B_BACK, B_CANCEL]]
    return (head("%s — Naya Text" % esc(spec["title"])) + "\n\n" + me_line(st)
            + "\n\U0001F5E3️ Language: %s" % lang_label(f.get("lang"))
            + "\n\U0001F4C4 Abhi ka: %s" % (("<code>%s</code>" % esc(cur)) if cur else "<i>(khali)</i>")
            + "\n\n✍️ Naya %s bhejo (max <b>%d</b> characters)." % (esc(spec["title"].lower()), spec["limit"])
            + ("\n\U0001F5D1️ Is language ka alag text hatana ho to <b>Remove This Text</b> dabao."
               if can_remove else ""),
            Kb(rows, "Type the new %s" % spec["title"].lower()))


BACK_OF["field_text"] = "lang_scope"


@handle("field_text")
def h_field_text(m, st, key):
    f = flow_of(st)
    spec = FIELDS[f["kind"]]
    if key == B_REMOVE_TEXT.key and (f.get("lang") or f["kind"] != "name"):
        f["new"] = ""
        goto(st, "field_confirm")
        return
    if m.content_type != "text":
        reshow(st, "Text me bhejo.")
        return
    new = (m.text or "").strip()
    if not new:
        reshow(st, "❌ Text khali nahi ho sakta. Dobara bhejo.")
        return
    if len(new) > spec["limit"]:
        reshow(st, "❌ Bahut lamba hai (%d characters). Max <b>%d</b> allowed hai, chhota bhejo."
               % (len(new), spec["limit"]))
        return
    f["new"] = new
    goto(st, "field_confirm")


@render("field_confirm")
def r_field_confirm(st):
    f = flow_of(st)
    spec = FIELDS[f["kind"]]
    new = f.get("new", "")
    cur = f.get("current", "")
    return (head("\U0001F9FE Confirm Karo") + "\n\n" + me_line(st)
            + "\n\U0001F4CC Field: %s" % esc(spec["title"])
            + "\n\U0001F5E3️ Language: %s" % lang_label(f.get("lang"))
            + "\n⬅️ Pehle: %s" % (("<code>%s</code>" % esc(cur)) if cur else "<i>(khali)</i>")
            + "\n➡️ Ab: %s" % (("<code>%s</code>" % esc(new)) if new else "<i>(hata diya jayega)</i>")
            + "\n\nApply kar du?",
            Kb([[B_APPLY], [B_EDIT_AGAIN, B_CANCEL]], "Apply this change?"))


BACK_OF["field_confirm"] = "field_text"


@handle("field_confirm")
def h_field_confirm(m, st, key):
    f = flow_of(st)
    spec = FIELDS[f["kind"]]
    if key == B_EDIT_AGAIN.key:
        goto(st, "field_text")
    elif key == B_APPLY.key:
        r = api(st, spec["set"], lang_data(f, {spec["param"]: f.get("new", "")}))
        kind = f["kind"]
        if r.get("ok"):
            chk = api(st, spec["get"], lang_data(f) or None)
            shown = (chk.get("result") or {}).get(spec["key"]) if chk.get("ok") else f.get("new", "")
            finish(st, "✅ <b>%s</b> update ho gaya!\n\U0001F5E3️ Language: %s\n\U0001F4C4 Ab: %s"
                   % (esc(spec["title"]), lang_label(f.get("lang")),
                      ("<code>%s</code>" % esc(shown)) if shown else "<i>(khali)</i>"),
                   "profile", B_EDIT_MORE, lambda s, k=kind: start_field(s, k))
        else:
            goto(st, "field_text", "❌ %s nahi badla: %s" % (esc(spec["title"]), esc(plain_error(r))))
    else:
        reshow(st, "Neeche ke buttons use karo.")


def do_language_overview(st):
    send(st["chat_id"], "\U0001F50D Languages check kar raha hoon, thoda time lagega...")
    token_ok = True
    rows = []
    base = {}
    for kind in ("name", "description", "about"):
        spec = FIELDS[kind]
        r = api(st, spec["get"])
        if not r.get("ok"):
            token_ok = False
            err = plain_error(r)
            break
        base[kind] = (r.get("result") or {}).get(spec["key"], "") or ""
    if not token_ok:
        goto(st, "profile", "❌ Overview nahi ban paya: %s" % esc(err))
        return
    for code, name, flag in LANGS:
        vals = {}
        for kind in ("name", "description", "about"):
            spec = FIELDS[kind]
            r = api(st, spec["get"], {"language_code": code})
            if r.get("ok"):
                v = (r.get("result") or {}).get(spec["key"], "") or ""
                if v and v != base[kind]:
                    vals[kind] = v
        if vals:
            rows.append("%s <b>%s</b> (%s): naam=%s, about=%s, description=%s" % (
                flag, name, code, esc(vals.get("name", "-")), esc(vals.get("about", "-")),
                esc(short(vals["description"], 40)) if vals.get("description") else "-"))
    text = (head("\U0001F30D Language Overview") + "\n\n\U0001F310 Default naam: <code>%s</code>\n"
            "\U0001F310 Default about: <code>%s</code>\n"
            "\U0001F310 Default description: <code>%s</code>\n\n"
            % (esc(base["name"]), esc(base["about"] or "-"), esc(short(base["description"], 80) or "-")))
    text += "\n".join(rows) if rows else "Abhi koi language-wise alag text set nahi hai (common languages check ki)."
    finish(st, text, "profile")


# -------------------------------------------------------------- photo
@render("photo_menu")
def r_photo_menu(st):
    return (head("\U0001F5BC️ Profile Photo") + "\n\n" + me_line(st)
            + "\n\nNayi photo lagao ya maujooda photo hata do.",
            Kb([[B_SET_PHOTO, B_RM_PHOTO], [B_BACK, B_HOME]], "Set or remove the photo"))


BACK_OF["photo_menu"] = "profile"


@handle("photo_menu")
def h_photo_menu(m, st, key):
    st["flow"] = {"back": "profile"}
    if key == B_SET_PHOTO.key:
        goto(st, "photo_wait")
    elif key == B_RM_PHOTO.key:
        goto(st, "photo_rm_confirm")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("photo_wait")
def r_photo_wait(st):
    return (head("\U0001F4F8 Nayi Photo Bhejo") + "\n\nPhoto bhejo (photo ki tarah ya image file ki tarah). "
            "Square JPG sabse achha rehta hai.", Kb([[B_BACK, B_CANCEL]], "Send the new photo"))


BACK_OF["photo_wait"] = "photo_menu"


def pick_image(m):
    """Return (file_id, size, mime) or None."""
    if m.content_type == "photo" and getattr(m, "photo", None):
        p = max(m.photo, key=lambda x: (getattr(x, "file_size", 0) or 0,
                                        (getattr(x, "width", 0) or 0) * (getattr(x, "height", 0) or 0)))
        return p.file_id, getattr(p, "file_size", None), "image/jpeg"
    if m.content_type == "document" and getattr(m, "document", None):
        d = m.document
        mime = (getattr(d, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            return d.file_id, getattr(d, "file_size", None), mime
    return None


def download_image(m, limit):
    """Return (bytes, error_text)."""
    pick = pick_image(m)
    if not pick:
        return None, "Mujhe photo chahiye (ya image file). Dobara bhejo."
    file_id, size, _mime = pick
    if size and size > min(limit, MAX_DOWNLOAD_BYTES):
        return None, "❌ Ye file bahut badi hai. Chhoti photo bhejo."
    try:
        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)
    except Exception as e:
        log.warning("download failed: %s", redact(e))
        return None, "❌ Photo download nahi ho payi. Dobara bhejo."
    if not data:
        return None, "❌ Photo khali aayi. Dobara bhejo."
    return data, None


@handle("photo_wait")
def h_photo_wait_text(m, st, key):
    reshow(st, "Mujhe photo chahiye (ya image file). Dobara bhejo.")


def m_photo_wait(m, st):
    data, err = download_image(m, MAX_DOWNLOAD_BYTES)
    if err:
        reshow(st, err)
        return
    flow_of(st)["photo"] = data
    goto(st, "photo_confirm")


MEDIA["photo_wait"] = m_photo_wait


@render("photo_confirm")
def r_photo_confirm(st):
    data = flow_of(st).get("photo") or b""
    return (head("\U0001F9FE Photo Confirm") + "\n\n" + me_line(st)
            + "\n\U0001F4E6 Photo mili: %d KB" % max(1, len(data) // 1024)
            + ("\n✨ Main ise square JPG bana dunga." if Image is not None else "")
            + "\n\nIse bot ki profile photo bana du?",
            Kb([[B_APPLY], [B_PHOTO_AGAIN, B_CANCEL]], "Apply this photo?"))


BACK_OF["photo_confirm"] = "photo_wait"


@handle("photo_confirm")
def h_photo_confirm(m, st, key):
    f = flow_of(st)
    if key == B_PHOTO_AGAIN.key:
        f.pop("photo", None)
        goto(st, "photo_wait")
    elif key == B_APPLY.key:
        data = f.get("photo")
        if not data:
            goto(st, "photo_wait", "Photo nahi mili, dobara bhejo.")
            return
        jpeg = prepare_image(data)
        r = set_profile_photo(st["token"], jpeg or data)
        st["ts"] = time.time()
        if r.get("ok"):
            f.pop("photo", None)
            finish(st, "✅ Profile photo badal gayi!", "profile",
                   B_CHANGE_AGAIN, lambda s: goto(s, "photo_wait"))
        else:
            err = plain_error(r)
            hints = "\n\n" + PHOTO_HINTS if ("bad request" in err.lower() or "not found" in err.lower()) else ""
            goto(st, "photo_confirm", "❌ Photo nahi badli: %s%s" % (esc(err), hints))
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("photo_rm_confirm")
def r_photo_rm(st):
    return (head("\U0001F5D1️ Photo Hatani Hai?") + "\n\n" + me_line(st)
            + "\n\nProfile photo hata di jayegi. Pakka?",
            Kb([[B_YES_REMOVE, B_KEEP], [B_BACK]], "Remove the photo?"))


BACK_OF["photo_rm_confirm"] = "photo_menu"


@handle("photo_rm_confirm")
def h_photo_rm(m, st, key):
    if key == B_YES_REMOVE.key:
        r = api(st, "removeMyProfilePhoto")
        if r.get("ok"):
            finish(st, "✅ Profile photo hata di gayi.", "profile")
        else:
            goto(st, "photo_rm_confirm", "❌ Photo nahi hati: %s" % esc(plain_error(r)))
    elif key == B_KEEP.key:
        goto(st, "photo_menu", "\U0001F6DF Photo waisi hi rahegi.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# ---------------------------------------------------- commands & menu btn
@render("cmdmenu")
def r_cmdmenu(st):
    return (head("\U0001F9E9 Commands & Menu") + "\n\n" + me_line(st)
            + "\n\nCommands dekho, set karo ya hatao, aur chat ka menu button badlo.",
            Kb([[B_CV, B_CS], [B_CD, B_MB], [B_BACK, B_HOME]], "Pick an action"))


BACK_OF["cmdmenu"] = "main"


@handle("cmdmenu")
def h_cmdmenu(m, st, key):
    kinds = {B_CV.key: "cmd_view", B_CS.key: "cmd_set", B_CD.key: "cmd_del"}
    if key in kinds:
        st["flow"] = {"kind": kinds[key], "back": "cmdmenu", "lang": "", "scope": "default"}
        goto(st, "cmd_scope")
    elif key == B_MB.key:
        st["flow"] = {"back": "cmdmenu"}
        goto(st, "mb_menu")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("cmd_scope")
def r_cmd_scope(st):
    return (head("\U0001F465 Commands Scope") + "\n\nKin chats ke liye commands?\n"
            "\U0001F30D Default = sab jagah (jab tak specific scope na ho)\n"
            "\U0001F464 Private = sirf private chats\n\U0001F465 Group = group chats\n"
            "\U0001F451 Admins = group admins",
            Kb([[B_SC_ALL], [B_SC_PRIV, B_SC_GRP], [B_SC_ADM], [B_BACK, B_CANCEL]], "Choose the scope"))


BACK_OF["cmd_scope"] = "cmdmenu"


@handle("cmd_scope")
def h_cmd_scope(m, st, key):
    if key in CMD_SCOPES:
        flow_of(st)["scope"] = CMD_SCOPES[key][0]
        goto(st, "lang_scope")
    else:
        reshow(st, "Neeche ke buttons use karo.")


def scope_label(f):
    for t, name in CMD_SCOPES.values():
        if t == f.get("scope"):
            return name
    return "Default"


def cmd_data(f, extra=None):
    d = dict(extra or {})
    d["scope"] = {"type": f.get("scope", "default")}
    if f.get("lang"):
        d["language_code"] = f["lang"]
    return d


def do_cmd_view(st):
    f = flow_of(st)
    r = api(st, "getMyCommands", cmd_data(f))
    if not r.get("ok"):
        goto(st, "lang_scope", "❌ Commands nahi mil paye: %s" % esc(plain_error(r)))
        return
    cmds = r.get("result") or []
    lines = ["/%s - %s" % (esc(c.get("command", "")), esc(c.get("description", ""))) for c in cmds]
    text = (head("\U0001F4CB Commands") + "\n\n\U0001F465 Scope: %s\n\U0001F5E3️ Language: %s\n\n"
            % (esc(scope_label(f)), lang_label(f.get("lang"))))
    text += "\n".join(lines) if lines else "<i>Is scope/language ke liye koi command set nahi hai.</i>"
    finish(st, text, "cmdmenu", B_VIEW_ANOTHER,
           lambda s: (s.__setitem__("flow", {"kind": "cmd_view", "back": "cmdmenu", "lang": "", "scope": "default"}),
                      goto(s, "cmd_scope")))


CMD_LINE = re.compile(r"^/?([A-Za-z0-9_]+)\s*(?:[-–—:]+\s*|\s+)(.+)$")


def parse_commands(text):
    """Parse 'command - description' lines. Returns (commands, errors)."""
    cmds, errors, seen = [], [], set()
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        mt = CMD_LINE.match(line)
        if not mt:
            errors.append("Line %d: <code>%s</code> — format <code>command - description</code> chahiye" % (n, esc(line[:40])))
            continue
        name, desc = mt.group(1).lower(), mt.group(2).strip()
        if len(name) > 32:
            errors.append("Line %d: command 32 characters se chhota ho" % n)
        elif len(desc) > 256:
            errors.append("Line %d: description 256 characters se chhoti ho" % n)
        elif name in seen:
            errors.append("Line %d: <code>%s</code> dobara aaya hai" % (n, esc(name)))
        else:
            seen.add(name)
            cmds.append({"command": name, "description": desc})
    if len(cmds) > 100:
        errors.append("Maximum 100 commands allowed hain (aapne %d diye)." % len(cmds))
    if not cmds and not errors:
        errors.append("Koi command nahi mili.")
    return cmds, errors


@render("cmd_text")
def r_cmd_text(st):
    f = flow_of(st)
    return (head("➕ Commands Bhejo") + "\n\n\U0001F465 Scope: %s\n\U0001F5E3️ Language: %s\n\n"
            "Har line me ek command bhejo, is format me:\n<code>start - Bot shuru karo\nhelp - Madad</code>\n\n"
            "Command: a-z, 0-9, _ (max 32). Description max 256. Ye puri list purani list ko replace karegi."
            % (esc(scope_label(f)), lang_label(f.get("lang"))),
            Kb([[B_BACK, B_CANCEL]], "start - Start the bot"))


BACK_OF["cmd_text"] = "lang_scope"


@handle("cmd_text")
def h_cmd_text(m, st, key):
    if m.content_type != "text":
        reshow(st, "Text me bhejo.")
        return
    cmds, errors = parse_commands(m.text)
    if errors:
        reshow(st, "❌ Kuch lines theek nahi hain:\n" + "\n".join(errors[:10]))
        return
    flow_of(st)["commands"] = cmds
    goto(st, "cmd_confirm")


@render("cmd_confirm")
def r_cmd_confirm(st):
    f = flow_of(st)
    lines = ["/%s - %s" % (esc(c["command"]), esc(c["description"])) for c in f.get("commands", [])]
    return (head("\U0001F9FE Commands Confirm") + "\n\n" + me_line(st)
            + "\n\U0001F465 Scope: %s\n\U0001F5E3️ Language: %s\n\n" % (esc(scope_label(f)), lang_label(f.get("lang")))
            + "\n".join(lines) + "\n\nIn %d commands ko set kar du?" % len(lines),
            Kb([[B_APPLY], [B_EDIT_AGAIN, B_CANCEL]], "Apply these commands?"))


BACK_OF["cmd_confirm"] = "cmd_text"


@handle("cmd_confirm")
def h_cmd_confirm(m, st, key):
    f = flow_of(st)
    if key == B_EDIT_AGAIN.key:
        goto(st, "cmd_text")
    elif key == B_APPLY.key:
        r = api(st, "setMyCommands", cmd_data(f, {"commands": f.get("commands", [])}))
        if r.get("ok"):
            n = len(f.get("commands", []))
            finish(st, "✅ %d commands set ho gayi! (%s, %s)" % (n, esc(scope_label(f)), strip_tags(lang_label(f.get("lang")))),
                   "cmdmenu")
        else:
            goto(st, "cmd_text", "❌ Commands set nahi hui: %s" % esc(plain_error(r)))
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("cmd_del_confirm")
def r_cmd_del(st):
    f = flow_of(st)
    return (head("\U0001F5D1️ Commands Delete?") + "\n\n" + me_line(st)
            + "\n\U0001F465 Scope: %s\n\U0001F5E3️ Language: %s\n\nIs scope ki commands delete ho jayengi "
            "(upar wali scope ki commands dikhne lagengi). Pakka?" % (esc(scope_label(f)), lang_label(f.get("lang"))),
            Kb([[B_YES_DELETE, B_KEEP], [B_BACK]], "Delete these commands?"))


BACK_OF["cmd_del_confirm"] = "lang_scope"


@handle("cmd_del_confirm")
def h_cmd_del(m, st, key):
    f = flow_of(st)
    if key == B_YES_DELETE.key:
        r = api(st, "deleteMyCommands", cmd_data(f))
        if r.get("ok"):
            finish(st, "✅ Commands delete ho gayi (%s)." % esc(scope_label(f)), "cmdmenu")
        else:
            goto(st, "cmd_del_confirm", "❌ Delete nahi hui: %s" % esc(plain_error(r)))
    elif key == B_KEEP.key:
        goto(st, "cmdmenu", "\U0001F6DF Commands waisi hi rahengi.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# menu button
@render("mb_menu")
def r_mb_menu(st):
    return (head("\U0001F518 Menu Button") + "\n\n" + me_line(st)
            + "\n\nChat ke neeche wala menu button chuno:\n"
            "\U0001F3AF Default = commands ki list\n⌨️ Commands = commands menu\n"
            "\U0001F310 Web App = aapki https website/mini app",
            Kb([[B_MB_VIEW], [B_MB_DEFAULT, B_MB_COMMANDS], [B_MB_WEB], [B_BACK, B_HOME]], "Pick a menu button type"))


BACK_OF["mb_menu"] = "cmdmenu"


def describe_menu_button(mb):
    t = (mb or {}).get("type", "default")
    if t == "web_app":
        return "\U0001F310 Web App: <b>%s</b> → %s" % (esc(mb.get("text", "")), esc((mb.get("web_app") or {}).get("url", "")))
    if t == "commands":
        return "⌨️ Commands menu"
    return "\U0001F3AF Default"


@handle("mb_menu")
def h_mb_menu(m, st, key):
    f = flow_of(st)
    if key == B_MB_VIEW.key:
        r = api(st, "getChatMenuButton")
        if r.get("ok"):
            finish(st, head("\U0001F518 Abhi ka Menu Button") + "\n\n" + describe_menu_button(r.get("result")), "mb_menu")
        else:
            reshow(st, "❌ Menu button nahi mila: %s" % esc(plain_error(r)))
    elif key == B_MB_DEFAULT.key:
        f["menu_button"] = {"type": "default"}
        goto(st, "mb_confirm")
    elif key == B_MB_COMMANDS.key:
        f["menu_button"] = {"type": "commands"}
        goto(st, "mb_confirm")
    elif key == B_MB_WEB.key:
        goto(st, "mb_text")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("mb_text")
def r_mb_text(st):
    return (head("\U0001F310 Web App Button — Text") + "\n\nButton par kya likha dikhe? (max 64 characters)\n"
            "Skip karoge to <b>Open</b> likha aayega.", Kb([[B_SKIP], [B_BACK, B_CANCEL]], "Button label, e.g. Open"))


BACK_OF["mb_text"] = "mb_menu"


@handle("mb_text")
def h_mb_text(m, st, key):
    f = flow_of(st)
    if key == B_SKIP.key:
        f["mb_text"] = "Open"
        goto(st, "mb_url")
        return
    t = (m.text or "").strip()
    if m.content_type != "text" or not t:
        reshow(st, "Text me bhejo ya Skip dabao.")
    elif len(t) > 64:
        reshow(st, "❌ Max 64 characters allowed hain.")
    else:
        f["mb_text"] = t
        goto(st, "mb_url")


@render("mb_url")
def r_mb_url(st):
    return (head("\U0001F517 Web App Button — URL") + "\n\nWeb App ka <b>https://</b> URL bhejo.",
            Kb([[B_BACK, B_CANCEL]], "https://example.com/app"))


BACK_OF["mb_url"] = "mb_text"


@handle("mb_url")
def h_mb_url(m, st, key):
    url = (m.text or "").strip() if m.content_type == "text" else ""
    p = urlparse(url)
    if p.scheme != "https" or not p.netloc or " " in url or len(url) > 512:
        reshow(st, "❌ Valid <b>https://</b> URL chahiye (Telegram sirf https allow karta hai).")
        return
    f = flow_of(st)
    f["menu_button"] = {"type": "web_app", "text": f.get("mb_text", "Open"), "web_app": {"url": url}}
    goto(st, "mb_confirm")


@render("mb_confirm")
def r_mb_confirm(st):
    return (head("\U0001F9FE Menu Button Confirm") + "\n\n" + me_line(st) + "\n\n"
            + describe_menu_button(flow_of(st).get("menu_button")) + "\n\nIse set kar du?",
            Kb([[B_APPLY], [B_BACK, B_CANCEL]], "Apply this menu button?"))


BACK_OF["mb_confirm"] = "mb_menu"


@handle("mb_confirm")
def h_mb_confirm(m, st, key):
    f = flow_of(st)
    if key == B_APPLY.key:
        r = api(st, "setChatMenuButton", {"menu_button": f.get("menu_button")})
        if r.get("ok"):
            finish(st, "✅ Menu button set ho gaya!\n" + describe_menu_button(f.get("menu_button")), "mb_menu")
        else:
            goto(st, "mb_confirm", "❌ Menu button set nahi hua: %s" % esc(plain_error(r)))
    else:
        reshow(st, "Neeche ke buttons use karo.")


# ----------------------------------------------------- info & status menu
@render("infomenu")
def r_infomenu(st):
    return (head("\U0001F4E1 Info & Status") + "\n\n" + me_line(st)
            + "\n\nBot ki jaankari, health check aur webhook yahan hain.",
            Kb([[B_BOTINFO, B_STATUS], [B_HEALTH, B_PENDING], [B_WH], [B_BACK, B_HOME]], "Pick an option"))


BACK_OF["infomenu"] = "main"

FLAG_LABELS = [
    ("can_join_groups", "Groups me add ho sakta hai"),
    ("can_read_all_group_messages", "Group ke saare messages padh sakta hai (privacy mode off)"),
    ("supports_inline_queries", "Inline mode"),
    ("can_connect_to_business", "Business se connect"),
    ("has_main_web_app", "Main Mini App"),
    ("has_topics_enabled", "Private chat topics"),
    ("allows_users_to_create_topics", "Users topics bana sakte hain"),
    ("can_manage_bots", "Doosre bots manage kar sakta hai"),
    ("supports_guest_queries", "Guest mode"),
    ("supports_join_request_queries", "Join request queries"),
]


def yn(v):
    return "✅" if v else "➖"


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "?"


@handle("infomenu")
def h_infomenu(m, st, key):
    if key == B_BOTINFO.key or key == B_REFRESH.key:
        do_bot_info(st)
    elif key == B_STATUS.key:
        do_status(st)
    elif key == B_HEALTH.key:
        do_health(st)
    elif key == B_PENDING.key:
        goto(st, "pending")
    elif key == B_WH.key:
        goto(st, "webhook")
    else:
        reshow(st, "Neeche ke buttons use karo.")


def do_bot_info(st):
    r = api(st, "getMe")
    if not r.get("ok"):
        goto(st, "infomenu", "❌ Bot info nahi mili: %s" % esc(plain_error(r)))
        return
    u = r.get("result") or {}
    st["bot"] = u
    lines = [head("\U0001F916 Bot Info"), "",
             "\U0001F194 ID: <code>%s</code>" % esc(u.get("id", "?")),
             "\U0001F4DB Naam: %s" % esc(u.get("first_name", "")),
             "\U0001F517 Username: @%s" % esc(u.get("username", "?")),
             "\U0001F310 Link: t.me/%s" % esc(u.get("username", "?")), ""]
    for k, label in FLAG_LABELS:
        if k in u:
            lines.append("%s %s" % (yn(u.get(k)), esc(label)))
    for kind in ("name", "description", "about"):
        spec = FIELDS[kind]
        rr = api(st, spec["get"])
        if rr.get("ok"):
            v = (rr.get("result") or {}).get(spec["key"], "")
            lines.append("")
            lines.append("<b>%s</b>: %s" % (esc(spec["title"]), esc(v) if v else "<i>(khali)</i>"))
    finish(st, "\n".join(lines), "infomenu", B_REFRESH, do_bot_info)


def do_status(st):
    t0 = time.time()
    r = api(st, "getMe")
    ms = int((time.time() - t0) * 1000)
    if not r.get("ok"):
        finish(st, head("\U0001FA7A Status Check") + "\n\n❌ <b>Token kaam nahi kar raha</b>\n%s" % esc(plain_error(r)),
               "infomenu", B_REFRESH, do_status)
        return
    u = r.get("result") or {}
    lines = [head("\U0001FA7A Status Check"), "", "✅ <b>Token valid hai</b> (@%s, %d ms)" % (esc(u.get("username", "?")), ms)]
    w = api(st, "getWebhookInfo")
    if w.get("ok"):
        wi = w.get("result") or {}
        if wi.get("url"):
            lines.append("\U0001F517 Webhook: <b>laga hua hai</b> (pending: %s)" % esc(wi.get("pending_update_count", 0)))
            if wi.get("last_error_message"):
                lines.append("⚠️ Last error: %s (%s)" % (esc(wi["last_error_message"]), fmt_ts(wi.get("last_error_date"))))
        else:
            lines.append("\U0001F517 Webhook: nahi laga (polling mode, pending: %s)" % esc(wi.get("pending_update_count", 0)))
    c = api(st, "getMyCommands")
    if c.get("ok"):
        lines.append("\U0001F9E9 Default commands: %d" % len(c.get("result") or []))
    mb = api(st, "getChatMenuButton")
    if mb.get("ok"):
        lines.append("\U0001F518 Menu button: " + describe_menu_button(mb.get("result")))
    nm = api(st, "getMyName")
    if nm.get("ok"):
        lines.append("\U0001F4DB Naam: %s" % esc((nm.get("result") or {}).get("name", "")))
    finish(st, "\n".join(lines), "infomenu", B_REFRESH, do_status)


# webhook
@render("webhook")
def r_webhook(st):
    return (head("\U0001F517 Webhook") + "\n\n" + me_line(st)
            + "\n\n\U0001F440 Check = abhi ki webhook jaankari\n"
            "\U0001F50C Set = naya webhook URL lagao\n"
            "\U0001F9F9 Reset = webhook hata kar <b>pending updates bhi hata do</b>\n"
            "\U0001F5D1️ Delete = sirf webhook hatao, pending updates rakho\n\n"
            "⚠️ Agar bot ka server webhook se chal raha hai to hatane par wo updates lena band kar dega.",
            Kb([[B_WH_CHECK, B_WH_SET], [B_WH_RESET, B_WH_DEL], [B_BACK, B_HOME]], "Check, set or reset the webhook"))


BACK_OF["webhook"] = "infomenu"


def do_webhook_check(st):
    r = api(st, "getWebhookInfo")
    if not r.get("ok"):
        goto(st, "webhook", "❌ Webhook info nahi mili: %s" % esc(plain_error(r)))
        return
    w = r.get("result") or {}
    lines = [head("\U0001F517 Webhook Info"), ""]
    if w.get("url"):
        lines.append("✅ Webhook laga hua hai")
        lines.append("\U0001F310 URL: <code>%s</code>" % esc(w["url"]))
    else:
        lines.append("➖ Koi webhook nahi (bot polling/getUpdates use kar sakta hai)")
    lines.append("\U0001F4E5 Pending updates: %s" % esc(w.get("pending_update_count", 0)))
    if w.get("ip_address"):
        lines.append("\U0001F4CD IP: %s" % esc(w["ip_address"]))
    if w.get("max_connections"):
        lines.append("\U0001F500 Max connections: %s" % esc(w["max_connections"]))
    if w.get("allowed_updates"):
        lines.append("\U0001F4DC Allowed updates: %s" % esc(", ".join(w["allowed_updates"])))
    if w.get("last_error_message"):
        lines.append("⚠️ Last error: %s (%s)" % (esc(w["last_error_message"]), fmt_ts(w.get("last_error_date"))))
    finish(st, "\n".join(lines), "webhook", B_REFRESH, do_webhook_check)


@handle("webhook")
def h_webhook(m, st, key):
    if key == B_WH_CHECK.key:
        do_webhook_check(st)
    elif key == B_WH_SET.key:
        st["flow"] = {"back": "webhook"}
        goto(st, "wh_url")
    elif key == B_WH_RESET.key:
        st["flow"] = {"back": "webhook", "drop": True}
        goto(st, "webhook_confirm")
    elif key == B_WH_DEL.key:
        st["flow"] = {"back": "webhook", "drop": False}
        goto(st, "webhook_confirm")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("webhook_confirm")
def r_webhook_confirm(st):
    drop = flow_of(st).get("drop")
    return (head("\U0001F9FE Webhook Confirm") + "\n\n" + me_line(st) + "\n\n"
            + ("\U0001F9F9 Webhook <b>delete</b> hoga aur saare <b>pending updates bhi hat jayenge</b>."
               if drop else "\U0001F5D1️ Webhook <b>delete</b> hoga, pending updates rahenge.")
            + "\n\nPakka?",
            Kb([[B_APPLY], [B_BACK, B_CANCEL]], "Confirm the webhook change"))


BACK_OF["webhook_confirm"] = "webhook"


@handle("webhook_confirm")
def h_webhook_confirm(m, st, key):
    if key == B_APPLY.key:
        drop = bool(flow_of(st).get("drop"))
        r = api(st, "deleteWebhook", {"drop_pending_updates": drop})
        if r.get("ok"):
            finish(st, "✅ Webhook hat gaya%s." % (" aur pending updates bhi clear ho gaye" if drop else ""),
                   "webhook", B_REFRESH, do_webhook_check)
        else:
            goto(st, "webhook_confirm", "❌ Webhook nahi hata: %s" % esc(plain_error(r)))
    else:
        reshow(st, "Neeche ke buttons use karo.")


# ----------------------------------------------------------- admin rights
@render("rights_scope")
def r_rights_scope(st):
    return (head("\U0001F6E1️ Default Admin Rights") + "\n\n" + me_line(st)
            + "\n\nJab koi aapke bot ko admin banata hai to ye rights suggest hote hain "
            "(user chahe to badal sakta hai).\nKiske liye?",
            Kb([[B_R_GROUPS, B_R_CHANNELS], [B_BACK, B_HOME]], "Groups or channels?"))


BACK_OF["rights_scope"] = "main"


@handle("rights_scope")
def h_rights_scope(m, st, key):
    if key in (B_R_GROUPS.key, B_R_CHANNELS.key):
        st["flow"] = {"back": "rights_scope", "channels": key == B_R_CHANNELS.key}
        goto(st, "rights_menu")
    else:
        reshow(st, "Neeche ke buttons use karo.")


def rights_spec(f):
    return RIGHTS_CHANNEL if f.get("channels") else RIGHTS_GROUP


@render("rights_menu")
def r_rights_menu(st):
    f = flow_of(st)
    return (head("%s Admin Rights" % ("\U0001F4E3 Channels" if f.get("channels") else "\U0001F465 Groups")) + "\n\n"
            + me_line(st) + "\n\nDekho, badlo ya default rights saaf karo.",
            Kb([[B_R_VIEW, B_R_EDIT], [B_R_CLEAR], [B_BACK, B_HOME]], "View, edit or clear"))


BACK_OF["rights_menu"] = "rights_scope"


def fetch_rights(st):
    f = flow_of(st)
    r = api(st, "getMyDefaultAdministratorRights", {"for_channels": bool(f.get("channels"))})
    if not r.get("ok"):
        return None, r
    return (r.get("result") or {}), r


def rights_lines(spec, cur):
    return "\n".join("%s %s %s" % (yn(cur.get(k)), em, esc(label)) for k, label, em in spec)


@handle("rights_menu")
def h_rights_menu(m, st, key):
    f = flow_of(st)
    spec = rights_spec(f)
    if key == B_R_VIEW.key:
        cur, r = fetch_rights(st)
        if cur is None:
            reshow(st, "❌ Rights nahi mile: %s" % esc(plain_error(r)))
            return
        any_on = any(cur.get(k) for k, _l, _e in spec)
        finish(st, head("\U0001F440 Current Default Rights") + "\n\n"
               + ("Koi default rights set nahi hain.\n\n" if not any_on else "") + rights_lines(spec, cur),
               "rights_menu")
    elif key == B_R_EDIT.key:
        cur, r = fetch_rights(st)
        if cur is None:
            reshow(st, "❌ Rights nahi mile: %s" % esc(plain_error(r)))
            return
        f["rights"] = {k: bool(cur.get(k)) for k, _l, _e in spec}
        goto(st, "rights_edit")
    elif key == B_R_CLEAR.key:
        goto(st, "rights_clear")
    else:
        reshow(st, "Neeche ke buttons use karo.")


def right_btn(k, label, em, on):
    # green + success when on, red + danger when off (both coloured, both with emoji)
    return Btn("\U0001F7E2" if on else "\U0001F534", "%s %s" % (em, label), S if on else D)


@render("rights_edit")
def r_rights_edit(st):
    f = flow_of(st)
    spec = rights_spec(f)
    btns = [right_btn(k, label, em, f["rights"].get(k)) for k, label, em in spec]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows += [[B_R_ALL, B_R_NONE], [B_R_SAVE], [B_BACK, B_CANCEL]]
    n = sum(1 for k, _l, _e in spec if f["rights"].get(k))
    return (head("\U0001F6E0️ Rights Edit") + "\n\nButton dabao to right on/off hoga. Abhi <b>%d/%d</b> on hain.\n"
            "Ho jaye to <b>Save Rights</b> dabao." % (n, len(spec)), Kb(rows, "Tap rights to toggle"))


BACK_OF["rights_edit"] = "rights_menu"


@handle("rights_edit")
def h_rights_edit(m, st, key):
    f = flow_of(st)
    spec = rights_spec(f)
    by_key = {norm("%s %s" % (em, label)): k for k, label, em in spec}
    if key in by_key:
        k = by_key[key]
        f["rights"][k] = not f["rights"].get(k)
        reshow(st)
    elif key == B_R_ALL.key:
        f["rights"] = {k: True for k, _l, _e in spec}
        reshow(st)
    elif key == B_R_NONE.key:
        f["rights"] = {k: False for k, _l, _e in spec}
        reshow(st)
    elif key == B_R_SAVE.key:
        goto(st, "rights_confirm")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("rights_confirm")
def r_rights_confirm(st):
    f = flow_of(st)
    spec = rights_spec(f)
    return (head("\U0001F9FE Rights Confirm") + "\n\n" + me_line(st)
            + "\n\U0001F4CC Kiske liye: %s\n\n" % ("Channels" if f.get("channels") else "Groups & Supergroups")
            + rights_lines(spec, f["rights"]) + "\n\nIn rights ko default bana du?",
            Kb([[B_APPLY], [B_KEEP_EDIT, B_CANCEL]], "Save these default rights?"))


BACK_OF["rights_confirm"] = "rights_edit"


@handle("rights_confirm")
def h_rights_confirm(m, st, key):
    f = flow_of(st)
    spec = rights_spec(f)
    if key == B_KEEP_EDIT.key:
        goto(st, "rights_edit")
    elif key == B_APPLY.key:
        rights = {k: False for k in RIGHTS_REQUIRED}
        rights.update({k: bool(f["rights"].get(k)) for k, _l, _e in spec})
        r = api(st, "setMyDefaultAdministratorRights", {"rights": rights, "for_channels": bool(f.get("channels"))})
        if r.get("ok"):
            finish(st, "✅ Default admin rights set ho gaye!\n\n" + rights_lines(spec, f["rights"]), "rights_menu")
        else:
            goto(st, "rights_edit", "❌ Rights set nahi hue: %s" % esc(plain_error(r)))
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("rights_clear")
def r_rights_clear(st):
    return (head("\U0001F9FD Default Rights Clear?") + "\n\n" + me_line(st)
            + "\n\nDefault admin rights hata diye jayenge. Pakka?",
            Kb([[B_YES_DELETE, B_KEEP], [B_BACK]], "Clear the default rights?"))


BACK_OF["rights_clear"] = "rights_menu"


@handle("rights_clear")
def h_rights_clear(m, st, key):
    f = flow_of(st)
    if key == B_YES_DELETE.key:
        r = api(st, "setMyDefaultAdministratorRights", {"for_channels": bool(f.get("channels"))})
        if r.get("ok"):
            finish(st, "✅ Default admin rights clear ho gaye.", "rights_menu")
        else:
            goto(st, "rights_clear", "❌ Clear nahi hua: %s" % esc(plain_error(r)))
    elif key == B_KEEP.key:
        goto(st, "rights_menu", "\U0001F6DF Rights waise hi rahenge.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# -------------------------------------------------------------- broadcast
BC_NOTE = ("ℹ️ <b>Zaroori:</b> bot token se users ki list nahi mil sakti. Broadcast sirf "
           "unhi chats tak pahunchta hai jinhone <b>aapka bot pehle start kiya hai</b> (ya jahan "
           "bot add hai). Aapko chat IDs khud paste karne hain ya updates se collect karne hain.")


def bc_ready(bc):
    return bool(bc["targets"]) and ((bc["kind"] == "text" and bc["text"]) or (bc["kind"] == "photo" and bc["photo"]))


@render("bc_menu")
def r_bc_menu(st):
    bc = st["bc"]
    msg_state = "<i>abhi nahi likha</i>"
    if bc["kind"] == "text" and bc["text"]:
        msg_state = "\U0001F48C Text (%d characters)" % len(bc["text"])
    elif bc["kind"] == "photo" and bc["photo"]:
        msg_state = "\U0001F304 Photo%s" % (" + caption" if bc["caption"] else "")
    rows = [[B_BC_TARGETS, B_BC_COLLECT], [B_BC_TEXT, B_BC_PHOTO]]
    if bc["targets"]:
        rows.append([B_BC_CLEAR])
    if bc_ready(bc):
        rows.insert(0, [B_BC_REVIEW])
    rows.append([B_BACK, B_HOME])
    return (head("\U0001F4E2 Broadcast") + "\n\n" + me_line(st) + "\n\n" + BC_NOTE
            + "\n\n\U0001F3AF Targets: <b>%d</b>\n\U0001F4E8 Message: %s" % (len(bc["targets"]), msg_state),
            Kb(rows, "Add targets and write a message"))


BACK_OF["bc_menu"] = "main"


@handle("bc_menu")
def h_bc_menu(m, st, key):
    bc = st["bc"]
    if key == B_BC_TARGETS.key:
        goto(st, "bc_targets")
    elif key == B_BC_COLLECT.key:
        do_collect(st)
    elif key == B_BC_TEXT.key:
        goto(st, "bc_text")
    elif key == B_BC_PHOTO.key:
        goto(st, "bc_photo")
    elif key == B_BC_CLEAR.key:
        bc["targets"] = []
        reshow(st, "\U0001F9F9 Targets clear ho gaye.")
    elif key == B_BC_REVIEW.key and bc_ready(bc):
        goto(st, "bc_confirm")
    else:
        reshow(st, "Neeche ke buttons use karo.")


_USERNAME = re.compile(r"^@([A-Za-z][A-Za-z0-9_]{3,31})$")


def parse_targets(text):
    """Return (targets, invalid_count). Targets are ints or '@username' strings."""
    out, bad = [], 0
    for tok in re.split(r"[\s,;]+", text or ""):
        if not tok:
            continue
        tok = tok.strip()
        link = re.match(r"^(?:https?://)?t\.me/", tok, flags=re.I)
        if link:
            tok = "@" + tok[link.end():].lstrip("@")
        if re.match(r"^-?\d{1,20}$", tok) and int(tok) != 0:
            out.append(int(tok))
            continue
        mt = _USERNAME.match(tok)
        if mt:
            out.append("@" + mt.group(1))
        else:
            bad += 1
    return out, bad


def add_targets(bc, new):
    have = set(bc["targets"])
    added = 0
    for t in new:
        if t not in have and len(bc["targets"]) < BC_MAX_TARGETS:
            bc["targets"].append(t)
            have.add(t)
            added += 1
    return added


@render("bc_targets")
def r_bc_targets(st):
    return (head("\U0001F3AF Targets Paste Karo") + "\n\nChat IDs (jaise <code>123456789</code>) ya "
            "<code>@channelusername</code> bhejo — space, comma ya nayi line se alag karke.\n\n"
            "• Users/groups ke liye numeric ID chahiye (user ne aapka bot start kiya ho).\n"
            "• @username sirf public channel/supergroup ke liye chalta hai.\n"
            "• Max %d targets." % BC_MAX_TARGETS, Kb([[B_BACK, B_CANCEL]], "123456789 -100123456 @mychannel"))


BACK_OF["bc_targets"] = "bc_menu"


@handle("bc_targets")
def h_bc_targets(m, st, key):
    if m.content_type != "text":
        reshow(st, "Text me bhejo.")
        return
    new, bad = parse_targets(m.text)
    if not new:
        reshow(st, "❌ Koi valid ID/username nahi mila. Dobara bhejo.")
        return
    added = add_targets(st["bc"], new)
    note = "✅ %d naye target jude (total %d)." % (added, len(st["bc"]["targets"]))
    if bad:
        note += " %d entries samajh nahi aayi, unhe chhod diya." % bad
    if added < len(new):
        note += " Duplicate/limit wale skip hue."
    goto(st, "bc_menu", note)


def chat_ids_from_updates(updates):
    """Only chat ids/types are read; message text and names are never kept."""
    seen = {}
    for u in updates or []:
        if not isinstance(u, dict):
            continue
        cands = []
        for v in u.values():
            if isinstance(v, dict):
                cands.append(v.get("chat"))
                cands.append((v.get("message") or {}).get("chat") if isinstance(v.get("message"), dict) else None)
        for c in cands:
            if isinstance(c, dict) and isinstance(c.get("id"), int):
                seen.setdefault(c["id"], c.get("type", "private"))
    return seen


def do_collect(st):
    token = st["token"]
    w = api(st, "getWebhookInfo")
    if not w.get("ok"):
        reshow(st, "❌ Webhook check nahi ho paya: %s" % esc(plain_error(w)))
        return
    if (w.get("result") or {}).get("url"):
        reshow(st, "⚠️ Is bot par <b>webhook laga hai</b>, isliye getUpdates se IDs nahi mil sakti. "
                   "Aap IDs paste kar sakte ho, ya Info & Status → Webhook se webhook hata sakte ho "
                   "(par tab bot ka server updates lena band kar dega).")
        return
    send(st["chat_id"], "\U0001F50D Recent updates se chat IDs dhoondh raha hoon...")
    r = call_api(token, "getUpdates", {"limit": 100, "timeout": 0})
    st["ts"] = time.time()
    if not r.get("ok"):
        reshow(st, "❌ Updates nahi mile: %s" % esc(plain_error(r)))
        return
    seen = chat_ids_from_updates(r.get("result"))
    st["bc"]["found"] = list(seen.keys())
    goto(st, "bc_collect")
    st["flow"]["types"] = Counter(seen.values())


@render("bc_collect")
def r_bc_collect(st):
    found = st["bc"]["found"]
    if not found:
        return (head("\U0001F4E5 Collect From Updates") + "\n\nAbhi koi recent chat nahi mili. Telegram updates 24 ghante "
                "tak (max 100 recent) rakhta hai, aur agar aapka bot chal raha hai to wo unhe pehle hi le chuka hoga. "
                "Paste wala tareeka use karo.", Kb([[B_FETCH_AGAIN], [B_BACK]], "Fetch again or go back"))
    return (head("\U0001F4E5 Chats Mile") + "\n\n\U0001F3AF <b>%d</b> alag chats recent updates me mile.\n"
            "(Sirf chat ID liye gaye — koi message padha ya save nahi kiya gaya.)\n\nIn sabko targets me jodu?" % len(found),
            Kb([[B_ADD_THESE], [B_FETCH_AGAIN], [B_BACK, B_CANCEL]], "Add these chats?"))


BACK_OF["bc_collect"] = "bc_menu"


@handle("bc_collect")
def h_bc_collect(m, st, key):
    bc = st["bc"]
    if key == B_ADD_THESE.key and bc["found"]:
        added = add_targets(bc, bc["found"])
        bc["found"] = []
        goto(st, "bc_menu", "✅ %d target jude (total %d)." % (added, len(bc["targets"])))
    elif key == B_FETCH_AGAIN.key:
        do_collect(st)
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("bc_text")
def r_bc_text(st):
    on = st["bc"]["html"]
    return (head("\U0001F48C Message Likho") + "\n\nJo text bhejna hai wo likho (max 4096 characters).\n"
            "\U0001F524 HTML formatting abhi <b>%s</b> hai (on par <code>&lt;b&gt;bold&lt;/b&gt;</code> chalta hai)." % ("ON" if on else "OFF"),
            Kb([[B_HTML_ON if on else B_HTML_OFF], [B_BACK, B_CANCEL]], "Write your broadcast message"))


BACK_OF["bc_text"] = "bc_menu"


@handle("bc_text")
def h_bc_text(m, st, key):
    bc = st["bc"]
    if key in (B_HTML_OFF.key, B_HTML_ON.key):
        bc["html"] = not bc["html"]
        reshow(st)
        return
    t = (m.text or "") if m.content_type == "text" else ""
    if not t.strip():
        reshow(st, "Text me message bhejo.")
    elif len(t) > 4096:
        reshow(st, "❌ Message bahut lamba hai (%d). Max 4096." % len(t))
    else:
        bc["kind"], bc["text"], bc["photo"], bc["caption"] = "text", t, None, ""
        goto(st, "bc_menu", "✅ Message save ho gaya.")


@render("bc_photo")
def r_bc_photo(st):
    return (head("\U0001F304 Broadcast Photo") + "\n\nWo photo bhejo jo broadcast karni hai (max 10 MB).",
            Kb([[B_BACK, B_CANCEL]], "Send the photo to broadcast"))


BACK_OF["bc_photo"] = "bc_menu"


@handle("bc_photo")
def h_bc_photo_text(m, st, key):
    reshow(st, "Mujhe photo chahiye (ya image file).")


def m_bc_photo(m, st):
    data, err = download_image(m, MAX_BC_PHOTO_BYTES)
    if err:
        reshow(st, err)
        return
    jpeg = prepare_image(data, square=False, max_side=2560)
    data = jpeg or data
    if len(data) > MAX_BC_PHOTO_BYTES:
        reshow(st, "❌ Photo 10 MB se badi hai.")
        return
    st["bc"]["pending_photo"] = data
    goto(st, "bc_caption")


MEDIA["bc_photo"] = m_bc_photo


@render("bc_caption")
def r_bc_caption(st):
    on = st["bc"]["html"]
    return (head("✍️ Caption") + "\n\nPhoto ke saath caption likho (max 1024 characters) ya Skip dabao.\n"
            "\U0001F524 HTML abhi <b>%s</b> hai." % ("ON" if on else "OFF"),
            Kb([[B_SKIP, B_HTML_ON if on else B_HTML_OFF], [B_BACK, B_CANCEL]], "Caption (optional)"))


BACK_OF["bc_caption"] = "bc_photo"


@handle("bc_caption")
def h_bc_caption(m, st, key):
    bc = st["bc"]
    if key in (B_HTML_OFF.key, B_HTML_ON.key):
        bc["html"] = not bc["html"]
        reshow(st)
        return
    if key == B_SKIP.key:
        cap = ""
    else:
        cap = (m.text or "").strip() if m.content_type == "text" else ""
        if not cap:
            reshow(st, "Caption text me bhejo ya Skip dabao.")
            return
        if len(cap) > 1024:
            reshow(st, "❌ Caption max 1024 characters ka ho sakta hai (aapka %d)." % len(cap))
            return
    bc["kind"], bc["photo"], bc["caption"], bc["text"] = "photo", bc.pop("pending_photo", None), cap, ""
    goto(st, "bc_menu", "✅ Photo message save ho gaya.")


@render("bc_confirm")
def r_bc_confirm(st):
    bc = st["bc"]
    n = len(bc["targets"])
    secs = int(n / float(BC_RATE_PER_SEC)) + 1
    if bc["kind"] == "text":
        prev = "\U0001F48C Text:\n<code>%s</code>" % esc(bc["text"][:300] + ("..." if len(bc["text"]) > 300 else ""))
    else:
        prev = "\U0001F304 Photo (%d KB)%s" % (max(1, len(bc["photo"] or b"") // 1024),
                                              ("\nCaption: <code>%s</code>" % esc(bc["caption"][:300])) if bc["caption"] else "")
    return (head("\U0001F9FE Broadcast Confirm") + "\n\n" + me_line(st)
            + "\n\U0001F3AF Targets: <b>%d</b> (lagbhag %d second)\n\U0001F524 HTML: %s\n\n%s\n\n"
            % (n, secs, "ON" if bc["html"] else "OFF", prev)
            + "ℹ️ Sirf wahi chats receive karengi jinhone aapka bot start kiya hai; baaki 'failed' me ginenge. "
            "Spam mat karo — Telegram aise bots ko limit/ban kar deta hai.\n\nBhej du?",
            Kb([[B_BC_SEND], [B_BC_TEST, B_BC_EDIT_MSG], [B_BC_EDIT_T, B_CANCEL]], "Send, test or edit"))


BACK_OF["bc_confirm"] = "bc_menu"


def bc_payload(bc):
    """Return (method, base data, files) for one send."""
    if bc["kind"] == "photo":
        d = {"caption": bc["caption"]} if bc["caption"] else {}
        if bc["html"] and bc["caption"]:
            d["parse_mode"] = "HTML"
        return "sendPhoto", d
    d = {"text": bc["text"], "disable_web_page_preview": True}
    if bc["html"]:
        d["parse_mode"] = "HTML"
    return "sendMessage", d


def send_one(token, bc, target, stats, cache):
    """Send the broadcast message to one target. Returns (ok, error_dict)."""
    method, d = bc_payload(bc)
    d = dict(d, chat_id=target)
    if method == "sendPhoto":
        if cache.get("file_id"):
            d["photo"] = cache["file_id"]
            r = call_api(token, method, d, max_wait=BC_MAX_WAIT, retries=3, stats=stats)
        else:
            r = call_api(token, method, d, files={"photo": ("photo.jpg", bc["photo"], "image/jpeg")},
                         max_wait=BC_MAX_WAIT, retries=3, stats=stats)
            if r.get("ok"):
                sizes = ((r.get("result") or {}).get("photo")) or []
                if sizes and isinstance(sizes[-1], dict) and sizes[-1].get("file_id"):
                    cache["file_id"] = sizes[-1]["file_id"]
    else:
        r = call_api(token, method, d, max_wait=BC_MAX_WAIT, retries=3, stats=stats)
    return bool(r.get("ok")), r


def reason_of(r):
    code = r.get("error_code")
    low = str(r.get("description") or "").lower()
    if code == 403:
        return "Bot block/start nahi kiya (403)"
    if "chat not found" in low:
        return "Chat nahi mili (ID galat ya bot start nahi hua)"
    if code == 429:
        return "Telegram flood limit"
    if r.get("network"):
        return "Network error"
    return "Other: %s" % str(r.get("description") or "unknown")[:60]


FATAL_MARKERS = ("can't parse entities", "message text is empty", "message is too long",
                 "wrong file", "image_process_failed", "photo_invalid", "wrong type of the web page")


def run_broadcast(st):
    token, bc = st["token"], st["bc"]
    targets = list(bc["targets"])
    stats = {"throttled": 0}
    sent, failed, reasons = 0, [], Counter()
    cache = {}
    interval = 1.0 / BC_RATE_PER_SEC
    last = 0.0
    fatal = None
    done = 0
    try:
        for i, t in enumerate(targets):
            if st["stop"]:
                break
            gap = interval - (time.monotonic() - last)
            if gap > 0:
                _sleep(gap)
            last = time.monotonic()
            st["ts"] = time.time()
            ok, r = send_one(token, bc, t, stats, cache)
            done = i + 1
            if ok:
                sent += 1
            else:
                low = str(r.get("description") or "").lower()
                if r.get("error_code") == 401 or any(x in low for x in FATAL_MARKERS):
                    fatal = plain_error(r)
                    break
                failed.append(t)
                reasons[reason_of(r)] += 1
            if len(targets) > 100 and done % 100 == 0 and done < len(targets):
                send(st["chat_id"], "⏳ Progress: %d/%d (sent %d, failed %d)" % (done, len(targets), sent, len(failed)),
                     RENDER["bc_sending"](st)[1])
    except Exception as e:  # keep the member informed whatever happens
        log.exception("broadcast error: %s", redact(e))
        fatal = "Andar ki koi gadbad ho gayi. Dobara try karo."
    stopped = st["stop"]
    lines = [head("\U0001F4CA Broadcast Report"), "",
             "✅ Sent: <b>%d</b>" % sent, "❌ Failed: <b>%d</b>" % len(failed)]
    left = len(targets) - done
    if stopped or fatal:
        lines.append("⏸️ Baaki (bheje nahi gaye): <b>%d</b>" % max(left, 0))
    if stopped:
        lines.append("\U0001F6D1 Aapne broadcast beech me roka.")
    if fatal:
        lines.append("⚠️ Broadcast ruk gaya: %s" % esc(fatal))
    if stats["throttled"]:
        lines.append("⏳ Telegram flood limit (429) %d baar aayi; bot ne ruk kar dobara try kiya." % stats["throttled"])
    if reasons:
        lines.append("")
        lines.append("<b>Failed ki wajah</b>")
        for why, n in reasons.most_common(6):
            lines.append("• %s: %d" % (esc(why), n))
        lines.append("Pehle kuch failed: " + esc(", ".join(str(x) for x in failed[:10])))
        lines.append("Ye wo chats hain jinhone bot start nahi kiya ya block kiya ho sakta hai.")
    st["busy"] = False
    st["stop"] = False
    finish(st, "\n".join(lines), "bc_menu")


@render("bc_sending")
def r_bc_sending(st):
    return (head("\U0001F680 Broadcast Chal Raha Hai") + "\n\nMessages ~%d/second ki speed se ja rahe hain. "
            "Khatam hone par report aayegi. Rokna ho to Stop dabao." % BC_RATE_PER_SEC,
            Kb([[B_BC_STOP]], "Broadcast running..."))


@handle("bc_sending")
def h_bc_sending(m, st, key):
    if key == B_BC_STOP.key:
        st["stop"] = True
        send(st["chat_id"], "\U0001F6D1 Rok raha hoon, bas current message ke baad...", RENDER["bc_sending"](st)[1])
    else:
        send(st["chat_id"], "⏳ Broadcast chal raha hai. Rokne ke liye Stop Broadcast dabao.",
             RENDER["bc_sending"](st)[1])


@handle("bc_confirm")
def h_bc_confirm(m, st, key):
    bc = st["bc"]
    if key == B_BC_SEND.key and bc_ready(bc):
        st["busy"], st["stop"] = True, False
        goto(st, "bc_sending")
        threading.Thread(target=run_broadcast, args=(st,), daemon=True).start()
    elif key == B_BC_TEST.key:
        uid = st["uid"]
        ok, r = send_one(st["token"], bc, uid, {"throttled": 0}, {})
        if ok:
            reshow(st, "\U0001F9EA Test aapko bhej diya gaya (aapke bot wali chat me dekho).")
        else:
            reshow(st, "\U0001F9EA Test nahi gaya: %s\nShayad aapne apna bot abhi tak start nahi kiya." % esc(plain_error(r)))
    elif key == B_BC_EDIT_MSG.key:
        goto(st, "bc_photo" if bc["kind"] == "photo" else "bc_text")
    elif key == B_BC_EDIT_T.key:
        goto(st, "bc_targets")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# ======================================================================
# More features: health report, pending updates, set webhook, chat tools,
# backup (export/import), templates, bulk apply, stars, share links
# ======================================================================
def take_flow(st, *keys):
    f = st.get("flow") or {}
    for k in keys:
        f.pop(k, None)


def short(text, n=60):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n - 1] + "…"


# ----------------------------------------------------------- health report
def do_health(st):
    send(st["chat_id"], "\U0001F3E5 Poora health check kar raha hoon, thoda time lagega...")
    r = api(st, "getMe")
    if not r.get("ok"):
        finish(st, head("\U0001F3E5 Health Report") + "\n\n❌ <b>Token kaam nahi kar raha</b>\n%s"
               % esc(plain_error(r)), "infomenu", B_REFRESH, do_health)
        return
    u = r.get("result") or {}
    st["bot"] = u
    items = []   # (ok, text)

    def add(ok_, good, bad):
        items.append((bool(ok_), good if ok_ else bad))

    for kind in ("name", "description", "about"):
        spec = FIELDS[kind]
        rr = api(st, spec["get"])
        if rr.get("ok"):
            v = (rr.get("result") or {}).get(spec["key"], "")
            add(v, "%s set hai" % spec["title"], "%s khali hai (users ko kuch dikhega nahi)" % spec["title"])
        else:
            items.append((False, "%s check nahi ho paya" % spec["title"]))
    counts = []
    for scope, label in CMD_SCOPES.values():
        rr = api(st, "getMyCommands", {"scope": {"type": scope}})
        if rr.get("ok"):
            counts.append((scope, label, len(rr.get("result") or [])))
    if counts:
        default_n = [n for s_, _l, n in counts if s_ == "default"]
        add(default_n and default_n[0] > 0, "Default commands: %d" % (default_n[0] if default_n else 0),
            "Default commands set nahi hain (users ko / menu me kuch nahi dikhega)")
        extra = ["%s: %d" % (l_, n) for s_, l_, n in counts if s_ != "default" and n]
        if extra:
            items.append((True, "Alag scope commands: " + ", ".join(extra)))
    w = api(st, "getWebhookInfo")
    if w.get("ok"):
        wi = w.get("result") or {}
        pend = int(wi.get("pending_update_count") or 0)
        if wi.get("url"):
            items.append((True, "Webhook laga hai (pending: %d)" % pend))
        else:
            items.append((True, "Webhook nahi laga (polling mode, pending: %d)" % pend))
        add(pend < 100, "Pending updates kam hain (%d)" % pend,
            "Pending updates jama hain (%d): bot updates nahi utha raha ho sakta hai" % pend)
        add(not wi.get("last_error_message"), "Webhook me koi error nahi",
            "Webhook error: %s (%s)" % (short(wi.get("last_error_message"), 80), fmt_ts(wi.get("last_error_date"))))
    mb = api(st, "getChatMenuButton")
    if mb.get("ok"):
        items.append((True, "Menu button: " + describe_menu_button(mb.get("result"))))
    for ch, label in ((False, "groups"), (True, "channels")):
        rr = api(st, "getMyDefaultAdministratorRights", {"for_channels": ch})
        if rr.get("ok"):
            n = sum(1 for v in (rr.get("result") or {}).values() if v is True)
            items.append((True, "Default admin rights (%s): %d on" % (label, n)))
    if u.get("can_join_groups") is False:
        items.append((False, "Bot groups me add nahi ho sakta (BotFather me Allow Groups off hai)"))
    good = [t for ok_, t in items if ok_]
    bad = [t for ok_, t in items if not ok_]
    lines = [head("\U0001F3E5 Health Report"), "", me_line(st),
             "\U0001F4CA Score: <b>%d/%d</b> theek" % (len(good), len(items)), ""]
    if bad:
        lines.append("<b>⚠️ Dhyan dene layak</b>")
        lines += ["• " + esc(t) for t in bad]
        lines.append("")
    lines.append("<b>✅ Theek hai</b>")
    lines += ["• " + esc(t) for t in good]
    finish(st, "\n".join(lines), "infomenu", B_REFRESH, do_health)


# -------------------------------------------------------- pending updates
@render("pending")
def r_pending(st):
    return (head("\U0001F4EE Pending Updates") + "\n\n" + me_line(st)
            + "\n\n\U0001F52D <b>Peek</b> = pending updates ginti aur type dekho (kuch delete nahi hota).\n"
            "\U0001F6BF <b>Clear Pending</b> = pending updates hata do, <b>webhook ko chhue bina</b> "
            "(sirf tab jab webhook laga na ho).",
            Kb([[B_PEEK, B_PEND_CLEAR], [B_BACK, B_HOME]], "Peek or clear pending updates"))


BACK_OF["pending"] = "infomenu"


@handle("pending")
def h_pending(m, st, key):
    if key == B_PEEK.key:
        do_peek(st)
    elif key == B_PEND_CLEAR.key:
        st["flow"] = {"back": "pending"}
        goto(st, "pend_confirm")
    else:
        reshow(st, "Neeche ke buttons use karo.")


def webhook_blocks_updates(st):
    """Returns (blocked, message). getUpdates does not work while a webhook is set."""
    w = api(st, "getWebhookInfo")
    if not w.get("ok"):
        return True, "❌ Webhook check nahi ho paya: %s" % esc(plain_error(w))
    wi = w.get("result") or {}
    if wi.get("url"):
        return True, ("⚠️ Is bot par <b>webhook laga hai</b> (pending: %s), isliye updates yahan se nahi "
                      "dekhe/hataye ja sakte. Webhook menu se reset karo ya server band karo."
                      % esc(wi.get("pending_update_count", 0)))
    return False, ""


def do_peek(st):
    blocked, why = webhook_blocks_updates(st)
    if blocked:
        finish(st, head("\U0001F52D Pending Updates") + "\n\n" + why, "pending", B_REFRESH, do_peek)
        return
    r = api(st, "getUpdates", {"limit": 100, "timeout": 0})
    if not r.get("ok"):
        finish(st, head("\U0001F52D Pending Updates") + "\n\n❌ Updates nahi mile: %s" % esc(plain_error(r)),
               "pending", B_REFRESH, do_peek)
        return
    ups = [u for u in (r.get("result") or []) if isinstance(u, dict)]
    if not ups:
        finish(st, head("\U0001F52D Pending Updates") + "\n\n✅ Koi pending update nahi hai.",
               "pending", B_REFRESH, do_peek)
        return
    kinds = Counter()
    dates = []
    for u in ups:
        for k, v in u.items():
            if k != "update_id":
                kinds[k] += 1
                if isinstance(v, dict) and isinstance(v.get("date"), int):
                    dates.append(v["date"])
    chats = chat_ids_from_updates(ups)
    lines = [head("\U0001F52D Pending Updates"), "",
             "\U0001F4E5 Updates: <b>%d</b>%s" % (len(ups), "+ (pehle 100 dekhe)" if len(ups) >= 100 else ""),
             "\U0001F465 Alag chats: <b>%d</b>" % len(chats), ""]
    lines += ["• %s: %d" % (esc(k), n) for k, n in kinds.most_common(8)]
    if dates:
        lines += ["", "\U0001F570️ Sabse purana: %s" % fmt_ts(min(dates)), "\U0001F195 Sabse naya: %s" % fmt_ts(max(dates))]
    lines += ["", "ℹ️ Sirf ginti dekhi gayi hai. Message ka text save ya dikhaya nahi gaya, aur kuch confirm/delete nahi hua."]
    finish(st, "\n".join(lines), "pending", B_REFRESH, do_peek)


@render("pend_confirm")
def r_pend_confirm(st):
    return (head("\U0001F6BF Pending Clear Karein?") + "\n\n" + me_line(st)
            + "\n\nSaare pending updates hamesha ke liye hat jayenge (bot unhe kabhi nahi dekh payega). "
            "Webhook waisa hi rahega. Pakka?",
            Kb([[B_YES_CLEAR, B_KEEP], [B_BACK]], "Clear all pending updates?"))


BACK_OF["pend_confirm"] = "pending"


@handle("pend_confirm")
def h_pend_confirm(m, st, key):
    if key == B_YES_CLEAR.key:
        do_clear_pending(st)
    elif key == B_KEEP.key:
        goto(st, "pending", "\U0001F6DF Pending updates waise hi rahenge.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


def do_clear_pending(st):
    blocked, why = webhook_blocks_updates(st)
    if blocked:
        goto(st, "pending", why)
        return
    # offset=-1 returns only the newest update and forgets everything before it;
    # confirming newest+1 then forgets that one too.
    r = api(st, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
    if not r.get("ok"):
        goto(st, "pend_confirm", "❌ Pending clear nahi hue: %s" % esc(plain_error(r)))
        return
    res = r.get("result") or []
    if not res:
        finish(st, "✅ Koi pending update tha hi nahi.", "pending")
        return
    last = res[-1].get("update_id")
    if not isinstance(last, int):
        goto(st, "pend_confirm", "❌ Pending clear nahi hue: Telegram ka jawab samajh nahi aaya.")
        return
    r2 = api(st, "getUpdates", {"offset": last + 1, "limit": 1, "timeout": 0})
    if not r2.get("ok"):
        goto(st, "pend_confirm", "❌ Pending clear nahi hue: %s" % esc(plain_error(r2)))
        return
    finish(st, "✅ Pending updates clear ho gaye. Webhook ko chhua nahi gaya.", "pending", B_PEEK, do_peek)


# ------------------------------------------------------------ set webhook
WH_PORTS = (80, 88, 443, 8443)


@render("wh_url")
def r_wh_url(st):
    return (head("\U0001F50C Naya Webhook") + "\n\n" + me_line(st)
            + "\n\nWebhook ka <b>https://</b> URL bhejo (jahan Telegram updates bhejega). Port 443, 80, 88 ya "
            "8443 ho sakta hai.\n\n⚠️ Webhook lagne ke baad bot ka server us URL par updates sambhalega; "
            "getUpdates se updates nahi milenge.",
            Kb([[B_BACK, B_CANCEL]], "Type the https:// webhook URL"))


BACK_OF["wh_url"] = "webhook"


def valid_webhook_url(url):
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and bool(p.hostname) and " " not in url and len(url) <= 512
            and (port is None or port in WH_PORTS) and not TOKEN_RE.search(url))


@handle("wh_url")
def h_wh_url(m, st, key):
    url = (m.text or "").strip() if m.content_type == "text" else ""
    if not valid_webhook_url(url):
        reshow(st, "❌ Valid <b>https://</b> URL chahiye (port 443/80/88/8443, bina space ke).")
        return
    f = flow_of(st)
    f["wh_url"] = url
    f["wh_drop"] = False
    goto(st, "wh_confirm")


@render("wh_confirm")
def r_wh_confirm(st):
    f = flow_of(st)
    drop = bool(f.get("wh_drop"))
    return (head("\U0001F9FE Webhook Confirm") + "\n\n" + me_line(st)
            + "\n\U0001F310 URL: <code>%s</code>" % esc(f.get("wh_url", ""))
            + "\n\U0001F4A4 Purane pending updates hatao: <b>%s</b>" % ("haan" if drop else "nahi")
            + "\n\nWebhook set kar du? (purana webhook, agar hai, badal jayega)",
            Kb([[B_APPLY], [B_DROP_ON if drop else B_DROP_OFF], [B_BACK, B_CANCEL]],
               "Set this webhook?"))


BACK_OF["wh_confirm"] = "wh_url"


@handle("wh_confirm")
def h_wh_confirm(m, st, key):
    f = flow_of(st)
    if key in (B_DROP_ON.key, B_DROP_OFF.key):
        f["wh_drop"] = not f.get("wh_drop")
        reshow(st)
    elif key == B_APPLY.key:
        r = api(st, "setWebhook", {"url": f.get("wh_url", ""), "drop_pending_updates": bool(f.get("wh_drop"))})
        if r.get("ok"):
            finish(st, "✅ Webhook set ho gaya!\n\U0001F310 <code>%s</code>\n\nCheck Webhook se dekho ki Telegram "
                       "us URL tak pahunch pa raha hai ya nahi." % esc(f.get("wh_url", "")),
                   "webhook", B_REFRESH, do_webhook_check)
        else:
            goto(st, "wh_confirm", "❌ Webhook set nahi hua: %s" % esc(plain_error(r)))
    else:
        reshow(st, "Neeche ke buttons use karo.")


# -------------------------------------------------------------- chat tools
CT_ACTIONS = {"info": "Chat Info", "test": "Test Message", "leave": "Leave Chat"}
CT_NOTE = ("ℹ️ Ye sirf un chats ke liye chalta hai jahan aapka bot <b>pehle se maujood</b> hai "
           "(group/channel me add ho ya user ne start kiya ho). Bot API se bot ki chats ki list nahi milti.")


@render("ct_menu")
def r_ct_menu(st):
    return (head("\U0001F6F0️ Chat Tools") + "\n\n" + me_line(st) + "\n\n"
            "\U0001F4C7 <b>Chat Info</b> = chat ka naam, type, members, bot ka status\n"
            "\U0001F4E9 <b>Send Test Message</b> = bot se ek chat me message bhejo\n"
            "\U0001F6B6 <b>Leave Chat</b> = bot ko group/channel se nikalo\n\n" + CT_NOTE,
            Kb([[B_CT_INFO], [B_CT_TEST, B_CT_LEAVE], [B_BACK, B_HOME]], "Pick a chat tool"))


BACK_OF["ct_menu"] = "main"


@handle("ct_menu")
def h_ct_menu(m, st, key):
    action = {B_CT_INFO.key: "info", B_CT_TEST.key: "test", B_CT_LEAVE.key: "leave"}.get(key)
    if action:
        st["flow"] = {"back": "ct_menu", "action": action}
        goto(st, "ct_id")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("ct_id")
def r_ct_id(st):
    a = flow_of(st).get("action", "info")
    return (head("\U0001F194 Chat ID Bhejo — %s" % esc(CT_ACTIONS.get(a, "Chat"))) + "\n\n"
            "Chat ki <b>numeric ID</b> (jaise <code>-1001234567890</code>) ya public chat ka "
            "<code>@username</code> bhejo.\n\n" + CT_NOTE,
            Kb([[B_BACK, B_CANCEL]], "Send the chat ID or @username"))


BACK_OF["ct_id"] = "ct_menu"


def parse_chat_ref(text):
    t = (text or "").strip()
    if re.fullmatch(r"-?\d{1,20}", t):
        return int(t)
    mt = re.fullmatch(r"(?:(?:https?://)?t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})", t)
    return "@" + mt.group(1) if mt else None


@handle("ct_id")
def h_ct_id(m, st, key):
    cid = parse_chat_ref(m.text if m.content_type == "text" else "")
    if cid is None:
        reshow(st, "❌ Chat ID number ho ya <code>@username</code> (jaise <code>-1001234567890</code>).")
        return
    f = flow_of(st)
    f["chat"] = cid
    a = f.get("action", "info")
    c = api(st, "getChat", {"chat_id": cid})
    if not c.get("ok"):
        reshow(st, "❌ Ye chat nahi khuli: %s\nBot is chat me hona chahiye." % esc(plain_error(c)))
        return
    ch = c.get("result") or {}
    f["chat_title"] = ch.get("title") or ch.get("username") or ch.get("first_name") or str(cid)
    f["chat_type"] = ch.get("type", "?")
    if a == "info":
        do_chat_info(st, ch)
    elif a == "test":
        goto(st, "ct_text")
    else:
        goto(st, "ct_leave_confirm")


def do_chat_info(st, ch):
    f = flow_of(st)
    cid = f.get("chat")
    lines = [head("\U0001F4C7 Chat Info"), "",
             "\U0001F4DB Naam: <b>%s</b>" % esc(f.get("chat_title", "?")),
             "\U0001F4C2 Type: %s" % esc(ch.get("type", "?")),
             "\U0001F194 ID: <code>%s</code>" % esc(ch.get("id", cid))]
    if ch.get("username"):
        lines.append("\U0001F517 Username: @%s" % esc(ch["username"]))
    if ch.get("description"):
        lines.append("\U0001F4DD Description: %s" % esc(short(ch["description"], 150)))
    n = api(st, "getChatMemberCount", {"chat_id": cid})
    if n.get("ok"):
        lines.append("\U0001F465 Members: <b>%s</b>" % esc(n.get("result")))
    me_id = (st.get("bot") or {}).get("id")
    if me_id:
        cm = api(st, "getChatMember", {"chat_id": cid, "user_id": me_id})
        if cm.get("ok"):
            lines.append("\U0001F916 Bot ka status: <b>%s</b>" % esc((cm.get("result") or {}).get("status", "?")))
    if ch.get("type") in ("group", "supergroup", "channel"):
        ad = api(st, "getChatAdministrators", {"chat_id": cid})
        if ad.get("ok"):
            lines.append("\U0001F451 Admins: <b>%d</b>" % len(ad.get("result") or []))
    finish(st, "\n".join(lines), "ct_menu", B_VIEW_ANOTHER,
           lambda s: (s.__setitem__("flow", {"back": "ct_menu", "action": "info"}), goto(s, "ct_id")))


@render("ct_text")
def r_ct_text(st):
    f = flow_of(st)
    return (head("\U0001F4E9 Test Message Likho") + "\n\n\U0001F4AC Chat: <b>%s</b>" % esc(f.get("chat_title", "?"))
            + "\n\nJo message bot ke naam se bhejna hai wo likho (max 4000 characters, plain text).",
            Kb([[B_BACK, B_CANCEL]], "Type the message to send"))


BACK_OF["ct_text"] = "ct_id"


@handle("ct_text")
def h_ct_text(m, st, key):
    t = (m.text or "").strip() if m.content_type == "text" else ""
    if not t:
        reshow(st, "Text me message bhejo.")
        return
    if len(t) > 4000:
        reshow(st, "❌ Bahut lamba hai (%d). Max 4000 characters." % len(t))
        return
    flow_of(st)["text"] = t
    goto(st, "ct_send_confirm")


@render("ct_send_confirm")
def r_ct_send_confirm(st):
    f = flow_of(st)
    return (head("\U0001F9FE Message Bhejna Hai?") + "\n\n\U0001F4AC Chat: <b>%s</b> (%s)"
            % (esc(f.get("chat_title", "?")), esc(f.get("chat_type", "?")))
            + "\n\n<code>%s</code>\n\nBot ke naam se ye message bhej du?" % esc(short(f.get("text", ""), 300)),
            Kb([[B_YES_SEND, B_NO], [B_EDIT_AGAIN, B_CANCEL]], "Send this message?"))


BACK_OF["ct_send_confirm"] = "ct_text"


@handle("ct_send_confirm")
def h_ct_send_confirm(m, st, key):
    f = flow_of(st)
    if key == B_YES_SEND.key:
        r = api(st, "sendMessage", {"chat_id": f.get("chat"), "text": f.get("text", "")})
        if r.get("ok"):
            finish(st, "✅ Message bhej diya gaya: <b>%s</b>" % esc(f.get("chat_title", "?")), "ct_menu")
        else:
            goto(st, "ct_send_confirm", "❌ Message nahi gaya: %s" % esc(plain_error(r)))
    elif key == B_EDIT_AGAIN.key:
        goto(st, "ct_text")
    elif key == B_NO.key:
        goto(st, "ct_menu", "\U0001F645 Message nahi bheja.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("ct_leave_confirm")
def r_ct_leave(st):
    f = flow_of(st)
    return (head("\U0001F6B6 Chat Chhodni Hai?") + "\n\n\U0001F4AC Chat: <b>%s</b> (%s)"
            % (esc(f.get("chat_title", "?")), esc(f.get("chat_type", "?")))
            + "\n\nBot is chat se nikal jayega. Wapas lane ke liye use dobara add karna padega. Pakka?",
            Kb([[B_YES_LEAVE, B_KEEP], [B_BACK]], "Make the bot leave this chat?"))


BACK_OF["ct_leave_confirm"] = "ct_id"


@handle("ct_leave_confirm")
def h_ct_leave(m, st, key):
    f = flow_of(st)
    if key == B_YES_LEAVE.key:
        r = api(st, "leaveChat", {"chat_id": f.get("chat")})
        if r.get("ok"):
            finish(st, "✅ Bot ne chat chhod di: <b>%s</b>" % esc(f.get("chat_title", "?")), "ct_menu")
        else:
            goto(st, "ct_leave_confirm", "❌ Chat nahi chhodi ja saki: %s" % esc(plain_error(r)))
    elif key == B_KEEP.key:
        goto(st, "ct_menu", "\U0001F6DF Bot chat me hi rahega.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# ------------------------------------------------- backup / snapshots core
SNAP_FORMAT = "bot-manager-pro-settings"
SNAP_SCOPES = [v[0] for v in CMD_SCOPES.values()]
RIGHT_KEYS = {k for k, _l, _e in RIGHTS_GROUP + RIGHTS_CHANNEL} | set(RIGHTS_REQUIRED)
ALL_PARTS = ("name", "description", "about", "commands", "menu", "rights")
BULK_PARTS = ("description", "about", "commands", "menu", "rights")   # never the name
BULK_MAX = 10
TPL_MAX = 5
IMPORT_MAX_BYTES = 1024 * 1024


def new_snapshot():
    return {"format": SNAP_FORMAT, "version": 1, "texts": {k: {} for k in FIELDS},
            "commands": {}, "menu_button": None, "rights": {"groups": {}, "channels": {}}}


def collect_snapshot(token, touch=None):
    """Read a bot's settings through the API. Returns (snapshot|None, [problems])."""
    errors = []
    snap = new_snapshot()

    def call(method, data=None):
        if touch:
            touch()
        return call_api(token, method, data)

    for kind, spec in FIELDS.items():
        r = call(spec["get"])
        if not r.get("ok"):
            if r.get("error_code") == 401 or r.get("network"):
                return None, [plain_error(r)]
            errors.append("%s: %s" % (spec["title"], plain_error(r)))
            continue
        base = (r.get("result") or {}).get(spec["key"], "") or ""
        snap["texts"][kind][""] = base
        for code, _n, _f in LANGS:
            rr = call(spec["get"], {"language_code": code})
            if rr.get("ok"):
                v = (rr.get("result") or {}).get(spec["key"], "") or ""
                if v and v != base:
                    snap["texts"][kind][code] = v

    def clean(cmds):
        return [{"command": c.get("command", ""), "description": c.get("description", "")}
                for c in cmds if isinstance(c, dict)]

    base_cmds = None
    for scope, _label in CMD_SCOPES.values():
        r = call("getMyCommands", {"scope": {"type": scope}})
        if r.get("ok"):
            cmds = clean(r.get("result") or [])
            if scope == "default":
                base_cmds = cmds
            if cmds:
                snap["commands"][scope + ":"] = cmds
        else:
            errors.append("Commands (%s): %s" % (scope, plain_error(r)))
    for code, _n, _f in LANGS:
        r = call("getMyCommands", {"scope": {"type": "default"}, "language_code": code})
        if r.get("ok"):
            cmds = clean(r.get("result") or [])
            if cmds and cmds != base_cmds:
                snap["commands"]["default:" + code] = cmds
    r = call("getChatMenuButton")
    if r.get("ok"):
        mb = r.get("result") or {}
        if mb.get("type") in ("default", "commands"):
            snap["menu_button"] = {"type": mb["type"]}
        elif mb.get("type") == "web_app" and (mb.get("web_app") or {}).get("url"):
            snap["menu_button"] = {"type": "web_app", "text": mb.get("text", "Open"),
                                   "web_app": {"url": mb["web_app"]["url"]}}
    else:
        errors.append("Menu button: " + plain_error(r))
    for key, ch in (("groups", False), ("channels", True)):
        r = call("getMyDefaultAdministratorRights", {"for_channels": ch})
        if r.get("ok"):
            snap["rights"][key] = {k: v for k, v in (r.get("result") or {}).items()
                                   if k in RIGHT_KEYS and isinstance(v, bool)}
        else:
            errors.append("Admin rights (%s): %s" % (key, plain_error(r)))
    return snap, errors


def validate_snapshot(obj):
    """Check an imported JSON object. Returns (clean snapshot|None, [problems])."""
    if not isinstance(obj, dict) or obj.get("format") != SNAP_FORMAT:
        return None, ["Ye Bot Manager Pro ki settings file nahi lag rahi."]
    if obj.get("version") != 1:
        return None, ["File ka version support nahi hai."]
    problems = []
    snap = new_snapshot()
    texts = obj.get("texts") if isinstance(obj.get("texts"), dict) else {}
    for kind, spec in FIELDS.items():
        d = texts.get(kind)
        if not isinstance(d, dict):
            continue
        for code, val in d.items():
            if not (isinstance(code, str) and (code == "" or re.fullmatch(r"[a-z]{2}", code))) \
                    or not isinstance(val, str):
                problems.append("%s: galat entry skip hui" % spec["title"])
                continue
            val = val.strip()
            if not val:
                continue
            if len(val) > spec["limit"]:
                problems.append("%s (%s) %d se lamba hai, skip" % (spec["title"], code or "default", spec["limit"]))
                continue
            snap["texts"][kind][code] = val
    cmds = obj.get("commands") if isinstance(obj.get("commands"), dict) else {}
    for key, lst in cmds.items():
        scope, sep, code = str(key).partition(":")
        if not sep or scope not in SNAP_SCOPES or not (code == "" or re.fullmatch(r"[a-z]{2}", code)) \
                or not isinstance(lst, list):
            problems.append("Commands list '%s' galat hai, skip" % short(key, 30))
            continue
        good, seen = [], set()
        for c in lst:
            name = c.get("command") if isinstance(c, dict) else None
            desc = c.get("description") if isinstance(c, dict) else None
            if (isinstance(name, str) and re.fullmatch(r"[a-z0-9_]{1,32}", name) and name not in seen
                    and isinstance(desc, str) and 1 <= len(desc.strip()) <= 256):
                seen.add(name)
                good.append({"command": name, "description": desc.strip()})
            else:
                problems.append("Ek galat command skip hui (%s)" % short(key, 30))
        if good:
            snap["commands"][key] = good[:100]
    mb = obj.get("menu_button")
    if isinstance(mb, dict):
        t = mb.get("type")
        wa = mb.get("web_app") if isinstance(mb.get("web_app"), dict) else {}
        if t in ("default", "commands"):
            snap["menu_button"] = {"type": t}
        elif t == "web_app" and isinstance(mb.get("text"), str) and 1 <= len(mb["text"]) <= 64 \
                and isinstance(wa.get("url"), str) and valid_https(wa["url"]):
            snap["menu_button"] = {"type": "web_app", "text": mb["text"], "web_app": {"url": wa["url"]}}
        else:
            problems.append("Menu button galat hai, skip")
    rights = obj.get("rights") if isinstance(obj.get("rights"), dict) else {}
    for key in ("groups", "channels"):
        d = rights.get(key)
        if isinstance(d, dict):
            snap["rights"][key] = {k: v for k, v in d.items() if k in RIGHT_KEYS and isinstance(v, bool)}
    if not snapshot_ops(snap, ALL_PARTS):
        return None, ["Is file me lagane layak kuch nahi mila."] + problems[:3]
    return snap, problems


def valid_https(url):
    p = urlparse(url)
    return p.scheme == "https" and bool(p.netloc) and " " not in url and len(url) <= 512


def snapshot_ops(snap, parts):
    """List of (label, method, data) that applying `snap` would run."""
    ops = []
    for kind, spec in FIELDS.items():
        if kind not in parts:
            continue
        for code, val in (snap.get("texts", {}).get(kind) or {}).items():
            if val:
                ops.append(("%s (%s)" % (spec["title"], code or "default"), spec["set"],
                            lang_data({"lang": code}, {spec["param"]: val})))
    if "commands" in parts:
        for key, cmds in (snap.get("commands") or {}).items():
            scope, _s, code = key.partition(":")
            ops.append(("Commands (%s%s)" % (scope, "/" + code if code else ""), "setMyCommands",
                        cmd_data({"scope": scope, "lang": code}, {"commands": cmds})))
    if "menu" in parts and snap.get("menu_button"):
        ops.append(("Menu button", "setChatMenuButton", {"menu_button": snap["menu_button"]}))
    if "rights" in parts:
        for key, ch in (("groups", False), ("channels", True)):
            rights = (snap.get("rights") or {}).get(key) or {}
            if rights:
                full = {k: False for k in RIGHTS_REQUIRED}
                full.update(rights)
                ops.append(("Admin rights (%s)" % key, "setMyDefaultAdministratorRights",
                            {"rights": full, "for_channels": ch}))
    return ops


def apply_snapshot(token, snap, parts, touch=None, stop=None):
    """Apply a snapshot to a bot. Returns (done_labels, [(label, reason)])."""
    done, failed = [], []
    for label, method, data in snapshot_ops(snap, parts):
        if stop and stop():
            break
        if touch:
            touch()
        r = call_api(token, method, data)
        if r.get("ok"):
            done.append(label)
        else:
            failed.append((label, plain_error(r)))
            if r.get("error_code") == 401:
                break
    return done, failed


def snapshot_summary(snap):
    t = snap.get("texts", {})
    lines = []
    for kind, spec in FIELDS.items():
        d = t.get(kind) or {}
        langs = [c for c in d if c]
        if d:
            lines.append("\U0001F4DD %s: %s%s" % (esc(spec["title"]), "default" if "" in d else "-",
                                                  (" + %d language" % len(langs)) if langs else ""))
    cmds = snap.get("commands") or {}
    if cmds:
        lines.append("\U0001F9E9 Commands: %d list, %d commands" % (len(cmds), sum(len(v) for v in cmds.values())))
    if snap.get("menu_button"):
        lines.append("\U0001F518 Menu button: %s" % esc(snap["menu_button"].get("type")))
    r = snap.get("rights") or {}
    if r.get("groups") or r.get("channels"):
        lines.append("\U0001F6E1️ Default rights: groups %s, channels %s"
                     % ("haan" if r.get("groups") else "nahi", "haan" if r.get("channels") else "nahi"))
    return "\n".join(lines) if lines else "(kuch nahi)"


def apply_report(title, done, failed, extra=""):
    lines = [head(title), "", "✅ Lag gaye: <b>%d</b>" % len(done), "❌ Fail: <b>%d</b>" % len(failed)]
    if extra:
        lines += ["", extra]
    if failed:
        lines.append("")
        lines += ["• %s: %s" % (esc(lbl), esc(short(why, 90))) for lbl, why in failed[:8]]
        if len(failed) > 8:
            lines.append("... aur %d" % (len(failed) - 8))
    return "\n".join(lines)


def send_doc(chat_id, name, data, caption=""):
    """Send a file to the member. Returns True on success."""
    try:
        f = io.BytesIO(data)
        f.name = name
        bot.send_document(chat_id, f, caption=caption[:900] or None)
        return True
    except Exception as e:
        log.warning("send_document failed: %s", redact(e))
        return False


def touch_fn(st):
    return lambda: st.__setitem__("ts", time.time())


# ------------------------------------------------------------ backup menu
BACKUP_NOTE = ("ℹ️ Profile photo is backup/clone me shamil nahi hai: Bot API se bot ki apni photo nikalne ka "
               "koi reliable tareeka nahi hai. Photo alag se Profile Photo menu se lagao.")


@render("backup")
def r_backup(st):
    return (head("\U0001F4BC Backup & Clone") + "\n\n" + me_line(st) + "\n\n"
            "\U0001F4E6 <b>Export</b> = bot ki settings ek JSON file me (token file me nahi hota)\n"
            "\U0001F4C2 <b>Import</b> = wo file is bot par lagao\n"
            "\U0001F5C2️ <b>Templates</b> = settings session me save karke baad me lagao\n"
            "\U0001F4DA <b>Bulk Apply</b> = is bot ki settings (naam chhod kar) kai bots par ek saath\n\n"
            + BACKUP_NOTE,
            Kb([[B_EXPORT, B_IMPORT], [B_TPL, B_BULK], [B_BACK, B_HOME]], "Backup, import or clone"))


BACK_OF["backup"] = "main"


@handle("backup")
def h_backup(m, st, key):
    if key == B_EXPORT.key:
        do_export(st)
    elif key == B_IMPORT.key:
        st["flow"] = {"back": "backup"}
        goto(st, "imp_wait")
    elif key == B_TPL.key:
        goto(st, "tpl_menu")
    elif key == B_BULK.key:
        st["flow"] = {"back": "backup", "bulk": []}
        goto(st, "bulk_tokens")
    else:
        reshow(st, "Neeche ke buttons use karo.")


def do_export(st):
    send(st["chat_id"], "\U0001F4E6 Settings padh raha hoon...")
    snap, errors = collect_snapshot(st["token"], touch_fn(st))
    if snap is None:
        goto(st, "backup", "❌ Export nahi ho paya: %s" % esc(errors[0] if errors else "?"))
        return
    uname = (st.get("bot") or {}).get("username", "bot")
    snap["source"] = uname
    snap["exported_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data = json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8")
    fname = "settings-%s.json" % re.sub(r"[^A-Za-z0-9_]", "", uname)[:40]
    if not send_doc(st["chat_id"], fname, data, "Bot Manager Pro settings: @%s" % uname):
        goto(st, "backup", "❌ File bhej nahi paya. Dobara try karo.")
        return
    text = head("\U0001F4E6 Export Ho Gaya") + "\n\nFile upar bheji gayi hai. Ismein ye hai:\n" + snapshot_summary(snap)
    if errors:
        text += "\n\n⚠️ Kuch cheezein padh nahi payi:\n" + "\n".join("• " + esc(short(e, 90)) for e in errors[:5])
    text += "\n\n" + BACKUP_NOTE + "\nFile me token nahi hai, phir bhi use sambhal ke rakho."
    finish(st, text, "backup")


# ----------------------------------------------------------------- import
@render("imp_wait")
def r_imp_wait(st):
    return (head("\U0001F4C2 Settings File Bhejo") + "\n\nExport se mili <b>.json</b> file yahan bhejo (file ki tarah, "
            "max 1 MB). Pehle main file check karunga aur dikhaunga ki kya badlega, tab apply hoga.",
            Kb([[B_BACK, B_CANCEL]], "Send the settings .json file"))


BACK_OF["imp_wait"] = "backup"


@handle("imp_wait")
def h_imp_wait_text(m, st, key):
    reshow(st, "Mujhe settings ki <b>.json file</b> chahiye (file ki tarah bhejo).")


def m_imp_wait(m, st):
    d = getattr(m, "document", None) if m.content_type == "document" else None
    if d is None:
        reshow(st, "Mujhe settings ki <b>.json file</b> chahiye (file ki tarah bhejo).")
        return
    size = getattr(d, "file_size", None)
    if size and size > IMPORT_MAX_BYTES:
        reshow(st, "❌ File bahut badi hai (max 1 MB).")
        return
    try:
        info = bot.get_file(d.file_id)
        raw = bot.download_file(info.file_path)
    except Exception as e:
        log.warning("import download failed: %s", redact(e))
        reshow(st, "❌ File download nahi ho payi. Dobara bhejo.")
        return
    if not raw or len(raw) > IMPORT_MAX_BYTES:
        reshow(st, "❌ File khali ya bahut badi hai.")
        return
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        reshow(st, "❌ File padhi nahi ja saki: ye valid JSON nahi hai.")
        return
    snap, problems = validate_snapshot(obj)
    if snap is None:
        reshow(st, "❌ " + esc(problems[0]))
        return
    f = flow_of(st)
    f["snap"], f["problems"] = snap, problems
    goto(st, "imp_confirm")


MEDIA["imp_wait"] = m_imp_wait


@render("imp_confirm")
def r_imp_confirm(st):
    f = flow_of(st)
    snap = f.get("snap") or new_snapshot()
    text = (head("\U0001F9FE Import Confirm") + "\n\n" + me_line(st)
            + "\n\nFile se ye lagega:\n" + snapshot_summary(snap)
            + "\n\n⚠️ Ye bot ke maujooda naam/texts/commands/menu/rights <b>badal dega</b> (jo file me hain). "
            "Khali entries skip hoti hain.")
    if f.get("problems"):
        text += "\n\nSkip hui: " + "; ".join(esc(p) for p in f["problems"][:4])
    return text, Kb([[B_APPLY, B_NO], [B_BACK]], "Apply the imported settings?")


BACK_OF["imp_confirm"] = "imp_wait"


@handle("imp_confirm")
def h_imp_confirm(m, st, key):
    f = flow_of(st)
    if key == B_APPLY.key and f.get("snap"):
        send(st["chat_id"], "\U0001F4C2 Settings laga raha hoon...")
        done, failed = apply_snapshot(st["token"], f["snap"], ALL_PARTS, touch_fn(st))
        f.pop("snap", None)
        finish(st, apply_report("\U0001F4C2 Import Report", done, failed), "backup")
    elif key == B_NO.key:
        goto(st, "backup", "\U0001F645 Import nahi kiya.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# -------------------------------------------------------------- templates
def tpl_buttons(st):
    out = []
    for i, t in enumerate(st.get("tpls") or []):
        out.append(Btn(TPL_EMOJI[i % len(TPL_EMOJI)], "%d. %s" % (i + 1, t["name"]), P if i % 2 == 0 else S))
    return out


def tpl_cur(st):
    i = flow_of(st).get("tpl")
    tpls = st.get("tpls") or []
    return tpls[i] if isinstance(i, int) and 0 <= i < len(tpls) else None


@render("tpl_menu")
def r_tpl_menu(st):
    tpls = st.get("tpls") or []
    rows = [[b_] for b_ in tpl_buttons(st)]
    if len(tpls) < TPL_MAX:
        rows.append([B_TPL_SAVE])
    rows.append([B_BACK, B_HOME])
    return (head("\U0001F5C2️ Templates") + "\n\n" + me_line(st) + "\n\n"
            + ("Saved templates: <b>%d/%d</b>. Ek chuno ya naya save karo." % (len(tpls), TPL_MAX) if tpls else
               "Abhi koi template nahi. <b>Save As Template</b> se is bot ki settings save karo.")
            + "\n\nℹ️ Templates sirf is session me rehte hain (kuch der idle rehne par ya restart par chale jate "
            "hain). Permanent copy ke liye Export file rakho.",
            Kb(rows, "Pick a template or save one"))


BACK_OF["tpl_menu"] = "backup"


@handle("tpl_menu")
def h_tpl_menu(m, st, key):
    if key == B_TPL_SAVE.key:
        if len(st.get("tpls") or []) >= TPL_MAX:
            reshow(st, "❌ Max %d templates. Pehle koi delete karo." % TPL_MAX)
            return
        st["flow"] = {"back": "tpl_menu"}
        goto(st, "tpl_name")
        return
    for i, b_ in enumerate(tpl_buttons(st)):
        if key == b_.key:
            st["flow"] = {"back": "tpl_menu", "tpl": i}
            goto(st, "tpl_view")
            return
    reshow(st, "Neeche ke buttons use karo.")


@render("tpl_name")
def r_tpl_name(st):
    return (head("\U0001F516 Template Ka Naam") + "\n\nTemplate ka chhota naam bhejo (max 20 characters). "
            "Is bot ki abhi ki settings usme save hongi.",
            Kb([[B_BACK, B_CANCEL]], "Type a short template name"))


BACK_OF["tpl_name"] = "tpl_menu"


@handle("tpl_name")
def h_tpl_name(m, st, key):
    name = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", (m.text or "") if m.content_type == "text" else "")).strip()
    if not name or len(name) > 20:
        reshow(st, "❌ Naam 1 se 20 characters ka hona chahiye.")
        return
    if any(t["name"].lower() == name.lower() for t in st.get("tpls") or []):
        reshow(st, "❌ Is naam ka template pehle se hai.")
        return
    send(st["chat_id"], "\U0001F516 Settings padh raha hoon...")
    snap, errors = collect_snapshot(st["token"], touch_fn(st))
    if snap is None:
        reshow(st, "❌ Template save nahi hua: %s" % esc(errors[0] if errors else "?"))
        return
    snap["source"] = (st.get("bot") or {}).get("username", "")
    st.setdefault("tpls", []).append({"name": name, "snap": snap})
    goto(st, "tpl_menu", "✅ Template <b>%s</b> save ho gaya." % esc(name))


@render("tpl_view")
def r_tpl_view(st):
    t = tpl_cur(st)
    if not t:
        return (head("\U0001F5C2️ Template") + "\n\nYe template ab nahi mila.",
                Kb([[B_BACK, B_HOME]], "Go back"))
    return (head("\U0001F5C2️ Template: %s" % esc(t["name"])) + "\n\n"
            + ("\U0001F916 Source: @%s\n\n" % esc(t["snap"].get("source")) if t["snap"].get("source") else "")
            + snapshot_summary(t["snap"]),
            Kb([[B_TPL_USE, B_TPL_DEL], [B_BACK, B_HOME]], "Apply or delete this template"))


BACK_OF["tpl_view"] = "tpl_menu"


@handle("tpl_view")
def h_tpl_view(m, st, key):
    if not tpl_cur(st):
        goto(st, "tpl_menu")
    elif key == B_TPL_USE.key:
        goto(st, "tpl_apply_confirm")
    elif key == B_TPL_DEL.key:
        goto(st, "tpl_del_confirm")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("tpl_apply_confirm")
def r_tpl_apply(st):
    t = tpl_cur(st) or {"name": "?", "snap": new_snapshot()}
    return (head("\U0001F9FE Template Lagana Hai?") + "\n\n" + me_line(st)
            + "\n\nTemplate <b>%s</b> is bot par lagega:\n%s\n\n⚠️ Maujooda settings (jo template me hain) badal jayengi."
            % (esc(t["name"]), snapshot_summary(t["snap"])),
            Kb([[B_APPLY, B_NO], [B_BACK]], "Apply this template?"))


BACK_OF["tpl_apply_confirm"] = "tpl_view"


@handle("tpl_apply_confirm")
def h_tpl_apply(m, st, key):
    t = tpl_cur(st)
    if not t:
        goto(st, "tpl_menu")
    elif key == B_APPLY.key:
        send(st["chat_id"], "\U0001F58C️ Template laga raha hoon...")
        done, failed = apply_snapshot(st["token"], t["snap"], ALL_PARTS, touch_fn(st))
        finish(st, apply_report("\U0001F58C️ Template Report: %s" % esc(t["name"]), done, failed), "tpl_menu")
    elif key == B_NO.key:
        goto(st, "tpl_view", "\U0001F645 Template nahi lagaya.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("tpl_del_confirm")
def r_tpl_del(st):
    t = tpl_cur(st) or {"name": "?"}
    return (head("\U0001F4A3 Template Delete?") + "\n\nTemplate <b>%s</b> hamesha ke liye hat jayega. Pakka?" % esc(t["name"]),
            Kb([[B_YES_DELETE, B_KEEP], [B_BACK]], "Delete this template?"))


BACK_OF["tpl_del_confirm"] = "tpl_view"


@handle("tpl_del_confirm")
def h_tpl_del(m, st, key):
    i = flow_of(st).get("tpl")
    t = tpl_cur(st)
    if not t:
        goto(st, "tpl_menu")
    elif key == B_YES_DELETE.key:
        st["tpls"].pop(i)
        goto(st, "tpl_menu", "✅ Template <b>%s</b> delete ho gaya." % esc(t["name"]))
    elif key == B_KEEP.key:
        goto(st, "tpl_view", "\U0001F6DF Template waisa hi rahega.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


# --------------------------------------------------------------- bulk apply
@render("bulk_tokens")
def r_bulk_tokens(st):
    n = len(flow_of(st).get("bulk") or [])
    return (head("\U0001F4DA Bulk Apply") + "\n\n" + me_line(st) + "\n\n"
            "Jin <b>aapke apne</b> bots par is bot ki settings lagani hain unke tokens bhejo "
            "(ek message me kai tokens bhi chalte hain, har line me ek). Max %d bots. Main token wale "
            "message delete kar dunga aur tokens sirf is kaam ke liye memory me rakhunga.\n\n"
            "Lagega: description, about, commands, menu button, default rights. <b>Naam nahi</b> badlega "
            "(sab bots ka naam ek na ho jaye)." % BULK_MAX
            + ("\n\n\U0001F4CB Ab tak jude: <b>%d</b>" % n if n else ""),
            Kb([[B_BACK, B_CANCEL]], "Paste the bot tokens here"))


BACK_OF["bulk_tokens"] = "backup"


@handle("bulk_tokens")
def h_bulk_tokens(m, st, key):
    reshow(st, "❌ Isme token jaisa kuch nahi mila. Format <code>123456:ABC...</code> hota hai.")


def handle_bulk_tokens(m, st, text):
    try:
        bot.delete_message(st["chat_id"], m.message_id)
    except Exception:
        pass
    f = flow_of(st)
    lst = f.setdefault("bulk", [])
    me_id = (st.get("bot") or {}).get("id")
    seen_ids = {x["id"] for x in lst} | {me_id}
    bad = dup = over = 0
    found = []
    for tk in TOKEN_RE.findall(text):
        if tk not in found:
            found.append(tk)
    for tk in found:
        if len(lst) >= BULK_MAX:
            over += 1
            continue
        r = call_api(tk, "getMe")
        st["ts"] = time.time()
        u = r.get("result") or {}
        if not r.get("ok") or not isinstance(u.get("id"), int):
            bad += 1
        elif u["id"] in seen_ids:
            dup += 1
        else:
            seen_ids.add(u["id"])
            lst.append({"token": tk, "user": u.get("username", "?"), "id": u["id"]})
    notes = []
    if bad:
        notes.append("❌ %d token sahi nahi nikle" % bad)
    if dup:
        notes.append("➖ %d pehle se juda/yehi bot tha" % dup)
    if over:
        notes.append("⚠️ Max %d bots, %d chhod diye" % (BULK_MAX, over))
    if lst:
        goto(st, "bulk_confirm", "\n".join(notes))
    else:
        goto(st, "bulk_tokens", "\n".join(notes) or "❌ Koi token nahi mila.")


@render("bulk_confirm")
def r_bulk_confirm(st):
    lst = flow_of(st).get("bulk") or []
    return (head("\U0001F9FE Bulk Apply Confirm") + "\n\n\U0001F4E4 Source: " + me_line(st)
            + "\n\n\U0001F3AF Ye bots badlenge (%d):\n" % len(lst)
            + "\n".join("• @%s" % esc(x.get("user", "?")) for x in lst)
            + "\n\nLagega: description, about, commands, menu button, default rights (naam nahi). "
            "In bots ke maujooda wahi texts badal jayenge. Pakka?",
            Kb([[B_YES_BULK, B_NO], [B_BACK]], "Apply to all these bots?"))


BACK_OF["bulk_confirm"] = "bulk_tokens"


@handle("bulk_confirm")
def h_bulk_confirm(m, st, key):
    if key == B_YES_BULK.key and flow_of(st).get("bulk"):
        st["busy"], st["stop"] = True, False
        goto(st, "bulk_running")
        threading.Thread(target=run_bulk, args=(st,), daemon=True).start()
    elif key == B_NO.key:
        goto(st, "backup", "\U0001F645 Bulk apply nahi kiya.")
    else:
        reshow(st, "Neeche ke buttons use karo.")


@render("bulk_running")
def r_bulk_running(st):
    return (head("\U0001F4DA Bulk Apply Chal Raha Hai") + "\n\nSettings padh kar bots par laga raha hoon. "
            "Khatam hone par report aayegi. Rokna ho to Stop dabao.",
            Kb([[B_BULK_STOP]], "Bulk apply running..."))


@handle("bulk_running")
def h_bulk_running(m, st, key):
    if key == B_BULK_STOP.key:
        st["stop"] = True
        send(st["chat_id"], "\U0001F6A7 Rok raha hoon, bas current bot ke baad...", RENDER["bulk_running"](st)[1])
    else:
        send(st["chat_id"], "⏳ Bulk apply chal raha hai. Rokne ke liye Stop Bulk dabao.", RENDER["bulk_running"](st)[1])


def run_bulk(st):
    lines, fatal = [], None
    targets = list(flow_of(st).get("bulk") or [])
    touch = touch_fn(st)
    try:
        snap, errors = collect_snapshot(st["token"], touch)
        if snap is None:
            fatal = errors[0] if errors else "Settings padh nahi paya."
        else:
            for i, t in enumerate(targets):
                if st["stop"]:
                    lines.append("⏸️ Baaki %d bots chhode gaye (aapne roka)." % (len(targets) - i))
                    break
                done, failed = apply_snapshot(t["token"], snap, BULK_PARTS, touch, lambda: st["stop"])
                mark = "✅" if done and not failed else ("⚠️" if done else "❌")
                line = "%s @%s: %d lage, %d fail" % (mark, esc(t.get("user", "?")), len(done), len(failed))
                if failed:
                    line += " (%s)" % esc(short(failed[0][1], 70))
                lines.append(line)
    except Exception as e:
        log.exception("bulk error: %s", redact(e))
        fatal = "Andar ki koi gadbad ho gayi. Dobara try karo."
    take_flow(st, "bulk")
    st["busy"] = False
    st["stop"] = False
    text = head("\U0001F4DA Bulk Apply Report") + "\n\n"
    if fatal:
        text += "❌ Source bot ki settings nahi padh payi: %s" % esc(fatal)
    else:
        text += "\n".join(lines) or "Kuch nahi hua."
        text += "\n\nℹ️ Naam, profile photo aur language-wise naam is run me nahi badle."
    finish(st, text, "backup")


# ------------------------------------------------------ stars & share links
@render("extras")
def r_extras(st):
    return (head("\U0001F381 Stars & Links") + "\n\n" + me_line(st) + "\n\n"
            "⭐ <b>Star Balance</b> = bot ke Telegram Stars\n"
            "\U0001F4B3 <b>Star History</b> = Stars transactions (10-10 karke)\n"
            "\U0001F4CE <b>Share Links</b> = bot ke t.me links (start / add to group)",
            Kb([[B_STARS, B_TXNS], [B_LINKS], [B_BACK, B_HOME]], "Stars or share links"))


BACK_OF["extras"] = "main"


@handle("extras")
def h_extras(m, st, key):
    if key == B_STARS.key:
        do_star_balance(st)
    elif key == B_TXNS.key:
        do_star_history(st, 0)
    elif key == B_LINKS.key:
        do_links(st)
    else:
        reshow(st, "Neeche ke buttons use karo.")


def fmt_stars(sa):
    sa = sa if isinstance(sa, dict) else {}
    amt = sa.get("amount", 0) or 0
    nano = sa.get("nanostar_amount", 0) or 0
    return "%s" % amt if not nano else "%s.%09d" % (amt, abs(nano))


def do_star_balance(st):
    r = api(st, "getMyStarBalance")
    if not r.get("ok"):
        goto(st, "extras", "❌ Star balance nahi mila: %s" % esc(plain_error(r)))
        return
    finish(st, head("⭐ Star Balance") + "\n\n" + me_line(st) + "\n⭐ Balance: <b>%s</b> Stars"
           % esc(fmt_stars(r.get("result"))), "extras", B_REFRESH, do_star_balance)


STAR_PAGE = 10


def do_star_history(st, offset):
    r = api(st, "getStarTransactions", {"offset": offset, "limit": STAR_PAGE})
    if not r.get("ok"):
        goto(st, "extras", "❌ Transactions nahi mile: %s" % esc(plain_error(r)))
        return
    txs = [t for t in ((r.get("result") or {}).get("transactions") or []) if isinstance(t, dict)]
    lines = [head("\U0001F4B3 Star History"), "", me_line(st), ""]
    if not txs:
        lines.append("Is range me koi transaction nahi hai." if offset else
                     "Abhi koi Star transaction nahi hai (bot par Stars payments hue hi na hon to aisa hota hai).")
    for t in txs:
        incoming = isinstance(t.get("source"), dict)
        partner = (t.get("source") if incoming else t.get("receiver")) or {}
        lines.append("%s %s ⭐ — %s, %s%s" % (
            "\U0001F4E5" if incoming else "\U0001F4E4", esc(fmt_stars(t)), fmt_ts(t.get("date")),
            "aaya" if incoming else "gaya", " (%s)" % esc(partner.get("type")) if partner.get("type") else ""))
    lines += ["", "ℹ️ Transactions purane se naye ke order me aate hain (#%d se)." % (offset + 1)]
    more = len(txs) >= STAR_PAGE
    finish(st, "\n".join(lines), "extras", B_OLDER if more else None,
           (lambda s, o=offset + STAR_PAGE: do_star_history(s, o)) if more else None)


def do_links(st):
    u = esc((st.get("bot") or {}).get("username", "?"))
    text = (head("\U0001F4CE Share Links") + "\n\n" + me_line(st)
            + "\n\n\U0001F517 Bot: https://t.me/%s\n\U0001F465 Group me add: https://t.me/%s?startgroup=true"
            "\n\nℹ️ Custom start link me payload bot ko <code>/start payload</code> ki tarah milta hai; "
            "use samajhna aapke bot ke code ka kaam hai." % (u, u))
    finish(st, text, "extras", B_LINK_PAYLOAD,
           lambda s: (s.__setitem__("flow", {"back": "extras"}), goto(s, "links_payload")))


@render("links_payload")
def r_links_payload(st):
    return (head("\U0001F9F7 Custom Start Link") + "\n\nPayload bhejo: sirf letters, numbers, <code>_</code> aur "
            "<code>-</code> (max 64), jaise <code>promo_2026</code>.",
            Kb([[B_BACK, B_CANCEL]], "Type the start payload"))


BACK_OF["links_payload"] = "extras"


@handle("links_payload")
def h_links_payload(m, st, key):
    p = (m.text or "").strip() if m.content_type == "text" else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p):
        reshow(st, "❌ Payload sirf letters, numbers, _ aur - ho sakta hai (1 se 64).")
        return
    u = esc((st.get("bot") or {}).get("username", "?"))
    finish(st, head("\U0001F9F7 Start Link Taiyar") + "\n\n\U0001F517 Private: https://t.me/%s?start=%s"
           "\n\U0001F465 Group: https://t.me/%s?startgroup=%s" % (u, esc(p), u, esc(p)),
           "extras", B_LINK_PAYLOAD, lambda s: (s.__setitem__("flow", {"back": "extras"}), goto(s, "links_payload")))


# ----------------------------------------------------------------- result
@render("result")
def r_result(st):
    res = st.get("res") or {"text": "Done.", "back": "main", "again": None}
    rows = [[res["again"]] if res.get("again") else None, [B_BACK, B_HOME]]
    return (res["text"], Kb(rows, "Choose what next"))


@handle("result")
def h_result(m, st, key):
    res = st.get("res") or {"back": "main"}
    if res.get("again") is not None and key == res["again"].key and res.get("again_fn"):
        res["again_fn"](st)
    else:
        reshow(st, "Neeche ke buttons use karo.")


BACK_OF["result"] = lambda st: (st.get("res") or {}).get("back", "main")


# ------------------------------------------------------- global navigation
def cancel_target(st):
    screen = st["screen"]
    if screen == "token":
        return "main"
    if screen in BC_SCREENS:
        return "bc_menu"
    return flow_of(st).get("back", "main")


def clear_flow_data(st):
    f = st.get("flow") or {}
    f.pop("photo", None)
    f.pop("bulk", None)
    f.pop("snap", None)
    st["bc"].pop("pending_photo", None)


def nav(m, st, key):
    """Handle Back / Cancel / Main Menu. Returns True if handled."""
    screen = st["screen"]
    if screen in ("bc_sending", "bulk_running"):
        return False
    if key == B_HOME.key:
        clear_flow_data(st)
        goto(st, "main")
        return True
    if key == B_CANCEL.key:
        clear_flow_data(st)
        dest = cancel_target(st)
        goto(st, dest, "❌ Cancel ho gaya.")
        return True
    if key == B_BACK.key:
        clear_flow_data(st)
        dest = BACK_OF.get(screen, "main")
        dest = dest(st) if callable(dest) else dest
        goto(st, dest)
        return True
    return False


# --------------------------------------------------------------- handlers
def route(m, st):
    screen = st["screen"]
    ctype = m.content_type
    text = (m.text or "").strip() if ctype == "text" else ""
    if ctype == "text" and screen == "bulk_tokens" and TOKEN_RE.search(text):
        handle_bulk_tokens(m, st, text)
        return
    if ctype == "text" and TOKEN_SHAPE.match(text):
        if st["busy"]:
            return
        handle_token(m, st, text)
        return
    if ctype in ("photo", "document"):
        fn = MEDIA.get(screen)
        if fn:
            fn(m, st)
        else:
            reshow(st, "Abhi photo ki zaroorat nahi hai. Neeche ke buttons use karo.")
        return
    if ctype != "text":
        reshow(st, "Ye type yahan supported nahi hai. Neeche ke buttons use karo.")
        return
    key = st["kbmap"].get(text)
    if key is None and screen not in FREE_TEXT:
        k2 = norm(text)
        key = k2 if k2 in st["kbkeys"] else None
    if key is not None:
        if nav(m, st, key):
            return
    elif text.startswith("/") and screen not in FREE_TEXT:
        reshow(st)
        return
    HANDLE[screen](m, st, key)


def bare_command(m):
    """True only for a lone /command (so pasted command lists starting with /start stay text)."""
    return len((getattr(m, "text", None) or "").split()) == 1


@bot.message_handler(commands=["start", "menu"], func=bare_command)
def cmd_start(m):
    if not is_private(m):
        send(m.chat.id, "Please mujhe <b>private chat</b> me use karo (token group me share mat karo).")
        return
    st, _expired = get_session(m.from_user.id, m.chat.id)
    with st["lock"]:
        if st["busy"]:
            reshow(st)
            return
        clear_flow_data(st)
        goto(st, "main", "\U0001F44B Namaste!" if not st.get("token") else "")


@bot.message_handler(commands=["help"], func=bare_command)
def cmd_help(m):
    if not is_private(m):
        send(m.chat.id, "Please mujhe <b>private chat</b> me use karo.")
        return
    st, _expired = get_session(m.from_user.id, m.chat.id)
    with st["lock"]:
        reshow(st, HOWTO_TEXT)


@bot.message_handler(commands=["cancel"], func=bare_command)
def cmd_cancel(m):
    if not is_private(m):
        return
    st, _expired = get_session(m.from_user.id, m.chat.id)
    with st["lock"]:
        if st["busy"]:
            st["stop"] = True
            reshow(st, "\U0001F6D1 Rok raha hoon...")
            return
        clear_flow_data(st)
        goto(st, "main", "❌ Cancel ho gaya.")


ALL_TYPES = ["text", "photo", "document", "sticker", "video", "voice", "audio",
             "animation", "contact", "location", "video_note"]


@bot.message_handler(func=lambda m: True, content_types=ALL_TYPES)
def on_message(m):
    try:
        if not is_private(m):
            if getattr(m, "content_type", "") == "text":
                send(m.chat.id, "Please mujhe <b>private chat</b> me use karo (token group me share mat karo).")
            return
        st, expired = get_session(m.from_user.id, m.chat.id)
        with st["lock"]:
            if expired:
                if m.content_type == "text" and TOKEN_SHAPE.match((m.text or "").strip()):
                    pass
                else:
                    goto(st, "main", "⌛ Session purana ho gaya tha isliye token hata diya gaya. "
                                     "Dobara Connect Bot dabao.")
                    return
            route(m, st)
    except Exception as e:
        log.exception("handler error: %s", redact(e))
        try:
            st, _ = get_session(m.from_user.id, m.chat.id)
            send(m.chat.id, "❌ Kuch gadbad ho gayi. Dobara try karo ya Main Menu dabao.",
                 Kb([[B_HOME]], "Tap Main Menu"))
            st["screen"] = "result"
            st["res"] = {"text": "", "back": "main", "again": None}
            st["kbmap"] = Kb([[B_HOME]], "x").text_map()
            st["kbkeys"] = {B_HOME.key}
        except Exception:
            pass


def main():
    if BOT_TOKEN == PLACEHOLDER or ":" not in BOT_TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable to this bot's token "
                         "(from @BotFather) before running.")
    log.info("Bot Manager Pro started.")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, logger_level=logging.WARNING)


if __name__ == "__main__":
    main()
