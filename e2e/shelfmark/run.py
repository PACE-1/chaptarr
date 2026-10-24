"""End-to-end check of Chaptarr's Shelfmark indexer and download client.

Drives a running Chaptarr through its API: adds Shelfmark as indexer and download client,
adds a book, searches, and waits until Shelfmark's download has been imported into the
library. Shelfmark itself runs for real, with fake_usenet.py standing in for its sources.

Usage: python run.py  (reads CHAPTARR_URL, CHAPTARR_API_KEY, SHELFMARK_HOST, SHELFMARK_API_KEY)
       ci.sh starts the whole rig in Docker and runs this; see the e2e-shelfmark workflow.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

CHAPTARR = os.environ.get("CHAPTARR_URL", "http://localhost:8789").rstrip("/") + "/api/v1"
CHAPTARR_KEY = os.environ["CHAPTARR_API_KEY"]
SHELFMARK_HOST = os.environ.get("SHELFMARK_HOST", "shelfmark")
SHELFMARK_KEY = os.environ.get("SHELFMARK_API_KEY", "e2e-admin-key")
LIBRARY = os.environ.get("LIBRARY_PATH", "/library")
# Default to a book with a small, clean author catalog: classics like Frankenstein have hundreds
# of edition rows, and the title matcher rightly refuses releases that fit several of them.
BOOK_TERM = os.environ.get("BOOK_TERM", "Project Hail Mary")
BOOK_AUTHOR = os.environ.get("BOOK_AUTHOR", "Weir")
FAKE_USENET = os.environ.get("FAKE_USENET_URL", "http://localhost:9000")
TIMEOUT = int(os.environ.get("E2E_TIMEOUT", "300"))


def api(method, path, body=None, ok=(200, 201, 202)):
    request = urllib.request.Request(
        CHAPTARR + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": CHAPTARR_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode()
        if error.code in ok:
            return json.loads(detail) if detail else None
        raise SystemExit(f"{method} {path} -> {error.code}: {detail[:2000]}") from None


def step(message):
    print(f"[e2e] {message}", flush=True)


def wait_for(description, check, timeout=TIMEOUT, interval=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(interval)
    raise SystemExit(f"Timed out waiting for {description}")


def provider(kind, implementation, fields):
    schema = next(s for s in api("GET", f"/{kind}/schema") if s["implementation"] == implementation)
    assert schema["protocol"] == "directDownload", f"{implementation} {kind} protocol is {schema['protocol']}"
    for field in schema["fields"]:
        if field["name"] in fields:
            field["value"] = fields[field["name"]]
    schema["name"] = "Shelfmark"
    schema["enable" if kind == "downloadclient" else "enableAutomaticSearch"] = True
    if kind == "indexer":
        schema["enableInteractiveSearch"] = True
        schema["enableRss"] = False
    api("POST", f"/{kind}/test", schema)
    return api("POST", f"/{kind}", schema)


def main():
    wait_for("Chaptarr", lambda: api("GET", "/system/status"), interval=3)

    step("checking delay profile defaults after migration 1001")
    delay = api("GET", "/delayprofile")[0]
    assert delay["enableDirectDownload"] is True and delay["directDownloadDelay"] == 0, delay

    step("adding root folder")
    folders = api("GET", "/rootfolder")
    folder = next((f for f in folders if f["path"].rstrip("/") == LIBRARY), None) or api("POST", "/rootfolder", {
        "name": "Library", "path": LIBRARY, "folderType": 0,
        "defaultQualityProfileId": 1, "defaultMetadataProfileId": 2,
        "ebook": {"qualityProfileId": 1, "metadataProfileId": 2, "monitored": True},
        "audiobook": {"qualityProfileId": 2, "metadataProfileId": 1, "monitored": True},
    })

    step("adding Shelfmark indexer (connection test included)")
    provider("indexer", "Shelfmark", {"baseUrl": f"http://{SHELFMARK_HOST}:8084", "apiKey": SHELFMARK_KEY})

    step("adding Shelfmark download client (connection test included)")
    provider("downloadclient", "Shelfmark", {"host": SHELFMARK_HOST, "port": 8084, "apiKey": SHELFMARK_KEY})

    step(f"looking up '{BOOK_TERM}'")
    results = api("GET", "/book/lookup?term=" + urllib.request.quote(BOOK_TERM))
    candidates = [b for b in results
                  if b["title"].lower().startswith(BOOK_TERM.lower()) and BOOK_AUTHOR.lower() in b["author"]["authorName"].lower()]
    assert candidates, f"no lookup result for {BOOK_TERM} by {BOOK_AUTHOR}"
    book = min(candidates, key=lambda b: len(b["title"]))
    author_name = book["author"]["authorName"]

    step(f"adding '{book['title']}' as a monitored ebook and searching")
    prof = folder.get("ebook") or {}
    added = api("POST", "/book", {
        **book,
        "mediaType": "ebook",
        "monitored": True,
        "ebookMonitored": True,
        "audiobookMonitored": False,
        "addOptions": {"addType": "manual", "searchForNewBook": False},
        "author": {
            **book["author"],
            "monitored": True,
            "lastSelectedMediaType": "ebook",
            "ebookRootFolderPath": folder["path"],
            "ebookQualityProfileId": prof.get("qualityProfileId") or 1,
            "ebookMetadataProfileId": prof.get("metadataProfileId") or 2,
            "ebookMonitored": True,
        },
    })
    book_id = added["id"]

    # Offer the release under the title Chaptarr actually stored: metadata can hydrate an edition
    # title (e.g. "Frankenstein (Pulp! The Classics)") that differs from the lookup result.
    title = api("GET", f"/book/{book_id}")["title"]
    step(f"offering '{author_name} - {title}' from the fake indexer")
    catalog = [
        {"id": "fake-ebook-1", "title": f"{author_name} - {title} [epub]", "book": title, "author": author_name, "category": 7020, "size": 524288},
        {"id": "fake-audio-1", "title": f"{author_name} - {title} [Audiobook] m4b", "book": title, "author": author_name, "category": 3030, "size": 104857600},
    ]
    urllib.request.urlopen(urllib.request.Request(FAKE_USENET + "/catalog", data=json.dumps(catalog).encode(), method="POST"), timeout=30)

    step("interactive search should list the Shelfmark release")
    response = api("GET", f"/release?bookId={book_id}")
    releases = response if isinstance(response, list) else (response.get("releases") or []) + (response.get("hiddenReleases") or [])
    shelfmark = [r for r in releases if r.get("protocol") == "directDownload"]
    for r in releases:
        print(f"    {r.get('protocol')} {r.get('title')} rejected={r.get('rejected')} {r.get('rejections')}")
    assert shelfmark, "Shelfmark returned no releases to Chaptarr"
    approved = [r for r in shelfmark if not r.get("rejected")]
    assert approved, "every Shelfmark release was rejected"

    step("automatic search should grab it")
    api("POST", "/command", {"name": "BookSearch", "bookIds": [book_id]})

    def grabbed():
        history = api("GET", f"/history?page=1&pageSize=20&bookId={book_id}") or {}
        records = history.get("records", history if isinstance(history, list) else [])
        return [h for h in records if h.get("eventType") == "grabbed"]

    grab = wait_for("the release to be grabbed", grabbed)[0]
    step(f"grabbed '{grab['sourceTitle']}' (download id {grab.get('downloadId')})")

    def imported():
        files = api("GET", f"/bookfile?bookId={book_id}") or []
        if files:
            return files
        queue = api("GET", "/queue?page=1&pageSize=50&includeUnknownAuthorItems=true") or {}
        for item in queue.get("records", []):
            print(f"    queue: {item.get('title')} status={item.get('status')} tracked={item.get('trackedDownloadState')} {item.get('statusMessages')}", flush=True)
        return None

    files = wait_for("the download to be imported", imported, interval=10)
    for f in files:
        step(f"imported {f['path']}")
    assert all(f["path"].startswith(LIBRARY) for f in files), files
    step("PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        sys.exit(f"[e2e] FAIL: {error}")
