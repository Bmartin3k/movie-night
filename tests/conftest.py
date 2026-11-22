"""Tests run against a throwaway database with no API keys; TMDB, OMDb and Radarr are faked where needed."""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="movie-night-tests-")
os.environ["DB_PATH"] = os.path.join(_tmp, "movies.db")
os.environ["BACKUP_KEEP"] = "0"          # no background backup thread during tests
os.environ["TMDB_DETAILS_WARM"] = "0"    # nor the TMDB details one
for k in ("TMDB_API_KEY", "OMDB_API_KEY", "ANTHROPIC_API_KEY", "EMBY_URL", "EMBY_API_KEY", "RADARR_URL", "RADARR_API_KEY"):
    os.environ[k] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import app as A  # noqa: E402

TABLES = ("watches", "movies", "people", "watchlist", "dismissed", "emby_queue", "not_watched", "skipped",
          "tmdb_remap", "meta", "cache")


@pytest.fixture(autouse=True)
def clean_db():
    conn = A.connect()
    for t in TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    A._radarr_lib.update(at=0.0, states={})
    A._lib_icon.update(at=0.0, img=None)
    A._omdb_pause_until = 0.0
    A._unlock_fails.update(n=0, until=0.0)
    A._pin_fails.clear()
    if os.path.exists(A.SETTINGS_PATH):
        os.remove(A.SETTINGS_PATH)
    yield


@pytest.fixture
def client():
    return A.app.test_client()


@pytest.fixture
def people(client):
    """Jane, Sam and Alex (a kid limited to PG). -> {"Jane": 1, ...}"""
    ids = {}
    for name in ("Jane", "Sam", "Alex"):
        ids[name] = client.post("/api/people", json={"name": name}).get_json()["id"]
    client.put(f"/api/people/{ids['Alex']}", json={"max_cert": "PG"})
    return ids


@pytest.fixture
def add_movie(client):
    def _add(tmdb_id, title, ratings, **extra):
        r = client.post("/api/movies", json={"tmdb_id": tmdb_id, "title": title, "year": 2020, **extra,
                                             "ratings": [{"person_id": p, "rating": v} for p, v in ratings.items()]})
        assert r.status_code == 201, r.get_json()
        return r.get_json()
    return _add


def as_person(client, pid, pin=None):
    """Pick who's rating on this test browser, like the 👤 button does."""
    r = client.post("/api/me", json={"person_id": pid, "pin": pin})
    assert r.status_code == 200, r.get_json()
