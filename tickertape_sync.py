"""
tickertape_sync.py - keeps the Tickertape fundamentals file the same everywhere it is used:

    this folder:   inputs/fundamentals_tickertape.csv
    GitHub:        dr-azygos/my-formula  inputs/fundamentals_tickertape.csv          (the cloud app)
    GitHub:        dr-azygos/my_bots     stock_broker/formula/fundamentals_tickertape.csv  (Stock Broker;
                   the bots' server updates itself within minutes of a change there)

Started in the background when a scan runs (start_in_background()). The newest copy, by its `asof` date, wins:
a newer file here is uploaded to both repositories; a newer file on GitHub is downloaded here. A file is only
ever written somewhere if it passes the same checks the monthly refresh uses (enough rows, F-scores present,
no empty column), so a broken export can't spread. It never stops or slows the scan; what it did goes to
output/sync_log.txt.

It needs a GitHub key that may change those two repositories, kept only on this computer: the file
github_token.txt in this folder (never uploaded: it is in .gitignore), or the GITHUB_TOKEN environment variable.
Without a key it only reads (and can still download a newer file from the public my-formula repository).
"""
from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(HERE, "inputs", "fundamentals_tickertape.csv")
LOG = os.path.join(HERE, "output", "sync_log.txt")
TOKEN_FILE = os.path.join(HERE, "github_token.txt")
API = "https://api.github.com"
COPIES = [  # (repository, path, branch)
    ("dr-azygos/my-formula", "inputs/fundamentals_tickertape.csv", "main"),
    ("dr-azygos/my_bots", "stock_broker/formula/fundamentals_tickertape.csv", "main"),
]
MIN_ROWS, MIN_FSCORES = 480, 450


def token() -> str:
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if not t and os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE, encoding="utf-8").read().strip()
    return t


def problems(raw: bytes) -> list[str]:
    """Why a file must not be used anywhere ([] = fine). Same checks as the monthly refresh."""
    try:
        t = pd.read_csv(io.BytesIO(raw))
    except Exception:
        return ["not a CSV"]
    out = []
    if len(t) < MIN_ROWS:
        out.append(f"only {len(t)} rows")
    if "asof" not in t or t["asof"].isna().all():
        out.append("no asof date")
    if "pitroskif" not in t or t["pitroskif"].notna().sum() < MIN_FSCORES:
        out.append("F-scores (pitroskif) missing")
    empty = [c for c in t.columns if t[c].isna().all()]
    if empty:
        out.append("empty column(s): " + ", ".join(empty))
    return out


def asof(raw: bytes) -> str:
    try:
        return str(pd.read_csv(io.BytesIO(raw), usecols=["asof"], nrows=1)["asof"].iloc[0])
    except Exception:
        return ""


def _request(method: str, url: str, tok: str, body: dict | None = None) -> dict | None:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "my-formula-sync",
                                          **({"Authorization": f"Bearer {tok}"} if tok else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def remote(repo: str, path: str, branch: str, tok: str) -> tuple[bytes, str] | None:
    """(file bytes, its sha) on GitHub, or None if it isn't there or can't be read without a key."""
    meta = _request("GET", f"{API}/repos/{repo}/contents/{path}?ref={branch}", tok)
    if not meta:
        return None
    if meta.get("content") and meta.get("encoding") == "base64":
        return base64.b64decode(meta["content"]), meta["sha"]
    blob = _request("GET", f"{API}/repos/{repo}/git/blobs/{meta['sha']}", tok)  # files over 1 MB
    return base64.b64decode(blob["content"]), meta["sha"]


def upload(repo: str, path: str, branch: str, tok: str, raw: bytes, sha: str | None, when: str) -> None:
    body = {"message": f"Tickertape fundamentals {when} (synced from the My Formula app)",
            "content": base64.b64encode(raw).decode(), "branch": branch}
    if sha:
        body["sha"] = sha
    _request("PUT", f"{API}/repos/{repo}/contents/{path}", tok, body)


def sync(log=print) -> list[str]:
    """One pass: the newest valid copy goes everywhere older. Returns what was done (also logged)."""
    tok = token()
    done: list[str] = []
    local = open(LOCAL, "rb").read() if os.path.exists(LOCAL) else b""
    copies = {}
    for repo, path, branch in COPIES:
        try:
            copies[repo] = remote(repo, path, branch, tok)
        except Exception as exc:
            log(f"{repo}: couldn't read ({exc.__class__.__name__}: {exc})")
    candidates = [("this folder", local)] + [(r, c[0]) for r, c in copies.items() if c]
    valid = [(where, raw) for where, raw in candidates if raw and not problems(raw)]
    for where, raw in candidates:
        if raw and problems(raw):
            log(f"{where}: not used ({'; '.join(problems(raw))})")
    if not valid:
        log("no valid copy anywhere; nothing changed")
        return done
    where, best = max(valid, key=lambda wr: asof(wr[1]))
    when = asof(best)
    local_bad = bool(local) and bool(problems(local))
    if not local or local_bad or asof(local) < when:
        os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
        if local_bad:  # kept aside, not lost: the scan uses the last good copy meanwhile
            with open(LOCAL.replace(".csv", ".rejected.csv"), "wb") as f:
                f.write(local)
        with open(LOCAL, "wb") as f:
            f.write(best)
        done.append(f"this folder updated to {when} (from {where})"
                    + (" - its own file was broken, kept as fundamentals_tickertape.rejected.csv" if local_bad else ""))
    for repo, path, branch in COPIES:
        c = copies.get(repo)
        if c is not None and asof(c[0]) >= when:
            continue
        if not tok:
            log(f"{repo}: has {asof(c[0]) if c else 'no file'}, newer is {when}; add github_token.txt to upload it")
            continue
        try:
            upload(repo, path, branch, tok, best, c[1] if c else None, when)
            done.append(f"{repo} updated to {when}")
        except Exception as exc:
            log(f"{repo}: upload failed ({exc.__class__.__name__}: {exc})")
    for line in done:
        log(line)
    if not done:
        log(f"everything already on {when}")
    return done


def _log_to_file(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {msg}\n")


def start_in_background() -> threading.Thread:
    """Run sync() beside the scan; it never raises into it."""
    def run():
        try:
            sync(_log_to_file)
        except Exception as exc:
            _log_to_file(f"sync failed: {exc.__class__.__name__}: {exc}")
    t = threading.Thread(target=run, name="tickertape-sync", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    t0 = time.time()
    sync()
    print(f"done in {time.time() - t0:.1f}s")
