"""Movie Night: log watched movies with per-person ratings and get suggestions.

Each household member rates movies separately. Suggestions are for any group of people:
  * TMDB "recommendations" for every movie the group rated, weighted by the group's average
    rating (loved movies pull their recommendations up, disliked ones push theirs down).
  * Claude, on demand, which reads the group's ratings and explains each pick.
Movies anyone in the group has seen are never suggested.
"""
import csv
import io
import json
import math
import re
import unicodedata
from datetime import date, datetime
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import anthropic
import requests
from flask import Flask, g, jsonify, request, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "/data/movies.db")
TMDB_KEY = os.environ.get("TMDB_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
TMDB_API = "https://api.themoviedb.org/3"
CACHE_TTL = 7 * 24 * 3600
EMBY_URL = os.environ.get("EMBY_URL", "").strip().rstrip("/")
EMBY_KEY = os.environ.get("EMBY_API_KEY", "").strip()
# Optional comma list of Emby user names to import; empty = every user
EMBY_USERS = [u.strip().lower() for u in os.environ.get("EMBY_USERS", "").split(",") if u.strip()]
EMBY_SYNC_HOURS = float(os.environ.get("EMBY_SYNC_HOURS", "6"))
OMDB_KEY = os.environ.get("OMDB_API_KEY", "").strip()   # optional: Rotten Tomatoes scores via omdbapi.com

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id          INTEGER PRIMARY KEY,
    tmdb_id     INTEGER UNIQUE,
    title       TEXT NOT NULL,
    year        INTEGER,
    poster_path TEXT,
    overview    TEXT,
    genres      TEXT DEFAULT '[]',
    watched_on  TEXT,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    emoji       TEXT DEFAULT '',
    aliases     TEXT DEFAULT '',            -- Emby / Netflix names that mean this person, comma separated
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    tmdb_id     INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    year        INTEGER,
    poster_path TEXT,
    overview    TEXT,
    reason      TEXT DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dismissed (
    tmdb_id     INTEGER PRIMARY KEY,
    title       TEXT,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS emby_queue (
    emby_id     TEXT PRIMARY KEY,
    tmdb_id     INTEGER,
    title       TEXT NOT NULL,
    year        INTEGER,
    poster_path TEXT,
    overview    TEXT,
    genres      TEXT DEFAULT '[]',
    watched_by  TEXT DEFAULT '',
    watched_on  TEXT,
    favorite    INTEGER DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | skipped
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS not_watched (    -- "this person did NOT watch it", even if their Emby/Netflix account did
    tmdb_id     INTEGER NOT NULL,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (tmdb_id, person_id)
);
CREATE TABLE IF NOT EXISTS skipped (        -- per person: "seen it, don't want to rate it"
    tmdb_id     INTEGER NOT NULL,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (tmdb_id, person_id)
);
CREATE TABLE IF NOT EXISTS tmdb_remap (     -- "Wrong movie?" fixes: imports matching from_id get to_id instead
    from_id     INTEGER PRIMARY KEY,
    to_id       INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    fetched_at  INTEGER NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


WATCHES_SQL = """
CREATE TABLE IF NOT EXISTS watches (           -- person has seen movie; rating NULL = not rated yet
    movie_id    INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    rating      INTEGER CHECK (rating BETWEEN 1 AND 10),
    notes       TEXT DEFAULT '',
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (movie_id, person_id)
);
CREATE INDEX IF NOT EXISTS watches_person ON watches(person_id);
"""


def _migrate(conn):
    # kid setting: highest US rating this person should be suggested (NULL = anything)
    pcols = [r[1] for r in conn.execute("PRAGMA table_info(people)")]
    if "max_cert" not in pcols:
        conn.execute("ALTER TABLE people ADD COLUMN max_cert TEXT")
    # birthdate: imported viewings from before `count_from_age` don't count for that person
    if "birthdate" not in pcols:
        conn.execute("ALTER TABLE people ADD COLUMN birthdate TEXT")
        conn.execute("ALTER TABLE people ADD COLUMN count_from_age INTEGER NOT NULL DEFAULT 5")
    # where a Watched movie came from (emby / netflix / Rental...): shared accounts show for everyone
    if "source" not in [r[1] for r in conn.execute("PRAGMA table_info(movies)")] and \
            "rating" not in [r[1] for r in conn.execute("PRAGMA table_info(movies)")]:
        conn.execute("ALTER TABLE movies ADD COLUMN source TEXT")
    # Netflix import: where a queued movie came from
    if "source" not in [r[1] for r in conn.execute("PRAGMA table_info(emby_queue)")]:
        conn.execute("ALTER TABLE emby_queue ADD COLUMN source TEXT NOT NULL DEFAULT 'emby'")
    # Per-person ratings: the old movies table had one rating + free-text watched_by.
    # (watches is created after the rename so its foreign key points at the new table.)
    if "rating" in [r[1] for r in conn.execute("PRAGMA table_info(movies)")]:
        old = conn.execute("SELECT * FROM movies").fetchall()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""
            ALTER TABLE movies RENAME TO movies_old;
            CREATE TABLE movies (id INTEGER PRIMARY KEY, tmdb_id INTEGER UNIQUE, title TEXT NOT NULL, year INTEGER,
                poster_path TEXT, overview TEXT, genres TEXT DEFAULT '[]', watched_on TEXT, created_at INTEGER NOT NULL);
            INSERT INTO movies (id,tmdb_id,title,year,poster_path,overview,genres,watched_on,created_at)
                SELECT id,tmdb_id,title,year,poster_path,overview,genres,watched_on,created_at FROM movies_old;
            DROP TABLE movies_old;
        """)
        conn.executescript(WATCHES_SQL)
        now = int(time.time())
        for m in old:
            for name in [n.strip() for n in (m["watched_by"] or "").split(",") if n.strip()] or ["Household"]:
                conn.execute("INSERT OR IGNORE INTO people (name, created_at) VALUES (?,?)", (name, now))
                pid = conn.execute("SELECT id FROM people WHERE name=?", (name,)).fetchone()[0]
                conn.execute("INSERT OR IGNORE INTO watches VALUES (?,?,?,?,?)", (m["id"], pid, m["rating"], m["notes"] or "", now))
        conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(WATCHES_SQL)


with connect() as _c:
    _c.executescript(SCHEMA)
    _migrate(_c)


def merge_names(*lists):
    """Union of comma-separated name lists, keeping first-seen spelling."""
    seen = {}
    for lst in lists:
        for n in (lst or "").split(","):
            n = n.strip()
            if n and n.lower() not in seen:
                seen[n.lower()] = n
    return ", ".join(sorted(seen.values(), key=str.lower))


def people_by_alias(conn):
    """{lowercased name or alias: person_id}"""
    out = {}
    for p in conn.execute("SELECT id, name, aliases FROM people"):
        for n in [p["name"], *(p["aliases"] or "").split(",")]:
            if n.strip():
                out.setdefault(n.strip().lower(), p["id"])
    return out


def resolve_names(conn, names):
    """'jsmith, Grandma' -> ({person ids}, [names not linked to anyone])"""
    idx = people_by_alias(conn)
    ids, unknown = set(), []
    for n in (names or "").split(","):
        n = n.strip()
        if not n:
            continue
        if n.lower() in idx:
            ids.add(idx[n.lower()])
        else:
            unknown.append(n)
    return ids, unknown


def declined_map(conn):
    """{tmdb_id: {person ids who said they did NOT watch it}}"""
    out = {}
    for r in conn.execute("SELECT tmdb_id, person_id FROM not_watched"):
        out.setdefault(r[0], set()).add(r[1])
    return out


def age_cutoffs(conn):
    """{person_id: 'YYYY-MM-DD'} = the day they turned count_from_age; viewings before it don't count."""
    out = {}
    for r in conn.execute("SELECT id, birthdate, count_from_age FROM people WHERE birthdate IS NOT NULL AND birthdate <> ''"):
        try:
            b = date.fromisoformat(r["birthdate"])
        except ValueError:
            continue
        age = r["count_from_age"] or 0
        try:
            out[r["id"]] = b.replace(year=b.year + age).isoformat()
        except ValueError:                      # born Feb 29
            out[r["id"]] = b.replace(year=b.year + age, day=28).isoformat()
    return out


def too_young(ids, watched_on, cutoffs):
    """The people in `ids` who were younger than their count-from age on `watched_on`."""
    if not watched_on:
        return set()
    day = str(watched_on)[:10]
    return {p for p in ids if p in cutoffs and day < cutoffs[p]}


def queue_watchers(conn, watched_by, tmdb_id, declined=None, watched_on=None, cutoffs=None):
    """People an Emby/Netflix/CSV entry credits, minus anyone who said "not me" and anyone who was
    too young when it was watched. -> (person ids, unlinked names, credited-to-anyone-at-all)"""
    who, unknown = resolve_names(conn, watched_by)
    declined = declined if declined is not None else declined_map(conn)
    cutoffs = cutoffs if cutoffs is not None else age_cutoffs(conn)
    return (who - declined.get(tmdb_id, set()) - too_young(who, watched_on, cutoffs),
            unknown, bool(who or unknown))


def tag_source(conn, mid, source):
    """Remember where a Watched movie came from. A shared account (Emby, rentals) wins over Netflix,
    which is per person, and is never overwritten."""
    if not (mid and source):
        return
    if source == "netflix":
        conn.execute("UPDATE movies SET source='netflix' WHERE id=? AND source IS NULL", (mid,))
    else:
        conn.execute("UPDATE movies SET source=? WHERE id=? AND (source IS NULL OR source='netflix')", (source, mid))


def is_shared(source):
    return bool(source) and source != "netflix"


def remapped(conn, tmdb_id):
    if not tmdb_id:
        return tmdb_id
    r = conn.execute("SELECT to_id FROM tmdb_remap WHERE from_id=?", (tmdb_id,)).fetchone()
    return r[0] if r else tmdb_id


def remember_remap(conn, old, new):
    if old and new and conn.execute("SELECT 1 FROM tmdb_remap WHERE from_id=? AND to_id=?", (new, old)).fetchone():
        conn.execute("DELETE FROM tmdb_remap WHERE from_id=?", (new,))   # switching back = undoing a fix
        conn.execute("UPDATE tmdb_remap SET to_id=? WHERE to_id=?", (new, old))  # and the fixes it re-pointed
        return
    if old and new and old != new:
        conn.execute("INSERT OR REPLACE INTO tmdb_remap VALUES (?,?,?)", (old, new, int(time.time())))
        conn.execute("DELETE FROM tmdb_remap WHERE from_id=?", (new,))        # don't map the right one away
        conn.execute("UPDATE tmdb_remap SET to_id=? WHERE to_id=?", (new, old))  # A->B then B->C => A->C


def tmdb_details(tid):
    d = slim(tmdb_get(f"/movie/{int(tid)}"))
    return d


def selected_people(conn, raw):
    """Parse ?people=1,2 / [1,2]; empty or 'all' means everyone."""
    allp = [r[0] for r in conn.execute("SELECT id FROM people ORDER BY id")]
    if isinstance(raw, str):
        raw = [x for x in raw.split(",") if x.strip()]
    ids = [int(x) for x in (raw or []) if str(x).strip().isdigit() and int(x) in allp]
    return ids or allp


def row_to_movie(r):
    d = dict(r)
    if "genres" in d:
        d["genres"] = json.loads(d["genres"] or "[]")
    return d


# ---------------------------------------------------------------- TMDB
class TMDBError(Exception):
    pass


def tmdb_get(path, params=None, cache=True):
    """GET from TMDB, cached in SQLite for a week. Works with either a v3 API key
    or a v4 read access token (the long eyJ... one)."""
    if not TMDB_KEY:
        raise TMDBError("TMDB_API_KEY is not set")
    params = dict(params or {})
    key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    conn = connect()
    try:
        if cache:
            hit = conn.execute(
                "SELECT value, fetched_at FROM cache WHERE key=?", (key,)
            ).fetchone()
            if hit and time.time() - hit["fetched_at"] < CACHE_TTL:
                return json.loads(hit["value"])
        headers = {"accept": "application/json"}
        if TMDB_KEY.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {TMDB_KEY}"
        else:
            params["api_key"] = TMDB_KEY
        resp = requests.get(TMDB_API + path, params=params, headers=headers, timeout=10)
        if resp.status_code == 401:
            raise TMDBError("TMDB rejected the API key")
        resp.raise_for_status()
        data = resp.json()
        if cache:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?,?,?)",
                (key, json.dumps(data), int(time.time())),
            )
            conn.commit()
        return data
    except requests.RequestException as e:
        raise TMDBError(f"TMDB request failed: {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------- Rotten Tomatoes (via OMDb)
_omdb_pause_until = 0.0     # set when OMDb rejects the key or the daily limit is hit


def rt_score(tid):
    """Rotten Tomatoes Tomatometer for a TMDB id, e.g. "92%", or None. OMDb is looked up by IMDb id
    (from TMDB). Results, including "no score", are cached for a week to stay under OMDb's daily limit."""
    global _omdb_pause_until
    key = f"omdb:{int(tid)}"
    conn = connect()
    try:
        hit = conn.execute("SELECT value, fetched_at FROM cache WHERE key=?", (key,)).fetchone()
        if hit and time.time() - hit["fetched_at"] < CACHE_TTL:
            return json.loads(hit["value"]).get("rt")
        if not OMDB_KEY or time.time() < _omdb_pause_until:
            return None
        try:
            imdb = tmdb_get(f"/movie/{int(tid)}").get("imdb_id")
        except TMDBError:
            return None
        rt = None
        if imdb:
            try:
                d = requests.get("https://www.omdbapi.com/", params={"i": imdb, "apikey": OMDB_KEY}, timeout=10).json()
            except (requests.RequestException, ValueError):
                return None
            err = d.get("Error") or ""
            if "key" in err.lower() or "limit" in err.lower():   # invalid/unactivated key or daily limit: retry in an hour
                _omdb_pause_until = time.time() + 3600
                app.logger.warning("OMDb: %s", err)
                return None
            rt = next((r["Value"] for r in d.get("Ratings") or [] if r.get("Source") == "Rotten Tomatoes"), None)
        conn.execute("INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?,?,?)",
                     (key, json.dumps({"rt": rt}), int(time.time())))
        conn.commit()
        return rt
    finally:
        conn.close()


def genre_map():
    try:
        return {g_["id"]: g_["name"] for g_ in tmdb_get("/genre/movie/list")["genres"]}
    except TMDBError:
        return {}


def slim(m, genres=None):
    """Reduce a TMDB movie object to what the UI needs."""
    genres = genres if genres is not None else {}
    date = m.get("release_date") or ""
    names = [genres[i] for i in m.get("genre_ids", []) if i in genres]
    if not names and m.get("genres"):
        names = [x["name"] for x in m["genres"]]
    return {
        "tmdb_id": m["id"],
        "title": m.get("title") or m.get("name"),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster_path": m.get("poster_path"),
        "overview": m.get("overview", ""),
        "genres": names,
        "vote_average": m.get("vote_average"),
        "vote_count": m.get("vote_count", 0),
    }


# ---------------------------------------------------------------- age ratings (kid setting)
CERT_ORDER = ["G", "PG", "PG-13", "R", "NC-17"]


def us_cert(tmdb_id):
    """US MPA rating from TMDB release dates (theatrical first), or None if there isn't one."""
    try:
        data = tmdb_get(f"/movie/{tmdb_id}/release_dates")
    except TMDBError:
        return None
    for country in data.get("results", []):
        if country.get("iso_3166_1") == "US":
            dates = sorted(country.get("release_dates", []), key=lambda d: (d.get("type") not in (3, 2), d.get("type", 9)))
            for d in dates:
                c = (d.get("certification") or "").strip().upper()
                if c in CERT_ORDER:
                    return c
    return None


def group_limit(conn, group):
    """Strictest max_cert among the people in the group, or None."""
    ranks = [CERT_ORDER.index(r[0]) for r in conn.execute(
        f"SELECT max_cert FROM people WHERE id IN ({','.join('?' * len(group))}) AND max_cert IS NOT NULL", list(group))
        if r[0] in CERT_ORDER]
    return CERT_ORDER[min(ranks)] if ranks else None


def apply_cert_limit(items, limit, keep=60):
    """Annotate items with their US rating; when there is a limit, drop anything above it or unrated.
    Checks in rank order and stops once `keep` items have passed."""
    if not limit:
        return items[:keep]
    allowed = set(CERT_ORDER[:CERT_ORDER.index(limit) + 1])
    out = []
    for i in range(0, len(items), 24):
        batch = [it for it in items[i:i + 24] if it.get("tmdb_id")]
        with ThreadPoolExecutor(max_workers=8) as pool:
            certs = list(pool.map(lambda it: us_cert(it["tmdb_id"]), batch))
        for it, c in zip(batch, certs):
            it["cert"] = c
            if c in allowed:
                out.append(it)
        if len(out) >= keep or i >= 240:      # don't fan out forever on a big candidate list
            break
    return out[:keep]


# ---------------------------------------------------------------- routes: app
@app.get("/")
def index():
    """index.html with ?v=<file time> on app.js / style.css, so phones pick up a new version on reload."""
    base = os.path.join(app.root_path, "static")
    html = open(os.path.join(base, "index.html"), encoding="utf-8").read()
    for f in ("app.js", "style.css"):
        v = int(os.path.getmtime(os.path.join(base, f)))
        html = html.replace(f'"{f}"', f'"{f}?v={v}"')
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/rt")
def rt_scores():
    """?ids=603,550 -> {"603": "83%", "550": null}: Rotten Tomatoes scores for the cards on screen."""
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()][:80]
    if not OMDB_KEY or not ids:
        return jsonify({})
    with ThreadPoolExecutor(8) as ex:
        return jsonify(dict(zip(map(str, ids), ex.map(rt_score, ids))))


@app.get("/api/status")
def status():
    conn = db()
    last = conn.execute("SELECT value FROM meta WHERE key='emby_last_sync'").fetchone()
    pending = conn.execute("SELECT COUNT(*) FROM emby_queue WHERE status='pending'").fetchone()[0]
    return jsonify(tmdb=bool(TMDB_KEY), claude=bool(os.environ.get("ANTHROPIC_API_KEY")),
                   emby=bool(EMBY_URL and EMBY_KEY), rt=bool(OMDB_KEY), emby_last_sync=json.loads(last[0]) if last else None,
                   emby_pending=pending, people=conn.execute("SELECT COUNT(*) FROM people").fetchone()[0])


@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    try:
        data = tmdb_get("/search/movie", {"query": q, "include_adult": "false"})
    except TMDBError as e:
        return jsonify(error=str(e)), 502
    genres = genre_map()
    watched = {r["tmdb_id"] for r in db().execute("SELECT tmdb_id FROM movies WHERE tmdb_id IS NOT NULL")}
    out = []
    for m in data.get("results", [])[:10]:
        s = slim(m, genres)
        s["watched"] = s["tmdb_id"] in watched
        out.append(s)
    return jsonify(out)


# ---------------------------------------------------------------- routes: people
@app.get("/api/people")
def list_people():
    conn = db()
    rows = conn.execute("""SELECT p.*, COUNT(w.rating) AS rated, COUNT(w.movie_id) - COUNT(w.rating) AS unrated
                           FROM people p LEFT JOIN watches w ON w.person_id = p.id GROUP BY p.id ORDER BY p.id""")
    people = [dict(r) for r in rows]
    # Emby / Netflix names nobody is linked to yet
    seen = set()
    for r in conn.execute("SELECT watched_by FROM emby_queue WHERE status='pending'"):
        seen |= {n.strip() for n in (r[0] or "").split(",") if n.strip()}
    last = conn.execute("SELECT value FROM meta WHERE key='emby_last_sync'").fetchone()
    if last:
        seen |= set(json.loads(last[0]).get("users", []))
    idx = people_by_alias(conn)
    unlinked = sorted({n for n in seen if n.lower() not in idx}, key=str.lower)
    return jsonify(people=people, unlinked=unlinked)


def clean_person(b, partial=False):
    out = {}
    if "name" in b or not partial:
        n = (b.get("name") or "").strip()[:40]
        if not n:
            raise ValueError("name is required")
        out["name"] = n
    if "emoji" in b:
        out["emoji"] = (b.get("emoji") or "").strip()[:8]
    if "aliases" in b:
        out["aliases"] = merge_names(b.get("aliases") or "")
    if "birthdate" in b:
        v = (b.get("birthdate") or "").strip()
        if v:
            try:
                if date.fromisoformat(v) > date.today():
                    raise ValueError
            except ValueError:
                raise ValueError("birthdate must be a past date (YYYY-MM-DD)")
        out["birthdate"] = v or None
    if "count_from_age" in b:
        try:
            a = int(b.get("count_from_age"))
        except (TypeError, ValueError):
            raise ValueError("count_from_age must be a number")
        if not 0 <= a <= 18:
            raise ValueError("count_from_age must be 0-18")
        out["count_from_age"] = a
    if "max_cert" in b:
        c = (b.get("max_cert") or "").strip().upper() or None
        if c is not None and c not in CERT_ORDER:
            raise ValueError("max_cert must be one of " + ", ".join(CERT_ORDER))
        out["max_cert"] = c
    return out


@app.post("/api/people")
def add_person():
    try:
        p = clean_person(request.get_json(force=True))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    conn = db()
    try:
        cur = conn.execute("INSERT INTO people (name, emoji, aliases, max_cert, created_at) VALUES (?,?,?,?,?)",
                           (p["name"], p.get("emoji", ""), p.get("aliases", ""), p.get("max_cert"), int(time.time())))
    except sqlite3.IntegrityError:
        return jsonify(error=f"{p['name']} already exists"), 409
    conn.commit()
    return jsonify(dict(conn.execute("SELECT * FROM people WHERE id=?", (cur.lastrowid,)).fetchone())), 201


@app.put("/api/people/<int:pid>")
def update_person(pid):
    conn = db()
    cur = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not cur:
        return jsonify(error="not found"), 404
    try:
        p = clean_person(request.get_json(force=True), partial=True)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    # Renaming keeps the old name as an alias so Emby/Netflix links don't break
    if "name" in p and p["name"].lower() != cur["name"].lower():
        p["aliases"] = merge_names(p.get("aliases", cur["aliases"]), cur["name"])
    if not p:
        return jsonify(error="nothing to update"), 400
    try:
        conn.execute(f"UPDATE people SET {','.join(k + '=?' for k in p)} WHERE id=?", [*p.values(), pid])
    except sqlite3.IntegrityError:
        return jsonify(error="Someone already has that name"), 409
    conn.commit()
    return jsonify(dict(conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()))


@app.delete("/api/people/<int:pid>")
def delete_person(pid):
    conn = db()
    conn.execute("DELETE FROM people WHERE id=?", (pid,))   # their ratings go with them (cascade)
    conn.commit()
    return "", 204


# ---------------------------------------------------------------- routes: watched
def movie_with_watches(conn, rows):
    rows = [row_to_movie(r) for r in rows]
    by_id = {m["id"]: m for m in rows}
    for m in rows:
        m["watches"] = []
    if by_id:
        q = f"SELECT movie_id, person_id, rating, notes FROM watches WHERE movie_id IN ({','.join('?' * len(by_id))})"
        for w in conn.execute(q, list(by_id)):
            by_id[w["movie_id"]]["watches"].append(
                {"person_id": w["person_id"], "rating": w["rating"], "notes": w["notes"] or ""})
    for m in rows:
        rs = [w["rating"] for w in m["watches"] if w["rating"]]
        m["avg_rating"] = round(sum(rs) / len(rs), 1) if rs else None
    return rows


@app.get("/api/movies")
def list_movies():
    conn = db()
    rows = conn.execute("SELECT * FROM movies ORDER BY COALESCE(watched_on,'') DESC, created_at DESC").fetchall()
    return jsonify(movie_with_watches(conn, rows))


def apply_watches(conn, mid, entries):
    """entries: [{person_id, rating (1-10 or null = seen, not rated), notes?, remove?}]"""
    valid = {r[0] for r in conn.execute("SELECT id FROM people")}
    now = int(time.time())
    for e in entries or []:
        pid = int(e.get("person_id", 0))
        if pid not in valid:
            raise ValueError(f"unknown person {pid}")
        tmdb = conn.execute("SELECT tmdb_id FROM movies WHERE id=?", (mid,)).fetchone()[0]
        if e.get("remove"):
            conn.execute("DELETE FROM watches WHERE movie_id=? AND person_id=?", (mid, pid))
            if tmdb:   # so the next Emby/Netflix sync doesn't put them back
                conn.execute("INSERT OR IGNORE INTO not_watched VALUES (?,?,?)", (tmdb, pid, now))
            continue
        if tmdb:
            conn.execute("DELETE FROM not_watched WHERE tmdb_id=? AND person_id=?", (tmdb, pid))
        r = e.get("rating")
        if r is not None:
            r = int(r)
            if not 1 <= r <= 10:
                raise ValueError("rating must be 1-10 (half stars)")
        conn.execute("""INSERT INTO watches (movie_id, person_id, rating, notes, updated_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(movie_id, person_id) DO UPDATE SET rating=excluded.rating,
                        notes=COALESCE(?, watches.notes), updated_at=excluded.updated_at""",
                     (mid, pid, r, (e.get("notes") or "").strip(), now,
                      (e.get("notes") or "").strip() if "notes" in e else None))


@app.post("/api/movies")
def add_movie():
    b = request.get_json(force=True)
    title = (b.get("title") or "").strip()
    if not title:
        return jsonify(error="title is required"), 400
    if not b.get("ratings"):
        return jsonify(error="Rate it for at least one person"), 400
    y = b.get("year")
    m = {"tmdb_id": b.get("tmdb_id"), "title": title, "year": int(y) if str(y or "").isdigit() else None,
         "poster_path": b.get("poster_path"), "overview": b.get("overview"),
         "genres": json.dumps(b.get("genres") or []), "watched_on": (b.get("watched_on") or "").strip() or None,
         "created_at": int(time.time())}
    if m["tmdb_id"] and TMDB_KEY:
        try:
            d = slim(tmdb_get(f"/movie/{int(m['tmdb_id'])}"))
            for k in ("poster_path", "overview", "year"):
                m[k] = m.get(k) or d[k]
            m["genres"] = json.dumps(d["genres"])
        except TMDBError:
            pass
    conn = db()
    if m["tmdb_id"]:
        existing = conn.execute("SELECT id FROM movies WHERE tmdb_id=?", (m["tmdb_id"],)).fetchone()
        if existing:
            return jsonify(error="Already in the watched list", id=existing["id"]), 409
    cur = conn.execute(f"INSERT INTO movies ({','.join(m)}) VALUES ({','.join('?' * len(m))})", list(m.values()))
    try:
        apply_watches(conn, cur.lastrowid, b["ratings"])
    except (ValueError, TypeError) as e:
        conn.rollback()
        return jsonify(error=str(e)), 400
    if m["tmdb_id"]:
        conn.execute("DELETE FROM watchlist WHERE tmdb_id=?", (m["tmdb_id"],))
        # it was waiting in To rate: keep the Emby/Netflix watchers as 'seen'
        for q in conn.execute("SELECT watched_by, watched_on FROM emby_queue WHERE tmdb_id=?", (m["tmdb_id"],)).fetchall():
            for pid in queue_watchers(conn, q[0], m["tmdb_id"], watched_on=q[1])[0]:
                conn.execute("INSERT OR IGNORE INTO watches (movie_id, person_id, rating, notes, updated_at) "
                             "VALUES (?,?,NULL,'',?)", (cur.lastrowid, pid, int(time.time())))
        conn.execute("DELETE FROM emby_queue WHERE tmdb_id=?", (m["tmdb_id"],))
    conn.commit()
    return jsonify(movie_with_watches(conn, conn.execute("SELECT * FROM movies WHERE id=?", (cur.lastrowid,)))[0]), 201


@app.put("/api/movies/<int:mid>")
def update_movie(mid):
    b = request.get_json(force=True)
    conn = db()
    if not conn.execute("SELECT 1 FROM movies WHERE id=?", (mid,)).fetchone():
        return jsonify(error="not found"), 404
    if "watched_on" in b:
        conn.execute("UPDATE movies SET watched_on=? WHERE id=?", ((b.get("watched_on") or "").strip() or None, mid))
    try:
        apply_watches(conn, mid, b.get("ratings"))
    except (ValueError, TypeError) as e:
        conn.rollback()
        return jsonify(error=str(e)), 400
    conn.commit()
    return jsonify(movie_with_watches(conn, conn.execute("SELECT * FROM movies WHERE id=?", (mid,)))[0])


@app.delete("/api/movies/<int:mid>")
def delete_movie(mid):
    conn = db()
    conn.execute("DELETE FROM movies WHERE id=?", (mid,))
    conn.commit()
    return "", 204


@app.post("/api/movies/<int:mid>/rematch")
def rematch_movie(mid):
    """Wrong movie? Point this Watched entry at another TMDB movie, keeping everyone's ratings.
    If the right movie is already in Watched, the two are merged (its existing ratings win)."""
    conn = db()
    cur = conn.execute("SELECT * FROM movies WHERE id=?", (mid,)).fetchone()
    try:
        new = int((request.get_json(force=True) or {}).get("tmdb_id"))
        d = tmdb_details(new)
    except (TypeError, ValueError):
        return jsonify(error="tmdb_id required"), 400
    except TMDBError as e:
        return jsonify(error=str(e)), 502
    if not cur:
        return jsonify(error="not found"), 404
    if cur["tmdb_id"] == new:
        return jsonify(movie_with_watches(conn, [cur])[0])
    other = conn.execute("SELECT id FROM movies WHERE tmdb_id=?", (new,)).fetchone()
    if other:
        conn.execute("""INSERT OR IGNORE INTO watches (movie_id, person_id, rating, notes, updated_at)
                        SELECT ?, person_id, rating, notes, updated_at FROM watches WHERE movie_id=?""", (other[0], mid))
        conn.execute("DELETE FROM movies WHERE id=?", (mid,))
        target = other[0]
    else:
        conn.execute("UPDATE movies SET tmdb_id=?, title=?, year=?, poster_path=?, overview=?, genres=? WHERE id=?",
                     (new, d["title"], d["year"], d["poster_path"], d["overview"], json.dumps(d["genres"]), mid))
        target = mid
    # "not watched" marks follow the movie
    conn.execute("UPDATE OR IGNORE not_watched SET tmdb_id=? WHERE tmdb_id=?", (new, cur["tmdb_id"]))
    remember_remap(conn, cur["tmdb_id"], new)
    conn.execute("DELETE FROM emby_queue WHERE tmdb_id=?", (new,))   # it was waiting in To rate too
    conn.commit()
    return jsonify(movie_with_watches(conn, conn.execute("SELECT * FROM movies WHERE id=?", (target,)))[0])


@app.post("/api/emby/queue/<emby_id>/rematch")
def rematch_queued(emby_id):
    """Wrong movie? Point a To rate entry at another TMDB movie."""
    conn = db()
    q = conn.execute("SELECT * FROM emby_queue WHERE emby_id=?", (emby_id,)).fetchone()
    try:
        new = int((request.get_json(force=True) or {}).get("tmdb_id"))
        d = tmdb_details(new)
    except (TypeError, ValueError):
        return jsonify(error="tmdb_id required"), 400
    except TMDBError as e:
        return jsonify(error=str(e)), 502
    if not q:
        return jsonify(error="not in queue"), 404
    result = "updated"
    rated = conn.execute("SELECT id FROM movies WHERE tmdb_id=?", (new,)).fetchone()
    dup = conn.execute("SELECT * FROM emby_queue WHERE tmdb_id=? AND emby_id<>?", (new, emby_id)).fetchone()
    if rated:            # the right movie is already rated: credit these watchers there as 'seen'
        tag_source(conn, rated[0], q["source"])
        for pid in queue_watchers(conn, q["watched_by"], q["tmdb_id"], watched_on=q["watched_on"])[0]:
            conn.execute("INSERT OR IGNORE INTO watches (movie_id, person_id, rating, notes, updated_at) "
                         "VALUES (?,?,NULL,'',?)", (rated[0], pid, int(time.time())))
        conn.execute("DELETE FROM emby_queue WHERE emby_id=?", (emby_id,))
        result = "merged_into_watched"
    elif dup:            # already waiting in To rate under the right movie: merge
        conn.execute("UPDATE emby_queue SET watched_by=?, watched_on=? WHERE emby_id=?",
                     (merge_names(dup["watched_by"], q["watched_by"]),
                      max(dup["watched_on"] or "", q["watched_on"] or "") or None, dup["emby_id"]))
        conn.execute("DELETE FROM emby_queue WHERE emby_id=?", (emby_id,))
        result = "merged"
    else:
        conn.execute("UPDATE emby_queue SET tmdb_id=?, title=?, year=?, poster_path=?, overview=?, genres=? WHERE emby_id=?",
                     (new, d["title"], d["year"], d["poster_path"], d["overview"], json.dumps(d["genres"]), emby_id))
    conn.execute("UPDATE OR IGNORE not_watched SET tmdb_id=? WHERE tmdb_id=?", (new, q["tmdb_id"]))
    remember_remap(conn, q["tmdb_id"], new)
    conn.commit()
    return jsonify(result=result, title=d["title"], year=d["year"])


# ---------------------------------------------------------------- routes: watchlist / dismiss
@app.get("/api/watchlist")
def get_watchlist():
    rows = db().execute("SELECT * FROM watchlist ORDER BY created_at DESC")
    return jsonify([dict(r) for r in rows])


@app.post("/api/watchlist")
def add_watchlist():
    b = request.get_json(force=True)
    if not b.get("tmdb_id") or not b.get("title"):
        return jsonify(error="tmdb_id and title required"), 400
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO watchlist (tmdb_id,title,year,poster_path,overview,reason,created_at) VALUES (?,?,?,?,?,?,?)",
        (b["tmdb_id"], b["title"], b.get("year"), b.get("poster_path"), b.get("overview", ""),
         b.get("reason", ""), int(time.time())),
    )
    conn.commit()
    return "", 204


@app.delete("/api/watchlist/<int:tmdb_id>")
def del_watchlist(tmdb_id):
    conn = db()
    conn.execute("DELETE FROM watchlist WHERE tmdb_id=?", (tmdb_id,))
    conn.commit()
    return "", 204


@app.post("/api/dismiss")
def dismiss():
    b = request.get_json(force=True)
    conn = db()
    conn.execute("INSERT OR REPLACE INTO dismissed (tmdb_id,title,created_at) VALUES (?,?,?)",
                 (b["tmdb_id"], b.get("title"), int(time.time())))
    conn.execute("DELETE FROM watchlist WHERE tmdb_id=?", (b["tmdb_id"],))
    conn.commit()
    return "", 204


@app.delete("/api/dismiss")
def clear_dismissed():
    conn = db()
    conn.execute("DELETE FROM dismissed")
    conn.commit()
    return "", 204


def excluded_ids(conn, group=None):
    """TMDB ids not to suggest to `group` (person ids; None = everyone): anything someone in the
    group has seen, anything dismissed, and queued Emby/Netflix movies watched by someone in the
    group (or by someone not linked to a person yet, to be safe)."""
    group = set(group) if group is not None else {r[0] for r in conn.execute("SELECT id FROM people")}
    ids = {r[0] for r in conn.execute("SELECT tmdb_id FROM dismissed")}
    ids |= {r[0] for r in conn.execute("SELECT tmdb_id, person_id FROM skipped") if r[1] in group}
    for r in conn.execute("SELECT m.tmdb_id, w.person_id FROM movies m LEFT JOIN watches w ON w.movie_id=m.id "
                          "WHERE m.tmdb_id IS NOT NULL"):
        if r[1] is None or r[1] in group:
            ids.add(r[0])
    dec, cut = declined_map(conn), age_cutoffs(conn)
    for r in conn.execute("SELECT tmdb_id, watched_by, watched_on FROM emby_queue WHERE tmdb_id IS NOT NULL"):
        who, unknown, credited = queue_watchers(conn, r[1], r[0], dec, r[2], cut)
        # unlinked names / no names at all: someone unknown watched it, keep it out to be safe.
        # Everyone credited said "not me": nobody known watched it, so it can be suggested.
        if unknown or not credited or who & group:
            ids.add(r[0])
    return ids


# ---------------------------------------------------------------- suggestions: TMDB
@app.get("/api/suggestions")
def suggestions():
    """Score every TMDB recommendation and "similar movie" of every movie the group rated.

    Each seed movie's weight is the group's average of (rating - 5.5) over the people in the group who
    rated it: 10/10 -> +4.5, 6/10 -> +0.5, 5/10 -> -0.5, 1/10 -> -4.5. So a movie one person loved and
    another hated mostly cancels out. TMDB's "similar" lists (genre/keyword matches) widen the pool, but
    they're noisy (the same popular movies turn up under unrelated seeds), so a movie gets only its single
    strongest "similar" match, at half weight, never a sum. Higher-ranked entries count more; a small
    popularity/quality term breaks ties. Movies someone *outside* the group loved that nobody in the
    group has seen are boosted too ("Ben loved this").
    """
    if not TMDB_KEY:
        return jsonify(error="TMDB_API_KEY is not set"), 503
    conn = db()
    group = selected_people(conn, request.args.get("people"))
    if not group:
        return jsonify([])
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people")}
    seeds = {}
    for r in conn.execute("SELECT m.tmdb_id, m.title, w.person_id, w.rating FROM movies m JOIN watches w ON w.movie_id=m.id "
                          "WHERE m.tmdb_id IS NOT NULL AND w.rating IS NOT NULL"):
        s_ = seeds.setdefault(r["tmdb_id"], {"title": r["title"], "in": [], "out": []})
        (s_["in"] if r["person_id"] in group else s_["out"]).append((r["person_id"], r["rating"]))
    seeds = {k: v for k, v in seeds.items() if v["in"]}
    if not seeds:
        return jsonify([])
    skip = excluded_ids(conn, group)
    saved = {r[0] for r in conn.execute("SELECT tmdb_id FROM watchlist")}

    # the strongest seeds first, capped so a big library doesn't fan out into hundreds of calls
    ranked = sorted(seeds.items(), key=lambda kv: -abs(sum(r for _, r in kv[1]["in"]) / len(kv[1]["in"]) - 5.5))[:80]

    SOURCES = (("recommendations", 1.0), ("similar", 0.5))      # (TMDB list, weight)

    def fetch(job):
        tid, kind = job
        try:
            return job, tmdb_get(f"/movie/{tid}/{kind}").get("results", [])
        except TMDBError:
            return job, []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(fetch, [(t, k) for t, _ in ranked for k, _ in SOURCES]))

    genres = genre_map()
    cands = {}
    for tid, seed in ranked:
        w = sum(r - 5.5 for _, r in seed["in"]) / len(seed["in"])
        fans = [names.get(p) for p, r in seed["in"] if r >= 8]
        avg = round(sum(r for _, r in seed["in"]) / len(seed["in"]), 1)
        for kind, kw in SOURCES:
            for pos, m in enumerate(results.get((tid, kind), [])):
                if m["id"] in skip or m["id"] == tid:
                    continue
                c = cands.setdefault(m["id"], {"movie": m, "score": 0.0, "sim_hi": 0.0, "sim_lo": 0.0, "because": {}})
                v = w * kw * (1 - pos / 40)
                if kind == "recommendations":
                    c["score"] += v
                else:                          # similar: keep only the strongest pull each way
                    c["sim_hi"], c["sim_lo"] = max(c["sim_hi"], v), min(c["sim_lo"], v)
                if w > 0:      # one "Because of" entry per seed, even if both lists have it
                    c["because"][tid] = (w, seed["title"], fans, avg, len(seed["in"]))

    # loved by someone outside the group, unseen by the group: a direct, strong signal
    loved_elsewhere = {}
    if len(group) < len(names):
        for r in conn.execute("SELECT m.*, w.person_id, w.rating FROM movies m JOIN watches w ON w.movie_id=m.id "
                              "WHERE m.tmdb_id IS NOT NULL AND w.rating >= 8"):
            if r["tmdb_id"] in skip or r["person_id"] in group:
                continue
            e = loved_elsewhere.setdefault(r["tmdb_id"], {"row": r, "fans": []})
            e["fans"].append((names.get(r["person_id"]), r["rating"]))

    out = []
    for c in cands.values():
        c["score"] += c["sim_hi"] + c["sim_lo"]
        if c["score"] <= 0 or not c["because"]:
            continue
        m = c["movie"]
        quality = (m.get("vote_average") or 0) / 10 * math.log10(1 + (m.get("vote_count") or 0)) * 0.3
        s_ = slim(m, genres)
        s_["score"] = round(c["score"] + quality, 2)
        s_["because"] = [{"title": t, "fans": f, "avg": a, "raters": n}
                         for _, t, f, a, n in sorted(c["because"].values(), key=lambda x: -x[0])[:3]]
        out.append(s_)
    for tid, e in loved_elsewhere.items():
        r = e["row"]
        best = max(x[1] for x in e["fans"])
        item = next((o for o in out if o["tmdb_id"] == tid), None)
        if item is None:
            item = {"tmdb_id": tid, "title": r["title"], "year": r["year"], "poster_path": r["poster_path"],
                    "overview": r["overview"], "genres": json.loads(r["genres"] or "[]"), "vote_average": None,
                    "score": 0.0, "because": []}
            out.append(item)
        item["score"] = round(item["score"] + (best - 5.5) * 1.5, 2)
        item["loved_by"] = [{"name": n, "rating": rt} for n, rt in e["fans"]]
    for o in out:
        o["saved"] = o["tmdb_id"] in saved
    out.sort(key=lambda x: x["score"], reverse=True)
    return jsonify(apply_cert_limit(out, group_limit(conn, group), keep=90))


# ---------------------------------------------------------------- suggestions: Claude
CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["title", "year", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You recommend movies for one or more members of a household, based on what each of them has
watched and how they rated it. Ratings are per person, out of 10. Read the whole list: notice the genres,
directors, eras and tones each person rates highly and poorly, and use their notes.

When the picks are for several people watching together, find common ground: favour movies that fit
what all of them enjoy, and avoid anything close to what one of them strongly disliked. When the picks
are for one person, tailor them to that person alone.

Suggest real, released feature films that nobody in the group has seen (never anything from the
watched or excluded lists). Movies another household member loved are fair game if the group hasn't seen
them. Mix a few well-known picks with a few less obvious ones. Each reason is one or two sentences
addressed to the group, naming the people and the specific movies on their lists it connects to."""


@app.post("/api/claude-suggestions")
def claude_suggestions():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify(error="ANTHROPIC_API_KEY is not set"), 503
    body = request.get_json(silent=True) or {}
    wish = (body.get("request") or "").strip()[:500]
    conn = db()
    group = selected_people(conn, body.get("people"))
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people")}
    if not group:
        return jsonify(error="Add the people in your household first"), 400
    rows = conn.execute("SELECT m.id, m.title, m.year, w.person_id, w.rating, w.notes FROM movies m "
                        "JOIN watches w ON w.movie_id=m.id ORDER BY m.title").fetchall()
    by_movie = {}
    for r in rows:
        by_movie.setdefault(r["id"], {"title": r["title"], "year": r["year"], "w": []})["w"].append(r)
    group_lines, elsewhere = [], []
    for m in by_movie.values():
        label = f"- {m['title']}" + (f" ({m['year']})" if m["year"] else "")
        mine = [w for w in m["w"] if w["person_id"] in group]
        if mine:
            parts = []
            for w in mine:
                p = f"{names[w['person_id']]} " + (f"{w['rating']}/10" if w["rating"] else "seen, not rated")
                if w["notes"]:
                    p += f' ("{w["notes"]}")'
                parts.append(p)
            group_lines.append(label + " — " + "; ".join(parts))
        else:
            fans = [f"{names[w['person_id']]} {w['rating']}/10" for w in m["w"] if (w["rating"] or 0) >= 8]
            if fans:
                elsewhere.append(label + " — " + ", ".join(fans))
    if not any("/10" in l for l in group_lines):
        return jsonify(error="Nobody in this group has rated anything yet"), 400
    who = [names[p] for p in group]
    movies = [{"title": m["title"], "year": m["year"]} for m in by_movie.values()
              if any(w["person_id"] in group for w in m["w"])]
    dismissed = conn.execute("SELECT title FROM dismissed WHERE title IS NOT NULL").fetchall()
    saved = conn.execute("SELECT title, year FROM watchlist").fetchall()

    if len(who) == 1:
        prompt = f"Suggest movies for {who[0]} on their own.\n\nWhat {who[0]} has watched:\n"
    else:
        prompt = (f"Suggest movies for {', '.join(who[:-1])} and {who[-1]} to watch together.\n\n"
                  "What they have watched, with each person's rating:\n")
    prompt += "\n".join(group_lines)
    if elsewhere:
        prompt += ("\n\nOther household members loved these, and nobody above has seen them (fair game):\n"
                   + "\n".join(elsewhere[:40]))
    if dismissed or saved:
        prompt += "\n\nAlready suggested and excluded (don't suggest these either):\n" + "\n".join(
            f"- {r['title']}" for r in [*dismissed, *saved])
    unrated = []
    dec, cut = declined_map(conn), age_cutoffs(conn)
    for r in conn.execute("SELECT tmdb_id, title, year, watched_by, watched_on FROM emby_queue ORDER BY watched_on DESC LIMIT 400"):
        ids, unknown, credited = queue_watchers(conn, r["watched_by"], r["tmdb_id"], dec, r["watched_on"], cut)
        if unknown or not credited or ids & set(group):
            unrated.append(r)
    if unrated:
        prompt += "\n\nAlso already watched, not rated yet (don't suggest these):\n" + "\n".join(
            f"- {r['title']}" + (f" ({r['year']})" if r["year"] else "") for r in unrated)
    limit = group_limit(conn, group)
    if limit:
        kids = [r[0] for r in conn.execute(
            f"SELECT name FROM people WHERE id IN ({','.join('?' * len(group))}) AND max_cert IS NOT NULL", group)]
        prompt += (f"\n\n{', '.join(kids)} {'is a child' if len(kids) == 1 else 'are children'}: only suggest movies "
                   f"rated {limit} or lower by the MPA (US), and make sure they suit that age.")
    prompt += "\n\n" + (f"What they're in the mood for: {wish}\n\n" if wish else "")
    prompt += "Suggest 8 movies."

    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": CLAUDE_SCHEMA},
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        return jsonify(error="Anthropic rejected the API key"), 502
    except anthropic.RateLimitError:
        return jsonify(error="Claude is rate limited, try again in a minute"), 503
    except anthropic.APIStatusError as e:
        detail = e.body.get("error", {}).get("message") if isinstance(e.body, dict) else None
        return jsonify(error=f"Claude API error {e.status_code}: {detail or e.message}"), 502
    except anthropic.APIConnectionError:
        return jsonify(error="Couldn't reach the Claude API"), 502

    if resp.stop_reason == "refusal":
        return jsonify(error="Claude declined this request"), 502
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        picks = json.loads(text)["suggestions"]
    except (ValueError, KeyError):
        return jsonify(error="Claude's answer couldn't be read" + (" (cut off)" if resp.stop_reason == "max_tokens" else "")), 502

    # Match each pick to TMDB for posters / ids, and drop anything already watched.
    skip = excluded_ids(conn, group)
    watched_titles = {(m["title"].lower(), m["year"]) for m in [*movies, *unrated]}
    genres = genre_map() if TMDB_KEY else {}

    def resolve(p):
        item = {"title": p["title"], "year": p["year"], "reason": p["reason"], "tmdb_id": None,
                "poster_path": None, "overview": "", "genres": []}
        if TMDB_KEY:
            try:
                res = tmdb_get("/search/movie", {"query": p["title"], "year": p["year"]}).get("results") \
                    or tmdb_get("/search/movie", {"query": p["title"]}).get("results")
                if res:
                    s = slim(res[0], genres)
                    item.update({k: s[k] for k in ("tmdb_id", "poster_path", "overview", "genres", "vote_average")})
                    item["title"], item["year"] = s["title"], s["year"] or p["year"]
            except TMDBError:
                pass
        return item

    with ThreadPoolExecutor(max_workers=8) as pool:
        items = list(pool.map(resolve, picks))
    saved_ids = {r[0] for r in conn.execute("SELECT tmdb_id FROM watchlist")}
    out = [i for i in items
           if not (i["tmdb_id"] and i["tmdb_id"] in skip)
           and (i["title"].lower(), i["year"]) not in watched_titles]
    for i in out:
        i["saved"] = i["tmdb_id"] in saved_ids
    if limit and TMDB_KEY:   # double-check Claude against the real ratings
        out = apply_cert_limit(out, limit, keep=len(out))
    return jsonify(out)


# ---------------------------------------------------------------- Netflix import
# Netflix: Account -> Profile -> Viewing activity -> "Download all" gives NetflixViewingHistory.csv
# with columns Title,Date. Episodes look like 'Show: Season 2: "Name"' / 'Show: Limited Series: Ep';
# movies are a bare title (which can itself contain a colon: "Mission: Impossible - Fallout").
# Only words that never start a movie subtitle. ("Part", "Vol.", "Chapter" do: "Dune: Part Two",
# "Kill Bill: Vol. 1", "John Wick: Chapter 4"; show episodes with those words have 2+ colons anyway.)
TV_MARKERS = re.compile(
    r":\s*(season|series \d|limited series|miniseries|episode|temporada|staffel|saison)\b", re.I)


def norm_title(t):
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = t.replace("&", "and")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"^(the|a|an) ", "", t.strip())


def parse_netflix_date(raw, day_first):
    raw = raw.strip()
    for fmt in ("%Y-%m-%d",) + (("%d/%m/%y", "%d/%m/%Y", "%d.%m.%y", "%d.%m.%Y") if day_first
                                 else ("%m/%d/%y", "%m/%d/%Y")):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def parse_netflix_csv(text):
    """-> (movie candidates {title: last_date}, episode_rows, bad_rows)"""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows or [c.strip().lower() for c in rows[0][:2]] != ["title", "date"]:
        raise ValueError("That doesn't look like NetflixViewingHistory.csv (expected a Title,Date header)")
    rows = [r for r in rows[1:] if len(r) >= 2 and r[0].strip()]
    # US exports are M/D/YY; other regions D/M/YY. Any first field > 12 means day-first.
    firsts = [int(m.group(1)) for r in rows if (m := re.match(r"(\d{1,2})[/.]", r[1].strip()))]
    day_first = any(f > 12 for f in firsts)

    # A prefix (before the first colon) that shows up with several different suffixes is a show.
    suffixes = {}
    for r in rows:
        if ":" in r[0]:
            pre, suf = r[0].split(":", 1)
            suffixes.setdefault(pre.strip().lower(), set()).add(suf.strip().lower())
    movies, episodes, bad = {}, 0, 0
    for r in rows:
        title = r[0].strip()
        d = parse_netflix_date(r[1], day_first)
        if not d:
            bad += 1
            continue
        if ":" in title:
            pre = title.split(":", 1)[0].strip().lower()
            if TV_MARKERS.search(title) or title.count(":") >= 2 or len(suffixes.get(pre, ())) > 1:
                episodes += 1
                continue
        movies[title] = max(movies.get(title, date.min), d)
    return movies, episodes, bad


def match_movie(title, watched=None, year=None):
    """Find the TMDB movie for an imported title. Only accept an exact (normalized) title match, released
    no later than it was watched; with a year hint, the closest year (within 1) wins, otherwise the
    most-voted. Returns (tmdb movie, None) or (None, why)."""
    want = norm_title(title)
    try:
        res = tmdb_get("/search/movie", {"query": title}).get("results", [])
    except TMDBError as e:
        return None, str(e)
    hits = []
    for m in res:
        if want not in (norm_title(m.get("title") or ""), norm_title(m.get("original_title") or "")):
            continue
        y = (m.get("release_date") or "")[:4]
        if y.isdigit() and watched and int(y) > watched.year:
            continue
        if year and not (y.isdigit() and abs(int(y) - year) <= 1):
            continue
        hits.append(m)
    if not hits:
        # A colon title with no movie match is most likely a one-off TV special / episode
        return None, "no exact movie match"
    if year:
        return min(hits, key=lambda m: (abs(int(m["release_date"][:4]) - year), -(m.get("vote_count") or 0))), None
    return max(hits, key=lambda m: m.get("vote_count") or 0), None


@app.post("/api/import/netflix")
def import_netflix():
    if not TMDB_KEY:
        return jsonify(error="The Netflix import needs TMDB_API_KEY to match titles"), 503
    f = request.files.get("file")
    who = (request.form.get("profile") or "").strip()
    if not f:
        return jsonify(error="Choose the NetflixViewingHistory.csv file"), 400
    raw = f.read(20_000_000)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        movies, episodes, bad = parse_netflix_csv(text)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    with ThreadPoolExecutor(max_workers=8) as pool:
        matched = list(pool.map(lambda kv: (kv[0], kv[1], *match_movie(*kv)), movies.items()))

    stats = queue_matches(db(), matched, who, "netflix")
    return jsonify(movies=len(movies), episodes=episodes, bad_rows=bad, **stats)


def queue_matches(conn, matched, who, source):
    """Put imported matches into the To rate queue. matched: [(title, watched date|None, tmdb movie|None, why)].
    Already-rated movies get `who` added as 'seen'; already-queued ones get `who` merged in."""
    genres = genre_map()
    rated = {r[0] for r in conn.execute("SELECT tmdb_id FROM movies WHERE tmdb_id IS NOT NULL")}
    queued = {r["tmdb_id"]: r for r in conn.execute(
        "SELECT emby_id, tmdb_id, watched_by, watched_on FROM emby_queue WHERE tmdb_id IS NOT NULL")}
    added = merged = already = 0
    unmatched, seen = [], {}
    now = int(time.time())
    for title, watched, m, why in matched:
        if not m:
            unmatched.append(title)
            continue
        fixed = remapped(conn, m["id"])
        if fixed != m["id"]:
            try:
                m = tmdb_get(f"/movie/{fixed}")
            except TMDBError:
                pass
        tid, day = m["id"], (watched.isoformat() if watched else "")
        if tid in seen:                       # two imported titles -> same movie
            seen[tid] = max(seen[tid], day)
            continue
        seen[tid] = day
        if tid in rated:
            already += 1
            mid = conn.execute("SELECT id FROM movies WHERE tmdb_id=?", (tid,)).fetchone()[0]
            tag_source(conn, mid, source)
            for pid in queue_watchers(conn, who, tid, watched_on=day or None)[0]:
                conn.execute("INSERT OR IGNORE INTO watches (movie_id, person_id, rating, notes, updated_at) "
                             "VALUES (?,?,NULL,'',?)", (mid, pid, now))
        elif tid in queued:
            q = queued[tid]
            conn.execute("UPDATE emby_queue SET watched_by=?, watched_on=? WHERE emby_id=?",
                         (merge_names(q["watched_by"], who), max(q["watched_on"] or "", day) or None, q["emby_id"]))
            merged += 1
        else:
            s_ = slim(m, genres)
            key = f"{'netflix' if source == 'netflix' else 'csv'}:{tid}"
            conn.execute(
                "INSERT INTO emby_queue (emby_id,tmdb_id,title,year,poster_path,overview,genres,watched_by,"
                "watched_on,favorite,status,created_at,source) VALUES (?,?,?,?,?,?,?,?,?,0,'pending',?,?)",
                (key, tid, s_["title"], s_["year"], s_["poster_path"], s_["overview"],
                 json.dumps(s_["genres"]), who, day or None, now, source))
            queued[tid] = {"emby_id": key, "watched_by": who, "watched_on": day}
            added += 1
    for tid, day in seen.items():             # later rows for the same movie: keep the newest date
        if day:
            conn.execute("UPDATE emby_queue SET watched_on=MAX(COALESCE(watched_on,''),?) WHERE tmdb_id=?", (day, tid))
    conn.commit()
    return {"added": added, "merged": merged, "already_rated": already, "unmatched": sorted(set(unmatched))}


# ---------------------------------------------------------------- generic CSV import (rental lists etc.)
COLUMN_GUESSES = {
    "title": ["title", "movie title", "movie", "film", "name", "product name", "product", "item", "video", "content title", "description"],
    "year": ["year", "release year", "released", "release date"],
    "date": ["date watched", "watched", "watched on", "rental date", "rented", "date rented", "order date", "purchase date",
             "transaction date", "date", "last watched", "start date"],
}
FORMAT_TAGS = re.compile(
    r"\s*[\(\[]\s*(hd|sd|uhd|4k|4k uhd|4k ultra hd|ultra hd|hdr|dolby vision|rental|rent|purchase|buy|digital|digital hd|"
    r"blu-?ray|dvd|extended( edition)?|theatrical( cut| version)?|director'?s cut|unrated|uncut|special edition|"
    r"[a-z ]*edition|subtitled|dubbed|english|plus bonus content|with bonus content|bonus)\s*[\)\]]",
    re.I)
TV_ROW = re.compile(r"\bS\d{1,2}\s*E\d{1,3}\b|\bseason\s+\d+|\bepisode\s+\d+|\bseries\s+\d+|\bep\.?\s*\d+", re.I)


def read_csv_upload(f):
    raw = f.read(20_000_000)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("That file has no rows under the header")
    return rows[0], rows[1:]


def guess_columns(header):
    h = [c.strip().lower() for c in header]
    out = {}
    for field, names in COLUMN_GUESSES.items():
        out[field] = next((h.index(n) for n in names if n in h), None)
        if out[field] is None:   # partial match: "Movie Title (US)", "Date Rented"
            out[field] = next((i for n in names for i, c in enumerate(h) if n in c and i not in out.values()), None)
    return out


def clean_rental_title(t):
    """'Dune: Part Two (4K UHD) (2024) - Rental' -> ('Dune: Part Two', 2024)"""
    t = re.sub(r"\s*[-–|]\s*(rental|rent|purchase|buy|hd|sd|uhd|4k)\s*$", "", t.strip(), flags=re.I)
    prev = None
    while prev != t:
        prev = t
        t = FORMAT_TAGS.sub("", t).strip()
    year = None
    m = re.search(r"\s*[\(\[](19\d{2}|20\d{2})[\)\]]\s*$", t)
    if m:
        year, t = int(m.group(1)), t[:m.start()].strip()
    return t, year


def parse_any_date(raw, day_first=False):
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    if raw.isdigit() and len(raw) in (10, 13):            # unix timestamp (s or ms)
        try:
            return datetime.utcfromtimestamp(int(raw) / (1000 if len(raw) == 13 else 1)).date()
        except (ValueError, OSError):
            return None
    raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw).replace(",", "")
    fmts = ["%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d-%m-%Y"] if day_first else ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"]
    fmts += ["%Y/%m/%d", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%a %b %d %Y", "%A %B %d %Y", "%b %Y", "%B %Y"]
    first = raw.split(" ")[0] if re.match(r"\d", raw) else raw
    for cand in dict.fromkeys([raw, first, " ".join(raw.split(" ")[:4]), " ".join(raw.split(" ")[:3])]):
        for fmt in fmts:
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                pass
    return None


@app.post("/api/import/csv/preview")
def import_csv_preview():
    f = request.files.get("file")
    if not f:
        return jsonify(error="Choose a CSV file"), 400
    try:
        header, rows = read_csv_upload(f)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(columns=header, guess=guess_columns(header), sample=rows[:5], rows=len(rows))


@app.post("/api/import/csv")
def import_csv():
    """Generic import: form fields file, profile (person name), source (label), title_col, year_col, date_col."""
    if not TMDB_KEY:
        return jsonify(error="The import needs TMDB_API_KEY to match titles"), 503
    f = request.files.get("file")
    who = (request.form.get("profile") or "").strip()
    source = re.sub(r"[^\w .&+-]", "", (request.form.get("source") or "").strip())[:24] or "Rental"
    if not f:
        return jsonify(error="Choose a CSV file"), 400
    try:
        header, rows = read_csv_upload(f)
        col = lambda k: int(request.form[k]) if str(request.form.get(k, "")).strip().lstrip("-").isdigit() and int(request.form[k]) >= 0 else None
        tc, yc, dc = col("title_col"), col("year_col"), col("date_col")
        if tc is None or tc >= len(header):
            raise ValueError("Pick which column has the movie titles")
    except ValueError as e:
        return jsonify(error=str(e)), 400

    # day-first dates if any first number is > 12
    day_first = dc is not None and any(
        (m := re.match(r"(\d{1,2})[/.-]", r[dc].strip())) and int(m.group(1)) > 12 for r in rows if dc < len(r))
    cands, tv, bad = {}, 0, 0
    for r in rows:
        if tc >= len(r) or not r[tc].strip():
            bad += 1
            continue
        title, year = clean_rental_title(r[tc])
        if yc is not None and yc < len(r) and not year:
            m = re.search(r"(19|20)\d{2}", r[yc])
            year = int(m.group(0)) if m else None
        if TV_ROW.search(title):
            tv += 1
            continue
        d = parse_any_date(r[dc], day_first) if dc is not None and dc < len(r) else None
        key = (title, year)
        cands[key] = max(cands.get(key) or date.min, d or date.min)
    with ThreadPoolExecutor(max_workers=8) as pool:
        matched = list(pool.map(
            lambda kv: (kv[0][0] + (f" ({kv[0][1]})" if kv[0][1] else ""),
                        None if kv[1] == date.min else kv[1],
                        *match_movie(kv[0][0], None if kv[1] == date.min else kv[1], kv[0][1])),
            cands.items()))
    stats = queue_matches(db(), matched, who, source)
    return jsonify(movies=len(cands), episodes=tv, bad_rows=bad, **stats)


# ---------------------------------------------------------------- Emby import
class EmbyError(Exception):
    pass


def emby_get(path, params=None):
    try:
        r = requests.get(f"{EMBY_URL}/emby{path}", params=params or {},
                         headers={"X-Emby-Token": EMBY_KEY, "accept": "application/json"}, timeout=30)
    except requests.RequestException as e:
        raise EmbyError(f"Couldn't reach Emby: {e}") from e
    if r.status_code in (401, 403):
        raise EmbyError("Emby rejected the API key")
    if not r.ok:
        raise EmbyError(f"Emby returned {r.status_code}")
    return r.json()


_sync_lock = threading.Lock()


def emby_sync():
    """Pull every played movie for each Emby user into the "To rate" queue.

    Movies already rated (matched by TMDB id, or title+year) are skipped. A movie already in the
    queue just gets its watchers / last-played date / favorite flag refreshed; skipped ones stay skipped."""
    if not (EMBY_URL and EMBY_KEY):
        raise EmbyError("EMBY_URL / EMBY_API_KEY are not set")
    if not _sync_lock.acquire(blocking=False):
        raise EmbyError("A sync is already running")
    try:
        users = [u for u in emby_get("/Users")
                 if not EMBY_USERS or u["Name"].lower() in EMBY_USERS]
        played = {}
        for u in users:
            items = emby_get(f"/Users/{u['Id']}/Items", {
                "IncludeItemTypes": "Movie", "Recursive": "true", "IsPlayed": "true",
                "Fields": "ProviderIds,ProductionYear,Overview,Genres,UserDataLastPlayedDate,UserDataPlayCount",
                "EnableUserData": "true",  # Emby 4.10 omits LastPlayedDate unless asked for in Fields
            }).get("Items", [])
            for it in items:
                ud = it.get("UserData") or {}
                p = played.setdefault(it["Id"], {"item": it, "users": [], "last": "", "fav": False})
                p["users"].append(u["Name"])
                p["last"] = max(p["last"], (ud.get("LastPlayedDate") or "")[:10])
                p["fav"] = p["fav"] or bool(ud.get("IsFavorite"))

        conn = connect()
        rated_ids = {r[0] for r in conn.execute("SELECT tmdb_id FROM movies WHERE tmdb_id IS NOT NULL")}
        rated_titles = {(r[0].lower(), r[1]) for r in conn.execute("SELECT title, year FROM movies")}
        queued = {r["emby_id"]: r for r in conn.execute("SELECT emby_id, watched_by FROM emby_queue")}
        queued_tmdb = {r["tmdb_id"]: r for r in conn.execute(
            "SELECT emby_id, tmdb_id, watched_by, watched_on, source FROM emby_queue WHERE tmdb_id IS NOT NULL")}

        def tmdb_id_of(it):
            v = (it.get("ProviderIds") or {}).get("Tmdb") or (it.get("ProviderIds") or {}).get("tmdb")
            return int(v) if v and str(v).isdigit() else None

        new = []
        for eid, p in played.items():
            it = p["item"]
            tid = remapped(conn, tmdb_id_of(it))
            if (tid and tid in rated_ids) or (it.get("Name", "").lower(), it.get("ProductionYear")) in rated_titles:
                if tid and tid in rated_ids:     # make sure each Emby watcher shows as having seen it
                    mid = conn.execute("SELECT id FROM movies WHERE tmdb_id=?", (tid,)).fetchone()[0]
                    tag_source(conn, mid, "emby")
                    for pid in queue_watchers(conn, ", ".join(p["users"]), tid, watched_on=p["last"] or None)[0]:
                        conn.execute("INSERT OR IGNORE INTO watches (movie_id, person_id, rating, notes, updated_at) "
                                     "VALUES (?,?,NULL,'',?)", (mid, pid, int(time.time())))
                continue
            if eid in queued:
                conn.execute("UPDATE emby_queue SET watched_by=?, watched_on=?, favorite=? WHERE emby_id=?",
                             (merge_names(queued[eid]["watched_by"], ", ".join(p["users"])), p["last"] or None,
                              int(p["fav"]), eid))
            elif tid and tid in queued_tmdb:      # already queued from Netflix etc.
                o = queued_tmdb[tid]
                conn.execute("UPDATE emby_queue SET watched_by=?, watched_on=?, favorite=MAX(favorite,?) WHERE emby_id=?",
                             (merge_names(o["watched_by"], ", ".join(p["users"])),
                              max(o["watched_on"] or "", p["last"] or "") or None, int(p["fav"]), o["emby_id"]))
            else:
                new.append((eid, tid, p))
        conn.commit()

        # Posters / genres come from TMDB (Emby image URLs would need the API key in the browser).
        genres = genre_map() if TMDB_KEY else {}

        def details(entry):
            eid, tid, p = entry
            d = None
            if tid and TMDB_KEY:
                try:
                    d = slim(tmdb_get(f"/movie/{tid}"), genres)
                except TMDBError:
                    pass
            return entry, d

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(details, new))
        now = int(time.time())
        for (eid, tid, p), d in results:
            it = p["item"]
            conn.execute(
                "INSERT OR IGNORE INTO emby_queue (emby_id,tmdb_id,title,year,poster_path,overview,genres,"
                "watched_by,watched_on,favorite,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (eid, tid, it.get("Name") or "Untitled", it.get("ProductionYear") or (d or {}).get("year"),
                 (d or {}).get("poster_path"), (d or {}).get("overview") or it.get("Overview", ""),
                 json.dumps((d or {}).get("genres") or it.get("Genres") or []),
                 ", ".join(sorted(p["users"])), p["last"] or None, int(p["fav"]), now))
        summary = {"at": now, "users": [u["Name"] for u in users], "played": len(played), "added": len(new)}
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('emby_last_sync', ?)", (json.dumps(summary),))
        conn.commit()
        conn.close()
        return summary
    finally:
        _sync_lock.release()


@app.post("/api/emby/sync")
def emby_sync_route():
    try:
        return jsonify(emby_sync())
    except EmbyError as e:
        return jsonify(error=str(e)), 502


@app.get("/api/emby/queue")
def emby_queue():
    conn = db()
    dec, cut = declined_map(conn), age_cutoffs(conn)
    out = []
    for r in conn.execute("SELECT * FROM emby_queue WHERE status='pending' "
                          "ORDER BY favorite DESC, COALESCE(watched_on,'') DESC"):
        m = row_to_movie(r)
        who, unknown, _ = queue_watchers(conn, r["watched_by"], r["tmdb_id"], dec, r["watched_on"], cut)
        linked, _ = resolve_names(conn, r["watched_by"])
        young = too_young(linked, r["watched_on"], cut)
        if young and not (linked - young) and not unknown:
            continue                      # only credited to people who were too young then: hide it
        m["too_young"] = sorted(young)
        m["watchers"] = sorted(who)
        m["not_watched"] = sorted(dec.get(r["tmdb_id"], set()))
        m.pop("overview", None)          # not shown on queue cards; keeps a big queue light
        out.append(m)
    return jsonify(out)


@app.get("/api/torate")
def torate():
    """One person's To rate list: queued movies credited to them or from a shared account (Emby/rentals,
    watched after their count-from age), plus Watched movies marked seen by them but not rated — minus what they said they didn't watch, skipped, or (if credited to
    them) watched before their count-from age. Movies the imports credit to them come first."""
    conn = db()
    try:
        pid = int(request.args.get("person_id") or 0)
    except ValueError:
        pid = 0
    if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
        return jsonify(error="Pick who's rating first"), 400
    dec, cut = declined_map(conn), age_cutoffs(conn)
    skipped = {r[0] for r in conn.execute("SELECT tmdb_id FROM skipped WHERE person_id=?", (pid,))}
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people")}
    out = []
    for r in conn.execute("SELECT * FROM emby_queue WHERE status='pending'"):
        if r["tmdb_id"] in skipped:
            continue
        linked, unknown = resolve_names(conn, r["watched_by"])
        young = too_young(linked, r["watched_on"], cut)
        if pid in young:                      # watched as a toddler: not on their list
            continue
        declined = dec.get(r["tmdb_id"], set())
        credited = pid in linked and pid not in declined
        # Only what they could have seen: credited to them, or on a shared account (Emby/rentals)
        # after they were old enough to count. Netflix profiles are per person, so others' don't show.
        shared = (r["source"] or "") != "netflix"
        if not credited and pid not in declined:
            if not shared or (pid in cut and r["watched_on"] and str(r["watched_on"])[:10] < cut[pid]):
                continue
        m = row_to_movie(r)
        m.update(kind="queue", key=r["emby_id"], credited=credited,
                 declined=pid in declined, watchers=sorted(linked - declined - young),
                 not_watched=sorted(declined), too_young=sorted(young), ratings=[])
        out.append(m)
    watches = {}
    for w in conn.execute("SELECT movie_id, person_id, rating FROM watches"):
        watches.setdefault(w[0], {})[w[1]] = w[2]
    for r in conn.execute("SELECT id, tmdb_id, title, year, poster_path, overview, genres, watched_on, source FROM movies"):
        mine = watches.get(r["id"], {})
        if mine.get(pid) is not None or r["tmdb_id"] in skipped:
            continue
        declined = dec.get(r["tmdb_id"], set())
        if pid not in mine and pid not in declined:   # not marked as seen by them: only if from a shared
            if not is_shared(r["source"]) or (pid in cut and r["watched_on"] and str(r["watched_on"])[:10] < cut[pid]):
                continue                              # account (Emby/rental) after their count-from age
        m = row_to_movie(r)
        m.update(kind="movie", key=str(r["id"]), credited=pid in mine, declined=pid in declined,
                 source="watched", from_source=r["source"], watched_by="", favorite=0,
                 ratings=[{"name": names.get(p, "?"), "rating": rt} for p, rt in mine.items() if rt])
        out.append(m)
    out.sort(key=lambda m: m["watched_on"] or "", reverse=True)       # newest first...
    # ...within: household Emby/rental watches first, then credited to them, then the rest
    out.sort(key=lambda m: (m["declined"], not (m["kind"] == "queue" and m.get("source") != "netflix"),
                            not m["credited"]))
    return jsonify(out)


@app.post("/api/mark")
def mark():
    """Per-person marks on any movie (queued or in Watched), by TMDB id.
    {"tmdb_id": 5548, "person_id": 1, "action": "not_watched" | "undo_not_watched" | "skip" | "undo_skip"}"""
    conn = db()
    b = request.get_json(force=True) or {}
    try:
        tid, pid = int(b.get("tmdb_id")), int(b.get("person_id"))
    except (TypeError, ValueError):
        return jsonify(error="tmdb_id and person_id required"), 400
    if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
        return jsonify(error="Pick who's rating first"), 400
    now, act = int(time.time()), b.get("action")
    if act == "not_watched":
        conn.execute("INSERT OR IGNORE INTO not_watched VALUES (?,?,?)", (tid, pid, now))
        conn.execute("DELETE FROM watches WHERE person_id=? AND movie_id IN (SELECT id FROM movies WHERE tmdb_id=?)", (pid, tid))
    elif act == "undo_not_watched":
        conn.execute("DELETE FROM not_watched WHERE tmdb_id=? AND person_id=?", (tid, pid))
    elif act == "skip":
        conn.execute("INSERT OR IGNORE INTO skipped VALUES (?,?,?)", (tid, pid, now))
    elif act == "undo_skip":
        conn.execute("DELETE FROM skipped WHERE tmdb_id=? AND person_id=?", (tid, pid))
    else:
        return jsonify(error="unknown action"), 400
    conn.commit()
    return "", 204


@app.post("/api/emby/queue/<emby_id>/not-me")
def emby_not_me(emby_id):
    """A person says they did NOT watch this queued movie (e.g. someone used their Emby account).
    {"person_id": 1, "undo": false}"""
    conn = db()
    q = conn.execute("SELECT tmdb_id FROM emby_queue WHERE emby_id=?", (emby_id,)).fetchone()
    b = request.get_json(force=True) or {}
    pid = int(b.get("person_id") or 0)
    if not q or not q[0]:
        return jsonify(error="not in queue"), 404
    if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
        return jsonify(error="Pick who's rating first"), 400
    if b.get("undo"):
        conn.execute("DELETE FROM not_watched WHERE tmdb_id=? AND person_id=?", (q[0], pid))
    else:
        conn.execute("INSERT OR IGNORE INTO not_watched VALUES (?,?,?)", (q[0], pid, int(time.time())))
    conn.commit()
    return "", 204


@app.post("/api/emby/queue/<emby_id>/resolve")
def emby_resolve(emby_id):
    """Move a queued movie to Watched with exactly these watches (the "Who watched?" dialog).
    {"watches": [{"person_id": 1, "rating": 8}, {"person_id": 3, "rating": null}], "not_watched": [2]}"""
    conn = db()
    q = conn.execute("SELECT * FROM emby_queue WHERE emby_id=?", (emby_id,)).fetchone()
    if not q:
        return jsonify(error="not in queue"), 404
    b = request.get_json(force=True) or {}
    watches = [w for w in b.get("watches") or [] if not w.get("remove")]
    if not watches:
        return jsonify(error="Pick at least one person who watched it"), 400
    existing = q["tmdb_id"] and conn.execute("SELECT id FROM movies WHERE tmdb_id=?", (q["tmdb_id"],)).fetchone()
    mid = existing[0] if existing else conn.execute(
        "INSERT INTO movies (tmdb_id,title,year,poster_path,overview,genres,watched_on,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (q["tmdb_id"], q["title"], q["year"], q["poster_path"], q["overview"], q["genres"],
         b.get("watched_on") or q["watched_on"], int(time.time()))).lastrowid
    tag_source(conn, mid, q["source"])
    try:
        apply_watches(conn, mid, watches + [{"person_id": p, "remove": True} for p in b.get("not_watched") or []])
    except (ValueError, TypeError) as e:
        conn.rollback()
        return jsonify(error=str(e)), 400
    conn.execute("DELETE FROM emby_queue WHERE emby_id=?", (emby_id,))
    if q["tmdb_id"]:
        conn.execute("DELETE FROM watchlist WHERE tmdb_id=?", (q["tmdb_id"],))
    conn.commit()
    return "", 204


@app.post("/api/emby/queue/<emby_id>/rate")
def emby_rate(emby_id):
    """Rate a queued movie as one person. It moves to Watched; the other people the source says
    watched it are added as 'seen, not rated' so it shows up as needing their rating."""
    conn = db()
    q = conn.execute("SELECT * FROM emby_queue WHERE emby_id=?", (emby_id,)).fetchone()
    if not q:
        return jsonify(error="not in queue"), 404
    b = request.get_json(force=True) or {}
    try:
        r, pid = int(b.get("rating", 0)), int(b.get("person_id", 0))
    except (TypeError, ValueError):
        r = pid = 0
    if not 1 <= r <= 10:
        return jsonify(error="rating must be 1-10"), 400
    if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
        return jsonify(error="Pick who's rating first"), 400
    existing = q["tmdb_id"] and conn.execute("SELECT id FROM movies WHERE tmdb_id=?", (q["tmdb_id"],)).fetchone()
    if existing:
        mid = existing[0]
    else:
        mid = conn.execute(
            "INSERT INTO movies (tmdb_id,title,year,poster_path,overview,genres,watched_on,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (q["tmdb_id"], q["title"], q["year"], q["poster_path"], q["overview"], q["genres"],
             q["watched_on"], int(time.time()))).lastrowid
    tag_source(conn, mid, q["source"])
    watchers = queue_watchers(conn, q["watched_by"], q["tmdb_id"], watched_on=q["watched_on"])[0]
    apply_watches(conn, mid, [{"person_id": p, "rating": None} for p in watchers - {pid}
                              if not conn.execute("SELECT 1 FROM watches WHERE movie_id=? AND person_id=?", (mid, p)).fetchone()]
                  + [{"person_id": pid, "rating": r}])
    conn.execute("DELETE FROM emby_queue WHERE emby_id=?", (emby_id,))
    if q["tmdb_id"]:
        conn.execute("DELETE FROM watchlist WHERE tmdb_id=?", (q["tmdb_id"],))
    conn.commit()
    return "", 204


@app.post("/api/emby/queue/<emby_id>/skip")
def emby_skip(emby_id):
    conn = db()
    conn.execute("UPDATE emby_queue SET status='skipped' WHERE emby_id=?", (emby_id,))
    conn.commit()
    return "", 204


def _emby_background():
    time.sleep(60)
    while True:
        try:
            s = emby_sync()
            print(f"emby sync: {s['played']} played, {s['added']} new", flush=True)
        except Exception as e:  # keep the loop alive whatever happens
            print(f"emby sync failed: {e}", flush=True)
        time.sleep(EMBY_SYNC_HOURS * 3600)


if EMBY_URL and EMBY_KEY and EMBY_SYNC_HOURS > 0:
    threading.Thread(target=_emby_background, daemon=True, name="emby-sync").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
