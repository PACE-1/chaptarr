"""A stand-in Newznab indexer and SABnzbd for end-to-end tests of Chaptarr's Shelfmark support.

Shelfmark searches it as a Newznab source and hands grabs to it as SABnzbd. A grab
"completes" at once by writing a small, valid EPUB into the shared downloads folder,
so the whole Chaptarr -> Shelfmark -> download -> import path runs without the internet.
"""

import json
import os
import re
import sys
import threading
import uuid
import zipfile
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

API_KEY = os.environ.get("FAKE_API_KEY", "fake-key")
DOWNLOADS = Path(os.environ.get("FAKE_DOWNLOADS", "/downloads"))
PUBLIC_URL = os.environ.get("FAKE_PUBLIC_URL", "http://fake-usenet:9000").rstrip("/")
_catalog_file = Path(os.environ.get("FAKE_CATALOG", "/app/catalog.json"))
CATALOG = json.loads(_catalog_file.read_text()) if _catalog_file.exists() else []

_lock = threading.Lock()
_history = []
_grabs = []
_webhooks = []


def _words(text):
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) > 2}


def _search(query, categories):
    wanted = _words(query)
    results = []
    for item in CATALOG:
        if categories and not set(categories) & {item["category"], item["category"] // 1000 * 1000}:
            continue
        if wanted and not wanted <= _words(item["title"] + " " + item.get("author", "")):
            continue
        results.append(item)
    return results


def _rss(items):
    entries = []
    for item in items:
        entries.append(f"""<item>
<title>{escape(item["title"])}</title>
<guid isPermaLink="false">{escape(item["id"])}</guid>
<link>{PUBLIC_URL}/nzb/{item["id"]}.nzb</link>
<pubDate>{formatdate(usegmt=True)}</pubDate>
<enclosure url="{PUBLIC_URL}/nzb/{item["id"]}.nzb" length="{item["size"]}" type="application/x-nzb"/>
<newznab:attr name="category" value="{item["category"]}"/>
<newznab:attr name="size" value="{item["size"]}"/>
<newznab:attr name="author" value="{escape(item.get("author", ""))}"/>
</item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel><title>Fake Usenet</title>
<newznab:response offset="0" total="{len(items)}"/>
{''.join(entries)}
</channel></rss>"""


CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps><server title="Fake Usenet"/><limits max="100" default="100"/>
<searching><search available="yes" supportedParams="q"/><book-search available="yes" supportedParams="q,author,title"/><audio-search available="yes" supportedParams="q"/></searching>
<categories><category id="3000" name="Audio"><subcat id="3030" name="Audiobook"/></category>
<category id="7000" name="Books"><subcat id="7020" name="EBook"/></category></categories></caps>"""


def write_epub(path, title, author):
    """A minimal EPUB 3 that ebook readers and tag readers accept."""
    book_id = f"urn:uuid:{uuid.uuid4()}"
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        epub.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
        epub.writestr("OEBPS/content.opf", f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="id">{book_id}</dc:identifier>
<dc:title>{escape(title)}</dc:title>
<dc:creator>{escape(author)}</dc:creator>
<dc:language>en</dc:language>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="c1"/></spine>
</package>""")
        epub.writestr("OEBPS/nav.xhtml", f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>{escape(title)}</title></head>
<body><nav epub:type="toc"><ol><li><a href="c1.xhtml">Chapter 1</a></li></ol></nav></body></html>""")
        epub.writestr("OEBPS/c1.xhtml", f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 1</title></head>
<body><h1>{escape(title)}</h1><p>{escape(author)}</p></body></html>""")


def _complete(name, item):
    job = DOWNLOADS / "complete" / name
    job.mkdir(parents=True, exist_ok=True)
    write_epub(job / f"{name}.epub", item["book"], item["author"])
    os.chmod(job, 0o777)
    for child in job.iterdir():
        os.chmod(child, 0o666)
    return str(job)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("fake-usenet: " + fmt % args + "\n")

    def _send(self, code, body, content_type="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, value, code=200):
        self._send(code, json.dumps(value))

    def do_GET(self):
        url = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}

        if url.path == "/health":
            return self._json({"status": "ok"})
        if url.path == "/grabs":
            with _lock:
                return self._json(list(_grabs))
        if url.path == "/webhooks":
            with _lock:
                return self._json(list(_webhooks))
        if url.path.startswith("/nzb/"):
            return self._send(200, '<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file subject="fake"><segments><segment bytes="1" number="1">fake@fake</segment></segments></file></nzb>', "application/x-nzb")
        if url.path in ("/newznab/api", "/newznab"):
            if params.get("apikey") != API_KEY:
                return self._send(200, '<error code="100" description="Incorrect user credentials"/>', "application/xml")
            if params.get("t") == "caps":
                return self._send(200, CAPS, "application/xml")
            categories = [int(c) for c in params.get("cat", "").split(",") if c.strip().isdigit()]
            query = " ".join(filter(None, [params.get("q"), params.get("author"), params.get("title")]))
            return self._send(200, _rss(_search(query, categories)), "application/rss+xml")
        if url.path in ("/sab/api", "/sab"):
            return self._sab(params)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if url.path == "/catalog":
            # The test names the releases to offer once it knows which book it added.
            with _lock:
                CATALOG[:] = json.loads(body)
            return self._json({"items": len(CATALOG)})
        if url.path == "/webhook":
            # Records Chaptarr's webhook notifications so the test can check they fire.
            with _lock:
                _webhooks.append(json.loads(body or b"{}"))
            return self._json({"ok": True})
        if url.path in ("/sab/api", "/sab"):
            if b"Content-Disposition" in body:
                for field in ("nzbname", "cat", "mode", "apikey"):
                    match = re.search(rb'name="' + field.encode() + rb'"\r\n\r\n([^\r]*)', body)
                    if match:
                        params.setdefault(field, match.group(1).decode())
                match = re.search(rb'name="name"; filename="([^"]*)"', body)
                if match:
                    params.setdefault("filename", match.group(1).decode())
            return self._sab(params)
        return self._json({"error": "not found"}, 404)

    def _sab(self, params):
        if params.get("apikey") != API_KEY:
            return self._json({"status": False, "error": "API Key Incorrect"})
        mode = params.get("mode")
        if mode == "version":
            return self._json({"version": "4.4.0"})
        if mode in ("addurl", "addfile"):
            source = params.get("name") or params.get("filename") or ""
            item_id = re.sub(r"\.nzb$", "", source.rsplit("/", 1)[-1])
            item = next((i for i in CATALOG if i["id"] == item_id), None)
            name = params.get("nzbname") or (item["title"] if item else item_id)
            if item is None:
                item = next((i for i in CATALOG if i["title"] == name), None)
            if item is None:
                return self._json({"status": False, "error": f"unknown nzb {source}"})
            nzo_id = f"SABnzbd_nzo_{uuid.uuid4().hex[:8]}"
            # A catalog item with "fail": true finishes as a failed download, like a broken NZB.
            failed = bool(item.get("fail"))
            storage = "" if failed else _complete(name, item)
            with _lock:
                _grabs.append({"nzo_id": nzo_id, "id": item["id"], "name": name, "category": params.get("cat")})
                _history.insert(0, {"nzo_id": nzo_id, "name": name, "nzb_name": name + ".nzb",
                                    "status": "Failed" if failed else "Completed",
                                    "fail_message": "Download failed: missing articles" if failed else "",
                                    "storage": storage, "category": params.get("cat") or "books", "bytes": item["size"]})
            return self._json({"status": True, "nzo_ids": [nzo_id]})
        if mode == "queue":
            if params.get("name") == "delete":
                return self._json({"status": True})
            return self._json({"queue": {"slots": [], "paused": False}})
        if mode == "history":
            if params.get("name") == "delete":
                with _lock:
                    ids = set((params.get("value") or "").split(","))
                    _history[:] = [h for h in _history if h["nzo_id"] not in ids]
                return self._json({"status": True})
            with _lock:
                return self._json({"history": {"slots": list(_history)}})
        if mode == "get_config":
            return self._json({"config": {"misc": {"complete_dir": str(DOWNLOADS / "complete")}, "categories": [{"name": "books", "dir": ""}]}})
        if mode == "fullstatus":
            return self._json({"status": {"completedir": str(DOWNLOADS / "complete")}})
        return self._json({"status": False, "error": f"unsupported mode {mode}"})


if __name__ == "__main__":
    (DOWNLOADS / "complete").mkdir(parents=True, exist_ok=True)
    os.chmod(DOWNLOADS / "complete", 0o777)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
