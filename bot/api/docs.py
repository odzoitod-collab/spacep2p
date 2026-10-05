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

STATIC = ROOT / "static"
FONTS = ("https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700&family=Manrope:wght@400;600;700"
         "&family=JetBrains+Mono:wght@400;600&display=swap")

# Strait Pay: sky blue on mist, white cards; the guides are a route — the sidebar is the strait you sail along
CSS = """
:root{--sky:#0a9bff;--deep:#0063e6;--mist:#eef7ff;--ice:#fff;--ink:#0b2540;--slate:#5a7390;--line:#cfe6fa;
--code:#f2f8ff;--glow:rgba(10,155,255,.16)}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--mist);color:var(--ink);font:16px/1.65 Manrope,-apple-system,"Segoe UI",Arial,sans-serif}
a{color:var(--deep)}a:focus-visible,summary:focus-visible{outline:3px solid var(--sky);outline-offset:2px;border-radius:6px}
.top{background:linear-gradient(115deg,#18b4ff 0%,#0a9bff 45%,#0063e6 100%);color:#fff}
.top .in{max-width:1180px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:12px}
.brand{display:flex;align-items:center;gap:10px;color:#fff;text-decoration:none;font:700 17px Unbounded,sans-serif;
letter-spacing:.01em}.brand img{width:34px;height:34px;border-radius:9px;box-shadow:0 4px 14px rgba(0,40,120,.25)}
.top .api{margin-left:auto;color:#fff;font-weight:600;font-size:14px;text-decoration:none;padding:7px 14px;
border:1px solid rgba(255,255,255,.55);border-radius:999px}.top .api:hover,.top .api.on{background:rgba(255,255,255,.18)}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px;display:grid;grid-template-columns:250px minmax(0,1fr);gap:32px}
.route{position:sticky;top:20px;align-self:start;min-width:0}
.route h4{margin:0 0 12px;font:700 12px Unbounded,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--slate)}
.route ol{list-style:none;margin:0;padding:0;position:relative}
.route ol:before{content:"";position:absolute;left:13px;top:14px;bottom:14px;width:2px;
background:linear-gradient(var(--sky),var(--line));border-radius:2px}
.route li a{position:relative;display:flex;gap:12px;align-items:center;padding:7px 8px 7px 0;color:var(--ink);
text-decoration:none;font-size:14.5px;line-height:1.3;border-radius:10px}
.route li a .n{flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;background:var(--ice);
border:2px solid var(--line);font:600 12px "JetBrains Mono",monospace;color:var(--slate);z-index:1;transition:.2s}
.route li a:hover .n{border-color:var(--sky);color:var(--deep)}
.route li a.on{font-weight:700}.route li a.on .n{background:var(--sky);border-color:var(--sky);color:#fff;
box-shadow:0 0 0 6px var(--glow)}
main{min-width:0}
article{background:var(--ice);border:1px solid var(--line);border-radius:20px;padding:10px 40px 32px;
box-shadow:0 18px 40px -28px rgba(0,80,180,.35)}
.eyebrow{display:inline-block;margin-top:26px;font:600 12px "JetBrains Mono",monospace;color:var(--deep);
background:var(--code);border:1px solid var(--line);padding:3px 10px;border-radius:999px}
h1{font:700 30px/1.2 Unbounded,sans-serif;letter-spacing:-.01em;margin:14px 0 18px}
h2{font:700 20px/1.3 Unbounded,sans-serif;margin:38px 0 10px;padding-top:22px;border-top:1px dashed var(--line)}
h3{font-size:17px;margin:24px 0 6px}h2 a.h,h3 a.h{color:inherit;text-decoration:none}
code{font:500 .9em "JetBrains Mono",monospace;background:var(--code);padding:1px 6px;border-radius:6px;color:#0a4fb3}
pre{background:#0b2540;color:#dceeff;padding:16px 18px;border-radius:14px;overflow-x:auto;font-size:13.5px;line-height:1.55}
pre code{background:none;color:inherit;padding:0}
blockquote{margin:16px 0;padding:12px 16px;border-left:4px solid var(--sky);background:var(--code);border-radius:0 12px 12px 0}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto;font-size:14.5px}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{font-weight:700;color:var(--slate);font-size:13px;text-transform:uppercase;letter-spacing:.04em}
hr{border:0;border-top:1px dashed var(--line);margin:24px 0}li{margin:4px 0}
.toc{margin:6px 0 4px;padding:12px 16px;background:var(--code);border-radius:14px;font-size:14px}
.toc summary{cursor:pointer;font-weight:700}.toc ol{margin:8px 0 0;padding-left:20px}
.next{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:22px}
.next a{display:block;padding:14px 18px;background:var(--ice);border:1px solid var(--line);border-radius:16px;
text-decoration:none;color:var(--ink);transition:.2s}.next a:hover{border-color:var(--sky);box-shadow:0 8px 22px -14px var(--sky)}
.next small{display:block;color:var(--slate);font-size:12.5px}.next b{font-size:15.5px}.next .fwd{text-align:right;grid-column:2}
.hero{display:grid;grid-template-columns:1.1fr 1fr;gap:18px;align-items:center;margin:20px 0 6px}
.hero img{width:100%;border-radius:18px;box-shadow:0 18px 40px -24px rgba(0,80,180,.6)}
.hero p{color:var(--slate);margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;margin:18px 0 6px}
.cards a{display:flex;gap:12px;padding:14px;border:1px solid var(--line);border-radius:16px;text-decoration:none;
color:var(--ink);background:var(--ice);transition:.2s}.cards a:hover{border-color:var(--sky);transform:translateY(-2px)}
.cards .n{flex:none;width:30px;height:30px;border-radius:50%;background:var(--mist);color:var(--deep);display:grid;
place-items:center;font:600 12px "JetBrains Mono",monospace}.cards span{display:block;color:var(--slate);font-size:13.5px}
footer{max-width:1180px;margin:0 auto 34px;padding:0 20px;color:var(--slate);font-size:13px}
@media(max-width:900px){.wrap{grid-template-columns:minmax(0,1fr);padding:16px 12px 40px;gap:14px}
.route{position:static}.route h4{display:none}.route ol{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px}
.route ol:before{display:none}.route li a{white-space:nowrap;padding:6px 12px 6px 6px;background:var(--ice);
border:1px solid var(--line);border-radius:999px}.route li a.on{border-color:var(--sky)}
article{padding:4px 18px 24px;border-radius:16px}h1{font-size:23px}h2{font-size:18px}.hero{grid-template-columns:1fr}
.next{grid-template-columns:1fr}.next .fwd{grid-column:1}}
@media(prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

# the route: every guide in reading order, then the API reference
ROUTE = [(slug, name, about) for slug, name, about in GUIDES if slug] + [("", "API для сервисов",
                                                                          "приём рублей с зачислением в USDT")]


def href(slug: str) -> str:
    return f"/docs/{slug}" if slug else "/docs"


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
            aid = html.escape(anchor(text), quote=True)
            out.append(f'<h{n} id="{aid}">' + (f'<a class=h href="#{aid}">{inline(text)}</a>' if n in (2, 3)
                                               else inline(text)) + f'</h{n}>')
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


def toc(md: str) -> str:
    """«На этой странице»: the guide's sections (## headings)."""
    heads = [m[1].strip() for m in re.finditer(r"(?m)^##\s+(.+)$", md) if m[1].strip() != "Содержание"]
    if len(heads) < 3:
        return ""
    return ("<details class=toc open><summary>На этой странице</summary><ol>"
            + "".join(f'<li><a href="#{html.escape(anchor(h), quote=True)}">{inline(h)}</a></li>' for h in heads)
            + "</ol></details>")


def page(title: str, body: str, active: str | None) -> str:
    route = "".join(f'<li><a href="{href(slug)}"{" class=on aria-current=page" if slug == active else ""}>'
                    f'<span class=n>{i}</span>{html.escape(name)}</a></li>' for i, (slug, name, _) in enumerate(ROUTE, 1))
    nav = ""
    slugs = [r[0] for r in ROUTE]
    if active is not None and active in slugs:
        i = slugs.index(active)
        prev = ROUTE[i - 1] if i > 0 else None
        nxt = ROUTE[i + 1] if i + 1 < len(ROUTE) else None
        nav = "<nav class=next aria-label='Дальше по инструкциям'>" + (
            f'<a href="{href(prev[0])}"><small>← Назад</small><b>{html.escape(prev[1])}</b></a>' if prev else "") + (
            f'<a class=fwd href="{href(nxt[0])}"><small>Дальше →</small><b>{html.escape(nxt[1])}</b></a>'
            if nxt else "") + "</nav>"
    step = (f"<span class=eyebrow>Шаг {slugs.index(active) + 1} из {len(ROUTE)}</span>"
            if active is not None and active in slugs else "")
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · Strait Pay'
            f'</title><link rel=icon href="/docs/static/logo.png"><link rel=preconnect href="https://fonts.gstatic.com" '
            f'crossorigin><link rel=stylesheet href="{FONTS}"><style>{CSS}</style></head><body>'
            f'<header class=top><div class=in><a class=brand href="/docs/help"><img src="/docs/static/logo.png" alt="">'
            f'Strait Pay</a><a class="api{" on" if active == "" else ""}" href="/docs">API</a></div></header>'
            f'<div class=wrap><aside class=route aria-label="Инструкции по шагам"><h4>Маршрут</h4><ol>{route}</ol></aside>'
            f'<main><article>{step}{body}</article>{nav}</main></div>'
            f'<footer>Strait Pay — P2P-обмен USDT ⇄ RUB с защитой сделки</footer>'
            "<script>var a=document.querySelector('.route a.on');if(a&&innerWidth<900)"
            "a.scrollIntoView({inline:'center',block:'nearest'})</script></body></html>")


def guide(slug: str) -> str | None:
    """HTML of a guide (None: no such page). "" — the API reference, "help" — the list of guides."""
    if slug == "":
        md = API.read_text(encoding="utf-8")
        return page("API", inject_toc(render(md), toc(md)), "")
    if slug == "help":
        cards = "".join(f'<a href="{href(s)}"><span class=n>{i}</span><div><b>{html.escape(n)}</b>'
                        f'<span>{html.escape(a)}</span></div></a>' for i, (s, n, a) in enumerate(ROUTE, 1))
        return page("Инструкции", "<h1>Инструкции Strait Pay</h1><div class=hero><div><p>Путь от заявки на вход до "
                                  "первой сделки, продажи на своей карте, ордерных заявок и вывода USDT. Идите по "
                                  "шагам сверху вниз или откройте нужный раздел.</p></div><img src=\"/docs/static/"
                                  "flow.jpg\" alt=\"Рубли с карты → Strait Pay → USDT в кошельке\"></div>"
                                  f"<div class=cards>{cards}</div>", "help")
    known = {s for s, _, _ in GUIDES if s}
    path = GUIDES_DIR / f"{slug}.md"
    if slug not in known or not path.is_file():
        return None
    name = next(n for s, n, _ in GUIDES if s == slug)
    md = path.read_text(encoding="utf-8")
    return page(name, inject_toc(render(md), toc(md)), slug)


def inject_toc(body: str, contents: str) -> str:
    """The contents right under the page's title."""
    if not contents or "</h1>" not in body:
        return body
    head, _, rest = body.partition("</h1>")
    return head + "</h1>" + contents + rest


def static(name: str) -> tuple[bytes, str] | None:
    """A file of docs/static (the logo, illustrations): (content, type) or None."""
    path = STATIC / name
    if not re.fullmatch(r"[a-z0-9_-]+\.(png|jpg)", name) or not path.is_file():
        return None
    return path.read_bytes(), "image/png" if name.endswith(".png") else "image/jpeg"
