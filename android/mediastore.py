"""
Saving downloads into the public Downloads folder on Android 10+ (MediaStore).
Used by both the app and the background download service, so it must not import Kivy.
"""

import mimetypes

import iadm_core as core


def android_context():
    """The Android Context to use: the background service when running inside it, otherwise the app."""
    from jnius import autoclass
    try:
        ctx = autoclass("org.kivy.android.PythonService").mService
        if ctx is not None:
            return ctx
    except Exception:
        pass
    return autoclass("org.kivy.android.PythonActivity").mActivity


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
    """One file being saved into the public Downloads folder."""
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
        self.resolver = android_context().getContentResolver()
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
