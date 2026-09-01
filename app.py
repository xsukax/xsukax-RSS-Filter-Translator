#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xsukax RSS Filter & Translator
==============================
A lightweight, self-hosted web application that generates customized RSS feed
URLs from an existing RSS feed, with keyword filtering and fully LOCAL,
server-side translation (Argos Translate — offline neural machine translation,
no cloud APIs).

Runs on port 6985. Protected by password login (default: xsukax).

Configuration via environment variables:
  XSUKAX_HOST          Bind address          (default: 0.0.0.0)
  XSUKAX_PORT          Bind port             (default: 6985)
  XSUKAX_DB            SQLite database path  (default: ./xsukax_rss.db)
  XSUKAX_FEED_TTL      Rendered-feed cache TTL in seconds (default: 900)
  XSUKAX_MAX_DL_BYTES  Max source feed download size      (default: 5242880)
  XSUKAX_TR_BUDGET     Total translation time budget per feed build (s, 0=off)
  XSUKAX_UA            User-Agent for fetching source feeds (browser default)
  XDG_DATA_HOME        Where Argos models live (default: <db dir>/xdg-data)
  XDG_CACHE_HOME       MiniSBD model cache    (default: <db dir>/xdg-cache)
  ARGOS_CHUNK_TYPE     Forced to MINISBD (no stanza / no huggingface downloads)
"""

import functools
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import closing

# --------------------------------------------------------------------------
# Configuration (must run before importing argostranslate)
# --------------------------------------------------------------------------

HOST = os.environ.get("XSUKAX_HOST", "0.0.0.0")
PORT = int(os.environ.get("XSUKAX_PORT", "6985"))
DB_PATH = os.path.abspath(os.environ.get(
    "XSUKAX_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "xsukax_rss.db"),
))
DATA_DIR = os.path.dirname(DB_PATH)

# Keep every downloaded artifact inside the app data dir, and force the
# MiniSBD sentence splitter so translation never needs stanza/huggingface.
# XDG_CONFIG_HOME matters too: argostranslate creates its config dir on
# import and crashes if the service user has no writable home directory.
os.environ.setdefault("XDG_DATA_HOME", os.path.join(DATA_DIR, "xdg-data"))
os.environ.setdefault("XDG_CACHE_HOME", os.path.join(DATA_DIR, "xdg-cache"))
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(DATA_DIR, "xdg-config"))
os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")

FEED_TTL = int(os.environ.get("XSUKAX_FEED_TTL", "900"))          # seconds
MAX_DL_BYTES = int(os.environ.get("XSUKAX_MAX_DL_BYTES", str(5 * 1024 * 1024)))
TR_BUDGET = float(os.environ.get("XSUKAX_TR_BUDGET", "120"))      # per build
HTTP_TIMEOUT = 15  # seconds

DEFAULT_MAX_ITEMS = 20
MAX_ITEMS_HARD_LIMIT = 200
MAX_KEYWORDS = 30
DEFAULT_PASSWORD = "xsukax"
SESSION_LIFETIME = 12 * 3600       # seconds
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT = 60                 # seconds

# A realistic browser User-Agent avoids "403 Forbidden" from picky sites.
USER_AGENT = os.environ.get(
    "XSUKAX_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
FETCH_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
              "text/xml;q=0.9, text/html;q=0.8, */*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

import feedparser                      # noqa: E402
import requests                        # noqa: E402
from flask import (Flask, abort, g, jsonify, redirect, render_template,  # noqa: E402
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("xsukax")

# --------------------------------------------------------------------------
# Database (SQLite)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    token       TEXT PRIMARY KEY,
    source_url  TEXT NOT NULL,
    keywords    TEXT NOT NULL DEFAULT '[]',
    match_mode  TEXT NOT NULL DEFAULT 'title',
    max_items   INTEGER NOT NULL DEFAULT 20,
    target_lang TEXT NOT NULL DEFAULT 'none',
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS translation_cache (
    from_lang   TEXT NOT NULL DEFAULT '',
    target_lang TEXT NOT NULL,
    src_hash    TEXT NOT NULL,
    translated  TEXT NOT NULL,
    PRIMARY KEY (from_lang, target_lang, src_hash)
);
CREATE TABLE IF NOT EXISTS feed_cache (
    token        TEXT PRIMARY KEY,
    xml          TEXT NOT NULL,
    generated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?",
                           (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))
    db.commit()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=15)) as db:
        db.executescript(SCHEMA)
        if not db.execute("SELECT 1 FROM settings WHERE key='password_hash'") \
                 .fetchone():
            db.execute("INSERT INTO settings (key, value) VALUES (?,?)",
                       ("password_hash", generate_password_hash(DEFAULT_PASSWORD)))
            log.info("Initialized with default password '%s' — change it "
                     "after first login!", DEFAULT_PASSWORD)
        db.commit()


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

def _secret_key():
    """Persisted random secret key for Flask sessions."""
    key_path = os.path.join(DATA_DIR, ".secret_key")
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="ascii") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(key_path, "w", encoding="ascii") as fh:
        fh.write(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


_login_state = {"fails": 0, "locked_until": 0.0}


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------------
# Local translation (Argos Translate — fully offline, lazy-loaded)
# --------------------------------------------------------------------------

_argo_pkg = None        # argostranslate.package module
_argo_tr = None         # argostranslate.translate module
_argo_lock = threading.Lock()
_tr_objects = {}        # (from, to) -> translation object (small LRU)
_TR_OBJECTS_MAX = 3


def _argo():
    """Import argostranslate lazily (it is heavy: ctranslate2 et al.)."""
    global _argo_pkg, _argo_tr
    if _argo_pkg is None:
        with _argo_lock:
            if _argo_pkg is None:
                import argostranslate.package
                import argostranslate.translate
                _argo_pkg = argostranslate.package
                _argo_tr = argostranslate.translate
    return _argo_pkg, _argo_tr


def _argo_code(code):
    """Normalize a language tag to an Argos code: 'fr-FR'->'fr', 'zh-CN'->'zh'."""
    code = (code or "").strip().lower().replace("_", "-")
    if not code:
        return ""
    if code.startswith("zh"):
        return "zh"
    return code.split("-")[0]


def installed_pairs():
    """List of dicts for installed translation models."""
    pkg_mod, _ = _argo()
    try:
        return [{"from_code": p.from_code, "to_code": p.to_code,
                 "from_name": p.from_name, "to_name": p.to_name,
                 "version": getattr(p, "package_version", "")}
                for p in pkg_mod.get_installed_packages()]
    except Exception as exc:                       # noqa: BLE001
        log.warning("could not list installed models: %s", exc)
        return []


def available_pairs():
    """Available (not yet installed) pairs from the Argos package index."""
    pkg_mod, _ = _argo()
    try:
        pkgs = pkg_mod.get_available_packages()
    except Exception as exc:                       # noqa: BLE001
        log.warning("could not fetch package index: %s", exc)
        return None                                # index unreachable
    installed = {(p["from_code"], p["to_code"]) for p in installed_pairs()}
    return [{"from_code": p.from_code, "to_code": p.to_code,
             "from_name": p.from_name, "to_name": p.to_name,
             "installed": (p.from_code, p.to_code) in installed}
            for p in sorted(pkgs, key=lambda p: (p.from_name, p.to_name))]


def _get_translation(from_code, to_code):
    """Cached Argos translation object; raises if the pair is not installed."""
    key = (from_code, to_code)
    if key in _tr_objects:
        return _tr_objects[key]
    _, tr_mod = _argo()
    langs = tr_mod.get_installed_languages()
    l_from = next((l for l in langs if l.code == from_code), None)
    l_to = next((l for l in langs if l.code == to_code), None)
    if not l_from or not l_to:
        raise RuntimeError(f"translation model {from_code} → {to_code} "
                           f"is not installed")
    translation = l_from.get_translation(l_to)   # raises if no path exists
    if len(_tr_objects) >= _TR_OBJECTS_MAX:      # tiny LRU: drop oldest
        _tr_objects.pop(next(iter(_tr_objects)))
    _tr_objects[key] = translation
    return translation


def _detect_lang(text):
    """Lightweight language detection fallback when the feed has no
    <language> tag."""
    try:
        from langdetect import detect
        return _argo_code(detect(text[:1000]))
    except Exception:                              # noqa: BLE001
        return ""


def _translate_one_local(text, target_lang, source_lang):
    translation = _get_translation(source_lang, target_lang)
    return translation.translate(text[:4500]) or text


def _translate_uncached(texts, target_lang, source_lang):
    """Translate locally, bounded by an overall time budget. Failures keep
    the original text so a feed never breaks."""
    out = list(texts)
    deadline = time.monotonic() + TR_BUDGET if TR_BUDGET > 0 else None
    for i, text in enumerate(texts):
        if deadline and time.monotonic() >= deadline:
            log.warning("translation budget exhausted; %d text(s) left "
                        "untranslated", len(texts) - i)
            break
        try:
            out[i] = _translate_one_local(text, target_lang, source_lang)
        except Exception as exc:                   # noqa: BLE001
            log.warning("local translation failed (%s); keeping original", exc)
            out[i] = text
            break    # model missing/broken: no point retrying every item
    return out


def translate_texts(texts, target_lang, source_lang):
    """Translate a list of strings, consulting/populating the SQLite cache."""
    if target_lang == "none" or not texts:
        return list(texts)
    if source_lang and source_lang == _argo_code(target_lang):
        return list(texts)      # already in the target language

    db = get_db()
    result = [None] * len(texts)
    missing_idx, missing_txt = [], []
    for i, text in enumerate(texts):
        if not text or not text.strip():
            result[i] = text
            continue
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = db.execute(
            "SELECT translated FROM translation_cache "
            "WHERE from_lang=? AND target_lang=? AND src_hash=?",
            (source_lang, target_lang, h)).fetchone()
        if row:
            result[i] = row["translated"]
        else:
            missing_idx.append(i)
            missing_txt.append(text)

    if missing_txt:
        translated = _translate_uncached(missing_txt, target_lang, source_lang)
        rows = []
        for idx, src, dst in zip(missing_idx, missing_txt, translated):
            result[idx] = dst
            rows.append((source_lang, target_lang,
                         hashlib.sha256(src.encode("utf-8")).hexdigest(), dst))
        db.executemany(
            "INSERT OR IGNORE INTO translation_cache "
            "(from_lang, target_lang, src_hash, translated) VALUES (?,?,?,?)",
            rows)
        db.commit()
    return result


# --------------------------------------------------------------------------
# Model management (background install with status)
# --------------------------------------------------------------------------

_model_tasks = {}        # "de->en" -> "downloading" | "installing" | "done" | "error: ..."
_model_tasks_lock = threading.Lock()


def _install_model_worker(from_code, to_code):
    key = f"{from_code}->{to_code}"
    pkg_mod, tr_mod = _argo()
    try:
        pkg_mod.update_package_index()
        pkg = next((p for p in pkg_mod.get_available_packages()
                    if p.from_code == from_code and p.to_code == to_code), None)
        if pkg is None:
            raise RuntimeError("package not found in index")
        with _model_tasks_lock:
            _model_tasks[key] = "downloading"
        path = pkg.download()
        with _model_tasks_lock:
            _model_tasks[key] = "installing"
        pkg_mod.install_from_path(path)
        try:
            os.remove(path)
        except OSError:
            pass
        # Warm-up: loads the model and fetches the tiny MiniSBD splitter
        # (one-time, from GitHub) so first feed build is fast and any error
        # shows up here on the Models page instead of inside a feed.
        with _model_tasks_lock:
            _model_tasks[key] = "warming up"
        tr_mod.translate("Hello", from_code, to_code)
        _tr_objects.pop((from_code, to_code), None)   # drop stale cache entry
        with _model_tasks_lock:
            _model_tasks[key] = "done"
        log.info("installed translation model %s", key)
    except Exception as exc:                         # noqa: BLE001
        with _model_tasks_lock:
            _model_tasks[key] = f"error: {exc}"
        log.warning("model install %s failed: %s", key, exc)


# --------------------------------------------------------------------------
# Feed fetching / filtering / rendering
# --------------------------------------------------------------------------

class FeedFetchError(Exception):
    pass


def fetch_source(url):
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, stream=True,
                            headers=FETCH_HEADERS)
        if resp.status_code == 403:
            log.info("403 from %s with browser UA; retrying with "
                     "feed-reader UA", url)
            resp.close()
            headers = dict(FETCH_HEADERS)
            headers["User-Agent"] = ("Mozilla/5.0 (compatible; FreshRSS/1.24; "
                                     "+https://freshrss.org)")
            resp = requests.get(url, timeout=HTTP_TIMEOUT, stream=True,
                                headers=headers)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("fetch failed for %s: %s", url, exc)
        raise FeedFetchError(f"Could not download source feed: {exc}") from exc

    chunks, size = [], 0
    try:
        for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
            size += len(chunk)
            if size > MAX_DL_BYTES:
                raise FeedFetchError(
                    "Source feed exceeds the size limit "
                    f"({MAX_DL_BYTES // 1024 // 1024} MB).")
            chunks.append(chunk)
    finally:
        resp.close()

    parsed = feedparser.parse(b"".join(chunks))
    if not parsed.entries and parsed.bozo:
        raise FeedFetchError("The URL does not appear to be a valid "
                             "RSS/Atom feed.")
    return parsed


def item_matches(entry, keywords, match_mode):
    """Case-insensitive OR match; keywords are used exactly as entered."""
    if not keywords:
        return True
    haystacks = [entry.get("title", "") or ""]
    if match_mode == "both":
        haystacks.append(entry.get("description", "")
                         or entry.get("summary", "") or "")
    hay = "\n".join(haystacks).casefold()
    return any(k.casefold() in hay for k in keywords)


def render_feed(feed_cfg, parsed, entries):
    token = feed_cfg["token"]
    target_lang = feed_cfg["target_lang"]
    source_title = (parsed.feed.get("title") or feed_cfg["source_url"]).strip()
    keywords = json.loads(feed_cfg["keywords"])

    # Source language: feed <language> tag, else detect from content.
    source_lang = _argo_code(parsed.feed.get("language", ""))
    if target_lang != "none" and not source_lang and entries:
        sample = " ".join((e.get("title", "") or "") for e in entries[:5])
        source_lang = _detect_lang(sample)
        log.info("detected source language '%s' for %s", source_lang,
                 feed_cfg["source_url"])

    # ---- Translate (fully local, SQLite-cached) ---------------------------
    if target_lang != "none" and source_lang != _argo_code(target_lang):
        titles = translate_texts([e.get("title", "") or "" for e in entries],
                                 target_lang, source_lang)
        descs = translate_texts(
            [(e.get("description", "") or e.get("summary", "") or "")
             for e in entries],
            target_lang, source_lang)
    else:
        titles = [e.get("title", "") or "" for e in entries]
        descs = [(e.get("description", "") or e.get("summary", "") or "")
                 for e in entries]

    # ---- Build RSS 2.0 XML ------------------------------------------------
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = f"{source_title} — xsukax filtered"
    ET.SubElement(channel, "link").text = parsed.feed.get(
        "link", feed_cfg["source_url"])
    desc_parts = ["Filtered & translated by xsukax RSS Filter & Translator."]
    if keywords:
        desc_parts.append("Keywords: " + ", ".join(keywords))
    if target_lang != "none":
        desc_parts.append(f"Translated locally to {target_lang}.")
    ET.SubElement(channel, "description").text = " ".join(desc_parts)
    if target_lang != "none":
        ET.SubElement(channel, "language").text = target_lang
    ET.SubElement(channel, "generator").text = "xsukax-rss-filter/1.1"

    self_url = url_for("serve_feed", token=token, _external=True)
    atom_link = ET.SubElement(channel, "atom:link")
    atom_link.set("href", self_url)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for entry, title, desc in zip(entries, titles, descs):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        link = entry.get("link", "") or ""
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "description").text = desc
        pub = entry.get("published", "") or entry.get("updated", "") or ""
        if pub:
            ET.SubElement(item, "pubDate").text = pub
        guid = ET.SubElement(item, "guid")
        guid.text = entry.get("id", "") or link or hashlib.sha1(
            title.encode("utf-8")).hexdigest()
        guid.set("isPermaLink",
                 "true" if (entry.get("id", "") or link).startswith("http")
                 else "false")

    return ("<?xml version='1.0' encoding='UTF-8'?>\n"
            + ET.tostring(rss, encoding="unicode"))


def build_feed_xml(feed_cfg):
    """Full pipeline: fetch -> filter -> translate -> limit -> render."""
    db = get_db()
    cached = db.execute(
        "SELECT xml, generated_at FROM feed_cache WHERE token=?",
        (feed_cfg["token"],)).fetchone()
    if cached and (time.time() - cached["generated_at"]) < FEED_TTL:
        return cached["xml"]

    parsed = fetch_source(feed_cfg["source_url"])
    keywords = json.loads(feed_cfg["keywords"])
    entries = [e for e in parsed.entries
               if item_matches(e, keywords, feed_cfg["match_mode"])
               ][: feed_cfg["max_items"]]

    xml = render_feed(feed_cfg, parsed, entries)
    db.execute(
        "INSERT INTO feed_cache (token, xml, generated_at) VALUES (?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET xml=excluded.xml, "
        "generated_at=excluded.generated_at",
        (feed_cfg["token"], xml, int(time.time())))
    db.commit()
    return xml


# --------------------------------------------------------------------------
# Web application
# --------------------------------------------------------------------------

app = Flask(__name__)


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------- auth ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        now = time.monotonic()
        if now < _login_state["locked_until"]:
            wait = int(_login_state["locked_until"] - now)
            error = f"Too many failed attempts. Try again in {wait}s."
        else:
            password = request.form.get("password", "")
            if check_password_hash(get_setting("password_hash", ""), password):
                _login_state.update(fails=0, locked_until=0.0)
                session.clear()
                session["auth"] = True
                session.permanent = True
                return redirect(url_for("index"))
            _login_state["fails"] += 1
            time.sleep(1.0)   # slow down brute force
            if _login_state["fails"] >= LOGIN_MAX_FAILS:
                _login_state.update(
                    fails=0, locked_until=time.monotonic() + LOGIN_LOCKOUT)
                error = (f"Too many failed attempts. Locked for "
                         f"{LOGIN_LOCKOUT}s.")
            else:
                error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    error = success = None
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(get_setting("password_hash", ""), current):
            error = "Current password is wrong."
            time.sleep(1.0)
        elif len(new) < 4:
            error = "New password must be at least 4 characters."
        elif new != confirm:
            error = "New passwords do not match."
        else:
            set_setting("password_hash", generate_password_hash(new))
            success = "Password changed successfully."
            log.info("password changed")
    return render_template("password.html", error=error, success=success)


# ---------- feed generator ----------

def _target_languages():
    """Target languages the user can pick: installed Argos models only."""
    langs = {"none": "No translation (keep original)"}
    for p in installed_pairs():
        langs.setdefault(p["to_code"],
                         f"{p['to_name']} (from {p['from_name']})")
    return langs


@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template("index.html",
                           languages=_target_languages(),
                           default_max_items=DEFAULT_MAX_ITEMS)


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    source_url = (request.form.get("source_url") or "").strip()
    match_mode = request.form.get("match_mode", "title")
    target_lang = _argo_code(request.form.get("target_lang", "none"))
    keywords = [k.strip() for k in request.form.getlist("keywords")
                if k and k.strip()][:MAX_KEYWORDS]

    languages = _target_languages()
    errors = []
    if not source_url.lower().startswith(("http://", "https://")):
        errors.append("Please enter a valid RSS feed URL "
                      "(http:// or https://).")
    if match_mode not in ("title", "both"):
        match_mode = "title"
    if target_lang != "none" and target_lang not in languages:
        errors.append(f"No local translation model for '{target_lang}' is "
                      "installed. Install one on the Models page first.")
        target_lang = "none"
    try:
        max_items = int(request.form.get("max_items", DEFAULT_MAX_ITEMS))
        if not (1 <= max_items <= MAX_ITEMS_HARD_LIMIT):
            raise ValueError
    except ValueError:
        errors.append("Max items must be a number between 1 and "
                      f"{MAX_ITEMS_HARD_LIMIT}.")
        max_items = DEFAULT_MAX_ITEMS

    if errors:
        return render_template("index.html", languages=languages,
                               default_max_items=DEFAULT_MAX_ITEMS,
                               errors=errors, form=request.form), 400

    token = secrets.token_urlsafe(8)
    db = get_db()
    db.execute(
        "INSERT INTO feeds (token, source_url, keywords, match_mode, "
        "max_items, target_lang, created_at) VALUES (?,?,?,?,?,?,?)",
        (token, source_url, json.dumps(keywords, ensure_ascii=False),
         match_mode, max_items, target_lang, int(time.time())))
    db.commit()

    feed_url = url_for("serve_feed", token=token, _external=True)
    return render_template("index.html", languages=languages,
                           default_max_items=DEFAULT_MAX_ITEMS,
                           feed_url=feed_url, form=request.form,
                           saved_keywords=keywords)


@app.route("/feed/<token>", methods=["GET"])
@app.route("/feed/<token>.xml", methods=["GET"])
def serve_feed(token):
    """Public on purpose: RSS readers cannot log in — the random token is
    the secret."""
    cfg = get_db().execute("SELECT * FROM feeds WHERE token=?",
                           (token,)).fetchone()
    if cfg is None:
        abort(404)
    try:
        xml = build_feed_xml(cfg)
    except FeedFetchError as exc:
        return (f"xsukax RSS Filter & Translator — upstream error:\n{exc}",
                502, {"Content-Type": "text/plain; charset=utf-8"})
    return app.response_class(xml, mimetype="application/rss+xml; charset=utf-8")


# ---------- translation model management ----------

@app.route("/models")
@login_required
def models_page():
    avail = available_pairs()
    with _model_tasks_lock:
        tasks = dict(_model_tasks)
    return render_template("models.html",
                           installed=installed_pairs(),
                           available=avail,
                           tasks=tasks)


@app.route("/models/install", methods=["POST"])
@login_required
def models_install():
    from_code = _argo_code(request.form.get("from_code", ""))
    to_code = _argo_code(request.form.get("to_code", ""))
    key = f"{from_code}->{to_code}"
    if not from_code or not to_code:
        abort(400)
    with _model_tasks_lock:
        status = _model_tasks.get(key, "")
        already = status in ("downloading", "installing", "warming up")
    if not already:
        with _model_tasks_lock:
            _model_tasks[key] = "starting"
        threading.Thread(target=_install_model_worker,
                         args=(from_code, to_code), daemon=True).start()
    return redirect(url_for("models_page"))


@app.route("/models/delete", methods=["POST"])
@login_required
def models_delete():
    from_code = _argo_code(request.form.get("from_code", ""))
    to_code = _argo_code(request.form.get("to_code", ""))
    pkg_mod, _ = _argo()
    for p in pkg_mod.get_installed_packages():
        if p.from_code == from_code and p.to_code == to_code:
            try:
                pkg_mod.uninstall(p)
                _tr_objects.pop((from_code, to_code), None)
                log.info("uninstalled translation model %s->%s",
                         from_code, to_code)
            except Exception as exc:               # noqa: BLE001
                log.warning("model uninstall failed: %s", exc)
            break
    return redirect(url_for("models_page"))


# ---------- health ----------

@app.route("/health", methods=["GET"])
def health():
    info = {"status": "ok", "service": "xsukax-rss-filter", "port": PORT,
            "translation": "local (argos)"}
    if request.args.get("check"):
        pairs = installed_pairs()
        info["installed_models"] = [f"{p['from_code']}->{p['to_code']}"
                                    for p in pairs]
        if pairs and request.args.get("tl"):
            tl = _argo_code(request.args["tl"])
            src = next((p["from_code"] for p in pairs
                        if p["to_code"] == tl), None)
            try:
                if not src:
                    raise RuntimeError(f"no installed model translating to {tl}")
                t0 = time.monotonic()
                info["translation_selftest"] = {
                    "ok": True, "pair": f"{src}->{tl}",
                    "sample": _translate_one_local(
                        "Hello world, this is a test.", tl, src),
                    "seconds": round(time.monotonic() - t0, 2)}
            except Exception as exc:               # noqa: BLE001
                info["translation_selftest"] = {"ok": False,
                                                "error": str(exc)[:300]}
    return jsonify(info)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    init_db()
    app.secret_key = _secret_key()
    app.permanent_session_lifetime = SESSION_LIFETIME
    try:
        from waitress import serve
        print(f"xsukax RSS Filter & Translator listening on "
              f"http://{HOST}:{PORT}")
        serve(app, host=HOST, port=PORT, threads=4,
              channel_timeout=120, cleanup_interval=10)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
