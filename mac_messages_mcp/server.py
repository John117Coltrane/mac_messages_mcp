#!/usr/bin/env python3
"""
Mac Messages MCP - Scoped to a configurable allow-list of chats.

Configuration is loaded from config.json in the project root, with env var overrides:
  - ALLOWED_CHAT_ID: Chat scope. Accepts:
        "abc123"          single chat (string)
        "*"               all chats (no restriction)
        "abc,def"         comma-separated list (env var form)
        ["abc", "def"]    JSON list (config.json form)
  - MCP_TRANSPORT: "stdio" or "sse" (default: "stdio")
  - CHUNK_SIZE_BYTES: Chunk size for attachment transfers (default: 524288)

Run `tool_list_chats` to discover chat identifiers, then set allowed_chat_id in config.json.
See config.example.json for the format.
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import sys
import tempfile
import threading
import uuid as _uuid

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from mac_messages_mcp.messages import (
    check_messages_db_access,
    extract_body_from_attributed,
    get_chat_mapping,
    get_contact_name,
    query_messages_db,
    run_applescript,
    send_message,
)

# Configure logging to stderr for debugging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger("mac_messages_mcp")

# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load config.json from project root, falling back to env vars / defaults."""
    config = {}
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            logger.info(f"Loaded config from {config_path}")
        except Exception as e:
            logger.warning(f"Failed to read {config_path}: {e}")

    # allowed_chat_id may be a string, "*", or a list. Env var (string) wins
    # if present, then falls back to whatever the file provided.
    raw_allowed: object
    env_val = os.environ.get("ALLOWED_CHAT_ID")
    if env_val is not None:
        raw_allowed = env_val
    else:
        raw_allowed = config.get("allowed_chat_id", "")

    return {
        "allowed_chat_id_raw": raw_allowed,
        "transport": os.environ.get("MCP_TRANSPORT", config.get("transport", "stdio")),
        "host": os.environ.get("MCP_HOST", config.get("host", "0.0.0.0")),
        "port": int(os.environ.get("MCP_PORT", config.get("port", 8000))),
    }


def _parse_allowed_chats(raw: object) -> tuple[set[str], bool]:
    """Normalize the allowed_chat_id config value into (id_set, allow_all).

    Accepts:
      - None / ""           → ({}, False)   nothing configured
      - "*"                 → ({}, True)    all chats allowed
      - "abc"               → ({"abc"}, False)
      - "abc,def"           → ({"abc", "def"}, False)   env-var list form
      - ["abc", "def"]      → ({"abc", "def"}, False)   config.json list form
      - any list containing "*" → ({}, True)
    """
    if raw is None or raw == "":
        return set(), False
    if isinstance(raw, str):
        if raw.strip() == "*":
            return set(), True
        ids = {s.strip() for s in raw.split(",") if s.strip()}
        if "*" in ids:
            return set(), True
        return ids, False
    if isinstance(raw, (list, tuple)):
        ids = {str(s).strip() for s in raw if str(s).strip()}
        if "*" in ids:
            return set(), True
        return ids, False
    logger.warning(f"Unrecognized allowed_chat_id value type: {type(raw).__name__}; treating as empty")
    return set(), False


CONFIG = _load_config()
ALLOWED_CHAT_IDS, ALLOW_ALL_CHATS = _parse_allowed_chats(CONFIG["allowed_chat_id_raw"])

if ALLOW_ALL_CHATS:
    logger.warning(
        "allowed_chat_id is set to '*' — single-chat lockdown is DISABLED. "
        "The server has access to ALL chats in the Messages database."
    )
elif not ALLOWED_CHAT_IDS:
    logger.error(
        "No allowed_chat_id configured. Set it in config.json or the ALLOWED_CHAT_ID env var. "
        "Run tool_list_chats to discover available chat identifiers."
    )
else:
    logger.info(f"Allow-list configured: {len(ALLOWED_CHAT_IDS)} chat(s)")


def _has_any_allowed() -> bool:
    """True if any chat (including '*') is allowed."""
    return ALLOW_ALL_CHATS or bool(ALLOWED_CHAT_IDS)


def _chat_is_allowed(chat_identifier: str) -> bool:
    """True if the given chat_identifier is in the allow list."""
    return ALLOW_ALL_CHATS or chat_identifier in ALLOWED_CHAT_IDS


def _allowed_chats_sql(column: str) -> tuple[str, tuple]:
    """Build a SQL fragment + params for restricting a column to the allow list.

    Returns ("1=1", ()) when all chats are allowed,
    ("0=1", ()) when nothing is allowed (defensive — caller should _require_allowed_chat first),
    ("col IN (?, ?, ...)", (id1, id2, ...)) otherwise.
    """
    if ALLOW_ALL_CHATS:
        return "1=1", ()
    if not ALLOWED_CHAT_IDS:
        return "0=1", ()
    placeholders = ",".join(["?"] * len(ALLOWED_CHAT_IDS))
    # Sort for stable parameter ordering (helps tests / logging)
    return f"{column} IN ({placeholders})", tuple(sorted(ALLOWED_CHAT_IDS))


def _resolve_target_chat(requested: str | None) -> tuple[str | None, str | None]:
    """Pick a chat to send to. Returns (chat_identifier, error_message).

    - If `requested` is given: validate it's in the allow list and return it.
    - If exactly one chat is allowed and no `requested`: return that one (back-compat).
    - If multiple chats are allowed and no `requested`: error (must disambiguate).
    - If '*' is set and no `requested`: error (sends require an explicit target).
    """
    if requested:
        requested = requested.strip()
        if not _chat_is_allowed(requested):
            return None, (
                f"Error: chat_identifier {requested!r} is not in the allow list. "
                "Use tool_list_chats to see allowed chats."
            )
        return requested, None
    if ALLOW_ALL_CHATS:
        return None, (
            "Error: chat_identifier is required when allowed_chat_id='*'. "
            "Use tool_list_chats to discover chat identifiers."
        )
    if len(ALLOWED_CHAT_IDS) == 1:
        return next(iter(ALLOWED_CHAT_IDS)), None
    if len(ALLOWED_CHAT_IDS) > 1:
        return None, (
            f"Error: {len(ALLOWED_CHAT_IDS)} chats are allowed; specify chat_identifier explicitly. "
            "Use tool_list_chats to see allowed chats."
        )
    return None, (
        "Error: No allowed chats configured. "
        "Set allowed_chat_id in config.json or the ALLOWED_CHAT_ID env var."
    )


def _get_local_ip() -> str:
    """Get the LAN IP address of this machine."""
    import socket
    try:
        # Connect to a public DNS to determine which interface is used
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_chat_guid(chat_identifier: str) -> str:
    """Resolve a chat_identifier to the guid that AppleScript needs."""
    results = query_messages_db(
        "SELECT guid FROM chat WHERE chat_identifier = ?", (chat_identifier,)
    )
    if results and "error" not in results[0]:
        return results[0]["guid"]
    return chat_identifier


# ── MCP Server ───────────────────────────────────────────────────────────────

# Note: FastMCP's `description=` keyword was renamed to `instructions=` upstream
# (PR #28, FastMCP API compatibility). Use `instructions=` for forward compat.
mcp = FastMCP(
    "MessageBridge",
    instructions="A bridge for interacting with a configurable allow list of iMessage chats",
)


@mcp.tool()
def tool_list_chats(ctx: Context) -> str:
    """
    List all named group chats from the Messages app.

    Marks each chat as (ACTIVE) if it is in the current allow list.
    Use this to find the chat_identifier to put in config.json.
    """
    logger.info("Listing available chats")
    try:
        query = "SELECT chat_identifier, display_name FROM chat WHERE display_name IS NOT NULL AND display_name != ''"
        results = query_messages_db(query)
        if not results:
            return "No named group chats found."
        if "error" in results[0]:
            return f"Error: {results[0]['error']}"
        lines = []
        for i, r in enumerate(results, 1):
            active = " (ACTIVE)" if _chat_is_allowed(r["chat_identifier"]) else ""
            lines.append(f"{i}. {r['display_name']} -> {r['chat_identifier']}{active}")
        header = "Available group chats"
        if ALLOW_ALL_CHATS:
            header += " (allowed_chat_id='*' — all chats are active)"
        elif ALLOWED_CHAT_IDS:
            header += f" ({len(ALLOWED_CHAT_IDS)} in allow list)"
        else:
            header += " (no allow list configured — all marked inactive)"
        return f"{header}:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error listing chats: {str(e)}"


def _require_allowed_chat() -> str | None:
    """Return an error string if no chat scope is configured, else None."""
    if not _has_any_allowed():
        return (
            "Error: No allowed chats configured. "
            "Use tool_list_chats to find your chat identifier(s), then set "
            "allowed_chat_id in config.json or the ALLOWED_CHAT_ID env var. "
            "Accepts a single id, a list, or '*' for all chats."
        )
    return None


# ── Shared message formatting ────────────────────────────────────────────────

# Track the last-seen Apple timestamp for tool_get_new_messages
_last_seen_timestamp: str = "0"

TAPBACK_TYPES = {
    2000: "❤️", 2001: "👍", 2002: "👎",
    2003: "😂", 2004: "‼️", 2005: "❓", 2006: "emoji",
}


def _apple_ts_to_str(apple_timestamp: int) -> str:
    """Convert an Apple epoch timestamp to a local timezone string."""
    from datetime import datetime, timezone as tz

    try:
        apple_epoch_unix = 978307200  # 2001-01-01 00:00:00 UTC in unix seconds
        if apple_timestamp > 1_000_000_000_000:  # nanoseconds
            unix_ts = apple_epoch_unix + apple_timestamp / 1_000_000_000
        else:
            unix_ts = apple_epoch_unix + apple_timestamp
        dt = datetime.fromtimestamp(unix_ts).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return "Unknown date"


def _format_messages(messages: list[dict]) -> str:
    """Format a list of raw message rows into a display string.

    Separates tapbacks, annotates reactions/flags/attachments,
    and returns messages in chronological (earliest-first) order.
    """
    if not messages:
        return "No messages found."
    if "error" in messages[0]:
        return f"Error accessing messages: {messages[0]['error']}"

    # Separate tapbacks from regular messages
    regular_messages = []
    tapback_map: dict = {}
    for msg in messages:
        assoc_type = msg.get("associated_message_type") or 0
        assoc_guid = msg.get("associated_message_guid") or ""
        if 2000 <= assoc_type <= 2006 and assoc_guid:
            target_guid = assoc_guid.split("/", 1)[-1] if "/" in assoc_guid else assoc_guid
            emoji = TAPBACK_TYPES.get(assoc_type, "?")
            if assoc_type == 2006:
                emoji = msg.get("associated_message_emoji") or "emoji"
            sender = "You" if msg["is_from_me"] else get_contact_name(msg["handle_id"])
            tapback_map.setdefault(target_guid, []).append(f"{emoji} {sender}")
        else:
            regular_messages.append(msg)

    # Sort chronologically (earliest first)
    regular_messages.sort(key=lambda m: m.get("date", 0))

    # Build attachment lookup
    msg_ids = [str(m["ROWID"]) for m in regular_messages]
    attachment_map: dict = {}
    if msg_ids:
        placeholders = ", ".join(["?" for _ in msg_ids])
        att_query = f"""
        SELECT maj.message_id, a.ROWID as att_id, a.transfer_name, a.mime_type, a.total_bytes
        FROM attachment a
        JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
        WHERE maj.message_id IN ({placeholders})
        """
        att_results = query_messages_db(att_query, tuple(msg_ids))
        if att_results and "error" not in att_results[0]:
            for att in att_results:
                attachment_map.setdefault(att["message_id"], []).append(att)

    chat_mapping = get_chat_mapping()
    formatted = []
    for msg in regular_messages:
        body = msg.get("text") or ""
        if not body and msg.get("attributedBody"):
            body = extract_body_from_attributed(msg["attributedBody"]) or ""

        attachments = attachment_map.get(msg["ROWID"], [])
        if not body and not attachments:
            continue

        date_str = _apple_ts_to_str(int(msg["date"]))
        direction = "You" if msg["is_from_me"] else get_contact_name(msg["handle_id"])
        group_chat_name = chat_mapping.get(msg.get("cache_roomnames"), "Group Chat")

        line = f"[{date_str}] [msg_id={msg['ROWID']}] [{group_chat_name}] {direction}: {body}"

        flags = []
        if msg.get("date_edited"):
            flags.append("EDITED")
        if msg.get("date_retracted"):
            flags.append("UNSENT")
        if msg.get("thread_originator_guid"):
            flags.append("REPLY")
        if flags:
            line += f" [{', '.join(flags)}]"

        reactions = tapback_map.get(msg.get("guid", ""), [])
        if reactions:
            line += f" [REACTIONS: {', '.join(reactions)}]"

        if attachments:
            att_parts = []
            for att in attachments:
                name = att.get("transfer_name", "unknown")
                mime = att.get("mime_type", "unknown")
                size = att.get("total_bytes", 0)
                att_parts.append(f"{name} ({mime}, {size} bytes, att_id={att['att_id']})")
            line += f" [ATTACHMENTS: {'; '.join(att_parts)}]"
        formatted.append(line)

    if not formatted:
        return "No messages found."
    return "\n".join(formatted)


_MESSAGE_COLUMNS = """
    m.ROWID, m.guid, m.date, m.text, m.attributedBody,
    m.is_from_me, m.handle_id, m.cache_roomnames,
    m.associated_message_guid, m.associated_message_type,
    m.associated_message_emoji,
    m.thread_originator_guid, m.date_edited, m.date_retracted
"""


def _apple_timestamp_for_hours_ago(hours: int) -> str:
    """Return an Apple-epoch nanosecond timestamp string for N hours ago."""
    from datetime import datetime, timedelta, timezone as tz

    current_time = datetime.now(tz.utc)
    hours_ago = current_time - timedelta(hours=hours)
    apple_epoch = datetime(2001, 1, 1, tzinfo=tz.utc)
    ns = int((hours_ago - apple_epoch).total_seconds() * 1_000_000_000)
    return str(ns)


# ── Message tools ────────────────────────────────────────────────────────────


@mcp.tool()
def tool_get_recent_messages(ctx: Context, hours: int = 24) -> str:
    """
    Get recent messages from the allowed chat(s), in chronological order.

    Args:
        hours: Number of hours to look back (default: 24)
    """
    if err := _require_allowed_chat():
        return err

    logger.info(f"Getting recent messages: hours={hours}")
    try:
        if hours < 0:
            return "Error: Hours cannot be negative."
        MAX_HOURS = 10 * 365 * 24
        if hours > MAX_HOURS:
            return f"Error: Hours value too large. Maximum allowed is {MAX_HOURS} hours."

        timestamp_str = _apple_timestamp_for_hours_ago(hours)
        clause, clause_params = _allowed_chats_sql("m.cache_roomnames")

        query = f"""
        SELECT {_MESSAGE_COLUMNS}
        FROM message m
        WHERE CAST(m.date AS TEXT) > ?
          AND {clause}
        ORDER BY m.date DESC
        LIMIT 100
        """
        messages = query_messages_db(query, (timestamp_str, *clause_params))
        return _format_messages(messages)
    except Exception as e:
        logger.error(f"Error in get_recent_messages: {str(e)}")
        return f"Error getting messages: {str(e)}"


@mcp.tool()
def tool_get_new_messages(ctx: Context) -> str:
    """
    Get only new messages since the last check, in chronological order.

    On the first call, returns messages from the last 1 hour.
    On subsequent calls, returns only messages received after the previous call.
    Scope is the entire allow list (or all chats if allowed_chat_id='*').
    """
    global _last_seen_timestamp
    if err := _require_allowed_chat():
        return err

    logger.info(f"Getting new messages since timestamp {_last_seen_timestamp}")
    try:
        # On first call, use 1 hour ago as the starting point
        if _last_seen_timestamp == "0":
            _last_seen_timestamp = _apple_timestamp_for_hours_ago(1)

        clause, clause_params = _allowed_chats_sql("m.cache_roomnames")

        query = f"""
        SELECT {_MESSAGE_COLUMNS}
        FROM message m
        WHERE CAST(m.date AS TEXT) > ?
          AND {clause}
        ORDER BY m.date DESC
        LIMIT 100
        """
        messages = query_messages_db(query, (_last_seen_timestamp, *clause_params))

        if not messages or "error" in messages[0]:
            return _format_messages(messages)

        # Update the high-water mark to the newest message's timestamp
        max_ts = max(int(m["date"]) for m in messages)
        _last_seen_timestamp = str(max_ts)

        return _format_messages(messages)
    except Exception as e:
        logger.error(f"Error in get_new_messages: {str(e)}")
        return f"Error getting new messages: {str(e)}"


@mcp.tool()
def tool_send_message(ctx: Context, message: str, chat_identifier: str = "") -> str:
    """
    Send a message to a chat in the allow list.

    Args:
        message: Message text to send
        chat_identifier: Target chat. Optional when exactly one chat is allowed
            (it will be used automatically). Required when multiple chats are
            allowed or when allowed_chat_id='*'.
    """
    if err := _require_allowed_chat():
        return err

    target, target_err = _resolve_target_chat(chat_identifier or None)
    if target_err:
        return target_err

    logger.info(f"Sending message to chat {target}")
    try:
        chat_guid = _get_chat_guid(target)
        result = send_message(recipient=chat_guid, message=message, group_chat=True)
        return result
    except Exception as e:
        logger.error(f"Error in send_message: {str(e)}")
        return f"Error sending message: {str(e)}"


@mcp.tool()
def tool_fuzzy_search_messages(
    ctx: Context, search_term: str, hours: int = 24, threshold: float = 0.6
) -> str:
    """
    Fuzzy search for messages in the allowed group chat.

    Args:
        search_term: The text to search for in messages.
        hours: How many hours back to search (default 24).
        threshold: Similarity threshold (0.0-1.0, default 0.6).
    """
    if err := _require_allowed_chat():
        return err

    if not (0.0 <= threshold <= 1.0):
        return "Error: Threshold must be between 0.0 and 1.0."
    if hours <= 0:
        return "Error: Hours must be a positive integer."

    logger.info(f"Fuzzy searching allowed chats for '{search_term}' in last {hours} hours")
    try:
        from datetime import datetime, timedelta, timezone

        from thefuzz import fuzz

        MAX_HOURS = 10 * 365 * 24
        if hours > MAX_HOURS:
            return f"Error: Hours value too large. Maximum is {MAX_HOURS} hours."

        current_time = datetime.now(timezone.utc)
        hours_ago_dt = current_time - timedelta(hours=hours)
        apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
        seconds_since_apple_epoch = (hours_ago_dt - apple_epoch).total_seconds()
        nanoseconds_since_apple_epoch = int(seconds_since_apple_epoch * 1_000_000_000)
        timestamp_str = str(nanoseconds_since_apple_epoch)

        clause, clause_params = _allowed_chats_sql("m.cache_roomnames")

        query = f"""
        SELECT
            m.ROWID, m.date, m.text, m.attributedBody,
            m.is_from_me, m.handle_id, m.cache_roomnames
        FROM message m
        WHERE CAST(m.date AS TEXT) > ?
          AND {clause}
        ORDER BY m.date DESC
        LIMIT 500
        """
        messages = query_messages_db(query, (timestamp_str, *clause_params))

        if not messages:
            return "No messages found in the specified time period."
        if "error" in messages[0]:
            return f"Error: {messages[0]['error']}"

        chat_mapping = get_chat_mapping()
        matched_messages = []
        int_threshold = int(threshold * 100)

        for msg in messages:
            if msg.get("text"):
                body = msg["text"]
            elif msg.get("attributedBody"):
                body = extract_body_from_attributed(msg["attributedBody"])
                if not body:
                    continue
            else:
                continue

            score = fuzz.WRatio(search_term.lower(), body.lower())
            if score >= int_threshold:
                try:
                    date_string = "2001-01-01"
                    mod_date = datetime.strptime(date_string, "%Y-%m-%d")
                    unix_timestamp = int(mod_date.timestamp()) * 1000000000
                    msg_timestamp = int(msg["date"])
                    if len(str(msg_timestamp)) > 10:
                        new_date = int((msg_timestamp + unix_timestamp) / 1000000000)
                    else:
                        new_date = mod_date.timestamp() + msg_timestamp
                    date_str = datetime.fromtimestamp(new_date).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, TypeError, OverflowError):
                    date_str = "Unknown date"

                direction = (
                    "You" if msg["is_from_me"] else get_contact_name(msg["handle_id"])
                )
                group_chat_name = chat_mapping.get(
                    msg.get("cache_roomnames"), "Group Chat"
                )
                matched_messages.append(
                    (
                        score,
                        f"[{date_str}] [{group_chat_name}] {direction} (score: {score}): {body}",
                    )
                )

        if not matched_messages:
            return f"No messages matching '{search_term}' found in the allowed chat."

        matched_messages.sort(key=lambda x: x[0], reverse=True)
        return "\n".join([m[1] for m in matched_messages[:50]])
    except Exception as e:
        logger.error(f"Error in fuzzy_search_messages: {e}", exc_info=True)
        return f"Error during fuzzy search: {str(e)}"


# ── Attachment tools ──────────────────────────────────────────────────────────

# In-memory upload sessions: upload_id -> {path, filename, bytes_written}
_upload_sessions: dict = {}


def _resolve_attachment(attachment_id: int) -> dict | str:
    """Look up an attachment by ID, enforcing the chat allow list.
    Returns the row dict on success or an error string."""
    clause, clause_params = _allowed_chats_sql("m.cache_roomnames")
    query = f"""
    SELECT a.ROWID, a.filename, a.mime_type, a.transfer_name, a.total_bytes, a.is_outgoing
    FROM attachment a
    JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
    JOIN message m ON maj.message_id = m.ROWID
    WHERE a.ROWID = ?
      AND {clause}
    LIMIT 1
    """
    results = query_messages_db(query, (attachment_id, *clause_params))
    if not results:
        return "Attachment not found or not in the allowed chats."
    if "error" in results[0]:
        return results[0]["error"]
    return results[0]


@mcp.tool()
def tool_get_attachment(ctx: Context, attachment_id: int) -> str:
    """
    Get attachment metadata and a download URL.

    The returned URL can be fetched directly over HTTP (e.g. curl, requests.get)
    to download the raw file. No base64 encoding, no chunking needed.

    Args:
        attachment_id: The att_id from a message listing.
    """
    if err := _require_allowed_chat():
        return err

    logger.info(f"Getting attachment metadata for {attachment_id}")
    try:
        result = _resolve_attachment(attachment_id)
        if isinstance(result, str):
            return f"Error: {result}"

        att = result
        filename = att.get("filename", "")
        if filename and filename.startswith("~"):
            filename = os.path.expanduser(filename)

        port = CONFIG.get("port", 8000)
        url_host = _get_local_ip()

        info = {
            "attachment_id": att["ROWID"],
            "transfer_name": att.get("transfer_name"),
            "mime_type": att.get("mime_type"),
            "total_bytes": att.get("total_bytes"),
            "is_outgoing": bool(att.get("is_outgoing")),
            "file_exists": os.path.exists(filename) if filename else False,
            "download_url": f"http://{url_host}:{port}/attachments/{att['ROWID']}",
        }
        return json.dumps(info)
    except Exception as e:
        logger.error(f"Error getting attachment: {e}")
        return f"Error getting attachment: {str(e)}"


@mcp.tool()
def tool_send_attachment(
    ctx: Context,
    filename: str,
    chunk_base64: str,
    upload_id: str = "",
    is_last: bool = True,
    chat_identifier: str = "",
) -> str:
    """
    Send a file/image attachment to a chat in the allow list, with chunked upload support.

    For small files (single chunk):
      Call once with chunk_base64=<all data>, is_last=True.

    For large files (multi-chunk):
      1. First call: provide filename, chunk_base64=<first chunk>, is_last=False.
         Returns an upload_id. Also pass chat_identifier on the first call if
         multiple chats are allowed.
      2. Subsequent calls: provide upload_id, chunk_base64=<next chunk>, is_last=False.
      3. Final call: provide upload_id, chunk_base64=<last chunk>, is_last=True.
         This assembles and sends the file.

    Args:
        filename: Desired filename with extension (e.g. "photo.jpg"). Required on first call.
        chunk_base64: Base64-encoded chunk of file data.
        upload_id: Upload session ID returned from the first call. Omit for a new upload.
        is_last: True if this is the final (or only) chunk. Triggers the send.
        chat_identifier: Target chat. Optional when exactly one chat is allowed.
            Required when multiple chats are allowed or when allowed_chat_id='*'.
            Only honored on the first chunk of a session (the target is then
            captured for the rest of the upload).
    """
    if err := _require_allowed_chat():
        return err

    logger.info(
        f"Send attachment chunk: filename={filename}, upload_id={upload_id or 'new'}, is_last={is_last}"
    )

    try:
        chunk_bytes = base64.b64decode(chunk_base64)
    except Exception as e:
        return json.dumps({"error": f"Invalid base64 data: {str(e)}"})

    try:
        if not upload_id:
            target, target_err = _resolve_target_chat(chat_identifier or None)
            if target_err:
                return json.dumps({"error": target_err})
            upload_id = _uuid.uuid4().hex
            _, ext = os.path.splitext(filename)
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=ext, prefix="imsg_att_"
            )
            tmp.close()
            _upload_sessions[upload_id] = {
                "path": tmp.name,
                "filename": filename,
                "bytes_written": 0,
                "target_chat": target,
            }

        session = _upload_sessions.get(upload_id)
        if not session:
            return json.dumps({"error": f"Unknown upload_id: {upload_id}"})

        with open(session["path"], "ab") as f:
            f.write(chunk_bytes)
        session["bytes_written"] += len(chunk_bytes)

        if not is_last:
            return json.dumps(
                {
                    "upload_id": upload_id,
                    "bytes_written": session["bytes_written"],
                    "status": "awaiting_next_chunk",
                }
            )

        # Final chunk — send the assembled file
        tmp_path = session["path"]
        final_filename = session["filename"]
        total_written = session["bytes_written"]
        target_chat = session["target_chat"]
        del _upload_sessions[upload_id]

        chat_guid = _get_chat_guid(target_chat)
        script = f'tell application "Messages" to send POSIX file "{tmp_path}" to chat id "{chat_guid}"'
        result = run_applescript(script)

        def cleanup():
            import time
            time.sleep(10)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        threading.Thread(target=cleanup, daemon=True).start()

        if result.startswith("Error:"):
            return json.dumps({"error": f"Error sending attachment: {result}"})

        return json.dumps(
            {
                "status": "sent",
                "filename": final_filename,
                "total_bytes": total_written,
            }
        )
    except Exception as e:
        logger.error(f"Error sending attachment: {e}")
        return json.dumps({"error": str(e)})


# ── Diagnostics ──────────────────────────────────────────────────────────────


@mcp.tool()
def tool_check_db_access(ctx: Context) -> str:
    """Diagnose database access issues."""
    logger.info("Checking database access")
    try:
        return check_messages_db_access()
    except Exception as e:
        return f"Error checking database access: {str(e)}"


# ── HTTP attachment endpoint ──────────────────────────────────────────────────


async def handle_attachment_download(request: Request) -> Response:
    """Serve an attachment file over plain HTTP GET /attachments/{id}."""
    try:
        attachment_id = int(request.path_params["attachment_id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "Invalid attachment ID"}, status_code=400)

    if not _has_any_allowed():
        return JSONResponse({"error": "No allowed chats configured"}, status_code=500)

    result = _resolve_attachment(attachment_id)
    if isinstance(result, str):
        return JSONResponse({"error": result}, status_code=404)

    att = result
    filename = att.get("filename", "")
    if filename and filename.startswith("~"):
        filename = os.path.expanduser(filename)

    if not filename or not os.path.exists(filename):
        return JSONResponse({"error": "File not found on disk"}, status_code=404)

    media_type = att.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    download_name = att.get("transfer_name") or os.path.basename(filename)

    return FileResponse(
        path=filename,
        media_type=media_type,
        filename=download_name,
    )


async def handle_attachment_upload(request: Request) -> Response:
    """Accept a file upload via POST /attachments/send and send it to a chat.

    Expects a multipart form with a 'file' field. Optional 'chat_identifier'
    form field selects the target chat — required when multiple chats are
    allowed or when allowed_chat_id='*'.
    """
    if not _has_any_allowed():
        return JSONResponse({"error": "No allowed chats configured"}, status_code=500)

    form = await request.form()
    upload = form.get("file")
    if upload is None:
        return JSONResponse({"error": "No 'file' field in form data"}, status_code=400)

    requested_chat = form.get("chat_identifier") or None
    if isinstance(requested_chat, str):
        requested_chat = requested_chat.strip() or None
    target, target_err = _resolve_target_chat(requested_chat if isinstance(requested_chat, str) else None)
    if target_err:
        return JSONResponse({"error": target_err}, status_code=400)

    _, ext = os.path.splitext(upload.filename or "file.bin")
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="imsg_att_") as tmp:
        contents = await upload.read()
        tmp.write(contents)
        tmp_path = tmp.name

    chat_guid = _get_chat_guid(target)
    script = f'tell application "Messages" to send POSIX file "{tmp_path}" to chat id "{chat_guid}"'
    result = run_applescript(script)

    def cleanup():
        import time
        time.sleep(10)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    threading.Thread(target=cleanup, daemon=True).start()

    if result.startswith("Error:"):
        return JSONResponse({"error": result}, status_code=500)

    return JSONResponse({
        "status": "sent",
        "filename": upload.filename,
        "total_bytes": len(contents),
        "chat_identifier": target,
    })


# ── Entrypoint ───────────────────────────────────────────────────────────────


def run_server():
    """Run the MCP server with proper error handling."""
    transport = CONFIG["transport"]
    if ALLOW_ALL_CHATS:
        scope_desc = "* (ALL CHATS)"
    elif not ALLOWED_CHAT_IDS:
        scope_desc = "[NOT SET]"
    elif len(ALLOWED_CHAT_IDS) == 1:
        scope_desc = next(iter(ALLOWED_CHAT_IDS))
    else:
        scope_desc = f"{len(ALLOWED_CHAT_IDS)} chats"
    logger.info(
        f"Starting Mac Messages MCP server (transport={transport}, allowed_chats={scope_desc})..."
    )

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    # SSE transport: build a custom Starlette app with MCP + attachment routes
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp._mcp_server.run(
                streams[0],
                streams[1],
                mcp._mcp_server.create_initialization_options(),
            )

    app = Starlette(
        debug=False,
        routes=[
            # MCP SSE routes
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
            # Attachment HTTP routes
            Route("/attachments/send", endpoint=handle_attachment_upload, methods=["POST"]),
            Route("/attachments/{attachment_id:int}", endpoint=handle_attachment_download),
        ],
    )

    host = CONFIG.get("host", "0.0.0.0")
    port = CONFIG.get("port", 8000)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    run_server()
