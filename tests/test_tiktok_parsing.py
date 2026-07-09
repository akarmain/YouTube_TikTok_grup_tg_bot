"""TikTok photo-post HTML parsing tests, no network.

Run: python tests/test_tiktok_parsing.py  (or pytest tests/)
"""

import json
import os
import sys

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.tiktok.sourse import _parse_photo_post_html  # noqa: E402


def _page(script: str) -> str:
    return f"<html><head></head><body>{script}</body></html>"


def _universal(item: dict) -> str:
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    return _page(
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(data)
        + "</script>"
    )


def _img(url: str, key: str = "imageURL", list_key: str = "urlList") -> dict:
    return {key: {list_key: [url]}}


def test_universal_data_item_struct():
    item = {
        "id": "42",
        "desc": "my caption",
        "imagePost": {"images": [_img("https://cdn.example/1.jpeg"), _img("https://cdn.example/2.jpeg")]},
    }
    urls, title, description = _parse_photo_post_html(_universal(item), post_id="42")
    assert urls == ["https://cdn.example/1.jpeg", "https://cdn.example/2.jpeg"]
    assert title is None  # no title field -> None, not the description
    assert description == "my caption"


def test_deeper_nesting_sigi_state():
    # imagePost buried deeper (SIGI_STATE ItemModule style) with snake_case url lists
    data = {
        "ItemModule": {
            "777": {
                "id": "777",
                "title": "post title",
                "desc": "post desc",
                "imagePost": {
                    "images": [
                        _img("https://cdn.example/a.webp", key="display_image", list_key="url_list"),
                        _img("https://cdn.example/b.webp", key="thumbnail", list_key="urlList"),
                    ]
                },
            }
        }
    }
    html = _page('<script id="SIGI_STATE" type="application/json">' + json.dumps(data) + "</script>")
    urls, title, description = _parse_photo_post_html(html, post_id="777")
    assert urls == ["https://cdn.example/a.webp", "https://cdn.example/b.webp"]
    assert title == "post title"
    assert description == "post desc"


def test_escaped_json_string():
    inner = json.dumps({"itemStruct": {"id": "9", "desc": "d", "imagePost": {"images": [_img("https://cdn.example/x.jpeg")]}}})
    data = {"props": {"pageProps": {"raw": inner}}}
    html = _page('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>")
    urls, _, description = _parse_photo_post_html(html, post_id="9")
    assert urls == ["https://cdn.example/x.jpeg"]
    assert description == "d"


def test_app_api_naming():
    item = {"aweme_id": "5", "description": "app desc",
            "image_post_info": {"images": [_img("https://cdn.example/app.jpeg", key="display_image", list_key="url_list")]}}
    urls, title, description = _parse_photo_post_html(_universal(item), post_id="5")
    assert urls == ["https://cdn.example/app.jpeg"]
    assert title is None and description == "app desc"


def test_missing_image_post():
    item = {"id": "42", "desc": "just a video", "video": {"duration": 10}}
    urls, title, description = _parse_photo_post_html(_universal(item), post_id="42")
    assert urls == [] and title is None and description is None


def test_no_json_blobs():
    assert _parse_photo_post_html("<html><body>captcha</body></html>") == ([], None, None)


def test_broken_json_is_ignored():
    html = _page('<script id="SIGI_STATE">{not json}</script>')
    assert _parse_photo_post_html(html) == ([], None, None)


def test_duplicates_deduplicated_in_order():
    item = {
        "id": "42",
        "imagePost": {
            "images": [
                _img("https://cdn.example/1.jpeg"),
                _img("https://cdn.example/2.jpeg"),
                _img("https://cdn.example/1.jpeg"),
            ]
        },
    }
    urls, _, _ = _parse_photo_post_html(_universal(item), post_id="42")
    assert urls == ["https://cdn.example/1.jpeg", "https://cdn.example/2.jpeg"]


def test_prefers_item_matching_post_id():
    related = {"id": "1", "desc": "related", "imagePost": {"images": [_img("https://cdn.example/rel.jpeg")]}}
    wanted = {"id": "2", "desc": "wanted", "imagePost": {"images": [_img("https://cdn.example/want.jpeg")]}}
    data = {"feed": [related, wanted]}
    html = _page('<script id="SIGI_STATE" type="application/json">' + json.dumps(data) + "</script>")
    urls, _, description = _parse_photo_post_html(html, post_id="2")
    assert urls == ["https://cdn.example/want.jpeg"]
    assert description == "wanted"


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            func()
            print(f"{name} OK")
    print("ALL OK")
