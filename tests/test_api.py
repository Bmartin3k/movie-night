import gzip
import json
import os
import sqlite3
import time

import pytest

import app as A


# ---------------------------------------------------------------- basics
def test_health_and_status(client, people):
    assert client.get("/healthz").data == b"ok"
    s = client.get("/api/status").get_json()
    assert s["people"] == 3
    assert not any(s[k] for k in ("tmdb", "claude", "emby", "rt", "radarr"))   # nothing configured in tests


def test_people_validation(client, people):
    assert client.post("/api/people", json={"name": "jane"}).status_code == 409      # names are unique, any case
    assert client.post("/api/people", json={"name": ""}).status_code == 400
    assert client.put(f"/api/people/{people['Alex']}", json={"max_cert": "X"}).status_code == 400
    assert client.put(f"/api/people/{people['Alex']}", json={"birthdate": "2999-01-01"}).status_code == 400


def test_renaming_keeps_the_old_name_as_an_alias(client, people):
    p = client.put(f"/api/people/{people['Sam']}", json={"name": "Samantha"}).get_json()
    assert p["name"] == "Samantha" and "Sam" in p["aliases"]


def test_ratings_are_per_person_and_averaged(client, people, add_movie):
    m = add_movie(603, "The Matrix", {people["Jane"]: 10, people["Sam"]: 7})
    assert m["avg_rating"] == 8.5
    assert {w["person_id"]: w["rating"] for w in m["watches"]} == {people["Jane"]: 10, people["Sam"]: 7}
    assert client.post("/api/movies", json={"tmdb_id": 603, "title": "The Matrix",
                                            "ratings": [{"person_id": people["Jane"], "rating": 9}]}).status_code == 409
    bad = client.put(f"/api/movies/{m['id']}", json={"ratings": [{"person_id": people["Jane"], "rating": 11}]})
    assert bad.status_code == 400
    assert client.post("/api/movies", json={"title": "No one rated it", "ratings": []}).status_code == 400


def test_i_didnt_watch_it_removes_the_watch_and_is_remembered(client, people, add_movie):
    m = add_movie(603, "The Matrix", {people["Jane"]: 10, people["Sam"]: None})
    client.put(f"/api/movies/{m['id']}", json={"ratings": [{"person_id": people["Sam"], "remove": True}]})
    conn = A.connect()
    assert A.declined_map(conn) == {603: {people["Sam"]}}
    conn.close()
    after = client.get("/api/movies").get_json()[0]
    assert [w["person_id"] for w in after["watches"]] == [people["Jane"]]


def test_to_rate_needs_a_person_and_lists_seen_but_unrated(client, people, add_movie):
    assert client.get("/api/torate").status_code == 400
    add_movie(603, "The Matrix", {people["Jane"]: 10, people["Sam"]: None})
    assert client.get(f"/api/torate?person_id={people['Jane']}").get_json() == []
    sams = client.get(f"/api/torate?person_id={people['Sam']}").get_json()
    assert [(q["title"], q["kind"], q["credited"]) for q in sams] == [("The Matrix", "movie", True)]
    client.post("/api/mark", json={"tmdb_id": 603, "person_id": people["Sam"], "action": "skip"})
    assert client.get(f"/api/torate?person_id={people['Sam']}").get_json() == []


# ---------------------------------------------------------------- watchlist: saved per person
def wl(client, group=None):
    q = "" if group is None else "?people=" + ",".join(map(str, group))
    return {m["title"]: sorted(m["saved_for_names"]) for m in client.get("/api/watchlist" + q).get_json()}


def save(client, tmdb_id, title, who):
    assert client.post("/api/watchlist", json={"tmdb_id": tmdb_id, "title": title, "people": who}).status_code == 204


def test_watchlist_is_saved_for_the_people_selected(client, people):
    j, s, a = people["Jane"], people["Sam"], people["Alex"]
    save(client, 1, "Up", [j])
    assert wl(client, [j]) == {"Up": ["Jane"]}
    assert wl(client, [s]) == {}                       # not Sam's
    assert wl(client, [j, s]) == {"Up": ["Jane"]}       # shows when anyone selected saved it
    save(client, 1, "Up", [s])                          # saving again for someone else adds them
    assert wl(client) == {"Up": ["Jane", "Sam"]}
    client.delete(f"/api/watchlist/1?people={j}")       # Remove only takes it off Jane's list
    assert wl(client) == {"Up": ["Sam"]}
    client.delete(f"/api/watchlist/1?people={s}")
    assert wl(client) == {}


def test_watchlist_hides_what_someone_selected_has_seen(client, people, add_movie):
    j, s = people["Jane"], people["Sam"]
    save(client, 1, "Up", [j, s])
    add_movie(1, "Up", {j: 9})                          # Jane watched and rated it
    assert wl(client) == {"Up": ["Sam"]}                # off Jane's list, still on Sam's
    assert wl(client, [j, s]) == {"Up": ["Sam"]}        # together: Jane loved it (4½★), Sam hasn't seen it
    assert client.get(f"/api/watchlist?people={j},{s}").get_json()[0]["rewatch"] == [{"name": "Jane", "rating": 9}]
    assert wl(client, [j]) == {}                        # Jane alone: she's seen it, nobody left to show it to
    assert wl(client, [s]) == {"Up": ["Sam"]}
    mid = client.get("/api/movies").get_json()[0]["id"]
    client.put(f"/api/movies/{mid}", json={"ratings": [{"person_id": s, "rating": 8}]})
    assert wl(client) == {}                             # everyone it was saved for has seen it


def test_watchlist_together_hides_it_when_a_viewer_rated_it_under_4_5(client, people, add_movie):
    j, s, a = people["Jane"], people["Sam"], people["Alex"]
    save(client, 1, "Up", [s])
    save(client, 2, "Coco", [s])
    save(client, 3, "Cars", [s])
    add_movie(1, "Up", {j: 8})                          # 4★: wouldn't watch it again
    add_movie(2, "Coco", {j: 10, a: 9})
    add_movie(3, "Cars", {j: None})                     # seen, no rating
    assert wl(client, [j, s]) == {"Coco": ["Sam"]}
    assert wl(client, [j, s, a]) == {"Coco": ["Sam"]}
    assert wl(client, [j, a]) == {}                     # saved for neither of them


def test_skipping_and_deleting_a_person_clear_their_saves(client, people):
    j, s, a = people["Jane"], people["Sam"], people["Alex"]
    save(client, 1, "Up", [s, a])
    save(client, 2, "Coco", [a])
    client.post("/api/mark", json={"tmdb_id": 1, "person_id": s, "action": "skip"})
    assert wl(client) == {"Up": ["Alex"], "Coco": ["Alex"]}
    client.delete(f"/api/people/{a}")
    assert wl(client) == {}


def test_dismissing_removes_it_from_the_watchlist_and_can_be_undone(client, people):
    save(client, 1, "Up", [people["Jane"]])
    client.post("/api/dismiss", json={"tmdb_id": 1, "title": "Up"})
    client.post("/api/dismiss", json={"tmdb_id": 2, "title": "Coco"})
    assert wl(client) == {}
    assert {d["title"] for d in client.get("/api/dismissed").get_json()} == {"Coco", "Up"}
    client.delete("/api/dismiss/1")
    assert [d["title"] for d in client.get("/api/dismissed").get_json()] == ["Coco"]
    client.delete("/api/dismiss")
    assert client.get("/api/dismissed").get_json() == []


# ---------------------------------------------------------------- suggestions (TMDB faked)
def rec(i, title, votes=1000, avg=7.0):
    return {"id": i, "title": title, "release_date": "2015-01-01", "vote_average": avg, "vote_count": votes,
            "genre_ids": [], "overview": "", "poster_path": None}


@pytest.fixture
def fake_tmdb(monkeypatch):
    """recs[seed tmdb id] = [movies TMDB recommends]; certs[tmdb id] = US rating."""
    recs, similar, certs = {}, {}, {}

    def tmdb_get(path, params=None, cache=True):
        parts = path.strip("/").split("/")
        if path == "/genre/movie/list":
            return {"genres": []}
        if parts[-1] == "recommendations":
            return {"results": recs.get(int(parts[1]), [])}
        if parts[-1] == "similar":
            return {"results": similar.get(int(parts[1]), [])}
        if parts[-1] == "release_dates":
            c = certs.get(int(parts[1]))
            return {"results": [{"iso_3166_1": "US", "release_dates": [{"type": 3, "certification": c}]}] if c else []}
        raise A.TMDBError("not faked: " + path)

    monkeypatch.setattr(A, "TMDB_KEY", "test")
    monkeypatch.setattr(A, "tmdb_get", tmdb_get)
    return recs, similar, certs


def titles(client, group):
    return [m["title"] for m in client.get("/api/suggestions?people=" + ",".join(map(str, group))).get_json()]


def test_suggestions_need_tmdb(client, people):
    assert client.get("/api/suggestions").status_code == 503


def test_loved_movies_pull_recommendations_up_and_disliked_push_down(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j = people["Jane"]
    add_movie(1, "Loved", {j: 10})
    add_movie(2, "Liked", {j: 7})
    add_movie(3, "Hated", {j: 2})
    recs[1] = [rec(101, "From loved"), rec(103, "Loved and hated")]
    recs[2] = [rec(102, "From liked")]
    recs[3] = [rec(103, "Loved and hated"), rec(104, "Only from hated"), rec(1, "Loved")]
    got = client.get(f"/api/suggestions?people={j}").get_json()
    assert [m["title"] for m in got][:2] == ["From loved", "From liked"]
    assert "Only from hated" not in [m["title"] for m in got]        # a negative score is never suggested
    assert "Loved" not in [m["title"] for m in got]                  # already seen
    assert got[0]["because"][0]["title"] == "Loved"


def test_nothing_anyone_in_the_group_has_seen_is_suggested(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Seed", {j: 10})
    add_movie(101, "Sam saw this", {s: 6})
    recs[1] = [rec(101, "Sam saw this"), rec(102, "New to both")]
    assert "Sam saw this" in titles(client, [j])
    assert titles(client, [j, s]) == ["New to both"]


def test_similar_movies_count_once_at_half_weight(client, people, add_movie, fake_tmdb):
    recs, similar, _ = fake_tmdb
    j = people["Jane"]
    for i in (1, 2, 3):
        add_movie(i, f"Seed {i}", {j: 10})
    recs[1] = [rec(101, "Recommended once")]
    for i in (1, 2, 3):                                # on three "similar" lists: still only its best match
        similar[i] = [rec(102, "Similar three times")]
    got = {m["title"]: m["score"] for m in client.get(f"/api/suggestions?people={j}").get_json()}
    assert got["Recommended once"] > got["Similar three times"]


def test_dismissed_picks_push_similar_movies_down(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j = people["Jane"]
    add_movie(1, "Seed", {j: 6})                       # a mild like: +0.5
    recs[1] = [rec(101, "Like the dismissed one"), rec(102, "Unrelated")]
    recs[900] = [rec(101, "Like the dismissed one")]
    assert titles(client, [j]) == ["Like the dismissed one", "Unrelated"]
    client.post("/api/dismiss", json={"tmdb_id": 900, "title": "Dismissed"})
    assert titles(client, [j]) == ["Unrelated"]        # 0.5 - 0.875 < 0


def test_kid_limit_filters_by_us_rating(client, people, add_movie, fake_tmdb):
    recs, _, certs = fake_tmdb
    j, a = people["Jane"], people["Alex"]              # Alex is limited to PG
    add_movie(1, "Seed", {j: 10, a: 10})
    recs[1] = [rec(101, "PG movie"), rec(102, "R movie"), rec(103, "Unrated movie")]
    certs.update({101: "PG", 102: "R"})
    assert set(titles(client, [j])) == {"PG movie", "R movie", "Unrated movie"}
    assert titles(client, [j, a]) == ["PG movie"]


def test_parents_five_stars_count_half_for_kids(client, people, add_movie, fake_tmdb):
    recs, _, certs = fake_tmdb
    j, s, a = people["Jane"], people["Sam"], people["Alex"]
    add_movie(1, "Jane's favourite", {j: 10})
    certs[1] = "PG"
    add_movie(3, "Sam's seed", {s: 10})                # people with no ratings of their own get no suggestions
    sam = next(m for m in client.get(f"/api/suggestions?people={s}").get_json() if m["tmdb_id"] == 1)
    add_movie(2, "Alex's seed", {a: 10})
    alex = next(m for m in client.get(f"/api/suggestions?people={a}").get_json() if m["tmdb_id"] == 1)
    assert sam["loved_by"] == [{"name": "Jane", "rating": 10}]
    assert sam["score"] == 6.75 and alex["score"] == pytest.approx(6.75 / 2, abs=0.01)   # scores are rounded to 2 places


def test_saved_picks_leave_suggestions_for_who_they_were_saved_for(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Seed", {j: 10, s: 10})
    recs[1] = [rec(101, "Pick")]
    save(client, 101, "Pick", [j])
    # saved for Jane: gone from hers and any group with her in it (it's on that watchlist), still offered to Sam
    assert titles(client, [j]) == [] and titles(client, [j, s]) == [] and titles(client, [s]) == ["Pick"]
    client.delete(f"/api/watchlist/101?people={j}")
    assert titles(client, [j]) == ["Pick"]


# ---------------------------------------------------------------- critic scores cache (OMDb)
def put_scores(tid, value, age_days):
    conn = A.connect()
    conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                 (f"omdb:{tid}", json.dumps(value), int(time.time() - age_days * 86400)))
    conn.commit()
    conn.close()


def test_scores_are_kept_90_days_and_no_score_14(client):
    put_scores(1, {"rt": "90%", "mc": 80}, 60)         # settled score, 60 days old: still good
    put_scores(2, {"rt": "90%", "mc": 80}, 100)        # too old
    put_scores(3, {"rt": None, "mc": None}, 10)        # "no score yet", 10 days old: still good
    put_scores(4, {"rt": None, "mc": None}, 20)        # re-check after 14
    put_scores(5, {"rt": "50%", "mc": 40, "ttl": 14 * 86400}, 20)   # was a new release when stored
    assert A.critic_scores(1) == {"rt": "90%", "mc": 80}
    assert A.critic_scores(3) == {"rt": None, "mc": None}
    for tid in (2, 4, 5, 6):                           # no OMDb key in tests, so these can't be looked up
        assert A.critic_scores(tid).get("pending")


def test_scores_that_cant_be_looked_up_are_left_out(client, monkeypatch):
    monkeypatch.setattr(A, "OMDB_KEY", "test")
    A._omdb_pause_until = time.time() + 3600           # as if the daily limit was just hit
    put_scores(1, {"rt": "90%", "mc": 80}, 1)
    assert client.get("/api/rt?ids=1,2").get_json() == {"1": {"rt": "90%", "mc": 80}}


# ---------------------------------------------------------------- Radarr (faked)
def test_radarr_needs_setup(client):
    assert client.get("/api/radarr/status?ids=1").status_code == 502


def test_radarr_library_is_fetched_once_a_minute(client, monkeypatch):
    calls = []

    def radarr(method, path, **kw):
        calls.append((method, path))
        return [{"tmdbId": 1, "hasFile": True, "monitored": True}, {"tmdbId": 2, "hasFile": False, "monitored": True},
                {"tmdbId": 3, "hasFile": False, "monitored": False}]

    monkeypatch.setattr(A, "radarr", radarr)
    for _ in range(3):
        got = client.get("/api/radarr/status?ids=1,2,3,4").get_json()
    assert got == {"1": "downloaded", "2": "wanted", "3": "unmonitored", "4": None}
    assert calls == [("GET", "/movie")]


# ---------------------------------------------------------------- page weight, backups
def test_big_responses_are_gzipped_when_the_browser_accepts_it(client, people, add_movie):
    for i in range(1, 40):
        add_movie(i, f"Movie number {i}", {people["Jane"]: 8}, overview="A long summary. " * 10)
    plain = client.get("/api/movies")
    packed = client.get("/api/movies", headers={"Accept-Encoding": "gzip, br"})
    assert "Content-Encoding" not in plain.headers
    assert packed.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(packed.data)) == plain.get_json()
    assert len(packed.data) < len(plain.data) / 3
    assert "Content-Encoding" not in client.get("/healthz", headers={"Accept-Encoding": "gzip"}).headers   # tiny


def test_backup_is_complete_verified_and_pruned(client, people, add_movie, monkeypatch):
    add_movie(603, "The Matrix", {people["Jane"]: 10})
    put_scores(603, {"rt": "83%", "mc": 73}, 1)
    monkeypatch.setattr(A, "BACKUP_KEEP", 2)
    os.makedirs(A.backup_dir(), exist_ok=True)
    for day in ("2020-01-01", "2020-01-02", "2020-01-03"):
        open(os.path.join(A.backup_dir(), f"movies-{day}.db"), "w").close()
    open(os.path.join(A.backup_dir(), "keep-me.txt"), "w").close()
    path = A.backup_db()
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT title FROM movies").fetchall() == [("The Matrix",)]
    assert copy.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1
    assert copy.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0      # the cache is left out
    copy.close()
    left = sorted(os.listdir(A.backup_dir()))
    assert left == ["keep-me.txt", "movies-2020-01-03.db", os.path.basename(path)]
    assert A.last_backup() == os.path.basename(path)[7:17]


def test_show_watched_tags_seen_picks(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Seed", {j: 10})
    add_movie(101, "Seen pick", {s: 10, j: 9})
    add_movie(103, "Jane only 3 stars", {s: 10, j: 6})
    add_movie(104, "Jane hasn't seen", {s: 10})
    recs[1] = [rec(101, "Seen pick"), rec(102, "Fresh pick"), rec(103, "Jane only 3 stars"), rec(104, "Jane hasn't seen")]
    get = lambda q, g=f"{j},{s}": client.get(f"/api/suggestions?people={g}{q}").get_json()
    assert [m["title"] for m in get("")] == ["Fresh pick"]
    both = {m["title"]: m for m in get("&watched=1")}
    # seen picks only when everyone selected who rated them gave 4½★+; not having rated it doesn't count against
    assert set(both) == {"Seen pick", "Fresh pick", "Jane hasn't seen"}
    assert both["Seen pick"]["seen_by"] == ["Jane", "Sam"] and both["Seen pick"]["five_by"] == ["Jane", "Sam"]
    assert both["Jane hasn't seen"]["seen_by"] == ["Sam"] and both["Jane hasn't seen"]["five_by"] == ["Sam"]
    jane = {m["title"]: m for m in get("&watched=1", str(j))}   # Jane alone: her own 4½★+ counts
    assert set(jane) == {"Seen pick", "Fresh pick", "Jane hasn't seen"} and "seen_by" not in jane["Jane hasn't seen"]
    assert "seen_by" not in both["Fresh pick"]


# ---------------------------------------------------------------- Ask Claude (API faked)
def test_claude_picks_follow_the_4_5_star_rule(client, people, add_movie, monkeypatch):
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Loved by Jane", {j: 9})
    add_movie(2, "Jane so-so", {j: 7})
    add_movie(3, "Both seen", {j: 10, s: 10})
    picks = [{"title": t, "year": 2020, "reason": "r"} for t in ("Loved by Jane", "Jane so-so", "Both seen", "New one")]
    ids = {"Loved by Jane": 1, "Jane so-so": 2, "Both seen": 3, "New one": 4}
    prompts = []

    class Fake:
        def __init__(self, **kw):
            self.beta = self
            self.messages = self

        def create(self, **kw):
            prompts.append(kw["messages"][0]["content"])
            block = type("B", (), {"type": "text", "text": json.dumps({"suggestions": picks})})
            return type("R", (), {"stop_reason": "end_turn", "content": [block]})

    def tmdb_get(path, params=None, cache=True):
        if path == "/genre/movie/list":
            return {"genres": []}
        if path == "/search/movie":
            t = params["query"]
            return {"results": [{"id": ids[t], "title": t, "release_date": "2020-01-01", "genre_ids": []}]}
        raise A.TMDBError(path)

    monkeypatch.setattr(A, "ANTHROPIC_KEY", "x")
    monkeypatch.setattr(A.anthropic, "Anthropic", Fake)
    monkeypatch.setattr(A, "TMDB_KEY", "test")
    monkeypatch.setattr(A, "tmdb_get", tmdb_get)
    got = {m["title"]: m for m in client.post("/api/claude-suggestions", json={"people": [j, s]}).get_json()}
    assert set(got) == {"Loved by Jane", "New one"}
    assert got["Loved by Jane"]["rewatch"] == [{"name": "Jane", "rating": 9}]
    assert "Loved by Jane (2020) — new to Sam" in prompts[0]
    got = {m["title"] for m in client.post("/api/claude-suggestions", json={"people": [j]}).get_json()}
    assert got == {"New one"}                       # Jane alone: nothing she's seen


# ---------------------------------------------------------------- settings page
def test_settings_override_env_and_never_return_secrets(client, monkeypatch):
    monkeypatch.setenv("OMDB_API_KEY", "env-omdb-key-1234")
    monkeypatch.setenv("RADARR_URL", "http://env:7878")
    get = lambda: {f["key"]: f for f in client.get("/api/settings").get_json()}
    assert get()["OMDB_API_KEY"]["source"] == "env" and get()["OMDB_API_KEY"]["hint"] == "…1234"
    assert "value" not in get()["OMDB_API_KEY"] and get()["RADARR_URL"]["value"] == "http://env:7878"
    r = client.put("/api/settings", json={"OMDB_API_KEY": "saved-omdb-key-9999", "RADARR_URL": "http://saved:7878/"})
    assert r.status_code == 200 and "saved-omdb-key-9999" not in r.get_data(as_text=True)
    assert get()["OMDB_API_KEY"] == {**get()["OMDB_API_KEY"], "source": "saved", "hint": "…9999"}
    assert A.OMDB_KEY == "saved-omdb-key-9999" and A.RADARR_URL == "http://saved:7878"   # live, no restart
    client.put("/api/settings", json={"OMDB_API_KEY": None})                            # forget: back to .env
    assert get()["OMDB_API_KEY"]["source"] == "env" and A.OMDB_KEY == "env-omdb-key-1234"
    assert client.put("/api/settings", json={"PUID": "0"}).status_code == 400            # only known settings
    client.put("/api/settings", json={"RADARR_URL": None})
    monkeypatch.delenv("OMDB_API_KEY")
    monkeypatch.delenv("RADARR_URL")
    A.apply_settings(A.connect())                   # don't leak these into other tests
