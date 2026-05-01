# itch-jams-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets LLM clients (Claude Desktop, Claude Code, any MCP-compatible app) browse and search game jams on [itch.io](https://itch.io/jams).

The server scrapes itch.io directly — no API key, no account required.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_active_jams` | Currently running jams. |
| `get_upcoming_jams` | Jams that have not yet started. |
| `get_recent_jams` | Recently ended jams. |
| `get_all_jams` | Active + upcoming + recent (single page). |
| `get_featured_jams` | Featured/highlighted upcoming jams. |
| `search_jams` | Keyword search by jam title, with optional `active`/`upcoming`/`recent` filter. |
| `get_jam_details` | Full description of a specific jam (title, host, dates, stats, rules). Accepts a slug or full URL. |

All listing tools take an optional `page` argument (default `1`).

## Requirements

- Python 3.11+
- The dependencies listed in `pyproject.toml` (`mcp`, `httpx`, `beautifulsoup4`).

## Install

```bash
git clone https://github.com/petrarka/itch-jams-mcp.git
cd itch-jams-mcp
pip install .
```

Or run straight from the checkout without installing:

```bash
pip install mcp httpx beautifulsoup4
python3 server.py
```

## Transports

The server speaks three transports. Pick whichever your client supports:

| Transport | Use case | Command |
| --- | --- | --- |
| `stdio` (default) | Local MCP clients (Claude Desktop, Claude Code) — one process per client. | `itch-jams-mcp` |
| `sse` | Older HTTP/SSE-based clients. | `itch-jams-mcp --transport sse --host 127.0.0.1 --port 8000` |
| `streamable-http` | Current MCP HTTP transport. Best for shared/remote deployments. | `itch-jams-mcp --transport streamable-http --host 127.0.0.1 --port 8000` |

Useful flags:

- `--mount-path PATH` — override the URL path (defaults: `/sse` for SSE, `/mcp` for Streamable HTTP).
- `--stateless` *(streamable-http)* — run without per-session state; each request is independent. Handy behind load balancers.
- `--json-response` *(streamable-http)* — return plain JSON instead of streaming SSE for non-streaming calls.

When running an HTTP transport, point your client at, e.g., `http://127.0.0.1:8000/sse` or `http://127.0.0.1:8000/mcp`.

## Configure your MCP client (stdio)

Copy `claude_mcp_config.example.json` and adjust the path to `server.py`.

For Claude Desktop, merge the entry into `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "itch-jams": {
      "command": "python3",
      "args": ["/absolute/path/to/itch-jams-mcp/server.py"]
    }
  }
}
```

For Claude Code:

```bash
claude mcp add itch-jams -- python3 /absolute/path/to/itch-jams-mcp/server.py
```

After installing as a package, you can also use the console script:

```json
{
  "mcpServers": {
    "itch-jams": { "command": "itch-jams-mcp" }
  }
}
```

## Example prompts

- *"Show me featured upcoming jams on itch.io."*
- *"Search active jams for the word 'horror'."*
- *"Open the details for the jam `mini-jame-gam-53` and summarize the rules."*

## Development

Install in editable mode with dev extras and run the test suite:

```bash
pip install -e .[dev]
pytest
```

Tests run fully offline — they exercise the HTML/JSON parsing helpers against fixtures.

## Notes & limitations

- itch.io may rate-limit aggressive requests; the server makes one HTTP call per tool invocation.
- The scraper depends on itch.io's current page structure. If the site changes, parsing helpers in `server.py` will need to be updated.
- Each listing tool returns a single page (~440 jams for `get_active_jams` at time of writing). Use `page` to paginate.

## License

MIT — see [LICENSE](LICENSE).
