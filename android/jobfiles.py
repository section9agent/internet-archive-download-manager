"""Small files the app and the background download service use to talk to each other."""

import json
import os

PRIV = os.environ.get("ANDROID_PRIVATE") or os.path.join(os.path.expanduser("~"), ".iadm_job")
JOB = os.path.join(PRIV, "iadm_job.json")        # app -> service: what to download
STATUS = os.path.join(PRIV, "iadm_status.json")  # service -> app: progress
CANCEL = os.path.join(PRIV, "iadm_cancel")       # app -> service: please stop


def set_dir(path):
    """Use this folder for the shared files (the app tells the service which one)."""
    global PRIV, JOB, STATUS, CANCEL
    PRIV = path
    JOB = os.path.join(PRIV, "iadm_job.json")
    STATUS = os.path.join(PRIV, "iadm_status.json")
    CANCEL = os.path.join(PRIV, "iadm_cancel")


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
