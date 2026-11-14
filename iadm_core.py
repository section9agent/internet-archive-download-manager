"""
Internet Archive Download Manager - shared engine
-------------------------------------------------
Everything here is plain Python with NO window code, so the same logic runs in the
desktop app (archive_downloader.py) and the Android app (android/main.py).

  * reading file lists (Internet Archive items and open-directory websites)
  * the downloader (resume, retries, skip-if-already-complete)
  * small helpers (sizes, extensions, safe file paths, version numbers)
"""

import html.parser
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

APP_TITLE = "Internet Archive Download Manager"
APP_FILE = "InternetArchiveDownloadManager"      # name of the built program
USER_AGENT = "InternetArchiveDownloadManager/1.0 (personal tool)"

# The release workflow stamps these two lines when it builds a tagged release.
# Building by hand? Set GITHUB_REPO to "your-name/your-repo" so the update check knows
# where to look (leave it empty to switch update checks off).
VERSION = "1.0.0"
GITHUB_REPO = ""
GITHUB_API = "https://api.github.com"

DONATE_URL = (
    "https://www.paypal.com/donate/?business=section9agent%40hotmail.com"
    "&no_recurring=0&item_name=Internet+Archive+Download+Manager&currency_code=USD"
)

# Open source software this program is built with: (name, licence, website)
CREDITS = [
    ("Python and its standard library", "Python Software Foundation License", "https://www.python.org/"),
    ("Tcl/Tk (the tkinter windows)", "Tcl/Tk License (BSD-style)", "https://www.tcl-lang.org/"),
    ("PyInstaller (packaging)", "GPL-2.0 with bootloader exception", "https://pyinstaller.org/"),
    ("Pillow (icon conversion at build time)", "HPND License", "https://python-pillow.org/"),
]
# Extra credits for the Android app's About window.
ANDROID_CREDITS = [
    ("Kivy (the Android windows)", "MIT License", "https://kivy.org/"),
    ("python-for-android and Buildozer (packaging)", "MIT License", "https://github.com/kivy/python-for-android"),
    ("Python and its standard library", "Python Software Foundation License", "https://www.python.org/"),
    ("Pyjnius (Android access)", "MIT License", "https://github.com/kivy/pyjnius"),
]
SERVICES = [
    ("Internet Archive", "file lists and downloads", "https://archive.org/"),
    ("GitHub", "release hosting for updates", "https://github.com/"),
]

CHUNK = 256 * 1024
RETRIES = 3

# Extensions offered in the "File types" dropdown.
FILTER_EXTENSIONS = ["mp3", "wav", "flac", "mp4", "mpg", "avi", "png", "jpg", "zip", "rar", "7z"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def human_size(n):
    if n is None or n < 0:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(seconds):
    if seconds is None or seconds < 0 or seconds > 99 * 3600:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_identifier(text):
    """Extract the archive.org item identifier from a URL or bare name."""
    text = text.strip()
    if not text:
        return None
    if "://" not in text and "/" not in text and "." not in text:
        return text  # bare identifier such as "my-cool-item"
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    if "archive.org" not in parsed.netloc.lower():
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in (
        "details", "download", "metadata", "embed", "stream", "compress"
    ):
        return urllib.parse.unquote(parts[1])
    return None


def parse_extensions(text):
    """'mp3, .FLAC; *.pdf' -> {'mp3', 'flac', 'pdf'}"""
    out = set()
    for piece in re.split(r"[,;\s]+", text or ""):
        piece = piece.strip().lstrip("*").lstrip(".").lower()
        if piece:
            out.add(piece)
    return out


def safe_join(base, relative):
    """Join relative archive path onto base, refusing to escape base."""
    rel = relative.replace("\\", "/").lstrip("/")
    segments = [s for s in rel.split("/") if s]
    if os.name == "nt":   # characters Windows doesn't allow in file names
        segments = [re.sub(r'[<>:"|?*]', "_", s).rstrip(" .") or "_" for s in segments]
    target = os.path.normpath(os.path.join(base, *segments))
    base_abs = os.path.abspath(base)
    if os.path.commonpath([base_abs, os.path.abspath(target)]) != base_abs:
        raise ValueError(f"Unsafe path in archive listing: {relative}")
    return target


def safe_folder_name(text):
    """A string that is safe to use as a single folder name."""
    return re.sub(r'[<>:"/\\|?*]', "_", text or "").strip() or "download"


def open_url(url, headers=None, timeout=30, method=None):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method=method
    )
    return urllib.request.urlopen(req, timeout=timeout)


def version_tuple(text):
    """'v1.2.3' -> (1, 2, 3, 0). Anything after a '-' or '+' is ignored."""
    nums = re.findall(r"\d+", (text or "").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) + (0,) * (4 - len(nums[:4]))


def fetch_latest_release(repo, asset_name):
    """Newest published (non-draft, non-prerelease) GitHub release, or None if there is none.

    asset_name is the file to look for among the release's downloads."""
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    try:
        with open_url(url, headers={"Accept": "application/vnd.github+json"}, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    tag = data.get("tag_name") or ""
    asset = next((a for a in data.get("assets", []) if a.get("name") == asset_name), None)
    return {
        "tag": tag,
        "version": version_tuple(tag),
        "notes": (data.get("body") or "").strip(),
        "page": data.get("html_url") or f"https://github.com/{repo}/releases",
        "asset_url": asset.get("browser_download_url") if asset else None,
        "asset_size": int(asset.get("size") or 0) if asset else 0,
    }


# --------------------------------------------------------------------------
# Getting file lists
# --------------------------------------------------------------------------
def fetch_archive_files(ident):
    """File list of an archive.org item, from its metadata."""
    url = f"https://archive.org/metadata/{urllib.parse.quote(ident)}"
    with open_url(url) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data or "files" not in data:
        raise ValueError("Item not found, or it has no files.")
    files = []
    for f in data["files"]:
        if str(f.get("private", "")).lower() == "true":
            continue
        name = f.get("name")
        if not name:
            continue
        try:
            size = int(f.get("size", -1))
        except (TypeError, ValueError):
            size = -1
        files.append({
            "name": name,
            "size": size,
            "source": f.get("source", ""),
            "url": f"https://archive.org/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(name)}",
        })
    return files


class _LinkParser(html.parser.HTMLParser):
    """Collects every link on a directory-listing page, plus the text after it
    (which is where servers put the date and size)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries = []      # (href, text after the link)
        self._cur = None
        self._in_a = False

    def _close(self):
        if self._cur:
            self.entries.append((self._cur[0], " ".join(self._cur[1])))
        self._cur = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._close()
            href = dict(attrs).get("href")
            self._in_a = True
            if href:
                self._cur = (href, [])
        elif tag in ("tr", "li", "br", "hr"):
            self._close()

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_a = False
        elif tag in ("tr", "li", "pre", "table"):
            self._close()

    def handle_data(self, data):
        if self._cur and not self._in_a:
            self._cur[1].append(data)

    def finish(self):
        self.close()
        self._close()
        return self.entries


_UNIT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}


def parse_listing_size(text):
    """Pull a file size out of the text next to a link ('2024-01-02 10:00  4.5M').

    Returns (size, exact). Sizes shown as 4.5M are rounded by the server, so they are
    only estimates (exact=False); plain byte counts are exact. (-1, False) if none."""
    text = re.sub(r"(\d)\s+([KMGTP]i?B|[KMGTP]|bytes?)\b", r"\1\2", text or "", flags=re.I)
    for tok in text.split():
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTP]?)(i?B|bytes?)?", tok, re.I)
        if not m:
            continue
        num, unit, suffix = float(m.group(1)), m.group(2).upper(), m.group(3)
        if unit:
            return int(num * _UNIT[unit]), False
        if suffix:
            return int(num), True
        if tok.isdigit():
            last = tok == text.split()[-1] and re.search(r"\d:\d\d", text)
            if (len(tok) >= 4 and not 1970 <= int(tok) <= 2100) or last:
                return int(tok), True   # plain byte count (nginx style)
    return -1, False


def normalize_dir_url(text):
    """Tidy up what the user typed into a proper http(s) directory address, or None."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    p = urllib.parse.urlparse(text)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    path = p.path or "/"
    if not path.endswith("/"):
        last = path.rsplit("/", 1)[-1]
        path = path.rsplit("/", 1)[0] + "/" if "." in last else path + "/"
    return urllib.parse.urlunparse((p.scheme, p.netloc, path, "", "", ""))


def folder_name_for_url(url):
    p = urllib.parse.urlparse(url)
    name = urllib.parse.unquote((p.netloc + p.path).strip("/")).replace("/", "_")
    return re.sub(r"[^\w.\- ]", "_", name) or "open_directory"


def fetch_text(url):
    with open_url(url) as resp:
        raw = resp.read(20 * 1024 * 1024)
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def crawl_open_directory(start_url, recurse, want_sizes, report, cancelled):
    """Walk an open-directory website and return (files, note)."""
    start = urllib.parse.urlparse(start_url)
    host, base_path = start.netloc.lower(), start.path
    todo, visited = deque([start_url]), set()
    files = {}              # url -> file dict
    problems = 0
    pages = 0

    while todo and not cancelled():
        page = todo.popleft()
        page_key = page.split("#")[0].split("?")[0]
        if page_key in visited:
            continue
        visited.add(page_key)
        pages += 1
        report(f"Scanning... {pages} folder(s) checked, {len(files)} file(s) found")
        try:
            body = fetch_text(page)
        except Exception:
            if pages == 1:
                raise
            problems += 1
            continue
        parser = _LinkParser()
        parser.feed(body)
        page_path = urllib.parse.urlparse(page).path

        for href, trailing in parser.finish():
            href = href.strip()
            if not href or href[0] in "?#":
                continue                      # sort links, anchors
            full = urllib.parse.urljoin(page, href).split("#")[0]
            u = urllib.parse.urlparse(full)
            if u.scheme not in ("http", "https") or u.netloc.lower() != host:
                continue                      # mailto:, other websites
            if not u.path.startswith(base_path):
                continue                      # "Parent Directory" and anything outside the chosen folder
            if u.path.endswith("/"):
                if u.query or not recurse:
                    continue
                key = u._replace(query="", fragment="").geturl()
                if key not in visited:
                    todo.append(key)
                continue
            if u.path == page_path or full in files:
                continue
            name = urllib.parse.unquote(u.path[len(base_path):])
            if not name:
                continue
            ext = name.rsplit(".", 1)[-1].upper() if "." in name.rsplit("/", 1)[-1] else ""
            size, exact = parse_listing_size(trailing)
            files[full] = {"name": name, "size": size, "exact": exact, "source": ext, "url": full}

    result = list(files.values())
    note = []
    if want_sizes and not cancelled():
        missing = [f for f in result if f["size"] < 0 or not f["exact"]]
        done = [0]

        def head(f):
            if cancelled():
                return
            try:
                with open_url(f["url"], timeout=15, method="HEAD") as resp:
                    length = resp.headers.get("Content-Length")
                    if length and length.isdigit():
                        f["size"] = int(length)
                        f["exact"] = True
            except Exception:
                pass
            done[0] += 1
            if done[0] % 5 == 0:
                report(f"Looking up file sizes... {done[0]} of {len(missing)}")

        if missing:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(head, missing))
    if cancelled():
        note.append("scan stopped early - showing what was found so far")
    if problems:
        note.append(f"{problems} folder(s) could not be read")
    return result, "; ".join(note)


# --------------------------------------------------------------------------
# Download worker
# --------------------------------------------------------------------------
class DownloadCancelled(Exception):
    pass


class LocalSink:
    """One file being written into an ordinary folder (a '.part' file that can be resumed)."""

    def __init__(self, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)   # keeps the folder structure
        self.dest = dest
        self.part = dest + ".part"
        self.offset = os.path.getsize(self.part) if os.path.exists(self.part) else 0

    def restart(self):
        """Throw away any partly downloaded data and start from zero."""
        if os.path.exists(self.part):
            os.remove(self.part)
        self.offset = 0

    def open(self):
        return open(self.part, "ab" if self.offset > 0 else "wb")

    def commit(self):
        os.replace(self.part, self.dest)

    def abort(self):
        pass   # keep the partial file so the next try can resume it


class LocalStore:
    """Saves files into a normal folder on disk."""

    def __init__(self, base_dir):
        self.base = base_dir

    def is_complete(self, name, size, exact):
        dest = safe_join(self.base, name)      # raises ValueError for unsafe names
        return bool(exact and size > 0 and os.path.isfile(dest) and os.path.getsize(dest) == size)

    def begin(self, name):
        return LocalSink(safe_join(self.base, name))


class Downloader(threading.Thread):
    """Downloads a list of files. Progress arrives as tuples on the `events` queue:
    ("file", name, index, count, size), ("bytes", ...), ("finished", ok, skipped, failed, cancelled)."""

    def __init__(self, files, dest_dir, events, store=None):
        super().__init__(daemon=True)
        self.files = files            # list of dicts: name, size, url (and optionally exact)
        self.dest_dir = dest_dir
        self.store = store or LocalStore(dest_dir)
        self.events = events          # queue.Queue
        self.cancel_flag = threading.Event()

    def cancel(self):
        self.cancel_flag.set()

    def run(self):
        total_bytes = sum(max(f["size"], 0) for f in self.files)
        done_bytes = 0
        ok, skipped, failed = 0, 0, []
        start = time.time()
        session_bytes = 0
        count = len(self.files)

        try:
            for idx, f in enumerate(self.files, 1):
                if self.cancel_flag.is_set():
                    raise DownloadCancelled()
                name, size = f["name"], f["size"]
                exact = f.get("exact", True)
                self.events.put(("file", name, idx, count, size))

                # Already complete?
                try:
                    complete = self.store.is_complete(name, size, exact)
                except ValueError as exc:
                    failed.append((name, str(exc)))
                    done_bytes += max(size, 0)
                    continue
                if complete:
                    skipped += 1
                    done_bytes += size
                    self.events.put(("bytes", size, size, done_bytes, total_bytes, 0.0, None))
                    continue

                last_err = None
                for attempt in range(1, RETRIES + 1):
                    try:
                        got = self._download_one(
                            f["url"], name, size, exact, done_bytes, total_bytes, start, session_bytes
                        )
                        session_bytes += got
                        ok += 1
                        last_err = None
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as exc:  # network errors, HTTP errors, disk errors
                        last_err = exc
                        time.sleep(min(2 * attempt, 6))
                if last_err is not None:
                    failed.append((name, str(last_err)))
                done_bytes += max(size, 0)
        except DownloadCancelled:
            self.events.put(("finished", ok, skipped, failed, True))
            return

        self.events.put(("finished", ok, skipped, failed, False))

    def _download_one(self, url, name, size, exact, done_before, total_bytes, start, session_before):
        sink = self.store.begin(name)
        try:
            return self._transfer(sink, url, size, exact, done_before, total_bytes, start, session_before)
        except BaseException:
            sink.abort()
            raise

    def _transfer(self, sink, url, size, exact, done_before, total_bytes, start, session_before):
        offset = sink.offset
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        try:
            resp = open_url(url, headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:  # bad range: restart this file cleanly
                sink.restart()
                offset = 0
                resp = open_url(url)
            else:
                raise

        with resp:
            if offset > 0 and getattr(resp, "status", 200) != 206:
                sink.restart()   # server ignored Range; start over
                offset = 0

            length = resp.headers.get("Content-Length")
            real_total = int(length) + offset if length and length.isdigit() else 0
            # the server's own length is the truth; a rounded listing size is only a guess
            file_total = real_total or (size if size > 0 else 0)
            expected = real_total or (size if exact and size > 0 else 0)

            written = offset
            transferred = 0
            last_emit = 0.0
            with sink.open() as out:
                while True:
                    if self.cancel_flag.is_set():
                        raise DownloadCancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    transferred += len(chunk)
                    now = time.time()
                    if now - last_emit >= 0.1:
                        last_emit = now
                        elapsed = max(now - start, 0.001)
                        speed = (session_before + transferred) / elapsed
                        overall_done = done_before + (written if size > 0 else 0)
                        remaining = (total_bytes - overall_done) / speed if speed > 0 and total_bytes else None
                        self.events.put(
                            ("bytes", written, file_total, overall_done, total_bytes, speed, remaining)
                        )

        if expected and written != expected:
            raise IOError(f"Incomplete download ({written} of {expected} bytes)")
        sink.commit()
        self.events.put(("bytes", written, file_total, done_before + max(size, 0), total_bytes, 0.0, None))
        return transferred
