"""Watch public web pages for meaningful text changes using a local SQLite history."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_BYTES = 2_000_000
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "main", "li", "tr", "h1", "h2", "h3", "h4", "br", "hr", "header", "footer"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class VisibleText(HTMLParser):
    def __init__(self, element_id: str = ""):
        super().__init__(convert_charrefs=True)
        self.element_id = element_id
        self.stack = []
        self.chunks = []
        self.found = not element_id

    def handle_starttag(self, tag, attrs):
        parent_skip = self.stack[-1][1] if self.stack else False
        parent_selected = self.stack[-1][2] if self.stack else not self.element_id
        selected = parent_selected or (self.element_id and dict(attrs).get("id") == self.element_id)
        skip = parent_skip or tag in {"script", "style", "noscript", "template"}
        if selected:
            self.found = True
        if selected and not skip and tag in BLOCK:
            self.chunks.append("\n")
        if tag not in VOID:
            self.stack.append((tag, skip, selected))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if self.stack[index][2] and not self.stack[index][1] and tag in BLOCK:
                    self.chunks.append("\n")
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack:
            _, skip, selected = self.stack[-1]
            if selected and not skip:
                self.chunks.append(data)
        elif not self.element_id:
            self.chunks.append(data)


def normalize(content: str, content_type: str, element_id: str = "", ignore: list[str] | None = None) -> str:
    if "html" in content_type:
        parser = VisibleText(element_id)
        parser.feed(content)
        parser.close()
        if not parser.found:
            raise ValueError("selected element ID was not found; baseline was preserved")
        content = "".join(parser.chunks)
    elif element_id:
        raise ValueError("element ID selection requires an HTML page")
    elif "json" in content_type:
        content = json.dumps(json.loads(content), ensure_ascii=False, indent=2, sort_keys=True)
    for expression in ignore or []:
        content = re.sub(expression, "", content, flags=re.MULTILINE)
    lines = [re.sub(r"\s+", " ", line).strip() for line in content.splitlines()]
    result = "\n".join(line for line in lines if line)
    if not result:
        raise ValueError("page has no monitored text; baseline was preserved")
    return result + "\n"


def fetch(url: str, etag: str = "", modified: str = "", timeout: float = 15) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("only HTTP(S) URLs without embedded credentials are supported")
    headers = {"User-Agent": "PageWatch/1.0 (personal text change monitor)", "Accept-Encoding": "identity"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    try:
        response = urlopen(Request(url, headers=headers), timeout=timeout)
    except HTTPError as exc:
        if exc.code == 304:
            exc.close()
            return {"not_modified": True}
        raise
    with response:
        content_type = response.headers.get_content_type()
        if not (content_type.startswith("text/") or content_type in {"application/json", "application/xhtml+xml"}):
            raise ValueError("only text, HTML and JSON responses are supported")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("unexpected compressed response")
        payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("response exceeds 2 MB")
        content = payload.decode(response.headers.get_content_charset() or "utf-8")
        return {"not_modified": False, "content": content, "content_type": content_type,
                "etag": response.headers.get("ETag", ""), "modified": response.headers.get("Last-Modified", "")}


class WatchStore:
    def __init__(self, path: str | Path = "data/watch.db"):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
                element_id TEXT NOT NULL, ignore_patterns TEXT NOT NULL,
                baseline TEXT, etag TEXT NOT NULL DEFAULT '', modified TEXT NOT NULL DEFAULT '',
                checked_at TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL, kind TEXT NOT NULL, diff TEXT NOT NULL
            );
        """)

    def close(self):
        self.connection.close()

    def add(self, name: str, url: str, element_id: str = "", ignore: list[str] | None = None) -> int:
        parsed = urlsplit(url)
        if not name.strip() or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("name and HTTP(S) URL without embedded credentials are required")
        for expression in ignore or []:
            re.compile(expression)
        with self.connection:
            cursor = self.connection.execute("INSERT INTO watches(name,url,element_id,ignore_patterns) VALUES(?,?,?,?)", (name.strip(), url, element_id, json.dumps(ignore or [])))
        return cursor.lastrowid

    def list(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute("SELECT id,name,url,element_id,checked_at,last_error FROM watches ORDER BY id")]

    def check(self, watch_id: int, fetcher=fetch) -> dict:
        row = self.connection.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        if not row:
            raise ValueError("watch not found")
        try:
            result = fetcher(row["url"], row["etag"], row["modified"])
            if result["not_modified"]:
                if row["baseline"] is None:
                    raise ValueError("server returned 304 before the first baseline")
                with self.connection:
                    self.connection.execute("UPDATE watches SET checked_at=?,last_error=NULL WHERE id=?", (now(), watch_id))
                return {"id": watch_id, "name": row["name"], "status": "unchanged"}
            text = normalize(result["content"], result["content_type"], row["element_id"], json.loads(row["ignore_patterns"]))
            status = "initial" if row["baseline"] is None else "changed" if text != row["baseline"] else "unchanged"
            difference = "" if status != "changed" else "".join(difflib.unified_diff(row["baseline"].splitlines(True), text.splitlines(True), fromfile="previous", tofile="current"))
            with self.connection:
                self.connection.execute("UPDATE watches SET baseline=?,etag=?,modified=?,checked_at=?,last_error=NULL WHERE id=?", (text, result["etag"], result["modified"], now(), watch_id))
                if status != "unchanged":
                    self.connection.execute("INSERT INTO events(watch_id,created_at,kind,diff) VALUES(?,?,?,?)", (watch_id, now(), status, difference))
            return {"id": watch_id, "name": row["name"], "status": status}
        except (OSError, ValueError, LookupError) as exc:
            # A failed request never replaces the last successful baseline or validators.
            error = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
            with self.connection:
                self.connection.execute("UPDATE watches SET checked_at=?,last_error=? WHERE id=?", (now(), error, watch_id))
            return {"id": watch_id, "name": row["name"], "status": "error", "error": error}

    def history(self, watch_id: int) -> list[dict]:
        return [dict(row) for row in self.connection.execute("SELECT id,created_at,kind,diff FROM events WHERE watch_id=? ORDER BY id DESC LIMIT 100", (watch_id,))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/watch.db")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("name")
    add.add_argument("url")
    add.add_argument("--element-id", default="")
    add.add_argument("--ignore", action="append", default=[], help="regex to remove before comparing; repeatable")
    commands.add_parser("list")
    check = commands.add_parser("check")
    check.add_argument("--id", type=int)
    check.add_argument("--interval", type=int, help="repeat checks every N seconds; minimum 30")
    history = commands.add_parser("history")
    history.add_argument("id", type=int)
    remove = commands.add_parser("remove")
    remove.add_argument("id", type=int)
    args = parser.parse_args()
    store = None
    try:
        if args.command == "check" and args.interval is not None and args.interval < 30:
            raise ValueError("interval must be at least 30 seconds")
        store = WatchStore(args.db)
        if args.command == "add":
            print(json.dumps({"id": store.add(args.name, args.url, args.element_id, args.ignore)}))
        elif args.command == "list":
            print(json.dumps(store.list(), ensure_ascii=False, indent=2))
        elif args.command == "history":
            print(json.dumps(store.history(args.id), ensure_ascii=False, indent=2))
        elif args.command == "remove":
            with store.connection:
                cursor = store.connection.execute("DELETE FROM watches WHERE id=?", (args.id,))
            print(json.dumps({"removed": cursor.rowcount}))
        else:
            while True:
                ids = [args.id] if args.id is not None else [item["id"] for item in store.list()]
                results = [store.check(watch_id) for watch_id in ids]
                print(json.dumps(results, ensure_ascii=False), flush=True)
                code = 2 if any(item["status"] == "error" for item in results) else 1 if any(item["status"] == "changed" for item in results) else 0
                if args.interval is None:
                    return code
                time.sleep(args.interval)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, re.error, sqlite3.Error) as exc:
        print(f"Unable to complete command ({type(exc).__name__}); check the URL, name and database path.")
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
