"""
Internet Archive Download Manager - Android app (Kivy)
------------------------------------------------------
Phone and tablet version. It uses the same engine as the desktop app (iadm_core.py):
two tabs (Internet Archive / Open Directory), a file list with folders and tick boxes,
file-type filters, dark mode, an About window, and a progress window while downloading.

Downloaded files go to the public Downloads folder, inside a folder named after the item,
with the same sub-folders as the website/archive (needs Android 10 or newer).
"""

import json
import mimetypes
import os
import queue
import threading
import webbrowser

# Android has no system certificate file Python can find, so point HTTPS at certifi's.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import (BooleanProperty, ListProperty, NumericProperty, ObjectProperty,
                             StringProperty)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.scrollview import ScrollView
from kivy.uix.widget import Widget
from kivy.utils import platform

import iadm_core as core

ON_ANDROID = platform == "android"
APK_NAME = "InternetArchiveDownloadManager-Android.apk"

PALETTES = {
    "light": dict(bg="#f3f3f3", fg="#1b1b1b", muted="#5f6368", field="#ffffff", button="#dcdcdc",
                  accent="#1a73e8", row_even="#ffffff", row_odd="#e6eefb", tick_border="#4a4f57"),
    "dark": dict(bg="#1e1f22", fg="#e8e8e8", muted="#9aa0a6", field="#2b2d31", button="#3a3c42",
                 accent="#4c8dff", row_even="#232428", row_odd="#2d2f35", tick_border="#b4b9c2"),
}


def hex_rgba(h, a=1.0):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)] + [a]


# --------------------------------------------------------------------------
# Android helpers (all safe to call on a desktop: they just do nothing / fall back)
# --------------------------------------------------------------------------
def open_external(url):
    """Open a link in the phone's browser."""
    if ON_ANDROID:
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            return
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


if ON_ANDROID:
    from android.runnable import run_on_ui_thread

    @run_on_ui_thread
    def set_keep_screen_on(flag):
        """Stop the screen sleeping while a download runs (it would pause the transfer)."""
        try:
            from jnius import autoclass
            window = autoclass("org.kivy.android.PythonActivity").mActivity.getWindow()
            (window.addFlags if flag else window.clearFlags)(128)   # FLAG_KEEP_SCREEN_ON
        except Exception:
            pass
else:
    def set_keep_screen_on(flag):
        pass


class _MediaStream:
    """Write-only stream into a MediaStore file (works like a Python file for the downloader)."""

    def __init__(self, java_stream):
        self.s = java_stream

    def write(self, data):
        self.s.write(data)

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class MediaStoreSink:
    """One file being saved into the public Downloads folder (Android 10+)."""
    offset = 0   # partly downloaded files can't be resumed this way

    def __init__(self, store, name):
        self.store, self.name, self.uri = store, name, None

    def restart(self):
        pass

    def open(self):
        self.uri, stream = self.store.create(self.name)
        return _MediaStream(stream)

    def commit(self):
        self.store.finish(self.uri)

    def abort(self):
        if self.uri is not None:
            self.store.remove(self.uri)
            self.uri = None


class MediaStoreStore:
    """Saves into Downloads/<folder>/<sub-folders>/<file> using Android's MediaStore,
    which needs no storage permission on Android 10 and newer."""

    def __init__(self, folder):
        from jnius import autoclass
        self.folder = core.safe_folder_name(folder)
        self.activity = autoclass("org.kivy.android.PythonActivity").mActivity
        self.resolver = self.activity.getContentResolver()
        self.ContentValues = autoclass("android.content.ContentValues")
        self.Integer = autoclass("java.lang.Integer")
        self.Cols = autoclass("android.provider.MediaStore$MediaColumns")
        self.collection = autoclass("android.provider.MediaStore$Downloads").EXTERNAL_CONTENT_URI

    def _where(self, name):
        parts = [p for p in name.replace("\\", "/").split("/") if p]
        if not parts or any(p in (".", "..") for p in parts):
            raise ValueError(f"Unsafe path in listing: {name}")
        parts = [core.safe_folder_name(p) for p in parts]
        rel = "Download/" + self.folder + ("/" + "/".join(parts[:-1]) if len(parts) > 1 else "")
        return rel, parts[-1]

    def is_complete(self, name, size, exact):
        rel, display = self._where(name)
        if not (exact and size > 0):
            return False
        try:
            cursor = self.resolver.query(
                self.collection, [self.Cols.SIZE],
                f"{self.Cols.RELATIVE_PATH} = ? AND {self.Cols.DISPLAY_NAME} = ?",
                [rel + "/", display], None)
            try:
                return bool(cursor.moveToFirst()) and int(cursor.getLong(0)) == size
            finally:
                cursor.close()
        except Exception:
            return False

    def begin(self, name):
        self._where(name)
        return MediaStoreSink(self, name)

    def create(self, name):
        rel, display = self._where(name)
        cv = self.ContentValues()
        cv.put(self.Cols.DISPLAY_NAME, display)
        cv.put(self.Cols.MIME_TYPE, mimetypes.guess_type(display)[0] or "application/octet-stream")
        cv.put(self.Cols.RELATIVE_PATH, rel)
        cv.put(self.Cols.IS_PENDING, self.Integer(1))
        uri = self.resolver.insert(self.collection, cv)
        if uri is None:
            raise IOError("Android would not create the file")
        return uri, self.resolver.openOutputStream(uri)

    def finish(self, uri):
        cv = self.ContentValues()
        cv.put(self.Cols.IS_PENDING, self.Integer(0))
        self.resolver.update(uri, cv, None, None)

    def remove(self, uri):
        try:
            self.resolver.delete(uri, None, None)
        except Exception:
            pass


# --------------------------------------------------------------------------
# The file tree (folders + files)
# --------------------------------------------------------------------------
class Node:
    __slots__ = ("id", "kind", "name", "depth", "children", "file", "checked", "open", "size", "files", "state")

    def __init__(self, nid, kind, name, depth, file=None):
        self.id, self.kind, self.name, self.depth = nid, kind, name, depth
        self.children, self.file, self.files = [], file, []
        self.checked, self.open, self.size, self.state = True, True, 0, "on"


def build_tree(files):
    """Group a flat list of files into folders. Returns (roots, nodes_by_id, file_nodes)."""
    def sort_key(f):
        parts = [p for p in f["name"].split("/") if p]
        return tuple((1 if i == len(parts) - 1 else 0, p.lower()) for i, p in enumerate(parts))

    roots, by_id, file_nodes, folders = [], {}, [], {}
    for f in sorted(files, key=sort_key):
        parts = [p for p in f["name"].split("/") if p]
        if not parts:
            continue
        siblings, path, ancestors = roots, "", []
        for part in parts[:-1]:
            path = f"{path}/{part}" if path else part
            folder = folders.get(path)
            if folder is None:
                folder = Node(len(by_id), "folder", part + "/", len(ancestors))
                by_id[folder.id] = folder
                folders[path] = folder
                siblings.append(folder)
            ancestors.append(folder)
            siblings = folder.children
        node = Node(len(by_id), "file", parts[-1], len(ancestors), file=f)
        by_id[node.id] = node
        siblings.append(node)
        file_nodes.append(node)
        for a in ancestors:
            a.files.append(node)
            a.size += max(f["size"], 0)
    return roots, by_id, file_nodes


# --------------------------------------------------------------------------
# Small widgets
# --------------------------------------------------------------------------
class Tick(Widget):
    """A big tick box: state is 'off', 'on' or 'part' (some of a folder's files ticked)."""
    state = StringProperty("off")

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self.redraw, size=self.redraw, state=self.redraw)
        app = App.get_running_app()
        if app is not None:
            app.bind(theme_rev=self.redraw)
        self.redraw()

    def redraw(self, *args):
        app = App.get_running_app()
        if app is None:
            return
        side = min(self.width, self.height)
        x, y = self.center_x - side / 2, self.center_y - side / 2
        b = max(dp(2), side / 10)
        self.canvas.clear()
        with self.canvas:
            Color(*app.tick_border)
            Rectangle(pos=(x, y), size=(side, side))
            Color(*(app.accent if self.state != "off" else app.field))
            Rectangle(pos=(x + b, y + b), size=(side - 2 * b, side - 2 * b))
            Color(1, 1, 1, 1)
            if self.state == "on":
                Line(points=[x + side * .22, y + side * .50, x + side * .42, y + side * .28,
                             x + side * .80, y + side * .74], width=dp(2.4), cap="round", joint="round")
            elif self.state == "part":
                Rectangle(pos=(x + side * .24, y + side * .43), size=(side * .52, side * .14))


class Expander(Label):
    """The blue + / - button in front of a folder."""
    is_folder = BooleanProperty(False)


class TickRow(BoxLayout):
    """Tick box with a label. Tapping anywhere on the row toggles it."""
    text = StringProperty("")
    checked = BooleanProperty(False)

    def __init__(self, **kw):
        self.register_event_type("on_toggle")
        super().__init__(**kw)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.opacity > 0 and not self.disabled:
            touch.grab(self)
            return True
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            if self.collide_point(*touch.pos):
                self.checked = not self.checked
                self.dispatch("on_toggle", self.checked)
            return True
        return super().on_touch_up(touch)

    def on_toggle(self, value):
        pass


class Row(RecycleDataViewBehavior, BoxLayout):
    """One line of the file list."""
    index = NumericProperty(0)
    node_id = NumericProperty(-1)
    text = StringProperty("")
    size_text = StringProperty("")
    depth = NumericProperty(0)
    is_folder = BooleanProperty(False)
    is_open = BooleanProperty(False)
    state = StringProperty("on")
    odd = BooleanProperty(False)
    panel = ObjectProperty(None, allownone=True)

    def refresh_view_attrs(self, rv, index, data):
        self.index = index
        return super().refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            touch.grab(self)
            return True
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is not self:
            return super().on_touch_up(touch)
        touch.ungrab(self)
        if not self.collide_point(*touch.pos) or self.panel is None:
            return True
        x = touch.x
        exp, tick = self.ids.exp, self.ids.tick
        if self.is_folder and exp.x - dp(8) <= x <= exp.right + dp(8):
            self.panel.toggle_open(self.node_id)
        elif not self.is_folder or tick.x - dp(8) <= x <= tick.right + dp(12):
            self.panel.toggle_check(self.node_id)
        else:
            self.panel.toggle_open(self.node_id)
        return True


class TabBtn(Button):
    active = BooleanProperty(False)


def popup_size(h_frac=0.8):
    w = min(Window.width * 0.94, dp(640))
    h = min(Window.height * h_frac, dp(900))
    return (w, h)


KV = r"""
#:import dp kivy.metrics.dp

<Lbl@Label>:
    color: app.fg
    font_size: app.fs
    halign: 'left'
    valign: 'middle'
    text_size: self.size

<Btn@Button>:
    background_normal: ''
    background_down: ''
    background_disabled_normal: ''
    background_color: app.accent if self.state == 'down' else (app.button if not self.disabled else app.field)
    color: app.fg if not self.disabled else app.muted
    font_size: app.fs
    size_hint_y: None
    height: dp(52)

<Inp@TextInput>:
    multiline: False
    size_hint_y: None
    height: dp(52)
    font_size: app.fs
    background_normal: ''
    background_active: ''
    background_color: app.field
    foreground_color: app.fg
    hint_text_color: app.muted
    cursor_color: app.accent
    padding: [dp(12), max(0, (self.height - self.line_height) / 2)]
    write_tab: False

<TabBtn>:
    background_normal: ''
    background_down: ''
    background_color: app.accent if self.active else app.button
    color: 'white' if self.active else app.fg
    font_size: app.fs
    bold: True
    size_hint_y: None
    height: dp(52)

<Expander>:
    color: 'white'
    font_size: app.fs
    bold: True
    canvas.before:
        Color:
            rgba: app.accent if self.is_folder else (0, 0, 0, 0)
        Rectangle:
            pos: self.pos
            size: self.size

<TickRow>:
    orientation: 'horizontal'
    size_hint_y: None
    height: dp(48)
    spacing: dp(10)
    Tick:
        size_hint: None, None
        size: dp(30), dp(30)
        pos_hint: {'center_y': .5}
        state: 'on' if root.checked else 'off'
    Lbl:
        text: root.text

<Row>:
    size_hint_y: None
    height: dp(56)
    orientation: 'horizontal'
    padding: dp(6), 0, dp(10), 0
    spacing: dp(6)
    canvas.before:
        Color:
            rgba: app.row_odd if root.odd else app.row_even
        Rectangle:
            pos: self.pos
            size: self.size
    Widget:
        size_hint_x: None
        width: root.depth * dp(20)
    Expander:
        id: exp
        size_hint: None, None
        size: dp(34), dp(34)
        pos_hint: {'center_y': .5}
        is_folder: root.is_folder
        text: ('-' if root.is_open else '+') if root.is_folder else ''
    Tick:
        id: tick
        size_hint: None, None
        size: dp(30), dp(30)
        pos_hint: {'center_y': .5}
        state: root.state
    Lbl:
        text: root.text
        shorten: True
        shorten_from: 'right'
    Lbl:
        size_hint_x: None
        width: dp(84)
        halign: 'right'
        text: root.size_text
        color: app.muted
        font_size: app.fs * .8

<Panel>:
    orientation: 'vertical'
    padding: dp(8)
    spacing: dp(6)
    BoxLayout:
        size_hint_y: None
        height: dp(52)
        spacing: dp(6)
        Inp:
            id: url
            hint_text: root.hint
            input_type: 'url'
            keyboard_suggestions: False
            on_text_validate: root.load_or_stop()
        Btn:
            size_hint_x: None
            width: dp(96)
            text: root.load_text
            on_release: root.load_or_stop()
    BoxLayout:
        id: options
        orientation: 'vertical'
        size_hint_y: None
        height: self.minimum_height
    BoxLayout:
        size_hint_y: None
        height: dp(52)
        spacing: dp(6)
        Btn:
            text: root.types_text
            on_release: root.open_types()
        Btn:
            size_hint_x: .75
            text: 'Expand'
            on_release: root.set_all_open(True)
        Btn:
            size_hint_x: .8
            text: 'Collapse'
            on_release: root.set_all_open(False)
    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(6)
        TickRow:
            id: sel_all
            size_hint_x: .5
            text: 'Select all'
            checked: root.all_checked
            on_toggle: root.toggle_all(args[1])
        Lbl:
            text: root.count_text
            halign: 'right'
            font_size: app.fs * .85
            color: app.muted
    RecycleView:
        id: rv
        viewclass: 'Row'
        bar_width: dp(8)
        scroll_type: ['bars', 'content']
        canvas.before:
            Color:
                rgba: app.field
            Rectangle:
                pos: self.pos
                size: self.size
        RecycleBoxLayout:
            default_size: None, dp(56)
            default_size_hint: 1, None
            size_hint_y: None
            height: self.minimum_height
            orientation: 'vertical'
    BoxLayout:
        size_hint_y: None
        height: dp(52)
        spacing: dp(6)
        Lbl:
            text: 'Save to: ' + root.save_text
            shorten: True
            shorten_from: 'left'
        Btn:
            size_hint_x: None
            width: dp(110)
            text: 'Change'
            on_release: root.change_save()
    BoxLayout:
        size_hint_y: None
        height: dp(56)
        spacing: dp(6)
        Lbl:
            text: root.status_text
            font_size: app.fs * .85
            shorten: True
            shorten_from: 'right'
        Btn:
            size_hint_x: None
            width: dp(150)
            height: dp(56)
            text: 'Download'
            disabled: not root.can_download
            on_release: root.start_download()

<Root>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: app.bg
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        size_hint_y: None
        height: dp(56)
        padding: dp(12), dp(2), dp(8), dp(2)
        spacing: dp(8)
        Lbl:
            text: 'Internet Archive Download Manager'
            bold: True
            shorten: True
        Btn:
            size_hint_x: None
            width: dp(96)
            text: 'Menu'
            on_release: app.open_menu()
    BoxLayout:
        size_hint_y: None
        height: dp(52)
        padding: dp(8), 0
        spacing: dp(6)
        TabBtn:
            text: 'Internet Archive'
            active: root.current == 'archive'
            on_release: root.select('archive')
        TabBtn:
            text: 'Open Directory'
            active: root.current == 'dir'
            on_release: root.select('dir')
    BoxLayout:
        id: body
"""


class Panel(BoxLayout):
    """One tab: address box, filters, file list, save folder, download button."""
    hint = StringProperty("")
    load_text = StringProperty("Load")
    types_text = StringProperty("File types")
    all_checked = BooleanProperty(True)
    count_text = StringProperty("")
    save_text = StringProperty("")
    status_text = StringProperty("")
    can_download = BooleanProperty(False)

    def __init__(self, kind, **kw):
        super().__init__(**kw)
        self.kind = kind
        self.app = App.get_running_app()
        self.roots, self.by_id, self.file_nodes = [], {}, []
        self.source_id = None
        self.pending_id = None
        self.save_custom = False
        self.loading = False
        self.job_q = None
        self.cancel_flag = threading.Event()
        self.ext_on = {e: False for e in core.FILTER_EXTENSIONS}
        self.custom_ext = ""
        if kind == "archive":
            self.hint = "archive.org link or item name"
            self.status_text = "Paste an archive.org link and tap Load."
        else:
            self.hint = "https://example.com/files/"
            self.status_text = "Paste an open directory address and tap Load."
            self.recurse = TickRow(text="Include subfolders", checked=True)
            self.sizes = TickRow(text="Look up missing file sizes (slower)", checked=False)
            self.ids.options.add_widget(self.recurse)
            self.ids.options.add_widget(self.sizes)

    # ---- address / loading ------------------------------------------------
    @property
    def url_text(self):
        return self.ids.url.text.strip()

    def load_or_stop(self):
        Window.release_all_keyboards()
        if self.loading:
            self.cancel_flag.set()
            self.load_text = "..."
        else:
            self.load()

    def load(self):
        if self.kind == "archive":
            ident = core.parse_identifier(self.url_text)
            if not ident:
                self.app.message("Not an archive link",
                                 "That doesn't look like an archive.org item link.\n\n"
                                 "Example: https://archive.org/details/some-item-name")
                return
            self.pending_id = ident
            self.start_job(f"Loading '{ident}'...", lambda report, cancelled: (core.fetch_archive_files(ident), ""))
        else:
            url = core.normalize_dir_url(self.url_text)
            if not url:
                self.app.message("Not a web address",
                                 "That doesn't look like a web address.\n\nExample: https://example.com/files/music/")
                return
            self.ids.url.text = url
            self.pending_id = core.folder_name_for_url(url)
            recurse, sizes = self.recurse.checked, self.sizes.checked
            self.start_job("Scanning...", lambda report, cancelled:
                           core.crawl_open_directory(url, recurse, sizes, report, cancelled))

    def start_job(self, label, work):
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
        self.load_text = "Stop"
        self.can_download = False
        self.status_text = label
        threading.Thread(target=runner, daemon=True).start()
        Clock.schedule_interval(self.poll_job, 0.1)

    def poll_job(self, dt):
        try:
            while True:
                ev = self.job_q.get_nowait()
                if ev[0] == "status":
                    self.status_text = ev[1]
                else:
                    self.finish_job(ev[1], ev[2], ev[3])
                    return False
        except queue.Empty:
            pass
        return True

    def finish_job(self, files, note, error):
        self.loading = False
        self.load_text = "Load"
        if error is not None:
            self.status_text = "Load failed."
            self.can_download = bool(self.file_nodes)
            self.app.message("Couldn't load", f"Could not load '{self.pending_id}':\n\n{error}")
            return
        self.show_files(files, note)

    def show_files(self, files, note=""):
        self.source_id = self.pending_id
        self.roots, self.by_id, self.file_nodes = build_tree(files)
        if not self.save_custom:
            self.set_default_save()
        msg = f"Loaded {len(self.file_nodes)} files from '{self.source_id}'." if self.file_nodes else "No files found there."
        if note:
            msg += f" ({note})"
        self.status_text = msg
        if self.active_extensions():
            self.apply_filter(quiet=True)
        self.refresh()

    # ---- list ---------------------------------------------------------------
    def refresh(self):
        """Recompute folder tick states and rebuild what the list shows."""
        for n in self.by_id.values():
            if n.kind == "folder":
                n.state = "off"
        for n in self.by_id.values():
            if n.kind == "folder":
                total = len(n.files)
                on = sum(1 for f in n.files if f.checked)
                n.state = "on" if on == total else ("part" if on else "off")
            else:
                n.state = "on" if n.checked else "off"
        rows, stack, i = [], list(reversed(self.roots)), 0
        while stack:
            n = stack.pop()
            is_folder = n.kind == "folder"
            rows.append({
                "node_id": n.id, "text": n.name, "depth": n.depth, "is_folder": is_folder,
                "is_open": n.open, "state": n.state, "odd": i % 2 == 1, "panel": self,
                "size_text": core.human_size(n.size if is_folder else n.file["size"]),
            })
            i += 1
            if is_folder and n.open:
                stack.extend(reversed(n.children))
        self.ids.rv.data = rows
        sel = [f for f in self.file_nodes if f.checked]
        self.all_checked = bool(self.file_nodes) and len(sel) == len(self.file_nodes)
        total = sum(max(f.file["size"], 0) for f in sel)
        self.count_text = f"{len(sel)} of {len(self.file_nodes)} selected ({core.human_size(total)})"
        self.can_download = bool(sel) and not self.loading

    def toggle_open(self, node_id):
        n = self.by_id.get(node_id)
        if n is not None and n.kind == "folder":
            n.open = not n.open
            self.refresh()

    def toggle_check(self, node_id):
        n = self.by_id.get(node_id)
        if n is None:
            return
        if n.kind == "folder":
            value = not all(f.checked for f in n.files)
            for f in n.files:
                f.checked = value
        else:
            n.checked = not n.checked
        self.refresh()

    def set_all_open(self, value):
        for n in self.by_id.values():
            if n.kind == "folder":
                n.open = value
        self.refresh()

    def toggle_all(self, value):
        for f in self.file_nodes:
            f.checked = bool(value)
        self.refresh()

    # ---- file-type filter -------------------------------------------------
    def active_extensions(self):
        return {e for e, on in self.ext_on.items() if on} | core.parse_extensions(self.custom_ext)

    def apply_filter(self, quiet=False):
        exts = self.active_extensions()
        if not self.file_nodes:
            return
        if not exts:
            if not quiet:
                self.status_text = "Pick at least one file type to filter by."
            return
        suffixes = tuple("." + e for e in exts)
        count = 0
        for f in self.file_nodes:
            f.checked = f.file["name"].lower().endswith(suffixes)
            count += f.checked
        self.status_text = f"Filter selected {count} file(s): {', '.join(sorted(exts))}"
        self.update_types_text()
        self.refresh()

    def clear_filter(self):
        self.ext_on = {e: False for e in core.FILTER_EXTENSIONS}
        self.custom_ext = ""
        for f in self.file_nodes:
            f.checked = True
        self.update_types_text()
        if self.file_nodes:
            self.status_text = "Filter cleared - all files selected."
        self.refresh()

    def update_types_text(self):
        n = len(self.active_extensions())
        self.types_text = f"File types ({n})" if n else "File types"

    def open_types(self):
        from kivy.uix.gridlayout import GridLayout
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        box.add_widget(Label(text="Tick the types to select", size_hint_y=None, height=dp(34),
                             color=self.app.fg, font_size=self.app.fs, halign="left", text_size=(popup_size()[0], None)))
        grid = GridLayout(cols=3, size_hint_y=None, spacing=dp(4))
        grid.bind(minimum_height=grid.setter("height"))
        rows = {}
        for ext in core.FILTER_EXTENSIONS:
            tr = TickRow(text=ext, checked=self.ext_on[ext])
            tr.bind(on_toggle=lambda w, v, e=ext: self.ext_on.__setitem__(e, v))
            grid.add_widget(tr)
            rows[ext] = tr
        scroll = ScrollView()
        scroll.add_widget(grid)
        box.add_widget(scroll)
        box.add_widget(Label(text="Other (e.g. pdf, epub):", size_hint_y=None, height=dp(34), color=self.app.fg,
                             font_size=self.app.fs, halign="left", text_size=(popup_size()[0], None)))
        other = Builder.load_string("Inp:\n    text: ''")
        other.text = self.custom_ext
        other.hint_text = "pdf, epub, txt"
        box.add_widget(other)
        buttons = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = self.app.make_popup("Select by type", box, 0.75)

        def apply(*a):
            self.custom_ext = other.text
            pop.dismiss()
            if not self.active_extensions():
                self.clear_filter()
            else:
                self.apply_filter()

        def clear(*a):
            pop.dismiss()
            self.clear_filter()

        b1 = Builder.load_string("Btn:\n    text: 'Clear'")
        b2 = Builder.load_string("Btn:\n    text: 'Apply'")
        b1.bind(on_release=clear)
        b2.bind(on_release=apply)
        buttons.add_widget(b1)
        buttons.add_widget(b2)
        box.add_widget(buttons)
        pop.open()

    # ---- save location ----------------------------------------------------
    def set_default_save(self):
        name = core.safe_folder_name(self.source_id)
        if ON_ANDROID:
            self.save_name = name
            self.save_text = f"Downloads/{name}"
        else:
            self.save_name = os.path.join(os.path.expanduser("~"), "Downloads", name)
            self.save_text = self.save_name

    def change_save(self):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        text = ("Files are saved in your Downloads folder, inside this folder name:"
                if ON_ANDROID else "Folder to save into:")
        box.add_widget(Label(text=text, color=self.app.fg, font_size=self.app.fs, halign="left", valign="middle",
                             text_size=(popup_size()[0] - dp(30), None), size_hint_y=None, height=dp(80)))
        inp = Builder.load_string("Inp:\n    text: ''")
        inp.text = getattr(self, "save_name", "") or ""
        box.add_widget(inp)
        box.add_widget(Widget())
        buttons = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = self.app.make_popup("Save to", box, 0.45)

        def ok(*a):
            value = inp.text.strip()
            if value:
                self.save_name = core.safe_folder_name(value) if ON_ANDROID else value
                self.save_text = f"Downloads/{self.save_name}" if ON_ANDROID else self.save_name
                self.save_custom = True
            pop.dismiss()

        b1 = Builder.load_string("Btn:\n    text: 'Cancel'")
        b2 = Builder.load_string("Btn:\n    text: 'OK'")
        b1.bind(on_release=lambda *a: pop.dismiss())
        b2.bind(on_release=ok)
        buttons.add_widget(b1)
        buttons.add_widget(b2)
        box.add_widget(buttons)
        pop.open()

    # ---- download ------------------------------------------------------------
    def start_download(self):
        if self.app.downloading:
            self.app.message("Busy", "A download is already running.")
            return
        selected = [{"name": f.file["name"], "size": f.file["size"], "url": f.file["url"],
                     "exact": f.file.get("exact", True)} for f in self.file_nodes if f.checked]
        if not selected:
            self.app.message("Nothing selected", "Tick at least one file to download.")
            return
        events = queue.Queue()
        try:
            if ON_ANDROID:
                store = MediaStoreStore(self.save_name)
                dest = "Downloads/" + core.safe_folder_name(self.save_name)
            else:
                dest = self.save_name
                os.makedirs(dest, exist_ok=True)
                store = core.LocalStore(dest)
        except Exception as exc:
            self.app.message("Can't save there", str(exc))
            return
        worker = core.Downloader(selected, dest, events, store=store)
        self.app.downloading = True
        set_keep_screen_on(True)
        ProgressPopup(self.app, worker, events, dest, self.after_download).open()
        worker.start()

    def after_download(self):
        self.app.downloading = False
        set_keep_screen_on(False)


class ProgressPopup(Popup):
    """Current file + overall progress while downloading."""

    def __init__(self, app, worker, events, dest, on_close, **kw):
        self.app_ref, self.worker, self.events, self.on_close = app, worker, events, on_close
        self.finished = False
        self.has_total = all(f["size"] > 0 for f in worker.files)
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        mk = lambda **k: Label(color=app.fg, font_size=app.fs, halign="left", valign="middle",
                               size_hint_y=None, height=dp(34), **k)
        self.l_file = mk(text="Starting...")
        self.p_file = ProgressBar(max=100, size_hint_y=None, height=dp(16))
        self.l_file2 = mk(text="")
        self.l_total = mk(text="Overall progress")
        self.p_total = ProgressBar(max=100, size_hint_y=None, height=dp(16))
        self.l_total2 = mk(text=f"Saving to {dest}")
        for w in (self.l_file, self.p_file, self.l_file2, self.l_total, self.p_total, self.l_total2):
            if isinstance(w, Label):
                w.bind(size=lambda i, v: setattr(i, "text_size", (v[0], None)))
            box.add_widget(w)
        box.add_widget(Widget())
        self.btn = Builder.load_string("Btn:\n    text: 'Cancel'")
        self.btn.bind(on_release=self.cancel)
        box.add_widget(self.btn)
        super().__init__(title="Downloading...", content=box, size_hint=(None, None), size=popup_size(0.6),
                         auto_dismiss=False, background="", background_color=app.field,
                         title_color=app.fg, separator_color=app.accent, title_size=app.fs, **kw)
        app.popups.append(self)
        self.timer = Clock.schedule_interval(self.poll, 0.1)

    def cancel(self, *a):
        if self.finished:
            self.timer.cancel()
            self.dismiss()
            if self in self.app_ref.popups:
                self.app_ref.popups.remove(self)
            self.on_close()
        else:
            self.btn.disabled = True
            self.btn.text = "Cancelling..."
            self.worker.cancel()

    def poll(self, dt):
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass

    def handle(self, ev):
        kind = ev[0]
        if kind == "file":
            _, name, idx, count, size = ev
            shown = name if len(name) <= 48 else "..." + name[-45:]
            self.l_file.text = f"File {idx} of {count}: {shown}"
            self.p_file.value = 0
            self.l_file2.text = ""
            if not self.has_total:
                self.p_total.value = (idx - 1) * 100 / count
                self.l_total2.text = f"{idx - 1} of {count} files done"
        elif kind == "bytes":
            _, f_done, f_total, o_done, o_total, speed, remaining = ev
            if f_total > 0:
                self.p_file.value = min(100, f_done * 100 / f_total)
                self.l_file2.text = f"{core.human_size(f_done)} of {core.human_size(f_total)}"
            else:
                self.l_file2.text = core.human_size(f_done)
            if o_total > 0 and self.has_total:
                self.p_total.value = min(100, o_done * 100 / o_total)
                extra = f"   {core.human_size(speed)}/s   ETA {core.human_time(remaining)}" if speed else ""
                self.l_total2.text = f"{core.human_size(o_done)} of {core.human_size(o_total)}{extra}"
        elif kind == "finished":
            _, ok, skipped, failed, cancelled = ev
            self.finished = True
            set_keep_screen_on(False)
            self.title = "Cancelled" if cancelled else "Finished"
            self.btn.disabled = False
            self.btn.text = "Close"
            if cancelled:
                self.l_file.text = "Download cancelled."
            else:
                self.p_total.value = self.p_file.value = 100
                msg = f"Done: {ok} downloaded"
                if skipped:
                    msg += f", {skipped} already there"
                if failed:
                    msg += f", {len(failed)} failed"
                self.l_file.text = msg
            if failed:
                details = "\n".join(f"- {n}: {e}" for n, e in failed[:8])
                if len(failed) > 8:
                    details += f"\n...and {len(failed) - 8} more"
                self.app_ref.message("Some files failed", details)


class Root(BoxLayout):
    current = StringProperty("archive")

    def select(self, which):
        self.current = which
        body = self.ids.body
        body.clear_widgets()
        body.add_widget(self.panels[which])


class IADMApp(App):
    title = core.APP_TITLE
    icon = "icon.png"
    dark = BooleanProperty(False)
    fs = NumericProperty(18)
    theme_rev = NumericProperty(0)
    bg = ListProperty()
    fg = ListProperty()
    muted = ListProperty()
    field = ListProperty()
    button = ListProperty()
    accent = ListProperty()
    row_even = ListProperty()
    row_odd = ListProperty()
    tick_border = ListProperty()

    def build(self):
        self.downloading = False
        self.popups = []
        self.fs = sp(17)
        self.cfg = self.load_config()
        self.dark = bool(self.cfg.get("dark_mode", False))
        self.auto_update = bool(self.cfg.get("auto_update", True))
        self.apply_theme()
        Window.softinput_mode = "below_target"
        Window.bind(on_keyboard=self.on_key)
        Builder.load_string(KV)
        root = Root()
        root.panels = {"archive": Panel("archive"), "dir": Panel("dir")}
        root.select("archive")
        self.root_widget = root
        if core.GITHUB_REPO and self.auto_update:
            Clock.schedule_once(lambda dt: self.check_updates(False), 3)
        return root

    def on_pause(self):
        return True     # keep running (and downloading) when the user switches apps

    # ---- settings ---------------------------------------------------------
    def config_path(self):
        return os.path.join(self.user_data_dir, "config.json")

    def load_config(self):
        try:
            with open(self.config_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save_config(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.config_path(), "w", encoding="utf-8") as fh:
                json.dump({"dark_mode": self.dark, "auto_update": self.auto_update}, fh)
        except Exception:
            pass

    def apply_theme(self):
        pal = PALETTES["dark" if self.dark else "light"]
        for key, value in pal.items():
            setattr(self, key, hex_rgba(value))
        Window.clearcolor = self.bg
        self.theme_rev += 1

    def set_dark(self, value):
        self.dark = bool(value)
        self.apply_theme()
        self.save_config()

    def set_auto_update(self, value):
        self.auto_update = bool(value)
        self.save_config()

    # ---- popups -----------------------------------------------------------
    def make_popup(self, title, content, h_frac=0.8):
        pop = Popup(title=title, content=content, size_hint=(None, None), size=popup_size(h_frac),
                    background="", background_color=self.field, title_color=self.fg,
                    separator_color=self.accent, title_size=self.fs)
        pop.bind(on_dismiss=lambda *a: self.popups.remove(pop) if pop in self.popups else None)
        self.popups.append(pop)
        return pop

    def message(self, title, text):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        scroll = ScrollView()
        lbl = Label(text=text, color=self.fg, font_size=self.fs, halign="left", valign="top",
                    size_hint_y=None, text_size=(popup_size()[0] - dp(40), None))
        lbl.bind(texture_size=lambda i, v: setattr(i, "height", v[1]))
        scroll.add_widget(lbl)
        box.add_widget(scroll)
        ok = Builder.load_string("Btn:\n    text: 'OK'")
        box.add_widget(ok)
        pop = self.make_popup(title, box, 0.5)
        ok.bind(on_release=lambda *a: pop.dismiss())
        pop.open()
        return pop

    def on_key(self, window, key, *args):
        """Back button: close the top popup first (but not a running download)."""
        if key in (27, 1001):
            if self.popups:
                top = self.popups[-1]
                if isinstance(top, ProgressPopup):
                    return True
                top.dismiss()
                return True
            if self.downloading:
                return True
        return False

    # ---- menu / about / updates ------------------------------------------
    def open_menu(self):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        pop = self.make_popup("Menu", box, 0.62)
        for label, fn in (("About", self.show_about), ("Donate with PayPal", lambda: open_external(core.DONATE_URL)),
                          ("Check for updates now", lambda: self.check_updates(True))):
            b = Builder.load_string("Btn:\n    text: ''")
            b.text = label
            b.bind(on_release=lambda *a, f=fn: (pop.dismiss(), f()))
            box.add_widget(b)
        t1 = TickRow(text="Dark mode", checked=self.dark)
        t1.bind(on_toggle=lambda w, v: self.set_dark(v))
        t2 = TickRow(text="Check for updates automatically", checked=self.auto_update)
        t2.bind(on_toggle=lambda w, v: self.set_auto_update(v))
        box.add_widget(t1)
        box.add_widget(t2)
        box.add_widget(Widget())
        close = Builder.load_string("Btn:\n    text: 'Close'")
        close.bind(on_release=lambda *a: pop.dismiss())
        box.add_widget(close)
        pop.open()

    def show_about(self):
        width = popup_size()[0] - dp(40)
        col = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8), padding=dp(8))
        col.bind(minimum_height=col.setter("height"))

        def text(t, bold=False, size=None, align="center", color=None, h=None):
            lbl = Label(text=t, bold=bold, color=color or self.fg, font_size=size or self.fs, halign=align,
                        valign="middle", size_hint_y=None, text_size=(width, None))
            lbl.bind(texture_size=lambda i, v: setattr(i, "height", v[1] + dp(6)))
            col.add_widget(lbl)
            return lbl

        if os.path.exists("icon.png"):
            col.add_widget(Image(source="icon.png", size_hint_y=None, height=dp(96)))
        text(core.APP_TITLE, bold=True, size=self.fs * 1.2)
        text(f"Version {core.VERSION}")
        text("Coded by Claude & Emmanuel Hoachlander", bold=True)
        text("If you enjoy this program, please consider a donation.")
        donate = Builder.load_string("Btn:\n    text: 'Donate with PayPal'")
        donate.bind(on_release=lambda *a: open_external(core.DONATE_URL))
        col.add_widget(donate)
        text("Open source software used", bold=True, align="left")
        for name, lic, url in core.ANDROID_CREDITS:
            b = Builder.load_string("Btn:\n    text: ''")
            b.text = f"{name}"
            b.bind(on_release=lambda *a, u=url: open_external(u))
            col.add_widget(b)
            text(lic, size=self.fs * 0.8, color=self.muted, align="left")
        text("Services used", bold=True, align="left")
        for name, what, url in core.SERVICES:
            b = Builder.load_string("Btn:\n    text: ''")
            b.text = f"{name}"
            b.bind(on_release=lambda *a, u=url: open_external(u))
            col.add_widget(b)
            text(what, size=self.fs * 0.8, color=self.muted, align="left")
        scroll = ScrollView()
        scroll.add_widget(col)
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        box.add_widget(scroll)
        close = Builder.load_string("Btn:\n    text: 'Close'")
        box.add_widget(close)
        pop = self.make_popup("About", box, 0.9)
        close.bind(on_release=lambda *a: pop.dismiss())
        pop.open()

    def check_updates(self, manual):
        if not core.GITHUB_REPO:
            if manual:
                self.message("Updates", "Updates aren't set up in this copy of the app "
                                        "(no GitHub repository is configured).")
            return
        q = queue.Queue()

        def runner():
            try:
                q.put((core.fetch_latest_release(core.GITHUB_REPO, APK_NAME), None))
            except Exception as exc:
                q.put((None, exc))

        def poll(dt):
            try:
                release, error = q.get_nowait()
            except queue.Empty:
                return True
            self.update_result(release, error, manual)
            return False

        threading.Thread(target=runner, daemon=True).start()
        Clock.schedule_interval(poll, 0.1)

    def update_result(self, release, error, manual):
        if error is not None:
            if manual:
                self.message("Updates", f"Couldn't check for updates:\n\n{error}")
            return
        if release is None:
            if manual:
                self.message("Updates", "No releases have been published yet.")
            return
        if release["version"] <= core.version_tuple(core.VERSION):
            if manual:
                self.message("Updates", f"You're up to date (version {core.VERSION}).")
            return
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        shown = (release["tag"] or "").lstrip("vV") or "a newer version"
        notes = release["notes"][:1500] or "No release notes."
        scroll = ScrollView()
        lbl = Label(text=f"You have version {core.VERSION}.\n\n{notes}\n\nTap Download, then open the file "
                         "to install it over the old one.", color=self.fg, font_size=self.fs, halign="left",
                    valign="top", size_hint_y=None, text_size=(popup_size()[0] - dp(40), None))
        lbl.bind(texture_size=lambda i, v: setattr(i, "height", v[1]))
        scroll.add_widget(lbl)
        box.add_widget(scroll)
        row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        later = Builder.load_string("Btn:\n    text: 'Not now'")
        get = Builder.load_string("Btn:\n    text: 'Download'")
        row.add_widget(later)
        row.add_widget(get)
        box.add_widget(row)
        pop = self.make_popup(f"Version {shown} is available", box, 0.7)
        later.bind(on_release=lambda *a: pop.dismiss())
        get.bind(on_release=lambda *a: (open_external(release["asset_url"] or release["page"]), pop.dismiss()))
        pop.open()


if __name__ == "__main__":
    IADMApp().run()
