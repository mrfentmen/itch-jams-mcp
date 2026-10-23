"""Tests for itch-jams-mcp pure helpers and tool dispatch."""

from unittest.mock import AsyncMock, patch

import pytest

import server


CALENDAR_HTML = """
<html><body>
<div class="calendar_wrapper">
<script>
R$$.app.add_jam_calendar(new R$$.FilteredJamCalendar({
  "jams": [
    {"id": 1, "title": "Test Jam", "url": "/jam/test-jam",
     "start_date": "2026-01-01 00:00:00", "end_date": "2026-01-03 00:00:00",
     "joined": 5, "highlight": true},
    {"id": 2, "title": "Another { Jam }", "url": "https://itch.io/jam/another",
     "start_date": "2026-02-01 00:00:00", "end_date": "2026-02-02 00:00:00",
     "joined": 0, "highlight": false}
  ]
}, {"some": "options"}));
</script>
</div>
</body></html>
"""

JAM_PAGE_HTML = """
<html><head>
<meta property="og:title" content="Fallback Title"/>
</head><body>
<h1 class="jam_title_header">Cozy Demo Jam</h1>
<div class="jam_host_header">Hosted by Someone</div>
<div class="jam_header_widget">
  Runs 2026-03-01 12:00:00 to 2026-03-04 12:00:00
</div>
<div class="stat_box">
  <div class="stat_label">Joined</div><div class="stat_value">42</div>
</div>
<div class="stat_box">
  <div class="stat_label">Entries</div><div class="stat_value">7</div>
</div>
<div class="jam_content">
  <h2>Theme</h2>
  <p>Make a cozy little game.</p>
  <h3>Rules</h3>
  <ul>
    <li>No AI generated content allowed</li>
    <li>Have fun</li>
  </ul>
  <hr/>
  <p>Good luck!</p>
</div>
</body></html>
"""


# ----- extract_jams_json -------------------------------------------------------


def test_extract_jams_json_parses_calendar():
    jams = server.extract_jams_json(CALENDAR_HTML)
    assert len(jams) == 2
    assert jams[0]["title"] == "Test Jam"
    assert jams[1]["title"] == "Another { Jam }"


def test_extract_jams_json_handles_missing_wrapper():
    assert server.extract_jams_json("<html><body>nothing here</body></html>") == []


def test_extract_jams_json_handles_missing_marker():
    html = '<div class="calendar_wrapper"><script>var x = 1;</script></div>'
    assert server.extract_jams_json(html) == []


# ----- jam_to_dict -------------------------------------------------------------


def test_jam_to_dict_prepends_host_to_relative_url():
    j = {"id": 1, "title": "T", "url": "/jam/t", "start_date": "", "end_date": "",
         "joined": 0, "highlight": False}
    out = server.jam_to_dict(j)
    assert out["url"] == "https://itch.io/jam/t"


def test_jam_to_dict_keeps_absolute_url():
    j = {"id": 1, "title": "T", "url": "https://itch.io/jam/t", "joined": 3, "highlight": True}
    out = server.jam_to_dict(j)
    assert out["url"] == "https://itch.io/jam/t"
    assert out["joined"] == 3
    assert out["highlight"] is True


def test_jam_to_dict_uses_defaults_for_missing_fields():
    out = server.jam_to_dict({})
    assert out["title"] == ""
    assert out["url"] == ""
    assert out["joined"] == 0
    assert out["highlight"] is False


# ----- format_jams -------------------------------------------------------------


def test_format_jams_empty():
    assert server.format_jams([]) == "No jams found."


def test_format_jams_marks_featured():
    jams = [server.jam_to_dict(j) for j in server.extract_jams_json(CALENDAR_HTML)]
    out = server.format_jams(jams)
    assert "### 1. Test Jam" in out
    assert "(Featured)" in out
    # Second jam is not featured -> the "(Featured)" line should appear exactly once.
    assert out.count("(Featured)") == 1
    assert "Participants: 5" in out


# ----- parse_jam_page ----------------------------------------------------------


def test_parse_jam_page_extracts_fields():
    d = server.parse_jam_page(JAM_PAGE_HTML, "https://itch.io/jam/cozy-demo")
    assert d["title"] == "Cozy Demo Jam"
    assert d["host"] == "Hosted by Someone"
    assert d["start_date"] == "2026-03-01 12:00:00"
    assert d["end_date"] == "2026-03-04 12:00:00"
    assert d["stats"] == {"Joined": "42", "Entries": "7"}
    assert "## Theme" in d["description"]
    assert "### Rules" in d["description"]
    assert "- No AI generated content allowed" in d["description"]
    assert "---" in d["description"]


def test_parse_jam_page_falls_back_to_og_title():
    html = '<html><head><meta property="og:title" content="OG Only"/></head><body></body></html>'
    d = server.parse_jam_page(html, "https://itch.io/jam/og-only")
    assert d["title"] == "OG Only"
    assert d["host"] == ""
    assert d["stats"] == {}


# ----- format_jam_details ------------------------------------------------------


def test_format_jam_details_renders_sections():
    d = server.parse_jam_page(JAM_PAGE_HTML, "https://itch.io/jam/cozy-demo")
    out = server.format_jam_details(d)
    assert out.startswith("# Cozy Demo Jam")
    assert "URL: https://itch.io/jam/cozy-demo" in out
    assert "Host: Hosted by Someone" in out
    assert "Dates: 2026-03-01 12:00:00 → 2026-03-04 12:00:00" in out
    assert "Joined: 42" in out and "Entries: 7" in out


def test_format_jam_details_skips_missing_pieces():
    out = server.format_jam_details({
        "title": "Bare", "url": "u", "host": "",
        "start_date": "", "end_date": "", "stats": {}, "description": "",
    })
    assert "Host:" not in out
    assert "Dates:" not in out
    assert "Stats:" not in out


# ----- call_tool dispatch (mocking the network) -------------------------------


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.mark.asyncio
async def test_call_tool_get_active_jams_uses_active_filter():
    captured = {}

    async def fake_fetch(filter_type="", page=1):
        captured["filter_type"] = filter_type
        captured["page"] = page
        return [server.jam_to_dict({"id": 1, "title": "Hi", "url": "/jam/hi"})]

    with patch.object(server, "fetch_jams", side_effect=fake_fetch):
        result = await server.call_tool("get_active_jams", {"page": 2})

    assert captured == {"filter_type": "active", "page": 2}
    assert result[0].type == "text"
    assert "Active Game Jams" in result[0].text
    assert "page 2" in result[0].text


@pytest.mark.asyncio
async def test_call_tool_search_jams_filters_by_query():
    fake_jams = [
        server.jam_to_dict({"id": 1, "title": "Cozy Spring", "url": "/jam/cs"}),
        server.jam_to_dict({"id": 2, "title": "Spooky Horror", "url": "/jam/sh"}),
        server.jam_to_dict({"id": 3, "title": "Another Cozy One", "url": "/jam/ac"}),
    ]

    async def fake_fetch(filter_type="", page=1):
        return fake_jams

    with patch.object(server, "fetch_jams", side_effect=fake_fetch):
        result = await server.call_tool("search_jams", {"query": "cozy"})

    text = result[0].text
    assert "2 of 3 jams matched" in text
    assert "Cozy Spring" in text
    assert "Another Cozy One" in text
    assert "Spooky Horror" not in text


@pytest.mark.asyncio
async def test_call_tool_get_jam_details_builds_url_from_slug():
    captured = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            captured["url"] = url
            return _FakeResp(JAM_PAGE_HTML)

    with patch.object(server.httpx, "AsyncClient", _Client):
        result = await server.call_tool("get_jam_details", {"jam": "cozy-demo"})

    assert captured["url"] == "https://itch.io/jam/cozy-demo"
    assert "# Cozy Demo Jam" in result[0].text


@pytest.mark.asyncio
async def test_call_tool_get_jam_details_accepts_full_url():
    captured = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            captured["url"] = url
            return _FakeResp(JAM_PAGE_HTML)

    with patch.object(server.httpx, "AsyncClient", _Client):
        await server.call_tool(
            "get_jam_details", {"jam": "https://itch.io/jam/cozy-demo"}
        )

    assert captured["url"] == "https://itch.io/jam/cozy-demo"


@pytest.mark.asyncio
async def test_call_tool_unknown_returns_friendly_message():
    result = await server.call_tool("nope", {})
    assert "Unknown tool" in result[0].text


@pytest.mark.asyncio
async def test_call_tool_swallows_http_error():
    import httpx

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            req = httpx.Request("GET", url)
            resp = httpx.Response(429, request=req, text="rate limited")
            raise httpx.HTTPStatusError("429", request=req, response=resp)

    with patch.object(server.httpx, "AsyncClient", _Client):
        result = await server.call_tool("get_active_jams", {})

    assert "HTTP error: 429" in result[0].text


# ----- list_tools sanity check -------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_exposes_all_tools():
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "get_active_jams",
        "get_upcoming_jams",
        "get_recent_jams",
        "get_all_jams",
        "get_featured_jams",
        "search_jams",
        "get_jam_details",
        "get_trending_games",
        "get_new_games",
        "get_top_sellers",
        "get_free_games",
        "browse_games",
        "get_games_by_tag",
        "get_games_by_genre",
        "search_games",
        "get_games_on_sale",
        "search_assets",
        "get_game_details",
        "get_game_devlogs",
        "get_creator_profile",
        "get_creator_games",
        "get_jam_entries",
    }


# ----- HTTP transports (smoke tests) ------------------------------------------


def test_build_sse_app_routes():
    app_ = server.build_sse_app(sse_path="/sse", message_path="/messages/")
    paths = {getattr(r, "path", None) for r in app_.routes}
    assert "/sse" in paths
    # Mount routes expose `path` too.
    assert any(getattr(r, "path", "").startswith("/messages") for r in app_.routes)


def test_build_streamable_http_app_routes():
    app_ = server.build_streamable_http_app(mount_path="/mcp")
    paths = [getattr(r, "path", None) for r in app_.routes]
    assert any(p == "/mcp" for p in paths)


def test_main_dispatches_stdio_by_default(monkeypatch):
    called = {}

    def fake_run(coro):
        called["ran"] = True
        coro.close()

    monkeypatch.setattr(server.asyncio, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["itch-jams-mcp"])
    server.main()
    assert called == {"ran": True}


def test_main_dispatches_sse(monkeypatch):
    captured = {}

    class _FakeUvicorn:
        @staticmethod
        def run(app, host, port):
            captured["host"] = host
            captured["port"] = port
            captured["app"] = app

    import sys
    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setattr("sys.argv", [
        "itch-jams-mcp", "--transport", "sse", "--host", "0.0.0.0", "--port", "9001"
    ])
    server.main()
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9001
    paths = {getattr(r, "path", None) for r in captured["app"].routes}
    assert "/sse" in paths


def test_main_dispatches_streamable_http(monkeypatch):
    captured = {}

    class _FakeUvicorn:
        @staticmethod
        def run(app, host, port):
            captured["app"] = app
            captured["port"] = port

    import sys
    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setattr("sys.argv", [
        "itch-jams-mcp", "--transport", "streamable-http",
        "--mount-path", "/rpc", "--port", "9002", "--stateless",
    ])
    server.main()
    assert captured["port"] == 9002
    paths = [getattr(r, "path", None) for r in captured["app"].routes]
    assert "/rpc" in paths
