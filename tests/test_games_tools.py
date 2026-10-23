"""Offline tests for the general itch.io tools (games, assets, profiles, sales)."""

from unittest.mock import AsyncMock, patch

import pytest

import server


LISTING_HTML = """
<html><body>
<div class="game_cell" data-game_id="123">
  <div class="game_thumb"><a href="https://a-dev.itch.io/cool-game">
    <img data-lazy_src="https://img.itch.zone/thumb.png"/></a></div>
  <div class="game_cell_data">
    <div class="game_title"><a href="https://a-dev.itch.io/cool-game" class="title game_link">Cool Game</a></div>
    <div class="game_text">A cool little game</div>
    <div class="game_author"><a href="https://a-dev.itch.io">A Dev</a></div>
    <div class="game_genre">Platformer</div>
    <div class="game_platform"><span class="web_flag">Play in browser</span>
      <span title="Download for Windows" class="icon icon-windows8"></span></div>
    <div class="price_value">$4.99</div>
  </div>
</div>
<div class="game_cell" data-game_id="124">
  <div class="game_cell_data">
    <div class="game_title"><a href="https://b-dev.itch.io/word-search" class="title game_link">Word Search</a></div>
    <div class="game_text">Find the words</div>
    <div class="game_author"><a href="https://b-dev.itch.io">B Dev</a></div>
  </div>
</div>
<a href="/games?page=2" class="next_page">Next</a>
</body></html>
"""

GAME_PAGE_HTML = """
<html><head>
<meta property="og:title" content="Cool Game"/>
<meta property="og:description" content="A cool little game"/>
<meta property="og:image" content="https://img.itch.zone/cover.png"/>
</head><body>
<div class="game_info_panel_widget"><table>
<tr><td>Published</td><td>2026-01-01</td></tr>
<tr><td>Status</td><td><a href="https://itch.io/games/status-released">Released</a></td></tr>
</table></div>
<div data-tooltip="4.5 average rating from 1,234 total ratings" class="aggregate_rating">
  <div content="4.5" itemprop="ratingValue" class="star_value"></div>
</div>
<span class="price_value">$4.99</span>
<a href="https://itch.io/games/tag-pixel-art">Pixel Art</a>
<a href="https://itch.io/games/tag-roguelike">Roguelike</a>
</body></html>
"""

DEVLOG_HTML = """
<html><body>
<a href="https://dev.itch.io/game/devlog">Back to game</a>
<a href="https://dev.itch.io/game/devlog/111/first-post">First post</a>
<a href="https://dev.itch.io/game/devlog/111/first-post">First post</a>
<a href="https://dev.itch.io/game/devlog/222/second-post">Second post</a>
</body></html>
"""

PROFILE_HTML = """
<html><body>
<div class="stat_header_widget">
  <h2>Cool Creator</h2>
  <div class="stats_container">
    <div class="stat_box"><div class="stat_value">5</div><div class="stat_label">Posts</div></div>
    <div class="stat_box"><div class="stat_value">999</div><div class="stat_label">Followers</div></div>
  </div>
</div>
<div class="user_data">A member registered <abbr title="x">Feb 11, 2024</abbr>
  <span class="divider"> · </span><a href="https://cool.itch.io">View creator page →</a></div>
<a href="https://twitter.com/cool">Twitter</a>
</body></html>
"""

SALES_HTML = """
<html><body>
<div class="sale_row" data-sale_id="1">
  <div class="sale_flag"><div class="rate">50</div><div class="mini_label">off</div></div>
  <div class="sale_data">
    <div class="sale_title"><a href="/s/1/autumn-sale">Autumn Sale</a></div>
    <div class="sale_author"><a href="https://dev.itch.io">Dev</a></div>
    <div class="game_count">Get Cool Game for <del>$10.00</del> <strong>$5.00</strong></div>
    <div class="sale_end data_row">Offer ends <span class="date_format">2026-10-31T00:00:00Z</span></div>
  </div>
</div>
</body></html>
"""

ENTRIES_HTML = """
<html><body>
<div class="game_cell index_game_cell_widget">
  <div class="bordered"><a href="https://x-dev.itch.io/entry-1" class="game_thumb"></a></div>
  <div class="label"><a href="https://x-dev.itch.io/entry-1" class="title">Entry One</a></div>
  <div class="user_row"><a href="https://x-dev.itch.io" class="user_link">X Dev</a></div>
</div>
</body></html>
"""


# ----- parse_game_cell / parse_listing -----------------------------------------


def test_parse_game_cell_extracts_all_fields():
    games, has_next = server.parse_listing(LISTING_HTML)
    assert has_next is True
    assert len(games) == 2
    g = games[0]
    assert g["id"] == "123"
    assert g["title"] == "Cool Game"
    assert g["url"] == "https://a-dev.itch.io/cool-game"
    assert g["author"] == "A Dev"
    assert g["genre"] == "Platformer"
    assert g["price"] == "$4.99"
    assert "Web" in g["platforms"] and "Windows" in g["platforms"]
    assert g["thumb"] == "https://img.itch.zone/thumb.png"


def test_parse_listing_missing_price_defaults_empty():
    games, _ = server.parse_listing(LISTING_HTML)
    assert games[1]["price"] == ""


# ----- build_browse_path --------------------------------------------------------


def test_build_browse_path_plain():
    assert server.build_browse_path() == "/games"


def test_build_browse_path_sort_is_not_counted_when_trending():
    assert server.build_browse_path(sort="newest", price="free") == "/games/newest/free"


def test_build_browse_path_two_modifiers():
    assert server.build_browse_path(price="free", platform="web") == \
        "/games/free/platform-web"
    assert server.build_browse_path(genre="platformer", tag="pixel-art") == \
        "/games/genre-platformer/tag-pixel-art"


def test_build_browse_path_rejects_three_modifiers():
    with pytest.raises(ValueError, match="at most 2"):
        server.build_browse_path(sort="newest", price="free", platform="web")


# ----- game_page_url ------------------------------------------------------------


def test_game_page_url_full_url_passthrough():
    assert server.game_page_url("https://a.itch.io/game/") == "https://a.itch.io/game"


def test_game_page_url_user_slug_pair():
    assert server.game_page_url("samandev/tamaweb") == "https://samandev.itch.io/tamaweb"


def test_game_page_url_rejects_bare_slug():
    with pytest.raises(ValueError, match="full game URL"):
        server.game_page_url("tamaweb")


# ----- parse_game_details -------------------------------------------------------


def test_parse_game_details_extracts_fields():
    d = server.parse_game_details(GAME_PAGE_HTML, "https://a-dev.itch.io/cool-game")
    assert d["title"] == "Cool Game"
    assert d["description"] == "A cool little game"
    assert d["image"] == "https://img.itch.zone/cover.png"
    assert d["author"] == "a-dev"
    assert d["price"] == "$4.99"
    assert d["rating"] == "4.5"
    assert d["rating_count"] == "1,234"
    assert d["tags"] == ["Pixel Art", "Roguelike"]
    assert d["info"]["Status"] == "Released"


def test_format_game_details_shows_rating_and_price():
    d = server.parse_game_details(GAME_PAGE_HTML, "https://a-dev.itch.io/cool-game")
    text = server.format_game_details(d)
    assert "Price: $4.99" in text
    assert "4.5 (1,234 ratings)" in text


# ----- parse_devlog_posts -------------------------------------------------------


def test_parse_devlog_posts_dedupes_and_skips_listing_link():
    posts = server.parse_devlog_posts(DEVLOG_HTML)
    assert [p["title"] for p in posts] == ["First post", "Second post"]
    assert posts[0]["url"] == "https://dev.itch.io/game/devlog/111/first-post"


# ----- parse_profile ------------------------------------------------------------


def test_parse_profile_extracts_stats_and_links():
    p = server.parse_profile(PROFILE_HTML, "https://itch.io/profile/cool")
    assert p["name"] == "Cool Creator"
    assert p["stats"] == {"Posts": "5", "Followers": "999"}
    assert p["joined"] == "Feb 11, 2024"
    assert "https://twitter.com/cool" in p["external_links"]


# ----- parse_sales --------------------------------------------------------------


def test_parse_sales_extracts_row():
    sales = server.parse_sales(SALES_HTML)
    assert len(sales) == 1
    s = sales[0]
    assert s["title"] == "Autumn Sale"
    assert s["url"] == "https://itch.io/s/1/autumn-sale"
    assert s["author"] == "Dev"
    assert s["discount"] == "50% off"
    assert s["ends"] == "2026-10-31T00:00:00Z"


# ----- parse_jam_entries --------------------------------------------------------


def test_parse_jam_entries_extracts_cells():
    entries = server.parse_jam_entries(ENTRIES_HTML)
    assert len(entries) == 1
    e = entries[0]
    assert e["title"] == "Entry One"
    assert e["url"] == "https://x-dev.itch.io/entry-1"
    assert e["author"] == "X Dev"


# ----- search_listing (monkeypatched fetch) --------------------------------------


async def test_search_listing_filters_locally():
    games, _ = server.parse_listing(LISTING_HTML)

    async def fake_fetch(path, params=None):
        return games, False

    with patch.object(server, "fetch_listing", AsyncMock(side_effect=fake_fetch)):
        matches = await server.search_listing("/games", "cool", max_pages=2)

    assert [m["title"] for m in matches] == ["Cool Game"]


async def test_search_listing_case_insensitive_description():
    games, _ = server.parse_listing(LISTING_HTML)

    async def fake_fetch(path, params=None):
        return games, False

    with patch.object(server, "fetch_listing", AsyncMock(side_effect=fake_fetch)):
        matches = await server.search_listing("/games", "FIND THE WORDS", max_pages=1)

    assert [m["title"] for m in matches] == ["Word Search"]


# ----- tool dispatch (offline, no network) ---------------------------------------


def test_list_tools_includes_all_new_tools():
    import asyncio

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    expected = {
        "get_trending_games", "get_new_games", "get_top_sellers", "get_free_games",
        "browse_games", "get_games_by_tag", "get_games_by_genre", "search_games",
        "get_games_on_sale", "search_assets", "get_game_details", "get_game_devlogs",
        "get_creator_profile", "get_creator_games", "get_jam_entries",
    }
    assert expected <= names
    # original jam tools still present
    assert {"get_active_jams", "search_jams", "get_jam_details"} <= names
