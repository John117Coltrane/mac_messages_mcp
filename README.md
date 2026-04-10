# Mac Messages MCP

A Python bridge for interacting with the macOS Messages app using MCP (Multiple Context Protocol).

> **Fork notice.** This is the `John117Coltrane/mac_messages_mcp` fork of
> [`carterlasalle/mac_messages_mcp`](https://github.com/carterlasalle/mac_messages_mcp).
> It diverges meaningfully from upstream: the server is **scoped to a single
> allowed chat**, runs over **SSE with HTTP attachment endpoints**, and ships
> a **`ui_automation` helper module** for tapbacks, threaded replies, and
> multi-attachment sends. It is **not** published to PyPI — run from source
> against a local `config.json`. See [Configuration](#configuration) and
> [CHANGELOG.md](CHANGELOG.md) for details.

[![PyPI Downloads](https://static.pepy.tech/badge/mac-messages-mcp)](https://pepy.tech/projects/mac-messages-mcp)

[![Trust Score](https://archestra.ai/mcp-catalog/api/badge/quality/carterlasalle/mac_messages_mcp)](https://archestra.ai/mcp-catalog/carterlasalle__mac_messages_mcp)

![a-diagram-of-a-mac-computer-with-the-tex_FvvnmbaBTFeKy6F2GMlLqA_IfCBMgJARcia1WTH7FaqwA](https://github.com/user-attachments/assets/dbbdaa14-fadd-434d-a265-9e0c0071c11d)

[![Verified on MseeP](https://mseep.ai/badge.svg)](https://mseep.ai/app/fdc62324-6ac9-44e2-8926-722d1157759a)


<a href="https://glama.ai/mcp/servers/gxvaoc9znc">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/gxvaoc9znc/badge" />
</a>

## Quick Install

### For Cursor Users

[![Install MCP Server](https://cursor.com/deeplink/mcp-install-light.svg)](https://cursor.com/install-mcp?name=mac-messages-mcp&config=eyJjb21tYW5kIjoidXZ4IG1hYy1tZXNzYWdlcy1tY3AifQ%3D%3D)

*Click the button above to automatically add Mac Messages MCP to Cursor*

### For Claude Desktop Users

See the [Integration section](#integration) below for setup instructions.

## Features

### Fork additions (0.8.0+)
- **Single-chat lockdown**: every tool is scoped to one `chat_identifier`
  loaded from `config.json`; the server refuses to read or send outside it.
- **SSE transport + HTTP attachment endpoints**: serves MCP over SSE on a
  Starlette app with `GET /attachments/{id}` (download) and
  `POST /attachments/send` (chunked upload) alongside the MCP routes.
- **Enriched message output** from `tool_get_recent_messages`: tapback
  annotations, `EDITED`/`UNSENT`/`REPLY` flags, `msg_id` for targeting,
  attachment metadata with LAN download URLs, chronological ordering, and
  timezone-aware timestamps.
- **`tool_get_new_messages`**: server-side high-water mark for incremental
  polling.
- **`tool_list_chats`**: discover chat identifiers during initial setup.
- **`ui_automation` helper module**: tapback reactions (standard names + any
  emoji), threaded replies, multi-attachment sends, and image-with-caption.
  Drives Messages.app via System Events / Accessibility. Currently a Python
  helper module — not yet wired as MCP tools. Requires Accessibility
  permission for the terminal app or `osascript`.

### Inherited from upstream
- **Universal Message Sending**: Automatically sends via iMessage or SMS/RCS based on recipient availability
- **Smart Fallback**: Seamless fallback to SMS when iMessage is unavailable (perfect for Android users)
- **Message Reading**: Read recent messages from the macOS Messages app
- **Contact Filtering**: Filter messages by specific contacts or phone numbers
- **Fuzzy Search**: Search through message content with intelligent matching
- **iMessage Detection**: Check if recipients have iMessage before sending
- **Cross-Platform**: Works with both iPhone/Mac users (iMessage) and Android users (SMS/RCS)

## Prerequisites

- macOS (tested on macOS 11+)
- Python 3.10+
- **uv package manager**

### Installing uv

If you're on Mac, install uv using Homebrew:

```bash
brew install uv
```

Otherwise, follow the installation instructions on the [uv website](https://github.com/astral-sh/uv).

⚠️ **Do not proceed before installing uv**

## Installation

### Full Disk Access Permission

⚠️ This application requires **Full Disk Access** permission for your terminal or application to access the Messages database. 

To grant Full Disk Access:
1. Open **System Preferences/Settings** > **Security & Privacy/Privacy** > **Full Disk Access**
2. Click the lock icon to make changes
3. Add your terminal app (Terminal, iTerm2, etc.) or Claude Desktop/Cursor to the list
4. Restart your terminal or application after granting permission

### Accessibility Permission (only if you use `ui_automation`)

⚠️ The `ui_automation` helper module drives Messages.app via System Events
to perform tapbacks, threaded replies, and other actions the standard
AppleScript `send` command cannot do. It requires **Accessibility** access
in addition to Full Disk Access.

1. Open **System Settings** > **Privacy & Security** > **Accessibility**
2. Add your terminal app (Terminal, iTerm2, etc.) — or `osascript` directly
3. Restart your terminal after granting permission

You can preflight this from Python with
`mac_messages_mcp.ui_automation.check_accessibility()`.

## Configuration

The fork is **scoped to a configurable allow list of chats**. Before
running the server, copy the example config and fill in your chat
identifier(s):

```bash
cp config.example.json config.json
```

`allowed_chat_id` accepts three forms:

| Form | Example | Behavior |
| --- | --- | --- |
| Single string | `"abc123def456"` | Lock the server to one chat (the original single-chat lockdown) |
| `"*"` | `"*"` | Allow **all** chats — lockdown disabled, logged as a warning at startup |
| JSON list | `["abc123", "fedcba654321"]` | Allow several specific chats |

A multi-chat config looks like:

```json
{
  "allowed_chat_id": ["abc123def456", "fedcba654321"],
  "transport": "sse",
  "host": "0.0.0.0",
  "port": 8000
}
```

To discover the right identifier(s), start the server once with any value
and call `tool_list_chats` over MCP — it returns every chat in the
Messages database and marks the ones currently in the allow list as
`(ACTIVE)`. Paste the right ones back into `config.json` and restart.

### Sending when multiple chats are allowed

When more than one chat is in the allow list (or when `allowed_chat_id`
is `"*"`), the send tools cannot guess a target. They take an optional
`chat_identifier` parameter:

| Tool | New parameter |
| --- | --- |
| `tool_send_message(message, chat_identifier="")` | required if multiple chats are allowed |
| `tool_send_attachment(filename, chunk_base64, ..., chat_identifier="")` | required on first chunk if multiple chats are allowed |
| `POST /attachments/send` | accepts a `chat_identifier` form field |

If the server is locked to a single chat, the parameter can be omitted —
back-compat is preserved.

### Environment variable overrides

Take precedence over `config.json`:

| Variable            | Purpose                                                        |
| ------------------- | -------------------------------------------------------------- |
| `ALLOWED_CHAT_ID`   | Chat scope. Accepts a single id, comma-separated list (`a,b,c`), or `*` |
| `MCP_TRANSPORT`     | `stdio` or `sse` (default `stdio` upstream, `sse` in this fork) |
| `MCP_HOST`          | Bind host (default `0.0.0.0`)                                  |
| `MCP_PORT`          | Bind port (default `8000`)                                     |
| `CHUNK_SIZE_BYTES`  | Chunk size for attachment transfers (default 524288)           |

`config.json` is gitignored — keep your chat IDs out of source control.

## Integration

### Claude Desktop Integration

1. Go to **Claude** > **Settings** > **Developer** > **Edit Config** > **claude_desktop_config.json**
2. Add the following configuration:

```json
{
    "mcpServers": {
        "messages": {
            "command": "uvx",
            "args": [
                "mac-messages-mcp"
            ]
        }
    }
}
```

### Cursor Integration

#### Option 1: One-Click Install (Recommended)

[![Install MCP Server](https://cursor.com/deeplink/mcp-install-light.svg)](https://cursor.com/install-mcp?name=mac-messages-mcp&config=eyJjb21tYW5kIjoidXZ4IG1hYy1tZXNzYWdlcy1tY3AifQ%3D%3D)

#### Option 2: Manual Setup

Go to **Cursor Settings** > **MCP** and paste this as a command:

```
uvx mac-messages-mcp
```

⚠️ Only run one instance of the MCP server (either on Cursor or Claude Desktop), not both

### Docker Container Integration

If you need to connect to `mac-messages-mcp` from a Docker container, you'll need to use the `mcp-proxy` package to bridge the stdio-based server to HTTP.

#### Setup Instructions

1. **Install mcp-proxy on your macOS host:**
```bash
npm install -g mcp-proxy
```

2. **Start the proxy server:**
```bash
# Using the published version
npx mcp-proxy uvx mac-messages-mcp --port 8000 --host 0.0.0.0

# Or using local development (if you encounter issues)
npx mcp-proxy uv run python -m mac_messages_mcp.server --port 8000 --host 0.0.0.0
```

3. **Connect from Docker:**
Your Docker container can now connect to:
- URL: `http://host.docker.internal:8000/mcp` (on macOS/Windows)
- URL: `http://<host-ip>:8000/mcp` (on Linux)

4. **Docker Compose example:**
```yaml
version: '3.8'
services:
  your-app:
    image: your-image
    environment:
      MCP_MESSAGES_URL: "http://host.docker.internal:8000/mcp"
    extra_hosts:
      - "host.docker.internal:host-gateway"  # For Linux hosts
```

5. **Running multiple MCP servers:**
```bash
# Terminal 1 - Messages MCP on port 8001
npx mcp-proxy uvx mac-messages-mcp --port 8001 --host 0.0.0.0

# Terminal 2 - Another MCP server on port 8002
npx mcp-proxy uvx another-mcp-server --port 8002 --host 0.0.0.0
```

**Note:** Binding to `0.0.0.0` exposes the service to all network interfaces. In production, consider using more restrictive host bindings and adding authentication.


### Install from source (this fork)

This fork is **not** published to PyPI — install from source:

```bash
# Clone the fork
git clone https://github.com/John117Coltrane/mac_messages_mcp.git
cd mac_messages_mcp

# Set up your chat scope (see Configuration above)
cp config.example.json config.json
$EDITOR config.json

# Install + run via uv
uv sync
uv run python -m mac_messages_mcp.server
```

The server will start on `http://0.0.0.0:8000` (SSE) by default and log
`Starting Mac Messages MCP server (transport=sse, allowed_chat=...)`.

### Install from PyPI (upstream only)

If you want the upstream package without the fork's single-chat lockdown
and SSE transport, use the published version instead:

```bash
uv pip install mac-messages-mcp
```


## Usage

### Smart Message Delivery

Mac Messages MCP automatically handles message delivery across different platforms:

- **iMessage Users** (iPhone, iPad, Mac): Messages sent via iMessage
- **Android Users**: Messages automatically fall back to SMS/RCS
- **Mixed Groups**: Optimal delivery method chosen per recipient

```python
# Send to iPhone user - uses iMessage
send_message("+1234567890", "Hey! This goes via iMessage")

# Send to Android user - automatically uses SMS
send_message("+1987654321", "Hey! This goes via SMS") 

# Check delivery method before sending
check_imessage_availability("+1234567890")  # Returns availability status
```

### As a Module

```python
from mac_messages_mcp import get_recent_messages, send_message

# Get recent messages
messages = get_recent_messages(hours=48)
print(messages)

# Send a message (automatically chooses iMessage or SMS)
result = send_message(recipient="+1234567890", message="Hello from Mac Messages MCP!")
print(result)  # Shows whether sent via iMessage or SMS
```

### As a Command-Line Tool

```bash
# Run the MCP server directly (this fork — SSE on 0.0.0.0:8000 by default)
uv run python -m mac_messages_mcp.server

# Or, if you've installed the upstream PyPI package:
mac-messages-mcp
```

### HTTP attachment endpoints (fork-only)

When running in SSE mode the server also exposes plain-HTTP routes for
attachment transfer alongside `/sse`:

| Method | Path                  | Purpose                                  |
| ------ | --------------------- | ---------------------------------------- |
| `GET`  | `/attachments/{id}`   | Download an attachment by Messages DB id |
| `POST` | `/attachments/send`   | Chunked upload + send to the locked chat |

`tool_get_recent_messages` includes a `download_url` per attachment that
resolves against the server's real LAN IP, so MCP clients can fetch
attachments directly without going through the MCP transport.

## Development

### Versioning

This project uses semantic versioning. See [VERSIONING.md](VERSIONING.md) for details on how the versioning system works and how to release new versions.

To bump the version:

```bash
python scripts/bump_version.py [patch|minor|major]
```

## Security Notes

This application accesses the Messages database directly, which contains personal communications. Please use it responsibly and ensure you have appropriate permissions.

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/carterlasalle-mac-messages-mcp-badge.png)](https://mseep.ai/app/carterlasalle-mac-messages-mcp)

## License

MIT

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. 
## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=carterlasalle/mac_messages_mcp&type=Date)](https://www.star-history.com/#carterlasalle/mac_messages_mcp&Date)
