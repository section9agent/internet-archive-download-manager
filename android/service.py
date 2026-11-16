"""
Background download service (Android). Runs as a foreground service so downloads continue
when the app is not on screen. The app hands it a job file and reads progress from a status file.
"""

import os
import queue
import time
import traceback

import iadm_core as core
import jobfiles as jf


def write_status(state):
    state["ts"] = time.time()
    try:
        jf.write_json(jf.STATUS, state)
    except Exception:
        pass


def main():
    job = jf.read_json(jf.JOB)
    token = job.get("token") if job else None
    state = {"token": token, "file": None, "bytes": None, "finished": None}
    if not job:
        state["finished"] = [0, 0, [["job", "No download job was found."]], False]
        write_status(state)
        return

    wake = None
    try:
        from jnius import autoclass
        ctx = autoclass("org.kivy.android.PythonService").mService
        wake = ctx.getSystemService("power").newWakeLock(1, "iadm:download")   # 1 = PARTIAL_WAKE_LOCK
        wake.acquire()
    except Exception:
        wake = None

    try:
        from mediastore import MediaStoreStore
        store = MediaStoreStore(job["folder"])
        events = queue.Queue()
        worker = core.Downloader(job["files"], "Downloads/" + core.safe_folder_name(job["folder"]), events, store=store)
        worker.start()
        last_write = 0.0
        cancelled = False
        while worker.is_alive() or not events.empty():
            try:
                ev = events.get(timeout=0.3)
            except queue.Empty:
                ev = None
            if ev is not None:
                if ev[0] == "file":
                    state["file"] = list(ev[1:])
                elif ev[0] == "bytes":
                    state["bytes"] = list(ev[1:])
                elif ev[0] == "finished":
                    _, ok, skipped, failed, was_cancelled = ev
                    state["finished"] = [ok, skipped, [list(x) for x in failed], was_cancelled]
            if not cancelled and os.path.exists(jf.CANCEL):
                cancelled = True
                worker.cancel()
            now = time.time()
            if now - last_write >= 0.5 or state["finished"]:
                write_status(state)
                last_write = now
        if not state["finished"]:
            state["finished"] = [0, 0, [["download", "The download stopped unexpectedly."]], cancelled]
        write_status(state)
    except Exception:
        state["finished"] = [0, 0, [["download", traceback.format_exc(limit=3)]], False]
        write_status(state)
    finally:
        if wake is not None:
            try:
                wake.release()
            except Exception:
                pass
        try:
            from jnius import autoclass
            svc = autoclass("org.kivy.android.PythonService").mService
            svc.stopForeground(True)
            svc.stopSelf()
        except Exception:
            pass


main()
