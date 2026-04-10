"""
UI automation for Messages.app via macOS Accessibility / System Events.

Provides tapback reactions, threaded replies, and multi-attachment sends
that are not possible through the standard AppleScript `send` command.

Requires: System Settings > Privacy & Security > Accessibility access
for the terminal app or osascript.
"""

import logging
import os
import time
from typing import Optional

from mac_messages_mcp.messages import query_messages_db, run_applescript

logger = logging.getLogger("mac_messages_mcp.ui_automation")

# ── Constants ────────────────────────────────────────────────────────────────

TAPBACK_MAP = {
    "love": 1,
    "like": 2,
    "dislike": 3,
    "laugh": 4,
    "emphasis": 5,
    "question": 6,
}

# ── Accessibility check ──────────────────────────────────────────────────────


def check_accessibility() -> tuple[bool, str]:
    """Check if we have accessibility permissions for UI automation."""
    result = run_applescript(
        'tell application "System Events" to return name of first process whose frontmost is true'
    )
    if result.startswith("Error:"):
        return False, (
            "Accessibility access not granted. "
            "Go to System Settings > Privacy & Security > Accessibility "
            "and add your terminal app."
        )
    return True, "Accessibility access is available."


# ── Messages.app UI discovery ────────────────────────────────────────────────


def _find_messages_group() -> Optional[str]:
    """Find the AX path to the Messages transcript group in the UI.

    Always rediscovers (no caching) since positional AX paths are fragile
    and shift when the window layout changes.
    """
    # Use Python to drive the BFS instead of doing it in AppleScript
    import subprocess

    def _ax_query(ax_path: str, query: str) -> str:
        script = (
            'tell application "System Events"\n'
            'tell process "Messages"\n'
            + query.replace("TARGET", ax_path) +
            '\nend tell\nend tell'
        )
        r = subprocess.run(
            ["osascript", "-"], input=script.encode(),
            capture_output=True, timeout=10
        )
        return r.stdout.decode().strip() if r.returncode == 0 else ""

    queue = ["window 1"]
    while queue:
        path = queue.pop(0)
        desc = _ax_query(path, "return description of TARGET")
        if desc == "Messages":
            role = _ax_query(path, "return role of TARGET")
            count = _ax_query(path, "return count of UI elements of TARGET")
            if role == "AXGroup" and count.isdigit() and int(count) > 3:
                result = path
                break
        count = _ax_query(path, "return count of UI elements of TARGET")
        if count.isdigit() and int(count) > 0:
            c = min(int(count), 10)
            for i in range(1, c + 1):
                queue.append(f"UI element {i} of {path}")
    else:
        result = "NOT_FOUND"

    if result.startswith("Error:") or result == "NOT_FOUND":
        logger.error(f"Could not find Messages transcript group: {result}")
        return None

    logger.info(f"Found Messages group at: {result}")
    return result


def _activate_and_navigate(chat_display_name: str) -> str:
    """Activate Messages.app and navigate to the specified chat.

    Returns success/error message.
    """
    # Activate Messages
    result = run_applescript('tell application "Messages" to activate')
    if result.startswith("Error:"):
        return f"Error activating Messages: {result}"
    time.sleep(0.5)

    # Check if we're already on the right chat by checking window title
    result = run_applescript('''
tell application "System Events"
tell process "Messages"
return name of window 1
end tell
end tell
''')

    if chat_display_name.strip() in result:
        return "OK"

    # Need to navigate: search for the chat in sidebar
    # Use Cmd+F or click the search field, type the chat name
    result = run_applescript(f'''
tell application "System Events"
tell process "Messages"
keystroke "f" using {{command down, shift down}}
delay 0.5
keystroke "{chat_display_name.strip()}"
delay 1
-- Press Return to select first result
keystroke return
delay 0.5
-- Press Escape to dismiss search
key code 53
delay 0.3
end tell
end tell
''')

    if result.startswith("Error:"):
        return f"Error navigating to chat: {result}"
    return "OK"


# ── Message finding ──────────────────────────────────────────────────────────


def get_message_info_for_ui(message_rowid: int, chat_identifier: str) -> Optional[dict]:
    """Look up a message by ROWID and return info needed for UI targeting.

    Returns dict with: text, guid, position_from_bottom, is_from_me, sender
    """
    from mac_messages_mcp.messages import get_contact_name

    # Get the target message
    results = query_messages_db(
        "SELECT ROWID, guid, text, attributedBody, is_from_me, handle_id, date "
        "FROM message WHERE ROWID = ? AND cache_roomnames = ?",
        (message_rowid, chat_identifier),
    )
    if not results or "error" in results[0]:
        return None

    msg = results[0]

    # Get text content
    text = msg.get("text") or ""
    if not text and msg.get("attributedBody"):
        from mac_messages_mcp.messages import extract_body_from_attributed
        text = extract_body_from_attributed(msg["attributedBody"]) or ""

    # Count how many messages come after this one (position from bottom)
    count_results = query_messages_db(
        "SELECT COUNT(*) as cnt FROM message "
        "WHERE cache_roomnames = ? AND date > ? ",
        (chat_identifier, msg["date"]),
    )
    position_from_bottom = 0
    if count_results and "error" not in count_results[0]:
        position_from_bottom = count_results[0]["cnt"]

    sender = "You" if msg["is_from_me"] else get_contact_name(msg["handle_id"])

    return {
        "text": text,
        "guid": msg["guid"],
        "rowid": msg["ROWID"],
        "position_from_bottom": position_from_bottom,
        "is_from_me": bool(msg["is_from_me"]),
        "sender": sender,
    }


def _escape_for_applescript(text: str) -> str:
    """Escape a string for safe embedding in AppleScript double-quoted strings."""
    return (
        text
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )


def _find_message_element(messages_group_path: str, message_text: str, sender: str,
                          position_from_bottom: int) -> Optional[str]:
    """Find a message bubble in the UI by matching text content.

    Returns the AX element path for the matching message, or None.
    """
    # Find the longest contiguous line (no newlines) for matching.
    # Newlines in the DB text may not match the UI description format,
    # so we pick a solid text fragment instead of blindly taking first 40 chars.
    lines = [ln.strip() for ln in message_text.split("\n") if ln.strip()]
    if not lines:
        return None
    # Use the longest line (most unique), capped at 50 chars
    best_line = max(lines, key=len)
    search_text = _escape_for_applescript(best_line[:50])

    script = (
        'tell application "System Events"\n'
        'tell process "Messages"\n'
        'set msgsGroup to ' + messages_group_path + '\n'
        'set c to count of UI elements of msgsGroup\n'
        'set matchIndices to {}\n'
        'repeat with i from 1 to c\n'
        'try\n'
        'set d to description of UI element i of msgsGroup\n'
        'if d contains "' + search_text + '" then\n'
        'set end of matchIndices to i\n'
        'end if\n'
        'end try\n'
        'end repeat\n'
        'set matchCount to count of matchIndices\n'
        'if matchCount is 0 then return "NOT_FOUND"\n'
        'set targetIndex to matchCount - ' + str(position_from_bottom) + '\n'
        'if targetIndex < 1 then set targetIndex to 1\n'
        'if targetIndex > matchCount then set targetIndex to matchCount\n'
        'set foundIdx to item targetIndex of matchIndices\n'
        'return foundIdx as text\n'
        'end tell\n'
        'end tell'
    )
    result = run_applescript(script)

    if result.startswith("Error:") or result == "NOT_FOUND":
        logger.warning(f"Could not find message in UI: {result}")
        return None

    try:
        idx = int(result)
        return f"UI element {idx} of {messages_group_path}"
    except ValueError:
        return None


# ── Tapback ──────────────────────────────────────────────────────────────────


def _open_tapback_menu(el_path: str) -> str:
    """Right-click a message element to open the tapback/context menu.

    Returns 'OK' or an error string.
    """
    script = (
        'tell application "System Events"\n'
        'tell process "Messages"\n'
        'set targetEl to ' + el_path + '\n'
        'perform action "AXShowMenu" of targetEl\n'
        'delay 0.8\n'
        'end tell\n'
        'end tell'
    )
    result = run_applescript(script)
    if not result.startswith("Error:"):
        return "OK"

    # Fallback: try the inner group
    script = (
        'tell application "System Events"\n'
        'tell process "Messages"\n'
        'set targetEl to UI element 1 of ' + el_path + '\n'
        'perform action "AXShowMenu" of targetEl\n'
        'delay 0.8\n'
        'end tell\n'
        'end tell'
    )
    result = run_applescript(script)
    if result.startswith("Error:"):
        return f"Error showing context menu: {result}"
    return "OK"


def _click_standard_tapback(el_path: str, tapback_index: int) -> str:
    """Click one of the 6 standard tapback buttons (1-6) from the context menu.

    After AXShowMenu, the tapback bar appears as a menu. We click the Nth menu item.
    If that fails, we search all window UI elements for a group with 6+ buttons.
    """
    idx = str(tapback_index)
    # Try menu item first (most common on Sequoia)
    script = (
        'tell application "System Events"\n'
        'tell process "Messages"\n'
        'try\n'
        'click menu item ' + idx + ' of menu 1 of ' + el_path + '\n'
        'return "OK"\n'
        'end try\n'
        'try\n'
        'click menu item ' + idx + ' of menu 1 of UI element 1 of ' + el_path + '\n'
        'return "OK"\n'
        'end try\n'
        'set w to window 1\n'
        'repeat with g in UI elements of w\n'
        'try\n'
        'set bc to count of buttons of g\n'
        'if bc >= 6 then\n'
        'click button ' + idx + ' of g\n'
        'return "OK"\n'
        'end if\n'
        'end try\n'
        'end repeat\n'
        'key code 53\n'
        'return "ERROR: Could not find tapback buttons"\n'
        'end tell\n'
        'end tell'
    )
    return run_applescript(script)


def _click_emoji_tapback(el_path: str, emoji: str) -> str:
    """Click the '+' button in the tapback menu to open emoji picker, then type the emoji.

    On macOS Sequoia, the tapback popover has 6 buttons + a '+' (or emoji face) button
    that opens the emoji search. We click that, type the emoji, and select it.
    """
    safe_emoji = _escape_for_applescript(emoji)
    # Click the last menu item (emoji/+ button) in the tapback menu,
    # then type the emoji in the picker search and select it.
    script = (
        'tell application "System Events"\n'
        'tell process "Messages"\n'
        'set found to false\n'
        'try\n'
        'set tapbackMenu to menu 1 of ' + el_path + '\n'
        'set itemCount to count of menu items of tapbackMenu\n'
        'click menu item itemCount of tapbackMenu\n'
        'set found to true\n'
        'end try\n'
        'if not found then\n'
        'try\n'
        'set tapbackMenu to menu 1 of UI element 1 of ' + el_path + '\n'
        'set itemCount to count of menu items of tapbackMenu\n'
        'click menu item itemCount of tapbackMenu\n'
        'set found to true\n'
        'end try\n'
        'end if\n'
        'if not found then\n'
        'set w to window 1\n'
        'repeat with g in UI elements of w\n'
        'try\n'
        'set bc to count of buttons of g\n'
        'if bc >= 6 then\n'
        'click button bc of g\n'
        'set found to true\n'
        'exit repeat\n'
        'end if\n'
        'end try\n'
        'end repeat\n'
        'end if\n'
        'if not found then\n'
        'key code 53\n'
        'return "ERROR: Could not find emoji picker button"\n'
        'end if\n'
        'delay 0.5\n'
        'keystroke "' + safe_emoji + '"\n'
        'delay 0.8\n'
        'keystroke return\n'
        'delay 0.3\n'
        'return "OK"\n'
        'end tell\n'
        'end tell'
    )
    return run_applescript(script)


def send_tapback(
    chat_identifier: str,
    chat_display_name: str,
    message_rowid: int,
    tapback_type: str,
) -> str:
    """Send a tapback reaction on a specific message.

    Args:
        chat_identifier: The chat_identifier from the DB.
        chat_display_name: Display name for navigation.
        message_rowid: ROWID of the message to react to.
        tapback_type: One of the standard names (love, like, dislike, laugh,
                      emphasis, question) OR any emoji character (e.g. "🖤", "🤔").

    Returns success or error message.
    """
    is_standard = tapback_type in TAPBACK_MAP
    if not is_standard and len(tapback_type) > 10:
        return f"Error: tapback_type must be a standard name ({', '.join(TAPBACK_MAP.keys())}) or an emoji."

    # Get message info
    msg_info = get_message_info_for_ui(message_rowid, chat_identifier)
    if not msg_info:
        return f"Error: Message {message_rowid} not found in the allowed chat."

    if not msg_info["text"]:
        return "Error: Cannot find message text to locate in UI."

    # Activate and navigate
    nav_result = _activate_and_navigate(chat_display_name)
    if nav_result != "OK":
        return nav_result

    time.sleep(0.3)

    # Find the Messages transcript group
    msgs_path = _find_messages_group()
    if not msgs_path:
        return "Error: Could not find Messages transcript in the UI."

    # Find the message element
    el_path = _find_message_element(
        msgs_path, msg_info["text"], msg_info["sender"], msg_info["position_from_bottom"]
    )
    if not el_path:
        return f"Error: Could not find message '{msg_info['text'][:40]}...' in the Messages UI."

    # Open the tapback menu
    menu_result = _open_tapback_menu(el_path)
    if menu_result != "OK":
        return menu_result

    # Click the appropriate tapback
    if is_standard:
        result = _click_standard_tapback(el_path, TAPBACK_MAP[tapback_type])
    else:
        result = _click_emoji_tapback(el_path, tapback_type)

    if "OK" in result:
        return f"Tapback '{tapback_type}' sent on message {message_rowid}."
    return f"Error sending tapback: {result}"


# ── Reply to message ─────────────────────────────────────────────────────────


def send_reply(
    chat_identifier: str,
    chat_display_name: str,
    message_rowid: int,
    reply_text: str,
) -> str:
    """Send a threaded reply to a specific message.

    Args:
        chat_identifier: The chat_identifier from the DB.
        chat_display_name: Display name for navigation.
        message_rowid: ROWID of the message to reply to.
        reply_text: The reply text to send.

    Returns success or error message.
    """
    msg_info = get_message_info_for_ui(message_rowid, chat_identifier)
    if not msg_info:
        return f"Error: Message {message_rowid} not found in the allowed chat."

    if not msg_info["text"]:
        return "Error: Cannot find message text to locate in UI."

    nav_result = _activate_and_navigate(chat_display_name)
    if nav_result != "OK":
        return nav_result

    time.sleep(0.3)

    msgs_path = _find_messages_group()
    if not msgs_path:
        return "Error: Could not find Messages transcript in the UI."

    el_path = _find_message_element(
        msgs_path, msg_info["text"], msg_info["sender"], msg_info["position_from_bottom"]
    )
    if not el_path:
        return f"Error: Could not find message '{msg_info['text'][:40]}...' in the Messages UI."

    # Right-click and select "Reply"
    safe_reply = _escape_for_applescript(reply_text)

    script = (
        'tell application "System Events"\n'
        'tell process "Messages"\n'
        'set targetEl to ' + el_path + '\n'
        'perform action "AXShowMenu" of targetEl\n'
        'delay 0.5\n'
        'try\n'
        'click menu item "Reply" of menu 1 of targetEl\n'
        'on error\n'
        'try\n'
        'click menu item "Reply" of menu 1 of UI element 1 of targetEl\n'
        'end try\n'
        'end try\n'
        'delay 0.5\n'
        'keystroke "' + safe_reply + '"\n'
        'delay 0.2\n'
        'keystroke return\n'
        'delay 0.3\n'
        'return "OK"\n'
        'end tell\n'
        'end tell'
    )
    result = run_applescript(script)

    if "OK" in result:
        return f"Reply sent to message {message_rowid}."
    return f"Error sending reply: {result}"


# ── Multi-attachment ─────────────────────────────────────────────────────────


def send_multiple_attachments(chat_guid: str, file_paths: list[str]) -> str:
    """Send multiple file attachments sequentially.

    Uses the standard AppleScript send command in a loop.
    Does NOT require UI automation.
    """
    results = []
    for path in file_paths:
        if not os.path.exists(path):
            results.append(f"SKIP {path}: file not found")
            continue
        script = f'tell application "Messages" to send POSIX file "{path}" to chat id "{chat_guid}"'
        r = run_applescript(script)
        if r.startswith("Error:"):
            results.append(f"FAIL {os.path.basename(path)}: {r}")
        else:
            results.append(f"OK {os.path.basename(path)}")
        time.sleep(1.5)  # Let each send complete

    return "\n".join(results)


# ── Caption + image ──────────────────────────────────────────────────────────


def send_image_with_caption(chat_guid: str, file_path: str, caption: str) -> str:
    """Send an image file followed by a text caption.

    Uses two sequential standard sends (not UI automation).
    """
    from mac_messages_mcp.messages import send_message

    if not os.path.exists(file_path):
        return f"Error: File not found: {file_path}"

    # Send the image first
    script = f'tell application "Messages" to send POSIX file "{file_path}" to chat id "{chat_guid}"'
    result = run_applescript(script)
    if result.startswith("Error:"):
        return f"Error sending image: {result}"

    time.sleep(1.5)

    # Send the caption
    result = send_message(recipient=chat_guid, message=caption, group_chat=True)

    return f"Image and caption sent. Image: OK, Caption: {result}"
