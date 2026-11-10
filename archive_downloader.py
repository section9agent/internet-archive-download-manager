#!/usr/bin/env python3
"""
Internet Archive Download Manager
---------------------------------
Paste an archive.org item URL (or just the item name), load the file list,
tick the files you want (or filter by file type), choose a save folder and
download them. Folder structure inside the archive is preserved.

Requires only Python 3.8+ with tkinter (included with the standard installers
on Windows and macOS; on Linux install the python3-tk package).
"""

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Internet Archive Download Manager"
USER_AGENT = "InternetArchiveDownloadManager/1.0 (personal tool)"
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
    target = os.path.normpath(os.path.join(base, *rel.split("/")))
    base_abs = os.path.abspath(base)
    if os.path.commonpath([base_abs, os.path.abspath(target)]) != base_abs:
        raise ValueError(f"Unsafe path in archive listing: {relative}")
    return target


def default_save_dir(identifier):
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    root = downloads if os.path.isdir(downloads) else os.path.expanduser("~")
    safe = re.sub(r'[<>:"/\\|?*]', "_", identifier)
    return os.path.join(root, safe)


def open_url(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
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
    def __init__(self, identifier, files, dest_dir, events):
        super().__init__(daemon=True)
        self.identifier = identifier
        self.files = files            # list of dicts: name, size
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
                self.events.put(("file", name, idx, count, size))

                try:
                    dest = safe_join(self.dest_dir, name)
                except ValueError as exc:
                    failed.append((name, str(exc)))
                    done_bytes += max(size, 0)
                    continue

                # Already complete?
                if os.path.isfile(dest) and size > 0 and os.path.getsize(dest) == size:
                    skipped += 1
                    done_bytes += size
                    self.events.put(("bytes", size, size, done_bytes, total_bytes, 0.0, None))
                    continue

                last_err = None
                for attempt in range(1, RETRIES + 1):
                    try:
                        got = self._download_one(
                            name, dest, size, done_bytes, total_bytes, start, session_bytes
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

    def _download_one(self, name, dest, size, done_before, total_bytes, start, session_before):
        os.makedirs(os.path.dirname(dest), exist_ok=True)   # keeps the archive's folder structure
        part = dest + ".part"
        url = (
            f"https://archive.org/download/{urllib.parse.quote(self.identifier)}/"
            f"{urllib.parse.quote(name)}"
        )

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
            file_total = size if size > 0 else (int(length) + offset if length else 0)

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

        if size > 0 and written != size:
            raise IOError(f"Incomplete download ({written} of {size} bytes)")
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
        self.transient(parent)
        self.app = app
        self.downloader = downloader
        self.events = events
        self.on_close = on_close
        self.finished = False
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
        self.grab_set()
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
        self.grab_release()
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
        elif kind == "bytes":
            _, f_done, f_total, o_done, o_total, speed, remaining = ev
            if f_total > 0:
                self.file_bar["value"] = min(100, f_done * 100 / f_total)
                self.file_detail.config(text=f"{human_size(f_done)} of {human_size(f_total)}")
            else:
                self.file_detail.config(text=f"{human_size(f_done)}")
            if o_total > 0:
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
# Main window
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1250x800")
        self.minsize(1000, 600)

        self.config_data = load_config()
        self.dark = bool(self.config_data.get("dark_mode", False))
        self.pal = PALETTES["dark" if self.dark else "light"]
        self.tk_widgets = []     # plain tk widgets that need recolouring: (widget, kind)
        self.filter_popup = None

        self.set_window_icon()
        self.setup_fonts()

        self.identifier = None
        self.items = {}          # file iid -> {"name","size","checked","source","format"}
        self.folders = {}        # folder iid -> {"path","files":[file iids],"size"}
        self.save_dir_custom = False
        self.loading = False

        self.url_var = tk.StringVar()
        self.dir_var = tk.StringVar()
        self.custom_ext_var = tk.StringVar()
        self.select_all_var = tk.BooleanVar(value=True)
        self.dark_var = tk.BooleanVar(value=self.dark)
        self.ext_vars = {e: tk.BooleanVar(value=False) for e in FILTER_EXTENSIONS}
        self.status_var = tk.StringVar(value="Paste an archive.org URL and click Load.")

        self.build_ui()
        self.apply_theme()

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

        self.tree.tag_configure("even", background=P["row_even"], foreground=P["fg"])
        self.tree.tag_configure("odd", background=P["row_odd"], foreground=P["fg"])

        self.recolor_tk()
        set_titlebar_dark(self, self.dark)

    def recolor_tk(self):
        """Recolour the plain tk widgets (ttk ones follow the style automatically)."""
        P = self.pal
        for widget, kind in self.tk_widgets:
            try:
                if kind == "label":
                    widget.config(bg=P["bg"], fg=P["fg"], activebackground=P["bg"])
                elif kind == "popup":
                    widget.config(bg=P["field"], highlightbackground=P["popup_border"],
                                  highlightcolor=P["popup_border"])
                elif kind == "popup_label":
                    widget.config(bg=P["field"], fg=P["fg"], activebackground=P["field"])
            except tk.TclError:
                pass

    # ---- UI -----------------------------------------------------------
    def build_ui(self):
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Archive URL:").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.load())
        entry.focus_set()
        self.load_btn = ttk.Button(top, text="Load", command=self.load)
        self.load_btn.pack(side="left")
        BigCheck(top, self, "Dark mode", self.dark_var, self.toggle_dark).pack(side="left", padx=(20, 0))

        # ---- Filter row ----
        frow = ttk.Frame(self, padding=(10, 4))
        frow.pack(fill="x")
        ttk.Label(frow, text="Select by type:").pack(side="left")
        self.type_btn = ttk.Button(frow, text="File types ▾", command=self.toggle_filter_popup)
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
        BigCheck(bar, self, "Select all", self.select_all_var, self.toggle_all).pack(side="left")
        ttk.Button(bar, text="Expand all", command=lambda: self.set_all_open(True)).pack(side="left", padx=(28, 4))
        ttk.Button(bar, text="Collapse all", command=lambda: self.set_all_open(False)).pack(side="left", padx=4)
        self.count_label = ttk.Label(bar, text="")
        self.count_label.pack(side="right")

        cols = ("size", "source")
        # Checkbox pictures, folder nesting and names live in the tree's first column (#0).
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="browse")
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

    # ---- File-type filter ---------------------------------------------
    def toggle_filter_popup(self):
        if self.filter_popup is not None:
            self.close_filter_popup()
            return
        P = self.pal
        pop = tk.Toplevel(self)
        pop.withdraw()
        pop.overrideredirect(True)
        pop.transient(self)
        pop.config(bg=P["popup_border"])
        body = tk.Frame(pop, bd=0, highlightthickness=2)
        body.pack(padx=0, pady=0)
        self.register_tk(body, "popup")
        self.filter_popup = pop

        cols = 2
        for i, ext in enumerate(FILTER_EXTENSIONS):
            chk = BigCheck(body, self, ext, self.ext_vars[ext], self.on_filter_changed)
            chk.config(bg=P["field"], padx=12, pady=5)
            chk.grid(row=i // cols, column=i % cols, sticky="we", padx=6, pady=1)
            self.tk_widgets[-1] = (chk, "popup_label")
        done = ttk.Button(body, text="Done", command=self.close_filter_popup)
        done.grid(row=(len(FILTER_EXTENSIONS) + cols - 1) // cols, column=0, columnspan=cols,
                  sticky="e", padx=10, pady=8)

        self.recolor_tk()
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
            self.tk_widgets = [(w, k) for (w, k) in self.tk_widgets if str(w).startswith(str(pop)) is False]
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
                self.status_var.set("Load an archive first.")
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

    # ---- Loading file list -------------------------------------------
    def load(self):
        if self.loading:
            return
        ident = parse_identifier(self.url_var.get())
        if not ident:
            messagebox.showerror(
                APP_TITLE,
                "That doesn't look like an archive.org item URL.\n\n"
                "Example: https://archive.org/details/some-item-name",
            )
            return
        self.loading = True
        self.load_btn.config(state="disabled")
        self.dl_btn.config(state="disabled")
        self.status_var.set(f"Loading '{ident}'...")
        threading.Thread(target=self._load_worker, args=(ident,), daemon=True).start()

    def _load_worker(self, ident):
        try:
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
                    "format": f.get("format", ""),
                })
            self.after(0, self._load_done, ident, files, None)
        except Exception as exc:
            self.after(0, self._load_done, ident, None, exc)

    def _load_done(self, ident, files, error):
        self.loading = False
        self.load_btn.config(state="normal")
        if error is not None:
            self.status_var.set("Load failed.")
            messagebox.showerror(APP_TITLE, f"Could not load '{ident}':\n\n{error}")
            return

        self.identifier = ident
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
                        parent, "end", text=f"  {part}/", image=self.row_imgs[("open", "on")],
                        open=True, values=("", ""),
                    )
                    folder_iids[path] = fid
                    self.folders[fid] = {"path": path, "files": [], "size": 0}
                ancestors.append(fid)
                parent = fid
            iid = self.tree.insert(
                parent, "end", text="  " + parts[-1], image=self.row_imgs[("file", "on")],
                values=(human_size(f["size"]), f["source"] or ""),
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
            self.dir_var.set(default_save_dir(ident))
        self.dl_btn.config(state="normal" if self.items else "disabled")
        self.status_var.set(f"Loaded {len(self.items)} files from '{ident}'.")
        self.update_count()
        if self.active_extensions():
            self.apply_filter(quiet=True)

    # ---- Checkbox handling -------------------------------------------
    def set_checked(self, iid, value):
        self.items[iid]["checked"] = value
        self.tree.item(iid, image=self.row_imgs[("file", "on" if value else "off")])

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
            self.tree.item(fid, image=self.row_imgs[(kind, state)])

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
        if iid in self.folders and event.x - left < self.box + self.gap // 2:
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

    # ---- Row shading / expand-collapse --------------------------------
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

    # ---- Save folder --------------------------------------------------
    def browse(self):
        start = self.dir_var.get()
        while start and not os.path.isdir(start):
            start = os.path.dirname(start)
        chosen = filedialog.askdirectory(title="Choose where to save the files", initialdir=start or None)
        if chosen:
            self.dir_var.set(os.path.normpath(chosen))
            self.save_dir_custom = True

    # ---- Download -----------------------------------------------------
    def start_download(self):
        selected = [
            {"name": i["name"], "size": i["size"]}
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
        worker = Downloader(self.identifier, selected, dest, events)
        self.dl_btn.config(state="disabled")
        self.load_btn.config(state="disabled")
        ProgressWindow(self, self, worker, events, self.download_closed)
        worker.start()

    def download_closed(self):
        self.dl_btn.config(state="normal")
        self.load_btn.config(state="normal")


if __name__ == "__main__":
    App().mainloop()
