#!/usr/bin/env python3
"""
Internet Archive Download Manager
---------------------------------
Two tabs:
  * Internet Archive - paste an archive.org item URL (or just the item name).
  * Open Directory   - paste the address of any open directory website
                       (the plain "Index of /..." file listings).
Load the file list, tick the files you want (or filter by file type), choose a
save folder and download them. Folder structure is preserved.

The Help menu has an About window and automatic updates from GitHub releases
(which can be switched off).

Requires only Python 3.8+ with tkinter (included with the standard installers
on Windows and macOS; on Linux install the python3-tk package).
"""

import json
import html.parser
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import webbrowser
import zipfile
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Internet Archive Download Manager"
APP_FILE = "InternetArchiveDownloadManager"      # name of the built program
USER_AGENT = "InternetArchiveDownloadManager/1.0 (personal tool)"

# The release workflow stamps these two lines when it builds a tagged release.
# Running from source or building by hand? Set GITHUB_REPO to "your-name/your-repo"
# so the update check knows where to look (leave it empty to switch updates off).
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
SERVICES = [
    ("Internet Archive", "file lists and downloads", "https://archive.org/"),
    ("GitHub", "release hosting for updates", "https://github.com/"),
]
CHUNK = 256 * 1024
RETRIES = 3
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".internet_archive_download_manager.json")

# Change this one number to make ALL text and checkboxes bigger or smaller.
BASE_FONT_SIZE = 15

# Extensions offered in the "File types" dropdown.
FILTER_EXTENSIONS = ["mp3", "wav", "flac", "mp4", "mpg", "avi", "png", "jpg", "zip", "rar", "7z"]

PALETTES = {
    "light": {
        "bg": "#f3f3f3", "fg": "#1b1b1b", "field": "#ffffff", "button": "#e1e1e1",
        "button_active": "#d0d0d0", "head": "#dcdcdc", "row_even": "#ffffff",
        "row_odd": "#e6eefb", "select_bg": "#bcd3fb", "select_fg": "#000000",
        "accent": "#1a73e8", "box_border": "#4a4f57", "box_fill": "#ffffff",
        "trough": "#d9d9d9", "popup_border": "#888888",
    },
    "dark": {
        "bg": "#1e1f22", "fg": "#e8e8e8", "field": "#2b2d31", "button": "#3a3c42",
        "button_active": "#4a4d55", "head": "#33353a", "row_even": "#232428",
        "row_odd": "#2d2f35", "select_bg": "#2f4f86", "select_fg": "#ffffff",
        "accent": "#4c8dff", "box_border": "#b4b9c2", "box_fill": "#1e1f22",
        "trough": "#2b2d31", "popup_border": "#666a73",
    },
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def resource_path(name):
    """Find a bundled file whether running as a script or as a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
    except Exception:
        pass


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


def default_save_dir(identifier):
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    root = downloads if os.path.isdir(downloads) else os.path.expanduser("~")
    safe = re.sub(r'[<>:"/\\|?*]', "_", identifier)
    return os.path.join(root, safe)


def open_url(url, headers=None, timeout=30, method=None):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method=method
    )
    return urllib.request.urlopen(req, timeout=timeout)


def set_titlebar_dark(win, dark):
    """Dark title bar on Windows 10/11 (silently does nothing elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)
            ) == 0:
                break
    except Exception:
        pass


# --------------------------------------------------------------------------
# Download worker
# --------------------------------------------------------------------------
class DownloadCancelled(Exception):
    pass


class Downloader(threading.Thread):
    def __init__(self, files, dest_dir, events):
        super().__init__(daemon=True)
        self.files = files            # list of dicts: name, size, url
        self.dest_dir = dest_dir
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

                try:
                    dest = safe_join(self.dest_dir, name)
                except ValueError as exc:
                    failed.append((name, str(exc)))
                    done_bytes += max(size, 0)
                    continue

                # Already complete?
                if exact and os.path.isfile(dest) and size > 0 and os.path.getsize(dest) == size:
                    skipped += 1
                    done_bytes += size
                    self.events.put(("bytes", size, size, done_bytes, total_bytes, 0.0, None))
                    continue

                last_err = None
                for attempt in range(1, RETRIES + 1):
                    try:
                        got = self._download_one(
                            f["url"], dest, size, exact, done_bytes, total_bytes, start, session_bytes
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

    def _download_one(self, url, dest, size, exact, done_before, total_bytes, start, session_before):
        os.makedirs(os.path.dirname(dest), exist_ok=True)   # keeps the archive's folder structure
        part = dest + ".part"

        offset = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        try:
            resp = open_url(url, headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:  # bad range: restart this file cleanly
                if os.path.exists(part):
                    os.remove(part)
                offset = 0
                resp = open_url(url)
            else:
                raise

        with resp:
            if offset > 0 and getattr(resp, "status", 200) != 206:
                offset = 0  # server ignored Range; start over
            mode = "ab" if offset > 0 else "wb"

            length = resp.headers.get("Content-Length")
            real_total = int(length) + offset if length and length.isdigit() else 0
            # the server's own length is the truth; a rounded listing size is only a guess
            file_total = real_total or (size if size > 0 else 0)
            expected = real_total or (size if exact and size > 0 else 0)

            written = offset
            transferred = 0
            last_emit = 0.0
            with open(part, mode) as out:
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
        os.replace(part, dest)
        self.events.put(("bytes", written, file_total, done_before + max(size, 0), total_bytes, 0.0, None))
        return transferred


# --------------------------------------------------------------------------
# Big clickable checkbox (a normal checkbox can't be enlarged)
# --------------------------------------------------------------------------
class BigCheck(tk.Label):
    def __init__(self, master, app, text, variable, command=None, **kw):
        super().__init__(
            master, text="  " + text, compound="left", cursor="hand2", anchor="w",
            font=app.big_font, **kw,
        )
        self.app = app
        self.var = variable
        self.command = command
        self.bind("<Button-1>", self.on_click)
        self.var.trace_add("write", self.refresh)
        self.refresh()
        app.register_tk(self, "label")

    def refresh(self, *args):
        self.config(image=self.app.img_on if self.var.get() else self.app.img_off)

    def on_click(self, event=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()


# --------------------------------------------------------------------------
# Progress window
# --------------------------------------------------------------------------
class ProgressWindow(tk.Toplevel):
    def __init__(self, parent, app, downloader, events, on_close):
        super().__init__(parent)
        self.withdraw()   # build invisibly, then centre, then show
        self.title("Downloading...")
        self.resizable(False, False)
        # Deliberately NOT transient and no grab: a window tied to the main one hides with it
        # when the main window is minimised, which made the app impossible to bring back.
        self.app = app
        app.downloader_window = self
        app.downloading = True
        self.downloader = downloader
        self.events = events
        self.on_close = on_close
        self.finished = False
        self.has_total = all(f["size"] > 0 for f in downloader.files)
        self.configure(bg=app.pal["bg"])

        pad = {"padx": 14, "pady": 3}
        frm = ttk.Frame(self, padding=8)
        frm.grid()

        self.file_label = ttk.Label(frm, text="Starting...", width=58, anchor="w")
        self.file_label.grid(row=0, column=0, sticky="w", **pad)
        self.file_bar = ttk.Progressbar(frm, length=700, maximum=100)
        self.file_bar.grid(row=1, column=0, sticky="ew", **pad)
        self.file_detail = ttk.Label(frm, text="", anchor="w")
        self.file_detail.grid(row=2, column=0, sticky="w", **pad)

        ttk.Separator(frm).grid(row=3, column=0, sticky="ew", pady=8)

        self.total_label = ttk.Label(frm, text="Overall progress", anchor="w")
        self.total_label.grid(row=4, column=0, sticky="w", **pad)
        self.total_bar = ttk.Progressbar(frm, length=700, maximum=100)
        self.total_bar.grid(row=5, column=0, sticky="ew", **pad)
        self.total_detail = ttk.Label(frm, text="", anchor="w")
        self.total_detail.grid(row=6, column=0, sticky="w", **pad)

        self.button = ttk.Button(frm, text="Cancel", command=self.cancel)
        self.button.grid(row=7, column=0, sticky="e", padx=14, pady=(10, 4))

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.center_on(parent)
        self.deiconify()
        set_titlebar_dark(self, app.dark)
        self.lift()
        self.after(100, self.poll)

    def center_on(self, parent):
        """Place this window in the middle of the main window."""
        self.update_idletasks()
        parent.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        x = max(0, x)
        y = max(0, y)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def cancel(self):
        if self.finished:
            self.close()
        else:
            self.button.config(state="disabled", text="Cancelling...")
            self.downloader.cancel()

    def close(self):
        self.app.downloading = False
        self.app.downloader_window = None
        self.destroy()
        self.on_close()

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                self.handle(ev)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self.poll)

    def handle(self, ev):
        kind = ev[0]
        if kind == "file":
            _, name, idx, count, size = ev
            shown = name if len(name) <= 56 else "..." + name[-53:]
            self.file_label.config(text=f"File {idx} of {count}: {shown}")
            self.file_bar["value"] = 0
            self.file_detail.config(text="")
            if not self.has_total:   # some sizes unknown: show progress by file count
                self.total_bar["value"] = (idx - 1) * 100 / count
                self.total_detail.config(text=f"{idx - 1} of {count} files done")
        elif kind == "bytes":
            _, f_done, f_total, o_done, o_total, speed, remaining = ev
            if f_total > 0:
                self.file_bar["value"] = min(100, f_done * 100 / f_total)
                self.file_detail.config(text=f"{human_size(f_done)} of {human_size(f_total)}")
            else:
                self.file_detail.config(text=f"{human_size(f_done)}")
            if o_total > 0 and self.has_total:
                self.total_bar["value"] = min(100, o_done * 100 / o_total)
                extra = f"   {human_size(speed)}/s   ETA {human_time(remaining)}" if speed else ""
                self.total_detail.config(
                    text=f"{human_size(o_done)} of {human_size(o_total)}{extra}"
                )
        elif kind == "finished":
            _, ok, skipped, failed, cancelled = ev
            self.finished = True
            self.title("Cancelled" if cancelled else "Finished")
            self.button.config(state="normal", text="Close")
            if cancelled:
                self.file_label.config(text="Download cancelled. Partial files are kept and can be resumed.")
            else:
                self.total_bar["value"] = 100
                self.file_bar["value"] = 100
                msg = f"Done: {ok} downloaded"
                if skipped:
                    msg += f", {skipped} already present"
                if failed:
                    msg += f", {len(failed)} failed"
                self.file_label.config(text=msg)
            if failed:
                details = "\n".join(f"- {n}: {e}" for n, e in failed[:15])
                if len(failed) > 15:
                    details += f"\n...and {len(failed) - 15} more"
                messagebox.showwarning("Some files failed", details, parent=self)


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
# Updates from GitHub releases
# --------------------------------------------------------------------------
ALLOWED_UPDATE_HOSTS = ("github.com", "githubusercontent.com")


def version_tuple(text):
    """'v1.2.3' -> (1, 2, 3, 0). Anything after a '-' or '+' is ignored."""
    nums = re.findall(r"\d+", (text or "").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) + (0,) * (4 - len(nums[:4]))


def platform_asset_name():
    if sys.platform == "win32":
        return f"{APP_FILE}-Windows.zip"
    if sys.platform == "darwin":
        return f"{APP_FILE}-macOS.zip"
    return f"{APP_FILE}-Linux.tar.gz"


def can_self_update():
    """Only the packaged program can replace itself; a plain .py script cannot."""
    return bool(getattr(sys, "frozen", False))


def trusted_update_url(url):
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_UPDATE_HOSTS)


def fetch_latest_release(repo):
    """Newest published (non-draft, non-prerelease) release, or None if there is none."""
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    try:
        with open_url(url, headers={"Accept": "application/vnd.github+json"}, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    tag = data.get("tag_name") or ""
    asset = next((a for a in data.get("assets", []) if a.get("name") == platform_asset_name()), None)
    return {
        "tag": tag,
        "version": version_tuple(tag),
        "notes": (data.get("body") or "").strip(),
        "page": data.get("html_url") or f"https://github.com/{repo}/releases",
        "asset_url": asset.get("browser_download_url") if asset else None,
        "asset_size": int(asset.get("size") or 0) if asset else 0,
    }


def download_update(url, dest_path, progress, cancelled, expected_size=0, validate=trusted_update_url):
    """Download the release file, refusing any address that is not GitHub over https."""
    if not validate(url):
        raise ValueError("Refusing to download the update from an untrusted address.")

    class Guard(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not validate(newurl):
                raise ValueError("The update download was redirected to an untrusted address.")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(Guard)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    got = 0
    with opener.open(req, timeout=30) as resp, open(dest_path, "wb") as out:
        length = resp.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else expected_size
        while True:
            if cancelled():
                raise DownloadCancelled()
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            progress(got, total)
    if expected_size and got != expected_size:
        raise IOError(f"Incomplete update download ({got} of {expected_size} bytes)")


def _extract_member(archive_path, wanted_name, out_path):
    """Copy the one file called wanted_name out of a .zip or .tar.gz (no other paths are used)."""
    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if not info.is_dir() and os.path.basename(info.filename) == wanted_name:
                    with zf.open(info) as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    return
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile() and os.path.basename(m.name) == wanted_name:
                    with tf.extractfile(m) as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    return
    raise FileNotFoundError(f"{wanted_name} was not found inside the downloaded update.")


def spawn_env():
    """A packaged app that starts another copy of itself must reset PyInstaller's environment."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def build_windows_script(new_exe, old_exe):
    q = lambda path: path.replace("%", "%%")
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "set TRIES=0\r\n"
        "ping 127.0.0.1 -n 3 >nul\r\n"
        ":retry\r\n"
        f'copy /y "{q(new_exe)}" "{q(old_exe)}" >nul 2>&1\r\n'
        "if not errorlevel 1 goto launch\r\n"
        "set /a TRIES+=1\r\n"
        "if %TRIES% GEQ 40 goto launch\r\n"
        "ping 127.0.0.1 -n 2 >nul\r\n"
        "goto retry\r\n"
        ":launch\r\n"
        f'start "" "{q(old_exe)}"\r\n'
        "exit /b 0\r\n"
    )


def build_mac_script(new_app, old_app, work_dir, pid):
    a, n, w = shlex.quote(old_app), shlex.quote(new_app), shlex.quote(work_dir)
    return (
        "#!/bin/sh\n"
        f"while kill -0 {int(pid)} 2>/dev/null; do sleep 0.5; done\n"
        f"rm -rf {a}.new {a}.old\n"
        f"ditto {n} {a}.new && mv {a} {a}.old && mv {a}.new {a} && rm -rf {a}.old\n"
        f"xattr -dr com.apple.quarantine {a} 2>/dev/null\n"
        f"open {a}\n"
        f"rm -rf {w}\n"
    )


def install_update(archive_path, work_dir, exe_path=None):
    """Unpack the downloaded release and arrange for it to replace the running program.

    The caller must quit the app straight afterwards. Raises if the install location
    can't be written to (the caller then offers the release page instead)."""
    exe = os.path.abspath(exe_path or sys.executable)
    if sys.platform == "win32":
        new_exe = os.path.join(work_dir, APP_FILE + ".exe")
        _extract_member(archive_path, APP_FILE + ".exe", new_exe)
        if not os.access(os.path.dirname(exe), os.W_OK):
            raise PermissionError(f"No permission to write to {os.path.dirname(exe)}")
        script = os.path.join(work_dir, "apply_update.bat")
        with open(script, "w", newline="") as fh:
            fh.write(build_windows_script(new_exe, exe))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(["cmd", "/c", script], creationflags=flags, env=spawn_env(), close_fds=True)
    elif sys.platform == "darwin":
        marker = ".app" + os.sep
        if marker not in exe:
            raise RuntimeError("This copy of the app is not inside an .app bundle.")
        old_app = exe.split(marker)[0] + ".app"
        if not os.access(os.path.dirname(old_app), os.W_OK):
            raise PermissionError(f"No permission to write to {os.path.dirname(old_app)}")
        out = os.path.join(work_dir, "unpacked")
        os.makedirs(out, exist_ok=True)
        subprocess.run(["ditto", "-x", "-k", archive_path, out], check=True)
        new_app = os.path.join(out, APP_FILE + ".app")
        if not os.path.isdir(new_app):
            raise FileNotFoundError("The update did not contain the application.")
        script = os.path.join(work_dir, "apply_update.sh")
        with open(script, "w") as fh:
            fh.write(build_mac_script(new_app, old_app, work_dir, os.getpid()))
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/sh", script], env=spawn_env(), start_new_session=True, close_fds=True)
    else:
        folder = os.path.dirname(exe)
        if not os.access(folder, os.W_OK):
            raise PermissionError(f"No permission to write to {folder}")
        staged = os.path.join(folder, "." + APP_FILE + ".update")
        _extract_member(archive_path, APP_FILE, staged)
        os.chmod(staged, 0o755)
        os.replace(staged, exe)               # replacing a running program is fine on Linux
        shutil.rmtree(work_dir, ignore_errors=True)
        subprocess.Popen([exe], env=spawn_env(), start_new_session=True, close_fds=True)


def center_window(win, parent):
    win.update_idletasks()
    parent.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    x = max(0, parent.winfo_rootx() + (parent.winfo_width() - w) // 2)
    y = max(0, parent.winfo_rooty() + (parent.winfo_height() - h) // 2)
    win.geometry(f"{w}x{h}+{x}+{y}")


class LinkLabel(tk.Label):
    """Clickable web link."""

    def __init__(self, master, app, text, url, **kw):
        super().__init__(master, text=text, cursor="hand2", font=app.link_font, anchor="w", **kw)
        self.bind("<Button-1>", lambda e: webbrowser.open(url))
        app.register_tk(self, "link")


class AboutWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.withdraw()
        self.title(f"About {APP_TITLE}")
        self.resizable(False, False)
        self.app = app
        self.configure(bg=app.pal["bg"])
        fam, size = app.big_font
        frm = ttk.Frame(self, padding=(26, 20))
        frm.grid()
        row = 0
        if getattr(app, "_icon", None) is not None:
            ttk.Label(frm, image=app._icon).grid(row=row, column=0, pady=(0, 6))
            row += 1
        ttk.Label(frm, text=APP_TITLE, font=(fam, size + 5, "bold")).grid(row=row, column=0)
        row += 1
        ttk.Label(frm, text=f"Version {VERSION}").grid(row=row, column=0, pady=(2, 8))
        row += 1
        ttk.Label(frm, text="Coded by Claude & Emmanuel Hoachlander", font=(fam, size, "bold")).grid(row=row, column=0)
        row += 1

        ttk.Separator(frm).grid(row=row, column=0, sticky="ew", pady=14)
        row += 1
        ttk.Label(frm, text="If you enjoy this program, please consider a donation.",
                  wraplength=560, justify="center").grid(row=row, column=0)
        row += 1
        ttk.Button(frm, text="Donate with PayPal", command=lambda: webbrowser.open(DONATE_URL)
                   ).grid(row=row, column=0, pady=(8, 0))
        row += 1

        ttk.Separator(frm).grid(row=row, column=0, sticky="ew", pady=14)
        row += 1
        ttk.Label(frm, text="Open source software used", font=(fam, size, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        credits = ttk.Frame(frm)
        credits.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        for i, (name, lic, url) in enumerate(CREDITS):
            LinkLabel(credits, app, name, url).grid(row=i, column=0, sticky="w", padx=(0, 18), pady=1)
            ttk.Label(credits, text=lic).grid(row=i, column=1, sticky="w", pady=1)
        row += 1
        ttk.Label(frm, text="Services used", font=(fam, size, "bold")).grid(row=row, column=0, sticky="w", pady=(12, 0))
        row += 1
        services = ttk.Frame(frm)
        services.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        for i, (name, what, url) in enumerate(SERVICES):
            LinkLabel(services, app, name, url).grid(row=i, column=0, sticky="w", padx=(0, 18), pady=1)
            ttk.Label(services, text=what).grid(row=i, column=1, sticky="w", pady=1)
        row += 1
        ttk.Button(frm, text="Close", command=self.destroy).grid(row=row, column=0, sticky="e", pady=(16, 0))

        app.recolor_tk()
        center_window(self, app)
        self.deiconify()
        set_titlebar_dark(self, app.dark)
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_set()


class UpdateWindow(tk.Toplevel):
    def __init__(self, app, release):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self.release = release
        self.title("Update available")
        self.resizable(False, False)
        self.configure(bg=app.pal["bg"])
        self.cancel_flag = threading.Event()
        self.q = queue.Queue()
        self.busy = False
        self.work_dir = None
        fam, size = app.big_font

        frm = ttk.Frame(self, padding=(22, 18))
        frm.grid()
        shown = (release["tag"] or "").lstrip("vV") or "a newer version"
        ttk.Label(frm, text=f"Version {shown} is available", font=(fam, size + 3, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frm, text=f"You have version {VERSION}.").grid(row=1, column=0, sticky="w", pady=(2, 8))

        if release["notes"]:
            box = tk.Text(frm, width=60, height=8, wrap="word", font=app.big_font, relief="flat", bd=0,
                          highlightthickness=1)
            box.insert("1.0", release["notes"][:2500])
            box.config(state="disabled")
            box.grid(row=2, column=0, sticky="ew")
            app.register_tk(box, "text")

        self.can_install = can_self_update() and bool(release["asset_url"])
        if not can_self_update():
            note = "You are running from source, so the update can't install itself."
        elif not release["asset_url"]:
            note = "This release has no download for your system yet."
        else:
            note = "The app will close, update itself and start again."
        ttk.Label(frm, text=note, wraplength=620).grid(row=3, column=0, sticky="w", pady=(10, 4))

        self.status = ttk.Label(frm, text="")
        self.status.grid(row=4, column=0, sticky="w")
        self.bar = ttk.Progressbar(frm, length=620, maximum=100)

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, sticky="e", pady=(12, 0))
        self.later_btn = ttk.Button(btns, text="Not now", command=self.on_later)
        self.later_btn.pack(side="right", padx=(8, 0))
        self.install_btn = ttk.Button(
            btns, text="Install update" if self.can_install else "Open release page", command=self.on_install
        )
        self.install_btn.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.on_later)
        app.recolor_tk()
        center_window(self, app)
        self.deiconify()
        set_titlebar_dark(self, app.dark)

    def on_later(self):
        if self.busy:
            self.cancel_flag.set()
            self.later_btn.config(state="disabled", text="Cancelling...")
            return
        self.app.updating = False
        self.destroy()

    def on_install(self):
        if not self.can_install:
            webbrowser.open(self.release["page"])
            self.destroy()
            return
        if self.app.downloading:
            messagebox.showinfo(APP_TITLE, "A download is running. Wait for it to finish or cancel it, then update.", parent=self)
            return
        self.busy = True
        self.app.updating = True
        self.install_btn.config(state="disabled")
        self.later_btn.config(text="Cancel")
        self.bar.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self.status.config(text="Downloading update...")
        self.work_dir = tempfile.mkdtemp(prefix="iadm-update-")
        archive = os.path.join(self.work_dir, platform_asset_name())
        url, expected = self.release["asset_url"], self.release["asset_size"]

        def work():
            try:
                download_update(url, archive, lambda g, t: self.q.put(("p", g, t)),
                                self.cancel_flag.is_set, expected_size=expected)
                self.q.put(("done", archive))
            except Exception as exc:
                self.q.put(("err", exc))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.poll)

    def poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "p":
                    _, got, total = ev
                    if total:
                        self.bar["value"] = min(100, got * 100 / total)
                    self.status.config(text=f"Downloading update... {human_size(got)}" + (f" of {human_size(total)}" if total else ""))
                elif ev[0] == "done":
                    self.apply(ev[1])
                    return
                else:
                    self.fail(ev[1])
                    return
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self.poll)

    def apply(self, archive):
        self.status.config(text="Installing...")
        self.update_idletasks()
        try:
            install_update(archive, self.work_dir)
        except Exception as exc:
            self.fail(exc)
            return
        self.status.config(text="Restarting...")
        self.after(400, self.app.destroy)

    def fail(self, exc):
        self.busy = False
        self.app.updating = False
        if self.work_dir:
            shutil.rmtree(self.work_dir, ignore_errors=True)
        if isinstance(exc, DownloadCancelled):
            self.destroy()
            return
        self.bar.grid_remove()
        self.status.config(text="")
        self.install_btn.config(state="normal", text="Open release page", command=lambda: (webbrowser.open(self.release["page"]), self.destroy()))
        self.later_btn.config(state="normal", text="Close")
        messagebox.showerror(
            APP_TITLE,
            f"The update couldn't be installed automatically:\n\n{exc}\n\n"
            "You can download it from the release page instead.",
            parent=self,
        )


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1250x860")
        self.minsize(1000, 650)

        self.config_data = load_config()
        self.dark = bool(self.config_data.get("dark_mode", False))
        self.pal = PALETTES["dark" if self.dark else "light"]
        self.tk_widgets = []     # plain tk widgets that need recolouring: (widget, kind)
        self.panels = []
        self.downloading = False
        self.downloader_window = None
        self.updating = False
        self.update_checking = False
        self.protocol("WM_DELETE_WINDOW", self.on_quit)

        self.set_window_icon()
        self.setup_fonts()

        self.dark_var = tk.BooleanVar(value=self.dark)
        self.auto_update_var = tk.BooleanVar(value=bool(self.config_data.get("auto_update", True)))
        self.build_menu()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.archive_panel = ArchivePanel(self.nb, self)
        self.dir_panel = OpenDirPanel(self.nb, self)
        self.nb.add(self.archive_panel, text="Internet Archive")
        self.nb.add(self.dir_panel, text="Open Directory")
        self.panels = [self.archive_panel, self.dir_panel]

        # Dark mode switch sits at the right end of the tab strip.
        dark_chk = BigCheck(self, self, "Dark mode", self.dark_var, self.toggle_dark)
        dark_chk.place(in_=self.nb, relx=1.0, x=-12, y=8, anchor="ne")
        dark_chk.lift()
        self.apply_theme()
        self.update_idletasks()
        need_h = self.winfo_reqheight()
        self.minsize(1000, need_h)
        self.geometry(f"1250x{max(860, need_h + 20)}")
        threading.Thread(target=self.clean_old_update_files, daemon=True).start()
        if GITHUB_REPO and self.auto_update_var.get():
            self.after(2500, lambda: self.check_updates(manual=False))   # quiet check at start-up

    # ---- Menu, About, updates -----------------------------------------
    @staticmethod
    def clean_old_update_files():
        """Remove leftovers of earlier updates (the helper script can't delete its own folder)."""
        try:
            root = tempfile.gettempdir()
            for name in os.listdir(root):
                if name.startswith("iadm-update-"):
                    path = os.path.join(root, name)
                    if time.time() - os.path.getmtime(path) > 600:
                        shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    def build_menu(self):
        bar = tk.Menu(self, tearoff=False)
        file_menu = tk.Menu(bar, tearoff=False)
        file_menu.add_command(label="Exit", command=self.on_quit)
        bar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label="About...", command=self.show_about)
        help_menu.add_command(label="Donate...", command=lambda: webbrowser.open(DONATE_URL))
        help_menu.add_separator()
        help_menu.add_checkbutton(label="Check for updates automatically", variable=self.auto_update_var,
                                  command=self.toggle_auto_update)
        help_menu.add_command(label="Check for updates now", command=lambda: self.check_updates(manual=True))
        bar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=bar)
        self.menus = [bar, file_menu, help_menu]

    def show_about(self):
        if getattr(self, "about_win", None) is not None and self.about_win.winfo_exists():
            self.about_win.lift()
            self.about_win.focus_set()
            return
        self.about_win = AboutWindow(self)

    def toggle_auto_update(self):
        self.config_data["auto_update"] = bool(self.auto_update_var.get())
        save_config(self.config_data)

    def run_bg(self, work, done):
        """Run work() in a thread; call done(result, error) back on the main thread."""
        q = queue.Queue()

        def runner():
            try:
                q.put((work(), None))
            except Exception as exc:
                q.put((None, exc))

        def poll():
            try:
                result, error = q.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            done(result, error)

        threading.Thread(target=runner, daemon=True).start()
        self.after(100, poll)

    def check_updates(self, manual=False):
        win = getattr(self, "update_win", None)
        if win is not None and self._alive(win):      # an update window is already open: show it
            win.lift()
            return
        if self.update_checking or self.updating:
            return
        if not GITHUB_REPO:
            if manual:
                messagebox.showinfo(APP_TITLE, "Updates aren't set up in this copy of the program "
                                    "(no GitHub repository is configured).")
            return
        self.update_checking = True

        def done(release, error):
            self.update_checking = False
            if error is not None:
                if manual:
                    messagebox.showerror(APP_TITLE, f"Couldn't check for updates:\n\n{error}")
                return
            if release is None:
                if manual:
                    messagebox.showinfo(APP_TITLE, "No releases have been published yet.")
            elif release["version"] > version_tuple(VERSION):
                self.updating = True      # blocks a second window / a new download until answered
                self.update_win = UpdateWindow(self, release)
            elif manual:
                messagebox.showinfo(APP_TITLE, f"You're up to date (version {VERSION}).")

        self.run_bg(lambda: fetch_latest_release(GITHUB_REPO), done)

    def on_quit(self):
        if self.downloading and self.downloader_window is not None:
            if not messagebox.askyesno(APP_TITLE, "A download is still running.\n\nCancel it and quit?"):
                return
            try:
                self.downloader_window.downloader.cancel()
            except Exception:
                pass
        self.destroy()

    # ---- Setup --------------------------------------------------------
    def register_tk(self, widget, kind):
        self.tk_widgets.append((widget, kind))

    def set_window_icon(self):
        try:
            self._icon = tk.PhotoImage(file=resource_path("icon.png"))
            self.iconphoto(True, self._icon)
        except Exception:
            pass   # no icon file found: use the default

    def setup_fonts(self):
        size = BASE_FONT_SIZE
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.big_font = (family, size)
        self.link_font = tkfont.Font(family=family, size=size, underline=True)
        self.box = int(size * 2.2)   # checkbox / +/- size in pixels

        style = ttk.Style(self)
        style.theme_use("clam")      # the theme that lets us colour everything (dark mode)
        self.style = style

        # Pictures: checkboxes (redrawn per theme) and the +/- folder buttons.
        s = self.box
        self.img_off = tk.PhotoImage(master=self, width=s, height=s)
        self.img_on = tk.PhotoImage(master=self, width=s, height=s)
        self.img_part = tk.PhotoImage(master=self, width=s, height=s)
        # Row pictures for the file list: [ +/- button ][ checkbox ].
        # Folders get a + (closed) or - (open); files get blank space instead.
        self.gap = 8
        rw = 2 * s + self.gap
        self.row_imgs = {}
        for kind in ("closed", "open", "file"):
            for state in ("on", "off", "part"):
                self.row_imgs[(kind, state)] = tk.PhotoImage(master=self, width=rw, height=s)
        self.btn_plus = tk.PhotoImage(master=self, width=s, height=s)
        self.btn_minus = tk.PhotoImage(master=self, width=s, height=s)
        self.draw_indicators()

        # Plain row layout: our own picture + text (no built-in tiny +/-).
        style.layout("Treeview.Item", [
            ("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.image", {"side": "left", "sticky": ""}),
                ("Treeitem.focus", {"side": "left", "sticky": "", "children": [
                    ("Treeitem.text", {"side": "left", "sticky": ""}),
                ]}),
            ]}),
        ])

    def draw_indicators(self):
        """Blue square with a white + or - (readable on light and dark)."""
        s = self.box
        t = max(3, s // 8)
        mid = s // 2
        lo, hi = int(s * 0.25), int(s * 0.75)
        for img in (self.btn_plus, self.btn_minus):
            img.blank()
            img.put("#1a73e8", to=(0, 0, s, s))
            img.put("#ffffff", to=(lo, mid - t // 2, hi, mid - t // 2 + t))      # horizontal bar
        self.btn_plus.put("#ffffff", to=(mid - t // 2, lo, mid - t // 2 + t, hi))  # vertical bar

    def compose_row_images(self):
        """Build the [+/-][checkbox] pictures from the current checkbox pictures."""
        s = self.box
        boxes = {"on": self.img_on, "off": self.img_off, "part": self.img_part}
        for (kind, state), img in self.row_imgs.items():
            img.blank()
            if kind == "closed":
                img.tk.call(img, "copy", self.btn_plus, "-to", 0, 0)
            elif kind == "open":
                img.tk.call(img, "copy", self.btn_minus, "-to", 0, 0)
            img.tk.call(img, "copy", boxes[state], "-to", s + self.gap, 0)

    def draw_checks(self):
        """(Re)draw the checkbox pictures in place using the current theme colours."""
        s, P = self.box, self.pal
        b = max(3, s // 9)
        inner = (b, b, s - b, s - b)
        for img in (self.img_off, self.img_on, self.img_part):
            img.blank()
            img.put(P["box_border"], to=(0, 0, s, s))
            img.put(P["box_fill"], to=inner)
        for img in (self.img_on, self.img_part):
            img.put(P["accent"], to=inner)

        # white tick: short stroke down-right, then long stroke up-right
        t = max(3, s // 8)
        x0, y0 = int(s * 0.22), int(s * 0.52)
        x1, y1 = int(s * 0.42), int(s * 0.72)
        x2, y2 = int(s * 0.80), int(s * 0.28)

        def stroke(ax, ay, bx, by):
            steps = max(abs(bx - ax), abs(by - ay)) * 2
            for i in range(steps + 1):
                x = int(ax + (bx - ax) * i / steps)
                y = int(ay + (by - ay) * i / steps)
                self.img_on.put("#ffffff", to=(x, y, min(x + t, s - b), min(y + t, s - b)))

        stroke(x0, y0, x1, y1)
        stroke(x1, y1, x2, y2)

        # "partly ticked" box for folders: blue with a white dash
        self.img_part.put("#ffffff", to=(int(s * 0.25), int(s * 0.42), int(s * 0.75), int(s * 0.58)))
        self.compose_row_images()

    # ---- Theme --------------------------------------------------------
    def toggle_dark(self):
        self.dark = bool(self.dark_var.get())
        self.pal = PALETTES["dark" if self.dark else "light"]
        self.config_data["dark_mode"] = self.dark
        save_config(self.config_data)
        self.apply_theme()

    def apply_theme(self):
        P, st = self.pal, self.style
        size = BASE_FONT_SIZE
        family = self.big_font[0]
        self.configure(bg=P["bg"])
        self.draw_checks()

        st.configure(".", background=P["bg"], foreground=P["fg"], fieldbackground=P["field"],
                     bordercolor=P["popup_border"], lightcolor=P["bg"], darkcolor=P["bg"],
                     troughcolor=P["trough"], font=self.big_font)
        st.configure("TFrame", background=P["bg"])
        st.configure("TLabel", background=P["bg"], foreground=P["fg"])
        st.configure("TSeparator", background=P["popup_border"])
        st.configure("TButton", background=P["button"], foreground=P["fg"],
                     padding=(18, 8), bordercolor=P["popup_border"],
                     lightcolor=P["button"], darkcolor=P["button"])
        st.map("TButton",
               background=[("disabled", P["bg"]), ("pressed", P["button_active"]), ("active", P["button_active"])],
               foreground=[("disabled", P["popup_border"])])
        st.configure("TEntry", fieldbackground=P["field"], foreground=P["fg"],
                     insertcolor=P["fg"], padding=6, bordercolor=P["popup_border"],
                     lightcolor=P["field"], darkcolor=P["field"])
        st.map("TEntry", fieldbackground=[("readonly", P["field"])])
        st.configure("Horizontal.TProgressbar", background=P["accent"], troughcolor=P["trough"],
                     bordercolor=P["popup_border"], lightcolor=P["accent"], darkcolor=P["accent"],
                     thickness=int(size * 1.6))
        st.configure("Vertical.TScrollbar", background=P["button"], troughcolor=P["trough"],
                     bordercolor=P["bg"], arrowcolor=P["fg"], lightcolor=P["button"], darkcolor=P["button"],
                     arrowsize=int(size * 1.4))
        st.map("Vertical.TScrollbar", background=[("active", P["button_active"])])

        st.configure("Treeview", background=P["field"], fieldbackground=P["field"], foreground=P["fg"],
                     font=(family, size), rowheight=max(int(size * 2.6), self.box + 10),
                     indent=2 * self.box + self.gap, bordercolor=P["popup_border"],
                     lightcolor=P["field"], darkcolor=P["field"])
        st.configure("Treeview.Heading", background=P["head"], foreground=P["fg"],
                     font=(family, size, "bold"), relief="flat", padding=6)
        st.map("Treeview.Heading", background=[("active", P["button_active"])])
        st.map("Treeview",
               background=[("selected", P["select_bg"])],
               foreground=[("selected", P["select_fg"])])

        st.configure("TNotebook", background=P["bg"], bordercolor=P["popup_border"],
                     lightcolor=P["bg"], darkcolor=P["bg"], tabmargins=(2, 4, 2, 0))
        st.configure("TNotebook.Tab", background=P["button"], foreground=P["fg"],
                     padding=(26, 10), font=(family, size, "bold"), bordercolor=P["popup_border"],
                     lightcolor=P["button"], darkcolor=P["button"])
        st.map("TNotebook.Tab",
               background=[("selected", P["accent"]), ("active", P["button_active"])],
               foreground=[("selected", "#ffffff")])
        for panel in self.panels:
            panel.tree.tag_configure("even", background=P["row_even"], foreground=P["fg"])
            panel.tree.tag_configure("odd", background=P["row_odd"], foreground=P["fg"])

        self.recolor_tk()
        for m in getattr(self, "menus", []):
            try:
                m.config(bg=P["field"], fg=P["fg"], activebackground=P["select_bg"],
                         activeforeground=P["select_fg"])
            except tk.TclError:
                pass
        set_titlebar_dark(self, self.dark)

    @staticmethod
    def _alive(widget):
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def recolor_tk(self):
        """Recolour the plain tk widgets (ttk ones follow the style automatically)."""
        P = self.pal
        self.tk_widgets = [(w, k) for (w, k) in self.tk_widgets if self._alive(w)]
        for widget, kind in self.tk_widgets:
            try:
                if kind == "label":
                    widget.config(bg=P["bg"], fg=P["fg"], activebackground=P["bg"])
                elif kind == "popup":
                    widget.config(bg=P["field"], highlightbackground=P["popup_border"],
                                  highlightcolor=P["popup_border"])
                elif kind == "popup_label":
                    widget.config(bg=P["field"], fg=P["fg"], activebackground=P["field"])
                elif kind == "link":
                    widget.config(bg=P["bg"], fg=P["accent"], activebackground=P["bg"])
                elif kind == "text":
                    widget.config(bg=P["field"], fg=P["fg"], insertbackground=P["fg"],
                                  highlightbackground=P["popup_border"], highlightcolor=P["popup_border"])
            except tk.TclError:
                pass


# --------------------------------------------------------------------------
# One tab: address bar, filters, file list, save folder, download button
# --------------------------------------------------------------------------
class SourcePanel(ttk.Frame):
    SOURCE_LABEL = "URL:"
    PROMPT = "Paste a link and click Load."

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.filter_popup = None
        self.source_id = None      # name of what is currently listed
        self.pending_id = None     # name of what is being loaded
        self.items = {}            # file iid -> {"name","size","url","checked","source"}
        self.folders = {}          # folder iid -> {"path","files":[file iids],"size"}
        self.save_dir_custom = False
        self.loading = False
        self.job_q = None
        self.cancel_flag = threading.Event()

        self.url_var = tk.StringVar()
        self.dir_var = tk.StringVar()
        self.custom_ext_var = tk.StringVar()
        self.select_all_var = tk.BooleanVar(value=True)
        self.ext_vars = {e: tk.BooleanVar(value=False) for e in FILTER_EXTENSIONS}
        self.status_var = tk.StringVar(value=self.PROMPT)
        self.build_ui()

    # ---- UI -----------------------------------------------------------
    def build_extra_row(self):
        """Subclasses can add options under the address bar."""

    def build_ui(self):
        top = ttk.Frame(self, padding=(10, 14, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text=self.SOURCE_LABEL).pack(side="left")
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.load_or_stop())
        self.url_entry = entry
        self.load_btn = ttk.Button(top, text="Load", command=self.load_or_stop)
        self.load_btn.pack(side="left")

        self.build_extra_row()

        # ---- Filter row ----
        frow = ttk.Frame(self, padding=(10, 4))
        frow.pack(fill="x")
        ttk.Label(frow, text="Select by type:").pack(side="left")
        self.type_btn = ttk.Button(frow, text="File types \u25be", command=self.toggle_filter_popup)
        self.type_btn.pack(side="left", padx=6)
        ttk.Label(frow, text="Other (e.g. pdf, epub):").pack(side="left", padx=(10, 0))
        ext_entry = ttk.Entry(frow, textvariable=self.custom_ext_var, width=22)
        ext_entry.pack(side="left", padx=6)
        ext_entry.bind("<Return>", lambda e: self.apply_filter())
        ttk.Button(frow, text="Apply", command=self.apply_filter).pack(side="left", padx=4)
        ttk.Button(frow, text="Clear", command=self.clear_filter).pack(side="left", padx=4)

        mid = ttk.Frame(self, padding=(10, 4))
        mid.pack(fill="both", expand=True)

        bar = ttk.Frame(mid)
        bar.pack(fill="x", pady=(0, 4))
        BigCheck(bar, self.app, "Select all", self.select_all_var, self.toggle_all).pack(side="left")
        ttk.Button(bar, text="Expand all", command=lambda: self.set_all_open(True)).pack(side="left", padx=(28, 4))
        ttk.Button(bar, text="Collapse all", command=lambda: self.set_all_open(False)).pack(side="left", padx=4)
        self.count_label = ttk.Label(bar, text="")
        self.count_label.pack(side="right")

        cols = ("size", "source")
        # Checkbox pictures, folder nesting and names live in the tree's first column (#0).
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="browse", height=6)
        self.tree.heading("#0", text="File", anchor="w")
        self.tree.heading("size", text="Size")
        self.tree.heading("source", text="Type")
        self.tree.column("#0", width=720, anchor="w", stretch=True)
        self.tree.column("size", width=130, anchor="e", stretch=False)
        self.tree.column("source", width=130, anchor="w", stretch=False)
        self.tree.bind("<<TreeviewOpen>>", lambda e: self.after_idle(self.on_open_close))
        self.tree.bind("<<TreeviewClose>>", lambda e: self.after_idle(self.on_open_close))
        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", self.on_space)

        bottom = ttk.Frame(self, padding=(10, 6, 10, 4))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Save to:").pack(side="left")
        ttk.Entry(bottom, textvariable=self.dir_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(bottom, text="Browse...", command=self.browse).pack(side="left")

        actions = ttk.Frame(self, padding=(10, 4, 10, 10))
        actions.pack(fill="x")
        ttk.Label(actions, textvariable=self.status_var).pack(side="left")
        self.dl_btn = ttk.Button(actions, text="Download", command=self.start_download, state="disabled")
        self.dl_btn.pack(side="right")

    def toggle_filter_popup(self):
        if self.filter_popup is not None:
            self.close_filter_popup()
            return
        P = self.app.pal
        pop = tk.Toplevel(self.winfo_toplevel())
        pop.withdraw()
        pop.overrideredirect(True)
        pop.transient(self.winfo_toplevel())
        pop.config(bg=P["popup_border"])
        body = tk.Frame(pop, bd=0, highlightthickness=2)
        body.pack(padx=0, pady=0)
        self.app.register_tk(body, "popup")
        self.filter_popup = pop

        cols = 2
        for i, ext in enumerate(FILTER_EXTENSIONS):
            chk = BigCheck(body, self.app, ext, self.ext_vars[ext], self.on_filter_changed)
            chk.config(bg=P["field"], padx=12, pady=5)
            chk.grid(row=i // cols, column=i % cols, sticky="we", padx=6, pady=1)
            self.app.tk_widgets[-1] = (chk, "popup_label")
        done = ttk.Button(body, text="Done", command=self.close_filter_popup)
        done.grid(row=(len(FILTER_EXTENSIONS) + cols - 1) // cols, column=0, columnspan=cols,
                  sticky="e", padx=10, pady=8)

        self.app.recolor_tk()
        pop.update_idletasks()
        x = self.type_btn.winfo_rootx()
        y = self.type_btn.winfo_rooty() + self.type_btn.winfo_height()
        pop.geometry(f"+{x}+{y}")
        pop.deiconify()
        pop.lift()
        pop.focus_force()
        pop.bind("<FocusOut>", self.on_popup_focus_out)
        pop.bind("<Escape>", lambda e: self.close_filter_popup())

    def on_popup_focus_out(self, event=None):
        self.after(120, self.check_popup_focus)

    def check_popup_focus(self):
        pop = self.filter_popup
        if pop is None:
            return
        try:
            focus = pop.focus_displayof()
        except tk.TclError:
            focus = None
        if focus is None or not str(focus).startswith(str(pop)):
            # clicked the button itself? let its command handle the toggle
            self.close_filter_popup()

    def close_filter_popup(self):
        pop = self.filter_popup
        self.filter_popup = None
        if pop is not None:
            self.app.tk_widgets = [(w, k) for (w, k) in self.app.tk_widgets if str(w).startswith(str(pop)) is False]
            try:
                pop.destroy()
            except tk.TclError:
                pass

    def on_filter_changed(self):
        n = sum(1 for v in self.ext_vars.values() if v.get())
        self.type_btn.config(text=f"File types ({n}) ▾" if n else "File types ▾")
        self.apply_filter(quiet=True)

    def active_extensions(self):
        exts = {e for e, v in self.ext_vars.items() if v.get()}
        return exts | parse_extensions(self.custom_ext_var.get())

    def apply_filter(self, quiet=False):
        """Tick only the files whose extension matches the chosen types."""
        exts = self.active_extensions()
        if not self.items:
            if not quiet:
                self.status_var.set("Load a file list first.")
            return
        if not exts:
            if not quiet:
                self.status_var.set("Pick at least one file type (or type extensions) to filter by.")
            return
        suffixes = tuple("." + e for e in exts)
        count = 0
        for iid, info in self.items.items():
            match = info["name"].lower().endswith(suffixes)
            self.set_checked(iid, match)
            count += match
        self.after_change()
        self.status_var.set(f"Filter selected {count} file(s): {', '.join(sorted(exts))}")

    def clear_filter(self):
        for v in self.ext_vars.values():
            v.set(False)
        self.custom_ext_var.set("")
        self.type_btn.config(text="File types ▾")
        for iid in self.items:
            self.set_checked(iid, True)
        self.after_change()
        if self.items:
            self.status_var.set("Filter cleared - all files selected.")

    def set_checked(self, iid, value):
        self.items[iid]["checked"] = value
        self.tree.item(iid, image=self.app.row_imgs[("file", "on" if value else "off")])

    def toggle_item(self, iid):
        """Tick/untick a file, or a whole folder (everything inside it)."""
        if iid in self.folders:
            files = self.folders[iid]["files"]
            new_value = not all(self.items[i]["checked"] for i in files)
            for i in files:
                self.set_checked(i, new_value)
        elif iid in self.items:
            self.set_checked(iid, not self.items[iid]["checked"])
        else:
            return
        self.after_change()

    def after_change(self):
        self.refresh_folders()
        self.sync_select_all()
        self.update_count()

    def refresh_folders(self):
        for fid, info in self.folders.items():
            states = [self.items[i]["checked"] for i in info["files"]]
            state = "on" if all(states) else ("part" if any(states) else "off")
            kind = "open" if self.is_open(fid) else "closed"
            self.tree.item(fid, image=self.app.row_imgs[(kind, state)])

    def is_open(self, iid):
        return str(self.tree.item(iid, "open")) in ("1", "true", "True")

    def on_open_close(self):
        self.refresh_folders()
        self.restripe()

    def on_click(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        if self.tree.identify_column(event.x) != "#0":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        try:
            element = self.tree.identify("element", event.x, event.y)
        except tk.TclError:
            element = "image"
        if "image" not in element:   # clicked the name: normal behaviour (select / double-click)
            return
        # Find where the picture starts, to tell the +/- button from the checkbox.
        left = event.x
        while left > 0 and "image" in self.tree.identify("element", left - 1, event.y):
            left -= 1
        if iid in self.folders and event.x - left < self.app.box + self.app.gap // 2:
            self.tree.item(iid, open=not self.is_open(iid))   # the +/- button
            self.on_open_close()
        else:
            self.toggle_item(iid)                             # the checkbox
        return "break"

    def on_space(self, event):
        iid = self.tree.focus()
        if iid:
            self.toggle_item(iid)
            return "break"

    def restripe(self):
        """Alternate row shading down the rows that are currently visible."""
        counter = [0]

        def walk(parent):
            for iid in self.tree.get_children(parent):
                self.tree.item(iid, tags=("odd" if counter[0] % 2 else "even",))
                counter[0] += 1
                if str(self.tree.item(iid, "open")) in ("1", "true", "True"):
                    walk(iid)

        walk("")

    def set_all_open(self, value):
        for fid in self.folders:
            self.tree.item(fid, open=value)
        self.on_open_close()

    def toggle_all(self):
        value = self.select_all_var.get()
        for iid in self.items:
            self.set_checked(iid, value)
        self.refresh_folders()
        self.update_count()

    def sync_select_all(self):
        self.select_all_var.set(bool(self.items) and all(i["checked"] for i in self.items.values()))

    def update_count(self):
        sel = [i for i in self.items.values() if i["checked"]]
        total = sum(max(i["size"], 0) for i in sel)
        self.count_label.config(text=f"{len(sel)} of {len(self.items)} selected ({human_size(total)})")

    def browse(self):
        start = self.dir_var.get()
        while start and not os.path.isdir(start):
            start = os.path.dirname(start)
        chosen = filedialog.askdirectory(title="Choose where to save the files", initialdir=start or None)
        if chosen:
            self.dir_var.set(os.path.normpath(chosen))
            self.save_dir_custom = True

    # ---- Loading the file list (runs in the background) --------------
    def load(self):
        """Subclasses check the address and call start_job()."""
        raise NotImplementedError

    def load_or_stop(self):
        if self.loading:
            self.cancel_flag.set()
            self.load_btn.config(state="disabled", text="Stopping...")
        else:
            self.load()

    def start_job(self, label, work):
        """Run work(report, cancelled) -> (files, note) in a thread."""
        self.job_q = queue.Queue()
        self.cancel_flag = threading.Event()
        q, flag = self.job_q, self.cancel_flag

        def runner():
            try:
                files, note = work(lambda msg: q.put(("status", msg)), flag.is_set)
                q.put(("done", files, note, None))
            except Exception as exc:
                q.put(("done", None, "", exc))

        self.loading = True
        self.load_btn.config(text="Stop")
        self.dl_btn.config(state="disabled")
        self.status_var.set(label)
        threading.Thread(target=runner, daemon=True).start()
        self.after(100, self.poll_job)

    def poll_job(self):
        try:
            while True:
                ev = self.job_q.get_nowait()
                if ev[0] == "status":
                    self.status_var.set(ev[1])
                else:
                    self.finish_job(ev[1], ev[2], ev[3])
                    return
        except queue.Empty:
            pass
        self.after(100, self.poll_job)

    def finish_job(self, files, note, error):
        self.loading = False
        self.load_btn.config(text="Load", state="normal")
        if error is not None:
            self.status_var.set("Load failed.")
            self.dl_btn.config(state="normal" if self.items else "disabled")
            messagebox.showerror(APP_TITLE, f"Could not load '{self.pending_id}':\n\n{error}")
            return
        self.show_files(files, note)

    def show_files(self, files, note=""):
        self.source_id = self.pending_id
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.folders.clear()
        folder_iids = {}   # "music/disc1" -> tree id

        def sort_key(f):
            parts = [p for p in f["name"].split("/") if p]
            # folders before files at every level
            return tuple((1 if i == len(parts) - 1 else 0, p.lower()) for i, p in enumerate(parts))

        for f in sorted(files, key=sort_key):
            parts = [p for p in f["name"].split("/") if p]
            if not parts:
                continue
            parent, path, ancestors = "", "", []
            for part in parts[:-1]:
                path = f"{path}/{part}" if path else part
                fid = folder_iids.get(path)
                if fid is None:
                    fid = self.tree.insert(
                        parent, "end", text=f"  {part}/", image=self.app.row_imgs[("open", "on")],
                        open=True, values=("", ""),
                    )
                    folder_iids[path] = fid
                    self.folders[fid] = {"path": path, "files": [], "size": 0}
                ancestors.append(fid)
                parent = fid
            iid = self.tree.insert(
                parent, "end", text="  " + parts[-1], image=self.app.row_imgs[("file", "on")],
                values=(("~" if f["size"] >= 0 and not f.get("exact", True) else "") + human_size(f["size"]),
                        f["source"] or ""),
            )
            self.items[iid] = {**f, "checked": True}
            for fid in ancestors:
                self.folders[fid]["files"].append(iid)
                self.folders[fid]["size"] += max(f["size"], 0)
        for fid, info in self.folders.items():
            self.tree.set(fid, "size", human_size(info["size"]))
        self.restripe()

        self.select_all_var.set(True)
        if not self.save_dir_custom:
            self.dir_var.set(default_save_dir(self.source_id))
        self.dl_btn.config(state="normal" if self.items else "disabled")
        msg = f"Loaded {len(self.items)} files from '{self.source_id}'."
        if not self.items:
            msg = "No files found there."
        if note:
            msg += f" ({note})"
        self.status_var.set(msg)
        self.update_count()
        if self.active_extensions():
            self.apply_filter(quiet=True)

    # ---- Download -----------------------------------------------------
    def start_download(self):
        if self.app.downloading:
            messagebox.showinfo(APP_TITLE, "A download is already running. Wait for it to finish or cancel it first.")
            return
        if self.app.updating:
            messagebox.showinfo(APP_TITLE, "An update is waiting. Answer the update window first.")
            return
        selected = [
            {"name": i["name"], "size": i["size"], "url": i["url"], "exact": i.get("exact", True)}
            for i in self.items.values() if i["checked"]
        ]
        if not selected:
            messagebox.showinfo(APP_TITLE, "Tick at least one file to download.")
            return
        dest = self.dir_var.get().strip()
        if not dest:
            messagebox.showerror(APP_TITLE, "Choose a save folder first.")
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Can't create the save folder:\n{exc}")
            return

        events = queue.Queue()
        worker = Downloader(selected, dest, events)
        self.dl_btn.config(state="disabled")
        self.load_btn.config(state="disabled")
        ProgressWindow(self.winfo_toplevel(), self.app, worker, events, self.download_closed)
        worker.start()

    def download_closed(self):
        self.dl_btn.config(state="normal" if self.items else "disabled")
        self.load_btn.config(state="normal")


class ArchivePanel(SourcePanel):
    SOURCE_LABEL = "Archive URL:"
    PROMPT = "Paste an archive.org URL and click Load."

    def build_ui(self):
        super().build_ui()
        self.url_entry.focus_set()

    def load(self):
        ident = parse_identifier(self.url_var.get())
        if not ident:
            messagebox.showerror(
                APP_TITLE,
                "That doesn't look like an archive.org item URL.\n\n"
                "Example: https://archive.org/details/some-item-name",
            )
            return
        self.pending_id = ident
        self.start_job(f"Loading '{ident}'...", lambda report, cancelled: (fetch_archive_files(ident), ""))


class OpenDirPanel(SourcePanel):
    SOURCE_LABEL = "Directory URL:"
    PROMPT = "Paste the address of an open directory (an 'Index of /...' page) and click Load."

    def build_extra_row(self):
        self.recurse_var = tk.BooleanVar(value=True)
        self.sizes_var = tk.BooleanVar(value=False)
        row = ttk.Frame(self, padding=(10, 4))
        row.pack(fill="x")
        BigCheck(row, self.app, "Include subfolders", self.recurse_var).pack(side="left")
        BigCheck(row, self.app, "Look up missing file sizes (slower)", self.sizes_var).pack(side="left", padx=(30, 0))

    def load(self):
        url = normalize_dir_url(self.url_var.get())
        if not url:
            messagebox.showerror(
                APP_TITLE,
                "That doesn't look like a web address.\n\n"
                "Example: https://example.com/files/music/",
            )
            return
        self.url_var.set(url)
        self.pending_id = folder_name_for_url(url)
        recurse, sizes = self.recurse_var.get(), self.sizes_var.get()
        self.start_job(
            "Scanning...",
            lambda report, cancelled: crawl_open_directory(url, recurse, sizes, report, cancelled),
        )


if __name__ == "__main__":
    App().mainloop()
