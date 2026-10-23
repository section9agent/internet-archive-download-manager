#!/usr/bin/env python3
"""
Archive.org Downloader
----------------------
Paste an archive.org item URL (or just the item name), load the file list,
tick the files you want, choose a save folder and download them.

Requires only Python 3.8+ with tkinter (included with the standard installers
on Windows and macOS; on Linux install the python3-tk package).
"""

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Archive.org Downloader"
USER_AGENT = "ArchiveDownloader/1.0 (personal tool)"
CHUNK = 256 * 1024
RETRIES = 3

# Change this one number to make ALL text and checkboxes bigger or smaller.
BASE_FONT_SIZE = 15

CHECKED = "\u2611"    # ☑
UNCHECKED = "\u2610"  # ☐


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
    if "://" not in text and "/" not in text and "." not in text.split("?")[0][:0]:
        # Bare identifier such as "my-cool-item"
        return text
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
        os.makedirs(os.path.dirname(dest), exist_ok=True)
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
# Progress window
# --------------------------------------------------------------------------
class ProgressWindow(tk.Toplevel):
    def __init__(self, parent, downloader, events, on_close):
        super().__init__(parent)
        self.title("Downloading...")
        self.resizable(False, False)
        self.transient(parent)
        self.downloader = downloader
        self.events = events
        self.on_close = on_close
        self.finished = False

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
        self.grab_set()
        self.after(100, self.poll)

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
            shown = name if len(name) <= 60 else "..." + name[-57:]
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
        self.geometry("1150x750")
        self.minsize(900, 550)
        self.setup_fonts()

        self.identifier = None
        self.items = {}          # file iid -> {"name","size","checked","source","format"}
        self.folders = {}        # folder iid -> {"path","files":[file iids],"size"}
        self.save_dir_custom = False
        self.loading = False

        self.url_var = tk.StringVar()
        self.dir_var = tk.StringVar()
        self.select_all_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Paste an archive.org URL and click Load.")

        self.build_ui()

    # ---- UI -----------------------------------------------------------
    def setup_fonts(self):
        size = BASE_FONT_SIZE
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        style = ttk.Style(self)
        box = int(size * 2.2)  # checkbox size in pixels
        style.configure("Treeview", font=(family, size), rowheight=max(int(size * 2.6), box + 10))
        style.configure("Treeview.Heading", font=(family, size, "bold"))
        style.configure("TButton", padding=(18, 8))
        style.configure("TEntry", padding=4)
        style.configure("Treeview", indent=int(size * 1.8))

        # Workaround so row colours (striping) show on all Tk versions.
        def fixed_map(option):
            return [e for e in style.map("Treeview", query_opt=option)
                    if e[:2] != ("!disabled", "!selected")]

        style.map("Treeview", foreground=fixed_map("foreground"), background=fixed_map("background"))
        self.big_font = (family, size)
        self.check_font = (family, int(size * 1.4))
        self.make_check_images(box)

    def make_check_images(self, s):
        """Draw big checkbox pictures (a normal checkbox can't be resized)."""
        border = max(3, s // 9)

        def blank():
            img = tk.PhotoImage(master=self, width=s, height=s)
            img.put("#222222", to=(0, 0, s, s))                                   # border
            img.put("#ffffff", to=(border, border, s - border, s - border))       # inside
            return img

        self.img_off = blank()
        self.img_on = blank()
        self.img_on.put("#1a73e8", to=(border, border, s - border, s - border))  # blue fill

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
                self.img_on.put("#ffffff", to=(x, y, min(x + t, s - border), min(y + t, s - border)))

        stroke(x0, y0, x1, y1)
        stroke(x1, y1, x2, y2)

        # "partly ticked" box for folders: blue with a white dash
        self.img_part = blank()
        self.img_part.put("#1a73e8", to=(border, border, s - border, s - border))
        self.img_part.put("#ffffff", to=(int(s * 0.25), int(s * 0.42), int(s * 0.75), int(s * 0.58)))

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

        mid = ttk.Frame(self, padding=(10, 4))
        mid.pack(fill="both", expand=True)

        bar = ttk.Frame(mid)
        bar.pack(fill="x", pady=(0, 4))
        # Big clickable "Select all" box (the native checkbox can't be enlarged).
        self.select_all_label = tk.Label(bar, cursor="hand2", font=self.big_font, compound="left")
        self.select_all_label.pack(side="left")
        self.select_all_label.bind("<Button-1>", self.on_select_all_click)
        self.select_all_var.trace_add("write", self.refresh_select_all_label)
        self.refresh_select_all_label()
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
        # Alternating row shading
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#e6eefb")
        self.tree.bind("<<TreeviewOpen>>", lambda e: self.after_idle(self.restripe))
        self.tree.bind("<<TreeviewClose>>", lambda e: self.after_idle(self.restripe))
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
        for f in sorted(files, key=lambda x: x["name"].lower()):
            parts = [p for p in f["name"].split("/") if p]
            if not parts:
                continue
            parent, path, ancestors = "", "", []
            for part in parts[:-1]:
                path = f"{path}/{part}" if path else part
                fid = folder_iids.get(path)
                if fid is None:
                    fid = self.tree.insert(
                        parent, "end", text=f"  {part}/", image=self.img_on,
                        open=True, values=("", ""),
                    )
                    folder_iids[path] = fid
                    self.folders[fid] = {"path": path, "files": [], "size": 0}
                ancestors.append(fid)
                parent = fid
            iid = self.tree.insert(
                parent, "end", text="  " + parts[-1], image=self.img_on,
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
        self.dl_btn.config(state="normal" if files else "disabled")
        self.status_var.set(f"Loaded {len(files)} files from '{ident}'.")
        self.update_count()

    # ---- Checkbox handling -------------------------------------------
    def set_checked(self, iid, value):
        self.items[iid]["checked"] = value
        self.tree.item(iid, image=self.img_on if value else self.img_off)

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
            if all(states):
                img = self.img_on
            elif any(states):
                img = self.img_part
            else:
                img = self.img_off
            self.tree.item(fid, image=img)

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
        if "image" not in element:   # clicked the name or the expand arrow: normal behaviour
            return
        self.toggle_item(iid)
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
                if self.tree.item(iid, "open") in (1, True, "1", "true", "True"):
                    walk(iid)

        walk("")

    def set_all_open(self, value):
        for fid in self.folders:
            self.tree.item(fid, open=value)
        self.restripe()

    def refresh_select_all_label(self, *args):
        img = self.img_on if self.select_all_var.get() else self.img_off
        self.select_all_label.config(image=img, text="  Select all")

    def on_select_all_click(self, event=None):
        self.select_all_var.set(not self.select_all_var.get())
        self.toggle_all()

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
        ProgressWindow(self, worker, events, self.download_closed)
        worker.start()

    def download_closed(self):
        self.dl_btn.config(state="normal")
        self.load_btn.config(state="normal")


if __name__ == "__main__":
    App().mainloop()
