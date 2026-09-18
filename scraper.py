#!/usr/bin/env python3
"""Scrape ALL English PIB press releases from the previous calendar day (IST)
and generate:

  docs/feed.xml          flat RSS 2.0 feed, one item per article
  docs/pib-news.epub     a single text-only EPUB of all articles

Source page: https://www.pib.gov.in/allreleasem.aspx?lang=1&reg=3
The page is ASP.NET and selects the date through a postback on dropdowns,
so we load the page, copy its hidden fields and post the target date back.

The full article text is embedded in each item's <description>, so an RSS
reader can build a single EPUB from the feed without downloading anything
else. Images are removed so the feed stays text-only.
"""

import calendar
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

BASE = "https://www.pib.gov.in"
LIST_URL = "https://www.pib.gov.in/allreleasem.aspx?lang=1&reg=3"
SITE_BASE = os.environ.get(
    "SITE_BASE_URL", "https://gkgangavarapu.github.io/pibindia-rss"
).rstrip("/")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(ROOT_DIR, "docs")
OUTPUT_FILE = os.path.join(DOCS_DIR, "feed.xml")
EPUB_FILE = os.path.join(DOCS_DIR, "pib-news.epub")

CHANNEL_TITLE = "PIB English Press Releases (Previous Day)"
CHANNEL_DESC = (
    "Every English press release published by the Press Information Bureau "
    "(PIB Delhi) on the previous calendar day."
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - not all streams support reconfigure
    pass

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback when tzdata is unavailable
    IST = timezone(timedelta(hours=5, minutes=30))


def log(message):
    print(message, flush=True)


def yesterday_ist():
    now = datetime.now(IST)
    return (now - timedelta(days=1)).date()


def escape_xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fetch_releases_page(session, day, month, year):
    """Return the parsed All Releases page for the given date."""
    log(f"Fetching All Releases page for {day:02d}-{month:02d}-{year} ...")
    response = session.get(LIST_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError("Could not find the ASP.NET form on the PIB page")

    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type") != "image":
            data[name] = inp.get("value", "")
    for select in form.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        option = select.find("option", selected=True) or select.find("option")
        data[name] = option.get("value")

    data["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$ddlday"
    data["__EVENTARGUMENT"] = ""
    data["ctl00$Bar1$ddlregion"] = "3"
    data["ctl00$Bar1$ddlLang"] = "1"
    data["ctl00$ContentPlaceHolder1$ddlMinistry"] = "0"
    data["ctl00$ContentPlaceHolder1$ddlday"] = str(day)
    data["ctl00$ContentPlaceHolder1$ddlMonth"] = str(month)
    data["ctl00$ContentPlaceHolder1$ddlYear"] = str(year)

    posted = session.post(
        LIST_URL, data=data, headers={**HEADERS, "Referer": LIST_URL}, timeout=60
    )
    posted.raise_for_status()

    result = BeautifulSoup(posted.text, "html.parser")
    label = result.find(id="ContentPlaceHolder1_lblDate")
    if label is None:
        raise RuntimeError("Could not parse the release-date label from PIB page")

    label_text = label.get_text(" ", strip=True)
    expected = f"{day:02d}-{calendar.month_name[month]}-{year}"
    log(f"PIB page reports: {label_text}")
    if expected not in label_text:
        raise RuntimeError(
            f"Expected releases for {expected} but PIB returned: {label_text}"
        )
    return result


def parse_articles(soup):
    """Return a flat, de-duplicated list of {prid, title, url}."""
    articles = []
    seen = set()
    for link in soup.select("ul.num a[href*=PressRel]"):
        href = link.get("href", "")
        match = re.search(r"PRID=(\d+)", href)
        if not match:
            continue
        prid = match.group(1)
        if prid in seen:
            continue
        seen.add(prid)
        title = (link.get("title") or link.get_text(" ", strip=True)).strip()
        articles.append(
            {"prid": prid, "title": title, "url": urljoin(BASE + "/", href)}
        )
    return articles


def clean_content(element):
    """Drop scripts, styles and images so the feed is text-only."""
    for tag in element.find_all(["script", "style", "img"]):
        tag.decompose()
    return element


def parse_posted_date(text):
    match = re.search(r"(\d{1,2}\s+\w{3}\s+\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)", text)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%d %b %Y %I:%M%p")
        return parsed.replace(tzinfo=IST)
    except ValueError:
        return None


def fetch_article(session, article, fallback_date):
    """Return (content_html, pub_datetime) for one article."""
    response = session.get(article["url"], headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    content = soup.find(id="ContentPlaceHolder1_PdfDiv")
    if content is None:
        content = soup.find(class_="innner-page-main-about-us-content-right-part")
    if content is None:
        raise RuntimeError("main content not found")

    content = clean_content(content)
    body = content.decode_contents().strip()

    pub_date = None
    date_node = soup.find(id="ContentPlaceHolder1_PrDateTime")
    if date_node is not None:
        pub_date = parse_posted_date(date_node.get_text(" ", strip=True))
    if pub_date is None:
        pub_date = datetime(
            fallback_date.year,
            fallback_date.month,
            fallback_date.day,
            0,
            0,
            tzinfo=IST,
        )
    return body, pub_date


def format_rfc822(value):
    return value.strftime("%a, %d %b %Y %H:%M:%S %z")


def build_feed(articles):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        f"    <title>{escape_xml(CHANNEL_TITLE)}</title>",
        f"    <link>{escape_xml(SITE_BASE + '/')}</link>",
        f"    <description>{escape_xml(CHANNEL_DESC)}</description>",
        "    <language>en-in</language>",
        f"    <lastBuildDate>{format_rfc822(datetime.now(IST))}</lastBuildDate>",
    ]
    for item in articles:
        parts.append("    <item>")
        parts.append(f"      <title>{escape_xml(item['title'])}</title>")
        parts.append(f"      <link>{escape_xml(item['url'])}</link>")
        parts.append(
            f'      <guid isPermaLink="false">pib-{item["prid"]}</guid>'
        )
        parts.append(f"      <pubDate>{format_rfc822(item['pubDate'])}</pubDate>")
        parts.append(
            "      <description><![CDATA["
            + item["description"].replace("]]>", "]]]]><![CDATA[>")
            + "]]></description>"
        )
        parts.append("    </item>")
    parts.append("  </channel>")
    parts.append("</rss>")
    return "\n".join(parts) + "\n"


XHTML_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
    "<head>\n<meta charset=\"utf-8\"/>\n"
    "<title>{title}</title>\n"
    '<link rel="stylesheet" type="text/css" href="style.css"/>\n'
    "</head>\n<body>\n{body}\n</body>\n</html>\n"
)

STYLE_CSS = (
    "body { font-family: serif; line-height: 1.5; margin: 5%; }\n"
    "h1 { font-size: 1.4em; }\n"
    ".date { color: #666; font-size: 0.9em; }\n"
    "img { max-width: 100%; height: auto; }\n"
)


def xhtml_body(title, body):
    heading = f"<h1>{escape_xml(title)}</h1>"
    fragment = heading + body
    try:
        ElementTree.fromstring(
            f'<div xmlns="http://www.w3.org/1999/xhtml">{fragment}</div>'
        )
        return fragment
    except ElementTree.ParseError:
        text = BeautifulSoup(fragment, "html.parser").get_text(" ", strip=True)
        return f"<h1>{escape_xml(title)}</h1><p>{escape_xml(text)}</p>"


def build_epub(articles, target, epub_path):
    book_id = f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = f"PIB Press Releases - {target.isoformat()}"

    chapters = []
    for index, item in enumerate(articles, start=1):
        content = item["description"]
        chapter_id = f"chap{index:04d}"
        filename = f"{chapter_id}.xhtml"
        chapters.append({"id": chapter_id, "file": filename, "title": item["title"]})
        body = (
            f'<p class="date">{format_rfc822(item["pubDate"])}</p>'
            f"{xhtml_body(item['title'], content)}"
        )
        item["_xhtml"] = XHTML_TEMPLATE.format(
            title=escape_xml(item["title"]), body=body
        )

    manifest = [
        '<item id="nav" href="nav.xhtml" '
        'media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []
    for item, chapter in zip(articles, chapters):
        manifest.append(
            f'<item id="{chapter["id"]}" href="{chapter["file"]}" '
            'media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{chapter["id"]}"/>')

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{book_id}</dc:identifier>\n'
        f"    <dc:title>{escape_xml(title)}</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        "    <dc:creator>Press Information Bureau</dc:creator>\n"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n    " + "\n    ".join(manifest) + "\n  </manifest>\n"
        '  <spine toc="ncx">\n    ' + "\n    ".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )

    nav_items = "\n      ".join(
        f'<li><a href="{chapter["file"]}">'
        f'{escape_xml(chapter["title"])}</a></li>'
        for chapter in chapters
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head><meta charset="utf-8"/><title>Contents</title></head>\n'
        '<body><nav epub:type="toc" id="toc"><h1>Contents</h1>\n'
        f"    <ol>\n      {nav_items}\n    </ol>\n"
        "</nav></body></html>\n"
    )

    nav_points = "\n".join(
        f'    <navPoint id="np{i}" playOrder="{i}">'
        f'<navLabel><text>{escape_xml(c["title"])}</text></navLabel>'
        f'<content src="{c["file"]}"/></navPoint>'
        for i, c in enumerate(chapters, start=1)
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{book_id}"/></head>\n'
        f"<docTitle><text>{escape_xml(title)}</text></docTitle>\n"
        f"<navMap>\n{nav_points}\n</navMap>\n</ncx>\n"
    )

    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n</container>\n"
    )

    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/nav.xhtml", nav)
        archive.writestr("OEBPS/toc.ncx", ncx)
        archive.writestr("OEBPS/style.css", STYLE_CSS)
        for item, chapter in zip(articles, chapters):
            archive.writestr(f'OEBPS/{chapter["file"]}', item["_xhtml"])
    log(f"Wrote {epub_path} with {len(articles)} chapter(s)")


def main():
    target = yesterday_ist()
    log(f"Target date (yesterday, Asia/Kolkata): {target.isoformat()}")

    session = requests.Session()
    soup = fetch_releases_page(session, target.day, target.month, target.year)

    articles = parse_articles(soup)
    log(f"Found {len(articles)} release(s) for {target.isoformat()}")
    if not articles:
        log("WARNING: PIB reported no releases for the target date.")

    results = []
    for index, article in enumerate(articles, start=1):
        log(f"[{index}/{len(articles)}] {article['title'][:80]}")
        try:
            description, pub_date = fetch_article(session, article, target)
        except Exception as error:  # keep processing the remaining articles
            log(f"  ERROR downloading {article['url']}: {error}")
            continue
        article["description"] = description
        article["pubDate"] = pub_date
        results.append(article)

    if articles and not results:
        log("ERROR: every article failed to download; refusing to write a feed.")
        sys.exit(1)

    os.makedirs(DOCS_DIR, exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
        handle.write(build_feed(results))
    log(f"Wrote {OUTPUT_FILE} with {len(results)} item(s)")

    if results:
        build_epub(results, target, EPUB_FILE)


if __name__ == "__main__":
    main()
