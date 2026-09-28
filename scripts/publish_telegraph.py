"""Publish docs/seller_manual.html to telegra.ph (or update the same page).

    .venv/bin/python scripts/publish_telegraph.py             # first run: creates the page, prints its link
    .venv/bin/python scripts/publish_telegraph.py --update    # later runs: edits the same page, the link stays

The Telegraph account token and page path are kept in .telegraph (git-ignored) — whoever has it can edit the page.
"""
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "seller_manual.html"
STATE = ROOT / ".telegraph"
TITLE = "Strait Pay — инструкция для продавцов"
AUTHOR = "Strait Pay"
TAGS = {"a", "aside", "b", "blockquote", "br", "code", "em", "h3", "h4", "hr", "i", "li", "ol", "p", "pre", "s",
        "strong", "u", "ul"}  # what Telegraph accepts
VOID = {"br", "hr"}


class Nodes(HTMLParser):
    """HTML -> Telegraph Node list ({"tag", "attrs", "children"} or strings)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS:
            raise ValueError(f"<{tag}> is not supported by Telegraph")
        node = {"tag": tag}
        href = dict(attrs).get("href")
        if href:
            node["attrs"] = {"href": href}
        self.stack[-1]["children"].append(node)
        if tag not in VOID:
            node["children"] = []
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag not in VOID:
            self.stack.pop()

    def handle_data(self, data):
        if data.strip() or (data and self.stack[-1] is not self.root):
            self.stack[-1]["children"].append(data)


def api(method: str, **params) -> dict:
    r = httpx.post(f"https://api.telegra.ph/{method}", data=params, timeout=30).json()
    if not r.get("ok"):
        raise SystemExit(f"Telegraph {method}: {r.get('error')}")
    return r["result"]


def main() -> None:
    parser = Nodes()
    parser.feed(SOURCE.read_text(encoding="utf-8"))
    content = json.dumps(parser.root["children"], ensure_ascii=False)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "token" not in state:
        state["token"] = api("createAccount", short_name="StraitPay", author_name=AUTHOR)["access_token"]
    if "--update" in sys.argv and "path" in state:
        page = api("editPage", access_token=state["token"], path=state["path"], title=TITLE, author_name=AUTHOR,
                   content=content)
    else:
        page = api("createPage", access_token=state["token"], title=TITLE, author_name=AUTHOR, content=content)
        state["path"] = page["path"]
    STATE.write_text(json.dumps(state))
    print(page["url"])


if __name__ == "__main__":
    main()
