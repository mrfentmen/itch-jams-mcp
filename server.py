#!/usr/bin/env python3
"""MCP server that parses game jams from itch.io"""

import argparse
import asyncio
import contextlib
import json
import re

import httpx
from bs4 import BeautifulSoup
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

BASE_URL = "https://itch.io/jams"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

app = Server("itch-jams")


def extract_jams_json(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cal = soup.select_one(".calendar_wrapper")
    if not cal:
        return []
    script_tag = cal.select_one("script")
    if not script_tag or not script_tag.string:
        return []

    script = script_tag.string
    marker = "FilteredJamCalendar("
    if marker not in script:
        return []

    start = script.index(marker) + len(marker)
    depth, i = 0, start
    while i < len(script):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1

    data = json.loads(script[start : i + 1])
    return data.get("jams", [])


def jam_to_dict(j: dict) -> dict:
    url = j.get("url", "")
    if url and not url.startswith("http"):
        url = "https://itch.io" + url
    return {
        "id": j.get("id"),
        "title": j.get("title", ""),
        "url": url,
        "start_date": j.get("start_date", ""),
        "end_date": j.get("end_date", ""),
        "joined": j.get("joined", 0),
        "highlight": j.get("highlight", False),
    }


def format_jams(jams: list[dict]) -> str:
    if not jams:
        return "No jams found."
    lines = []
    for i, jam in enumerate(jams, 1):
        lines.append(f"### {i}. {jam['title']}")
        lines.append(f"URL: {jam['url']}")
        lines.append(f"Start: {jam['start_date']}  |  End: {jam['end_date']}")
        lines.append(f"Participants: {jam['joined']}")
        if jam["highlight"]:
            lines.append("(Featured)")
        lines.append("")
    return "\n".join(lines)


async def fetch_jams(filter_type: str = "", page: int = 1) -> list[dict]:
    params: dict = {}
    if filter_type:
        params["filter"] = filter_type
    if page > 1:
        params["page"] = page

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
        resp = await client.get(BASE_URL, params=params)
        resp.raise_for_status()
        raw = extract_jams_json(resp.text)
        return [jam_to_dict(j) for j in raw]


async def fetch_jams_from_url(url: str, params: dict | None = None) -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
        resp = await client.get(url, params=params or {})
        resp.raise_for_status()
        raw = extract_jams_json(resp.text)
        return [jam_to_dict(j) for j in raw]


def parse_jam_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # title — from jam_title_header or og:title
    title_el = soup.select_one(".jam_title_header")
    if title_el:
        title = title_el.get_text(strip=True)
    else:
        og = soup.find("meta", property="og:title")
        title = og["content"] if og else ""

    # host
    host_el = soup.select_one(".jam_host_header")
    host = host_el.get_text(strip=True) if host_el else ""

    # stats (entries, ratings, etc.)
    stats = {}
    for box in soup.select(".stat_box"):
        label = box.select_one(".stat_label")
        val = box.select_one(".stat_value")
        if label and val:
            stats[label.get_text(strip=True)] = val.get_text(strip=True)

    # dates from jam_header_widget or meta
    start_date, end_date = "", ""
    header_widget = soup.select_one(".jam_header_widget")
    if header_widget:
        text = header_widget.get_text(" ", strip=True)
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text)
        if m:
            start_date = m.group(1)
        dates = re.findall(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text)
        if len(dates) >= 2:
            start_date, end_date = dates[0], dates[1]

    # description — full text of .jam_content, preserving structure
    content_el = soup.select_one(".jam_content")
    description = ""
    if content_el:
        lines = []
        for tag in content_el.find_all(["h1", "h2", "h3", "h4", "p", "li", "hr", "details"]):
            if tag.name == "hr":
                lines.append("\n---")
                continue
            text = tag.get_text(strip=True)
            if not text:
                continue
            if tag.name in ("h1", "h2"):
                lines.append(f"\n## {text}")
            elif tag.name in ("h3", "h4"):
                lines.append(f"\n### {text}")
            elif tag.name == "li":
                lines.append(f"- {text}")
            else:
                lines.append(text)
        description = "\n".join(lines).strip()

    return {
        "title": title,
        "url": url,
        "host": host,
        "start_date": start_date,
        "end_date": end_date,
        "stats": stats,
        "description": description,
    }


def format_jam_details(d: dict) -> str:
    lines = [f"# {d['title']}", f"URL: {d['url']}"]
    if d["host"]:
        lines.append(f"Host: {d['host']}")
    if d["start_date"] or d["end_date"]:
        lines.append(f"Dates: {d['start_date']} → {d['end_date']}")
    if d["stats"]:
        stats_str = "  |  ".join(f"{k}: {v}" for k, v in d["stats"].items())
        lines.append(f"Stats: {stats_str}")
    if d["description"]:
        lines.append("\n---\n")
        lines.append(d["description"])
    return "\n".join(lines)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_active_jams",
            description="Get currently active/running game jams on itch.io",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "Page number (default 1)", "default": 1}
                },
            },
        ),
        types.Tool(
            name="get_upcoming_jams",
            description="Get upcoming game jams on itch.io (not yet started)",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "Page number (default 1)", "default": 1}
                },
            },
        ),
        types.Tool(
            name="get_recent_jams",
            description="Get recently ended game jams on itch.io",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "Page number (default 1)", "default": 1}
                },
            },
        ),
        types.Tool(
            name="get_all_jams",
            description="Get all game jams on itch.io visible on the main page (active + upcoming + recent)",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "Page number (default 1)", "default": 1}
                },
            },
        ),
        types.Tool(
            name="get_featured_jams",
            description="Get featured/highlighted upcoming jams on itch.io",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="search_jams",
            description="Search for game jams on itch.io by keyword in the title",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword"},
                    "filter": {
                        "type": "string",
                        "description": "Optional filter: 'active', 'upcoming', 'recent'",
                        "enum": ["", "active", "upcoming", "recent"],
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_jam_details",
            description=(
                "Fetch the full description and details of a specific itch.io game jam page. "
                "Pass either a full URL (https://itch.io/jam/...) or just the jam slug (e.g. 'micro-jam-055')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jam": {
                        "type": "string",
                        "description": "Full URL or slug of the jam, e.g. 'micro-jam-055' or 'https://itch.io/jam/micro-jam-055'",
                    }
                },
                "required": ["jam"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    page = int(arguments.get("page", 1))

    try:
        if name == "get_active_jams":
            jams = await fetch_jams(filter_type="active", page=page)
            text = f"## Active Game Jams on itch.io (page {page}, total: {len(jams)})\n\n{format_jams(jams)}"

        elif name == "get_upcoming_jams":
            jams = await fetch_jams(filter_type="upcoming", page=page)
            text = f"## Upcoming Game Jams on itch.io (page {page}, total: {len(jams)})\n\n{format_jams(jams)}"

        elif name == "get_recent_jams":
            jams = await fetch_jams(filter_type="recent", page=page)
            text = f"## Recent Game Jams on itch.io (page {page}, total: {len(jams)})\n\n{format_jams(jams)}"

        elif name == "get_all_jams":
            jams = await fetch_jams(page=page)
            text = f"## All Game Jams on itch.io (page {page}, total: {len(jams)})\n\n{format_jams(jams)}"

        elif name == "get_featured_jams":
            jams = await fetch_jams_from_url("https://itch.io/jams/upcoming/featured")
            featured = [j for j in jams if j["highlight"]]
            text = (
                f"## Featured Upcoming Game Jams on itch.io (total: {len(featured)})\n\n"
                f"{format_jams(featured or jams)}"
            )

        elif name == "search_jams":
            query = arguments.get("query", "").lower().strip()
            filter_type = arguments.get("filter", "")
            jams = await fetch_jams(filter_type=filter_type)
            matches = [j for j in jams if query in j["title"].lower()]
            text = (
                f"## Search results for '{query}' on itch.io "
                f"({len(matches)} of {len(jams)} jams matched)\n\n"
                f"{format_jams(matches)}"
            )

        elif name == "get_jam_details":
            jam_arg = arguments.get("jam", "").strip()
            if jam_arg.startswith("http"):
                jam_url = jam_arg
            else:
                slug = jam_arg.lstrip("/")
                jam_url = f"https://itch.io/jam/{slug}"
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.get(jam_url)
                resp.raise_for_status()
                details = parse_jam_page(resp.text, jam_url)
            text = format_jam_details(details)

        else:
            text = f"Unknown tool: {name}"

    except httpx.HTTPStatusError as e:
        text = f"HTTP error: {e.response.status_code} {e.response.reason_phrase}"
    except Exception as e:
        text = f"Error: {type(e).__name__}: {e}"

    return [types.TextContent(type="text", text=text)]


async def _serve_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def build_sse_app(message_path: str = "/messages/", sse_path: str = "/sse"):
    """Build a Starlette app that serves the MCP server over SSE."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    transport = SseServerTransport(message_path)

    async def handle_sse(request):
        async with transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
        from starlette.responses import Response
        return Response()

    return Starlette(
        routes=[
            Route(sse_path, endpoint=handle_sse, methods=["GET"]),
            Mount(message_path, app=transport.handle_post_message),
        ]
    )


def build_streamable_http_app(mount_path: str = "/mcp", *, stateless: bool = False, json_response: bool = False):
    """Build a Starlette app that serves the MCP server over Streamable HTTP."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(
        app=app, stateless=stateless, json_response=json_response
    )

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(routes=[Mount(mount_path, app=handle)], lifespan=lifespan)


def main() -> None:
    parser = argparse.ArgumentParser(prog="itch-jams-mcp", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport to expose the MCP server on (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind for HTTP transports (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind for HTTP transports (default: 8000).")
    parser.add_argument(
        "--mount-path",
        default=None,
        help="URL path to mount the HTTP transport on. Default: /sse for sse, /mcp for streamable-http.",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="(streamable-http only) Run without per-session state — each request is independent.",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="(streamable-http only) Return JSON responses instead of streaming SSE for non-streaming calls.",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        asyncio.run(_serve_stdio())
        return

    import uvicorn

    if args.transport == "sse":
        sse_path = args.mount_path or "/sse"
        starlette_app = build_sse_app(sse_path=sse_path)
    else:  # streamable-http
        mount_path = args.mount_path or "/mcp"
        starlette_app = build_streamable_http_app(
            mount_path=mount_path,
            stateless=args.stateless,
            json_response=args.json_response,
        )

    uvicorn.run(starlette_app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
