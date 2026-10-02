"""The docs site: docs/API.md at /docs, docs/guides/<slug>.md at /docs/<slug>, the list of guides at /docs/help.
Markdown is turned into a clean HTML page here (no extra dependencies): headings with anchors, paragraphs, lists,
quotes, tables, code. /docs.md keeps the raw API reference for tools."""
import html
import re
from pathlib import Path

from bot.guides import GUIDES

ROOT = Path(__file__).resolve().parent.parent.parent / "docs"
API = ROOT / "API.md"
GUIDES_DIR = ROOT / "guides"

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--text:#16181d;--muted:#5b6170;--line:#e4e7ec;--accent:#2f6fec;--code:#f1f3f7}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e8eaef;--muted:#9aa1b1;--line:#262b35;
--accent:#6b9bff;--code:#1f232c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
header{border-bottom:1px solid var(--line);background:var(--card)}
.bar{max-width:860px;margin:0 auto;padding:12px 16px;display:flex;gap:6px 14px;flex-wrap:wrap;align-items:center}
.bar b{margin-right:8px}.bar a{color:var(--muted);text-decoration:none;font-size:14px}.bar a.on,.bar a:hover{color:var(--accent)}
main{max-width:860px;margin:24px auto;padding:0 16px}
article{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:8px 28px 24px}
h1{font-size:28px;line-height:1.25}h2{font-size:21px;margin-top:32px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:17px;margin-top:22px}a{color:var(--accent)}
code{background:var(--code);padding:1px 5px;border-radius:5px;font-size:.92em}
pre{background:var(--code);padding:12px 14px;border-radius:10px;overflow-x:auto}pre code{background:none;padding:0}
blockquote{margin:12px 0;padding:8px 14px;border-left:3px solid var(--accent);background:var(--code);border-radius:6px}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}th,td{border:1px solid var(--line);
padding:6px 9px;text-align:left;vertical-align:top}th{background:var(--code)}hr{border:0;border-top:1px solid var(--line)}
.guides a{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:10px;margin:8px 0;
text-decoration:none;color:var(--text)}.guides a span{display:block;color:var(--muted);font-size:14px}
footer{max-width:860px;margin:0 auto 32px;padding:0 16px;color:var(--muted);font-size:13px}
"""


def anchor(text: str) -> str:
    """GitHub-style heading id: lowercase, punctuation dropped, spaces -> hyphens (Cyrillic kept)."""
    return re.sub(r"\s", "-", re.sub(r"[^\w\- ]", "", text.strip().lower()))


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    parts = re.split(r"(`[^`]+`)", text)  # no markup inside code
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append(f"<code>{p[1:-1]}</code>")
            continue
        p = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{html.escape(m[2], quote=True)}">{m[1]}</a>', p)
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
        p = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", p)
        out.append(p)
    return "".join(out)


def render(md: str) -> str:
    lines, out, i = md.splitlines(), [], 0
    para: list[str] = []

    def flush():
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        st = line.strip()
        if st.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
        elif not st:
            flush()
        elif m := re.match(r"^(#{1,4})\s+(.*)$", st):
            flush()
            n, text = len(m[1]), m[2]
            out.append(f'<h{n} id="{html.escape(anchor(text), quote=True)}">{inline(text)}</h{n}>')
        elif st in ("---", "***"):
            flush()
            out.append("<hr>")
        elif st.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            cells = lambda row: [c.strip() for c in row.strip().strip("|").split("|")]  # noqa: E731
            head = cells(st)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(cells(lines[i]))
                i += 1
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</table>")
            continue
        elif st.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{inline(' '.join(quote))}</blockquote>")
            continue
        elif re.match(r"^([-*]|\d+\.)\s+", st):
            flush()
            ordered = bool(re.match(r"^\d+\.", st))
            items = []
            while i < len(lines) and (re.match(r"^\s*([-*]|\d+\.)\s+", lines[i])
                                      or (lines[i].startswith("  ") and lines[i].strip() and items)):
                cur = lines[i]
                if re.match(r"^\s*([-*]|\d+\.)\s+", cur):
                    items.append(re.sub(r"^\s*([-*]|\d+\.)\s+", "", cur))
                else:
                    items[-1] += " " + cur.strip()
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        else:
            para.append(st)
        i += 1
    flush()
    return "\n".join(out)


def page(title: str, body: str, active: str) -> str:
    nav = "".join(f'<a href="/docs/{slug}"{" class=on" if slug == active else ""}>{html.escape(name)}</a>'
                  for slug, name, _ in [("help", "Все инструкции", "")] + GUIDES if slug) \
        + f'<a href="/docs"{" class=on" if active == "" else ""}>API</a>'
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · Strait Pay'
            f'</title><style>{CSS}</style></head><body><header><nav class="bar"><b>Strait Pay</b>{nav}</nav></header>'
            f'<main><article>{body}</article></main><footer>Strait Pay — P2P-обмен USDT ⇄ RUB с защитой сделки'
            f'</footer></body></html>')


def guide(slug: str) -> str | None:
    """HTML of a guide (None: no such page). "" — the API reference, "help" — the list of guides."""
    if slug == "":
        return page("API", render(API.read_text(encoding="utf-8")), "")
    if slug == "help":
        items = "".join(f'<a href="/docs/{s or ""}"><b>{html.escape(n)}</b><span>{html.escape(a)}</span></a>'
                        for s, n, a in GUIDES).replace('href="/docs/"', 'href="/docs"')
        return page("Инструкции", "<h1>Инструкции Strait Pay</h1><p>Как покупать и продавать USDT, работать с "
                                  f"заявками, командами и кошельком.</p><div class=guides>{items}</div>", "help")
    known = {s for s, _, _ in GUIDES if s}
    path = GUIDES_DIR / f"{slug}.md"
    if slug not in known or not path.is_file():
        return None
    name = next(n for s, n, _ in GUIDES if s == slug)
    return page(name, render(path.read_text(encoding="utf-8")), slug)
