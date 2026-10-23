#!/usr/bin/env python3
"""MCP server that parses game jams and games from itch.io"""

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


# --- Generic itch.io listing parsers ---------------------------------------

BASE_ITCH_URL = "https://itch.io"


def parse_game_cell(cell) -> dict | None:
    """Parse one .game_cell from a browse listing (games or game assets)."""
    title_el = cell.select_one(".game_title a.title")
    if not title_el:
        return None
    author_el = cell.select_one(".game_author a")
    genre_el = cell.select_one(".game_genre")
    text_el = cell.select_one(".game_text")
    price_el = cell.select_one(".price_value")
    thumb_el = cell.select_one("img[data-lazy_src]") or cell.select_one("img")
    platforms: list[str] = []
    web = cell.select_one(".web_flag")
    if web:
        platforms.append("Web")
    for icon in cell.select(".game_platform .icon[title]"):
        platforms.append(icon.get("title", "").replace("Download for ", ""))
    return {
        "id": cell.get("data-game_id", ""),
        "title": title_el.get_text(strip=True),
        "url": title_el["href"],
        "author": author_el.get_text(strip=True) if author_el else "",
        "author_url": author_el["href"] if author_el else "",
        "description": text_el.get_text(strip=True) if text_el else "",
        "genre": genre_el.get_text(strip=True) if genre_el else "",
        "price": price_el.get_text(strip=True) if price_el else "",
        "platforms": platforms,
        "thumb": thumb_el.get("data-lazy_src", "") if thumb_el else "",
    }


def parse_listing(html: str) -> tuple[list[dict], bool]:
    """Parse all game cells from a listing page, plus whether a next page exists."""
    soup = BeautifulSoup(html, "html.parser")
    games = [g for g in (parse_game_cell(c) for c in soup.select(".game_cell")) if g]
    return games, soup.select_one(".next_page") is not None


def format_games(games: list[dict]) -> str:
    if not games:
        return "No games found."
    lines = []
    for i, g in enumerate(games, 1):
        title = g["title"]
        if g.get("price"):
            title += f" — {g['price']}"
        lines.append(f"### {i}. {title}")
        lines.append(f"URL: {g['url']}")
        if g.get("author"):
            lines.append(f"By: {g['author']}")
        bits = []
        if g.get("genre"):
            bits.append(g["genre"])
        if g.get("platforms"):
            bits.append(", ".join(g["platforms"]))
        if bits:
            lines.append("Type: " + " | ".join(bits))
        if g.get("description"):
            lines.append(g["description"])
        lines.append("")
    return "\n".join(lines)


async def fetch_listing(path: str, params: dict | None = None) -> tuple[list[dict], bool]:
    """Fetch an itch.io listing page (path may be absolute or site-relative)."""
    url = path if path.startswith("http") else BASE_ITCH_URL + path
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
        resp = await client.get(url, params=params or {})
        resp.raise_for_status()
        return parse_listing(resp.text)


def build_browse_path(sort: str = "", price: str = "", platform: str = "",
                      genre: str = "", tag: str = "") -> str:
    """Build an itch.io browse URL segment chain.

    itch.io accepts at most 2 combined modifiers after /games (verified live);
    longer chains return HTTP 403.
    """
    segments = []
    if sort and sort != "trending":
        segments.append(sort)  # newest, top-sellers
    if price:
        segments.append(price)
    if platform:
        segments.append(f"platform-{platform}")
    if genre:
        segments.append(f"genre-{genre}")
    if tag:
        segments.append(f"tag-{tag}")
    if len(segments) > 2:
        raise ValueError(
            "itch.io allows at most 2 combined browse filters per URL "
            "(sort counts as one); drop a filter and try again."
        )
    return "/games" + ("/" + "/".join(segments) if segments else "")


async def search_listing(path: str, query: str, max_pages: int = 3) -> list[dict]:
    """Fetch up to max_pages of a listing and filter it locally by query.

    itch.io's /search route is robots-disallowed, so text search is done by
    scanning public browse pages and matching titles/descriptions locally.
    """
    q = query.lower().strip()
    if not q:
        return []
    results: list[dict] = []
    page = 1
    has_next = True
    while page <= max_pages and has_next:
        games, has_next = await fetch_listing(path, {"page": page} if page > 1 else {})
        results.extend(games)
        page += 1
    return [
        g for g in results
        if q in g["title"].lower() or q in g.get("description", "").lower()
    ]


def game_page_url(game: str) -> str:
    """Accept a full game URL or a 'user/game' pair."""
    g = game.strip().rstrip("/")
    if g.startswith("http"):
        return g
    if "/" in g:
        user, slug = g.split("/", 1)
        return f"https://{user.strip()}.itch.io/{slug.strip()}"
    raise ValueError(
        "Pass a full game URL (https://user.itch.io/game) or a 'user/game' pair."
    )


def parse_game_details(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def og(prop: str) -> str:
        m = soup.find("meta", property=prop)
        return m["content"] if m and m.has_attr("content") else ""

    info: dict[str, str] = {}
    panel = soup.select_one(".game_info_panel_widget")
    if panel:
        for row in panel.select("tr"):
            tds = row.find_all(["td", "th"])
            if len(tds) >= 2:
                links = tds[1].select("a")
                val = ", ".join(a.get_text(strip=True) for a in links) if links \
                    else tds[1].get_text(" ", strip=True)
                info[tds[0].get_text(strip=True)] = val

    rating, rating_count = "", ""
    rating_el = soup.select_one(".aggregate_rating")
    if rating_el:
        val = rating_el.select_one('[itemprop="ratingValue"]')
        rating = val.get("content", "") if val else rating_el.get_text(strip=True)
        m = re.search(r"from (\d[\d,]*)", rating_el.get("data-tooltip", ""))
        if m:
            rating_count = m.group(1)

    price_el = soup.select_one(".price_value")
    tags: list[str] = []
    for a in soup.select("a[href*='itch.io/games/tag-']"):
        t = a.get_text(strip=True)
        if t and t not in tags:
            tags.append(t)

    title = og("og:title")
    if not title:
        h1 = soup.select_one("h1.game_title")
        title = h1.get_text(strip=True) if h1 else og("og:description")

    return {
        "title": title,
        "url": url,
        "description": og("og:description"),
        "image": og("og:image"),
        "author": url.split("//", 1)[-1].split(".itch.io")[0],
        "price": price_el.get_text(strip=True) if price_el else "Free / name your own price",
        "rating": rating,
        "rating_count": rating_count,
        "tags": tags,
        "info": info,
    }


def format_game_details(d: dict) -> str:
    lines = [f"# {d['title']}", f"URL: {d['url']}", f"By: {d['author']}"]
    if d["price"]:
        lines.append(f"Price: {d['price']}")
    if d["rating"]:
        count = f" ({d['rating_count']} ratings)" if d["rating_count"] else ""
        lines.append(f"Rating: {d['rating']}{count}")
    if d["tags"]:
        lines.append(f"Tags: {', '.join(d['tags'])}")
    if d["description"]:
        lines.append("\n" + d["description"])
    extra = "  |  ".join(f"{k}: {v}" for k, v in d["info"].items())
    if extra:
        lines.append("\nInfo: " + extra)
    return "\n".join(lines)


def parse_devlog_posts(html: str) -> list[dict]:
    """Parse devlog post titles + links from a game's /devlog listing."""
    soup = BeautifulSoup(html, "html.parser")
    posts, seen = [], set()
    for a in soup.select("a[href*='/devlog/']"):
        href = a["href"]
        if not re.search(r"/devlog/\d+", href) or href in seen:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        seen.add(href)
        posts.append({"title": title, "url": href})
    return posts


def parse_profile(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one(".stat_header_widget")
    name = header.h2.get_text(strip=True) if header and header.h2 else ""
    stats: dict[str, str] = {}
    for box in soup.select(".stat_box"):
        v, l = box.select_one(".stat_value"), box.select_one(".stat_label")
        if v and l:
            stats[l.get_text(strip=True)] = v.get_text(strip=True)
    joined = ""
    user_data = soup.select_one(".user_data")
    if user_data:
        m = re.search(r"registered ([^·]+)", user_data.get_text(" ", strip=True))
        if m:
            joined = m.group(1).strip()
    links = []
    for a in soup.select("a[href^='http']"):
        href = a["href"]
        if ("itch.io" not in href and "img." not in href and "static." not in href
                and href not in links):
            links.append(href)
    return {"name": name, "url": url, "stats": stats, "joined": joined,
            "external_links": links[:10]}


def parse_sale_row(row) -> dict | None:
    title_a = row.select_one(".sale_title a")
    if not title_a:
        return None
    author = row.select_one(".sale_author a")
    rate = row.select_one(".sale_flag .rate")
    discount = ""
    if rate:
        discount = rate.get_text(strip=True).strip("%") + "% off"
    details = row.select_one(".game_count")
    end = row.select_one(".sale_end .date_format")
    href = title_a.get("href", "")
    return {
        "title": title_a.get_text(strip=True),
        "url": BASE_ITCH_URL + href if href.startswith("/") else href,
        "author": author.get_text(strip=True) if author else "",
        "discount": discount,
        "details": details.get_text(" ", strip=True) if details else "",
        "ends": end.get_text(strip=True) if end else "",
    }


def parse_sales(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    return [s for s in (parse_sale_row(r) for r in soup.select(".sale_row")) if s]


def parse_jam_entry_cell(cell) -> dict | None:
    a = cell.select_one(".label a.title")
    if not a:
        return None
    user = cell.select_one(".user_row a.user_link")
    return {
        "title": a.get_text(strip=True),
        "url": a.get("href", ""),
        "author": user.get_text(strip=True) if user else "",
        "author_url": user.get("href", "") if user else "",
    }


def parse_jam_entries(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    return [
        e for e in
        (parse_jam_entry_cell(c) for c in soup.select(".game_cell"))
        if e
    ]


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
        # --- General itch.io tools -------------------------------------------
        types.Tool(
            name="get_trending_games",
            description="Get the featured/trending indie games currently shown on the itch.io games front page",
            inputSchema={
                "type": "object",
                "properties": {"page": {"type": "integer", "description": "Page number (default 1)", "default": 1}},
            },
        ),
        types.Tool(
            name="get_new_games",
            description="Get the newest indie games uploaded to itch.io",
            inputSchema={
                "type": "object",
                "properties": {"page": {"type": "integer", "description": "Page number (default 1)", "default": 1}},
            },
        ),
        types.Tool(
            name="get_top_sellers",
            description="Get the current top-selling paid games on itch.io",
            inputSchema={
                "type": "object",
                "properties": {"page": {"type": "integer", "description": "Page number (default 1)", "default": 1}},
            },
        ),
        types.Tool(
            name="get_free_games",
            description="Get free games on itch.io",
            inputSchema={
                "type": "object",
                "properties": {"page": {"type": "integer", "description": "Page number (default 1)", "default": 1}},
            },
        ),
        types.Tool(
            name="browse_games",
            description=(
                "Browse itch.io games with combined filters. sort: trending/newest/top-sellers; "
                "price: free/paid; platform: windows/mac/linux/android/ios/web; "
                "genre: e.g. platformer, action, puzzle; tag: e.g. pixel-art, roguelike. "
                "itch.io allows at most 2 combined filters per URL (sort counts as one)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sort": {"type": "string", "enum": ["trending", "newest", "top-sellers"], "default": "trending"},
                    "price": {"type": "string", "enum": ["free", "paid"]},
                    "platform": {"type": "string", "description": "windows, mac, linux, android, ios or web"},
                    "genre": {"type": "string", "description": "e.g. platformer, action, puzzle"},
                    "tag": {"type": "string", "description": "e.g. pixel-art, roguelike"},
                    "page": {"type": "integer", "default": 1},
                },
            },
        ),
        types.Tool(
            name="get_games_by_tag",
            description="Browse itch.io games by tag, e.g. 'pixel-art', 'roguelike', 'cozy'",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Tag slug, e.g. pixel-art"},
                    "page": {"type": "integer", "default": 1},
                },
                "required": ["tag"],
            },
        ),
        types.Tool(
            name="get_games_by_genre",
            description="Browse itch.io games by genre, e.g. 'platformer', 'action', 'puzzle'",
            inputSchema={
                "type": "object",
                "properties": {
                    "genre": {"type": "string", "description": "Genre slug, e.g. platformer"},
                    "page": {"type": "integer", "default": 1},
                },
                "required": ["genre"],
            },
        ),
        types.Tool(
            name="search_games",
            description=(
                "Search itch.io games by keyword. itch.io's search route is robots-disallowed, "
                "so this scans public browse pages and matches titles/descriptions locally. "
                "scope: trending/newest/top-sellers/free/paid"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to match"},
                    "scope": {"type": "string", "enum": ["trending", "newest", "top-sellers", "free", "paid"], "default": "trending"},
                    "max_pages": {"type": "integer", "description": "Browse pages to scan (default 3, max 10)", "default": 3},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_games_on_sale",
            description="Get games and asset packs currently discounted on itch.io",
            inputSchema={
                "type": "object",
                "properties": {"page": {"type": "integer", "default": 1}},
            },
        ),
        types.Tool(
            name="search_assets",
            description=(
                "Search itch.io game assets (sprites, tilesets, music, UI kits) by keyword. "
                "Matches titles/descriptions on public asset browse pages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to match"},
                    "price": {"type": "string", "enum": ["free", "paid"]},
                    "tag": {"type": "string", "description": "Optional tag slug, e.g. tileset, pixel-art"},
                    "max_pages": {"type": "integer", "description": "Browse pages to scan (default 3, max 10)", "default": 3},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_game_details",
            description=(
                "Get details for one itch.io game: description, price, rating, tags, platforms. "
                "Pass a full URL (https://user.itch.io/game) or a 'user/game' pair."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Full URL or 'user/game', e.g. 'samandev/tamaweb'"},
                },
                "required": ["game"],
            },
        ),
        types.Tool(
            name="get_game_devlogs",
            description=(
                "List devlog posts for an itch.io game. "
                "Pass a full URL (https://user.itch.io/game) or a 'user/game' pair."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Full URL or 'user/game'"},
                },
                "required": ["game"],
            },
        ),
        types.Tool(
            name="get_creator_profile",
            description=(
                "Get an itch.io creator's public profile: name, follower/post counts, "
                "registration date and external links. Pass a username, e.g. 'nopanamaman'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "itch.io username"},
                },
                "required": ["user"],
            },
        ),
        types.Tool(
            name="get_creator_games",
            description=(
                "List the games shown on an itch.io creator's profile page. "
                "Pass a username, e.g. 'nopanamaman'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "itch.io username"},
                },
                "required": ["user"],
            },
        ),
        types.Tool(
            name="get_jam_entries",
            description=(
                "List the entries submitted to an itch.io game jam. "
                "Pass a full jam URL (https://itch.io/jam/...) or just the jam slug (e.g. 'gmtk-2020')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jam": {"type": "string", "description": "Full URL or slug of the jam"},
                    "page": {"type": "integer", "default": 1},
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

        # --- General itch.io tools -------------------------------------------
        elif name == "get_trending_games":
            games, _ = await fetch_listing("/games", {"page": page} if page > 1 else {})
            text = f"## Trending games on itch.io (page {page}, {len(games)} shown)\n\n{format_games(games)}"

        elif name == "get_new_games":
            games, _ = await fetch_listing("/games/newest", {"page": page} if page > 1 else {})
            text = f"## Newest games on itch.io (page {page}, {len(games)} shown)\n\n{format_games(games)}"

        elif name == "get_top_sellers":
            games, _ = await fetch_listing("/games/top-sellers", {"page": page} if page > 1 else {})
            text = f"## Top-selling games on itch.io (page {page}, {len(games)} shown)\n\n{format_games(games)}"

        elif name == "get_free_games":
            games, _ = await fetch_listing("/games/free", {"page": page} if page > 1 else {})
            text = f"## Free games on itch.io (page {page}, {len(games)} shown)\n\n{format_games(games)}"

        elif name == "browse_games":
            path = build_browse_path(
                sort=arguments.get("sort", ""),
                price=arguments.get("price", ""),
                platform=arguments.get("platform", ""),
                genre=arguments.get("genre", ""),
                tag=arguments.get("tag", ""),
            )
            games, _ = await fetch_listing(path, {"page": page} if page > 1 else {})
            text = f"## itch.io games: {path} (page {page}, {len(games)} shown)\n\n{format_games(games)}"

        elif name == "get_games_by_tag":
            tag = arguments.get("tag", "").strip().lstrip("/")
            games, _ = await fetch_listing(f"/games/tag-{tag}", {"page": page} if page > 1 else {})
            text = f"## itch.io games tagged '{tag}' (page {page}, {len(games)} shown)\n\n{format_games(games)}"

        elif name == "get_games_by_genre":
            genre = arguments.get("genre", "").strip().lstrip("/")
            games, _ = await fetch_listing(f"/games/genre-{genre}", {"page": page} if page > 1 else {})
            text = f"## itch.io games in genre '{genre}' (page {page}, {len(games)} shown)\n\n{format_games(games)}"

        elif name == "search_games":
            query = arguments.get("query", "")
            scope = arguments.get("scope", "trending")
            max_pages = min(int(arguments.get("max_pages", 3)), 10)
            paths = {
                "trending": "/games",
                "newest": "/games/newest",
                "top-sellers": "/games/top-sellers",
                "free": "/games/free",
                "paid": "/games/paid",
            }
            matches = await search_listing(paths[scope], query, max_pages)
            text = (
                f"## Search results for '{query}' (scope: {scope}, {len(matches)} matched)\n\n"
                f"{format_games(matches)}"
            )

        elif name == "get_games_on_sale":
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.get(BASE_ITCH_URL + "/sales", params={"page": page} if page > 1 else {})
                resp.raise_for_status()
                sales = parse_sales(resp.text)
            lines = []
            for i, s in enumerate(sales, 1):
                lines.append(f"### {i}. {s['title']} ({s['discount']})")
                lines.append(f"URL: {s['url']}")
                if s["author"]:
                    lines.append(f"By: {s['author']}")
                if s["details"]:
                    lines.append(s["details"])
                if s["ends"]:
                    lines.append(f"Ends: {s['ends']}")
                lines.append("")
            body = "\n".join(lines) if sales else "No sales found."
            text = f"## Sales on itch.io (page {page}, {len(sales)} shown)\n\n{body}"

        elif name == "search_assets":
            query = arguments.get("query", "")
            max_pages = min(int(arguments.get("max_pages", 3)), 10)
            segments = []
            price = arguments.get("price", "")
            tag = arguments.get("tag", "").strip().lstrip("/")
            if price:
                segments.append(price)
            if tag:
                segments.append(f"tag-{tag}")
            path = "/game-assets" + ("/" + "/".join(segments) if segments else "")
            matches = await search_listing(path, query, max_pages)
            text = (
                f"## itch.io asset search for '{query}' ({path}, {len(matches)} matched)\n\n"
                f"{format_games(matches)}"
            )

        elif name == "get_game_details":
            game_url = game_page_url(arguments.get("game", ""))
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.get(game_url)
                resp.raise_for_status()
                details = parse_game_details(resp.text, game_url)
            text = format_game_details(details)

        elif name == "get_game_devlogs":
            game_url = game_page_url(arguments.get("game", ""))
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.get(game_url.rstrip("/") + "/devlog")
                resp.raise_for_status()
                posts = parse_devlog_posts(resp.text)
            lines = [f"- {p['title']} — {p['url']}" for p in posts]
            body = "\n".join(lines) if posts else "No devlog posts found."
            text = f"## Devlog posts ({len(posts)})\n\n{body}"

        elif name == "get_creator_profile":
            user = arguments.get("user", "").strip().lstrip("/")
            profile_url = f"https://itch.io/profile/{user}"
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.get(profile_url)
                resp.raise_for_status()
                profile = parse_profile(resp.text, profile_url)
            lines = [f"# {profile['name'] or user}", f"URL: {profile['url']}"]
            if profile["joined"]:
                lines.append(f"Joined: {profile['joined']}")
            if profile["stats"]:
                lines.append("Stats: " + "  |  ".join(f"{k}: {v}" for k, v in profile["stats"].items()))
            if profile["external_links"]:
                lines.append("Links: " + ", ".join(profile["external_links"]))
            text = "\n".join(lines)

        elif name == "get_creator_games":
            user = arguments.get("user", "").strip().lstrip("/")
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.get(f"https://itch.io/profile/{user}")
                resp.raise_for_status()
                games, _ = parse_listing(resp.text)
            text = f"## Games by {user} ({len(games)} shown)\n\n{format_games(games)}"

        elif name == "get_jam_entries":
            jam_arg = arguments.get("jam", "").strip()
            if jam_arg.startswith("http"):
                entries_url = jam_arg.rstrip("/") + "/entries"
            else:
                entries_url = f"https://itch.io/jam/{jam_arg.lstrip('/')}/entries"
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
                resp = await client.get(entries_url, params={"page": page} if page > 1 else {})
                resp.raise_for_status()
                entries = parse_jam_entries(resp.text)
            lines = []
            for i, e in enumerate(entries, 1):
                lines.append(f"### {i}. {e['title']}")
                lines.append(f"URL: {e['url']}")
                if e["author"]:
                    lines.append(f"By: {e['author']}")
                lines.append("")
            body = "\n".join(lines) if entries else "No entries found."
            text = f"## Jam entries ({len(entries)} shown, page {page})\n\n{body}"

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
