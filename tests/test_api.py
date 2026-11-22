import gzip
import json
import os
import sqlite3
import time

import pytest

import app as A
from conftest import as_person


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
    as_person(client, people["Jane"])
    bad = client.put(f"/api/movies/{m['id']}", json={"ratings": [{"person_id": people["Jane"], "rating": 11}]})
    assert bad.status_code == 400
    assert client.post("/api/movies", json={"title": "No one rated it", "ratings": []}).status_code == 400


def test_i_didnt_watch_it_removes_the_watch_and_is_remembered(client, people, add_movie):
    m = add_movie(603, "The Matrix", {people["Jane"]: 10, people["Sam"]: None})
    as_person(client, people["Sam"])
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
    as_person(client, people["Sam"])
    client.post("/api/mark", json={"tmdb_id": 603, "person_id": people["Sam"], "action": "skip"})
    assert client.get(f"/api/torate?person_id={people['Sam']}").get_json() == []


def test_quick_rate_answers_can_be_undone(client, people, add_movie):
    """⚡ Quick rate: rating a queued movie returns where it went, so Undo can make it "seen, not rated" again;
    undoing "didn't watch" puts the seen mark back."""
    conn = A.connect()
    conn.execute("INSERT INTO emby_queue (emby_id, tmdb_id, title, watched_by, created_at) VALUES ('e1', 329865, 'Arrival', 'Jane, Sam', 0)")
    conn.commit(); conn.close()
    jane, sam = people["Jane"], people["Sam"]
    as_person(client, jane)
    r = client.post("/api/emby/queue/e1/rate", json={"rating": 8, "person_id": jane})
    assert r.status_code == 200
    mid = r.get_json()["movie_id"]
    todo = lambda pid: [(q["title"], q["kind"], q["key"]) for q in client.get(f"/api/torate?person_id={pid}").get_json()
                        if not q["declined"]]
    assert todo(jane) == [] and todo(sam) == [("Arrival", "movie", str(mid))]
    client.put(f"/api/movies/{mid}", json={"ratings": [{"person_id": jane, "rating": None}]})      # Undo
    assert todo(jane) == [("Arrival", "movie", str(mid))]

    as_person(client, sam)
    client.post("/api/mark", json={"tmdb_id": 329865, "person_id": sam, "action": "not_watched"})
    assert todo(sam) == []
    client.post("/api/mark", json={"tmdb_id": 329865, "person_id": sam, "action": "undo_not_watched"})   # Undo
    client.put(f"/api/movies/{mid}", json={"ratings": [{"person_id": sam, "rating": None}]})
    assert todo(sam) == [("Arrival", "movie", str(mid))]


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
    assert wl(client, [j, s]) == {"Up": ["Sam"]}        # together: Sam hasn't seen it, Jane liked it
    assert client.get(f"/api/watchlist?people={j},{s}").get_json()[0]["rewatch"] == [{"name": "Jane", "rating": 9}]
    assert wl(client, [j]) == {}                        # Jane alone: she's seen it, nobody left to show it to
    assert wl(client, [s]) == {"Up": ["Sam"]}
    mid = client.get("/api/movies").get_json()[0]["id"]
    client.put(f"/api/movies/{mid}", json={"ratings": [{"person_id": s, "rating": 8}]})
    assert wl(client) == {}                             # everyone it was saved for has seen it


def test_watchlist_together_hides_it_when_a_viewer_rated_it_under_2(client, people, add_movie):
    j, s, a = people["Jane"], people["Sam"], people["Alex"]
    for tid, t in ((1, "Up"), (2, "Coco"), (3, "Cars"), (4, "Elio")):
        save(client, tid, t, [s])
    add_movie(1, "Up", {j: 4})                          # 2★: still fine for Sam
    add_movie(2, "Coco", {j: 10, a: 3})                 # Alex gave it 1½★
    add_movie(3, "Cars", {j: None})                     # seen, no rating: fine
    add_movie(4, "Elio", {j: 3})                        # Jane gave it 1½★
    assert wl(client, [j, s]) == {"Up": ["Sam"], "Coco": ["Sam"], "Cars": ["Sam"]}
    assert wl(client, [j, s, a]) == {"Up": ["Sam"], "Cars": ["Sam"]}
    assert wl(client, [s]) == {"Up": ["Sam"], "Coco": ["Sam"], "Cars": ["Sam"], "Elio": ["Sam"]}   # Jane not selected
    assert wl(client, [j, a]) == {}                     # saved for neither of them


def test_skipping_and_deleting_a_person_clear_their_saves(client, people):
    j, s, a = people["Jane"], people["Sam"], people["Alex"]
    save(client, 1, "Up", [s, a])
    save(client, 2, "Coco", [a])
    as_person(client, s)
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


def test_nothing_the_whole_group_has_seen_is_suggested(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Seed", {j: 10})
    add_movie(101, "Both saw this", {s: 6, j: 8})
    recs[1] = [rec(101, "Both saw this"), rec(102, "New to both")]
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


def test_suggests_what_someone_in_the_group_hasnt_seen(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Seed", {j: 10})
    add_movie(101, "Both seen", {s: 10, j: 9})
    add_movie(104, "Jane hasn't seen", {s: 6})
    add_movie(105, "Sam saw, no rating", {s: None})
    add_movie(106, "Sam hated it", {s: 3})                      # 1½★: under 2★, kept from Jane too
    recs[1] = [rec(101, "Both seen"), rec(102, "Fresh pick"), rec(104, "Jane hasn't seen"), rec(105, "Sam saw, no rating"),
               rec(106, "Sam hated it")]
    get = lambda g: {m["title"]: m for m in client.get(f"/api/suggestions?people={g}").get_json()}
    both = get(f"{j},{s}")
    # shown while at least one selected person hasn't seen it, unless someone who saw it rated it under 2★
    # ("Jane hasn't seen" at 3★ stays)
    assert set(both) == {"Fresh pick", "Jane hasn't seen", "Sam saw, no rating"}
    assert both["Jane hasn't seen"]["seen_by"] == [{"name": "Sam", "rating": 6}]
    assert both["Sam saw, no rating"]["seen_by"] == [{"name": "Sam", "rating": None}]
    assert "seen_by" not in both["Fresh pick"]
    assert not {"Both seen", "Jane hasn't seen", "Sam saw, no rating"} & set(get(str(s)))   # Sam alone: nothing he's seen
    jane = get(str(j))   # Sam isn't selected, so his 1½★ doesn't count
    assert set(jane) == {"Fresh pick", "Jane hasn't seen", "Sam saw, no rating", "Sam hated it"} and "seen_by" not in jane["Jane hasn't seen"]


# ---------------------------------------------------------------- Ask Claude (API faked)
def test_claude_only_picks_what_nobody_in_the_group_has_seen(client, people, add_movie, monkeypatch):
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Loved by Jane", {j: 9})
    add_movie(2, "Jane hated it", {j: 3})
    add_movie(3, "Both seen", {j: 10, s: 10})
    picks = [{"title": t, "year": 2020, "reason": "r"} for t in ("Loved by Jane", "Jane hated it", "Both seen", "New one")]
    ids = {"Loved by Jane": 1, "Jane hated it": 2, "Both seen": 3, "New one": 4}
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
    assert set(got) == {"New one"}                  # new to Sam isn't enough: Jane has seen it
    assert "rewatch" not in got["New one"]
    assert "Loved by Jane (2020) — Jane 9/10\n" in prompts[0] and "[new to" not in prompts[0]
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
    assert oct(os.stat(A.SETTINGS_PATH).st_mode & 0o777) == "0o600"                    # own file, owner-only
    conn = A.connect()
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone()   # not in the DB/backups
    # a new Radarr URL with the old key would hand the key to that server: it has to be re-entered
    client.put("/api/settings", json={"RADARR_API_KEY": "radarr-key-5678"})
    r = client.put("/api/settings", json={"RADARR_URL": "http://elsewhere:7878"})
    assert r.status_code == 400 and A.RADARR_URL == "http://saved:7878"
    assert client.put("/api/settings", json={"RADARR_URL": "http://elsewhere:7878", "RADARR_API_KEY": "k2-abcdefgh"}).status_code == 200
    client.put("/api/settings", json={"RADARR_API_KEY": None})
    client.put("/api/settings", json={"RADARR_URL": None})
    monkeypatch.delenv("OMDB_API_KEY")
    monkeypatch.delenv("RADARR_URL")
    A.apply_settings(A.connect())                   # don't leak these into other tests


def test_settings_password_guards_grown_up_changes_only(client, people, monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)             # no 1s pause on wrong guesses
    alex = people["Alex"]
    assert client.get("/api/lock").get_json() == {"password_set": False, "unlocked": True}
    assert client.put("/api/password", json={"password": "abc"}).status_code == 400      # too short
    assert client.put("/api/password", json={"password": "grownups"}).status_code == 200
    assert "grownups" not in open(A.SETTINGS_PATH).read()                                # hashed

    kid = A.app.test_client()                                        # another browser, never unlocked
    assert kid.get("/api/settings").status_code == 403
    assert kid.put("/api/settings", json={"CLAUDE_MODEL": "x"}).get_json()["locked"] is True
    assert kid.put(f"/api/people/{alex}", json={"max_cert": None}).status_code == 403  # can't lift own limit
    assert kid.put(f"/api/people/{alex}", json={"birthdate": "2000-01-01"}).status_code == 403
    assert kid.delete(f"/api/people/{people['Jane']}").status_code == 403
    assert kid.post("/api/people", json={"name": "Free", "max_cert": "PG"}).status_code == 403
    assert kid.put("/api/password", json={"password": ""}).status_code == 403            # can't turn it off
    assert kid.put(f"/api/people/{alex}", json={"emoji": "🦖"}).status_code == 200       # everyday edits stay open
    assert kid.get("/api/people").status_code == 200

    assert kid.post("/api/unlock", json={"password": "nope"}).status_code == 403
    assert kid.post("/api/unlock", json={"password": "grownups"}).status_code == 200
    assert kid.put(f"/api/people/{alex}", json={"max_cert": "PG-13"}).status_code == 200
    kid.post("/api/lock")
    assert kid.get("/api/settings").status_code == 403

    for _ in range(5):                                               # guessing: a minute's pause after 5 misses
        kid.post("/api/unlock", json={"password": "guess"})
    assert kid.post("/api/unlock", json={"password": "grownups"}).status_code == 429
    A._unlock_fails.update(n=0, until=0.0)
    assert client.put("/api/password", json={"password": ""}).status_code == 200         # the unlocked browser turns it off
    assert kid.get("/api/settings").status_code == 200


# ---------------------------------------------------------------- media servers (Jellyfin / Plex faked)
def queue_rows():
    return {r["emby_id"]: dict(r) for r in A.connect().execute("SELECT * FROM emby_queue")}


def test_jellyfin_sync_queues_played_movies(client, people, add_movie, monkeypatch):
    def get(path, params=None):
        if path == "/Users":
            return [{"Id": "u1", "Name": "Jane"}, {"Id": "u2", "Name": "Kilo"}]
        return {"Items": [
            {"Id": "a", "Name": "Arrival", "ProductionYear": 2016, "ProviderIds": {"Tmdb": "329865"},
             "UserData": {"LastPlayedDate": "2026-10-01T20:00:00Z", "IsFavorite": path.startswith("/Users/u1")}},
            {"Id": "b", "Name": "Already Rated", "ProductionYear": 2020, "ProviderIds": {"Tmdb": "5"}, "UserData": {}},
        ]}
    monkeypatch.setattr(A, "jellyfin_get", get)
    monkeypatch.setattr(A, "JELLYFIN_URL", "http://jf:8096")
    monkeypatch.setattr(A, "JELLYFIN_KEY", "k")
    add_movie(5, "Already Rated", {people["Sam"]: 8})
    r = client.post("/api/sync").get_json()
    assert r["jellyfin"]["played"] == 2 and r["jellyfin"]["added"] == 1 and "emby" not in r
    q = queue_rows()["jellyfin:a"]
    assert (q["tmdb_id"], q["source"], q["watched_by"], q["watched_on"], q["favorite"]) == \
        (329865, "jellyfin", "Jane, Kilo", "2026-10-01", 1)
    jane_seen = client.get("/api/movies").get_json()[0]["watches"]
    assert {w["person_id"] for w in jane_seen} == {people["Sam"], people["Jane"]}   # Jane marked as seen
    assert client.get("/api/status").get_json()["syncs"]["jellyfin"]["users"] == ["Jane", "Kilo"]


def test_plex_sync_reads_history_for_every_account(client, people, monkeypatch):
    def get(path, params=None):
        if path == "/accounts":
            return {"MediaContainer": {"Account": [{"id": 1, "name": "Jane"}, {"id": 7, "name": "Sam"}, {"id": 0}]}}
        if path == "/status/sessions/history/all":
            return {"MediaContainer": {"Metadata": [
                {"type": "movie", "ratingKey": "10", "title": "Up", "viewedAt": 1767225600, "accountID": 1},
                {"type": "movie", "ratingKey": "10", "title": "Up", "viewedAt": 1764547200, "accountID": 7},
                {"type": "episode", "ratingKey": "11", "title": "Pilot", "viewedAt": 1764547200, "accountID": 1},
                {"type": "movie", "ratingKey": "12", "title": "Gone", "year": 1999, "viewedAt": 1764547200, "accountID": 7},
            ]}}
        if path == "/library/metadata/10":
            return {"MediaContainer": {"Metadata": [{"title": "Up", "year": 2009, "summary": "Balloons.",
                    "Guid": [{"id": "imdb://tt1049413"}, {"id": "tmdb://14160"}], "Genre": [{"tag": "Animation"}]}]}}
        raise A.EmbyError("Plex returned 404")            # deleted from the library since
    monkeypatch.setattr(A, "plex_get", get)
    monkeypatch.setattr(A, "PLEX_URL", "http://plex:32400")
    monkeypatch.setattr(A, "PLEX_TOKEN", "t")
    r = client.post("/api/sync").get_json()["plex"]
    assert r["played"] == 2 and r["added"] == 2 and r["users"] == ["Jane", "Sam"]
    q = queue_rows()
    assert (q["plex:10"]["tmdb_id"], q["plex:10"]["watched_by"], q["plex:10"]["year"], q["plex:10"]["source"]) == \
        (14160, "Jane, Sam", 2009, "plex")
    assert q["plex:10"]["watched_on"] == "2026-01-01"                # latest viewing
    assert (q["plex:12"]["tmdb_id"], q["plex:12"]["title"]) == (None, "Gone")
    monkeypatch.setattr(A, "PLEX_USERS", ["sam"])                    # only some accounts
    A.connect().execute("DELETE FROM emby_queue").connection.commit()
    client.post("/api/sync")
    assert queue_rows()["plex:10"]["watched_by"] == "Sam"


def test_sync_with_nothing_set_up(client):
    assert client.post("/api/sync").status_code == 400


# ---------------------------------------------------------------- PINs: only you change your ratings
def test_pins_guard_who_is_rating_and_their_ratings(client, people, add_movie, monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    j, s = people["Jane"], people["Sam"]
    assert client.put(f"/api/people/{j}/pin", json={"pin": "12a4"}).status_code == 400
    as_person(client, j)
    assert client.put(f"/api/people/{j}/pin", json={"pin": "1234"}).get_json() == {"pin_set": True}
    assert client.get("/api/me").get_json() == {"person_id": j}                  # still Jane after setting it
    people_out = client.get("/api/people").get_json()["people"]
    assert all("pin_hash" not in p for p in people_out) and next(p for p in people_out if p["id"] == j)["pin_set"]

    kid = A.app.test_client()
    assert kid.post("/api/me", json={"person_id": j, "pin": "0000"}).status_code == 403
    assert kid.get("/api/me").get_json() == {"person_id": None}
    as_person(kid, s)                                                            # Sam has no PIN: free
    m = add_movie(603, "The Matrix", {j: 10})
    put = lambda c, r: c.put(f"/api/movies/{m['id']}", json={"ratings": r}).status_code
    assert put(kid, [{"person_id": j, "rating": 2}]) == 403                      # can't change Jane's
    assert put(kid, [{"person_id": j, "remove": True}]) == 403                   # or remove it
    assert put(kid, [{"person_id": j, "rating": 10}, {"person_id": s, "rating": 6}]) == 200   # unchanged + own: fine
    assert put(kid, [{"person_id": s, "rating": 7}]) == 200                      # own rating can change
    assert kid.delete(f"/api/movies/{m['id']}").status_code == 403              # would delete Jane's too
    assert kid.post("/api/mark", json={"tmdb_id": 603, "person_id": j, "action": "skip"}).status_code == 403
    assert kid.put(f"/api/people/{j}/pin", json={"pin": ""}).status_code == 403  # can't clear Jane's PIN
    m2 = add_movie(604, "Up", {s: 8})
    assert put(client, [{"person_id": s, "rating": 1}]) == 403                   # Jane can't change Sam's either
    assert client.put(f"/api/movies/{m2['id']}", json={"ratings": [{"person_id": j, "rating": 9}]}).status_code == 200

    as_person(kid, j, "1234")                                                   # right PIN: Jane on this browser
    assert put(kid, [{"person_id": j, "rating": 9}]) == 200
    client.put(f"/api/people/{j}/pin", json={"pin": "5678"})                     # PIN changed elsewhere:
    assert kid.get("/api/me").get_json() == {"person_id": None}                 # other browsers are signed out

    for _ in range(5):                                                           # 5 wrong: a minute's pause
        kid.post("/api/me", json={"person_id": j, "pin": "0000"})
    assert kid.post("/api/me", json={"person_id": j, "pin": "5678"}).status_code == 429


def test_settings_password_overrides_pins(client, people, add_movie):
    j, s = people["Jane"], people["Sam"]
    as_person(client, s)
    client.put(f"/api/people/{s}/pin", json={"pin": "1111"})
    client.put("/api/password", json={"password": "grownups"})                   # this browser is unlocked
    m = add_movie(603, "The Matrix", {j: 10})
    assert client.put(f"/api/movies/{m['id']}", json={"ratings": [{"person_id": j, "rating": 8}]}).status_code == 200
    other = A.app.test_client()
    assert other.put(f"/api/people/{s}/pin", json={"pin": ""}).get_json()["locked"] is True
    client.post("/api/lock")
    assert client.put(f"/api/movies/{m['id']}", json={"ratings": [{"person_id": j, "rating": 7}]}).status_code == 403
    client.post("/api/unlock", json={"password": "grownups"})
    assert client.put(f"/api/people/{s}/pin", json={"pin": ""}).status_code == 200   # grown-up resets a PIN


def test_everyone_in_the_group_gets_an_equal_say(client, people, add_movie, fake_tmdb, monkeypatch):
    monkeypatch.setattr(A, "SUGGEST_KEEP", 200)       # scoring test: look past the top 15
    recs, _, _ = fake_tmdb
    j, s = people["Jane"], people["Sam"]
    for i in range(100):                               # Jane has rated 100 movies at 5★, each with its own pick
        add_movie(1000 + i, f"Jane seed {i}", {j: 10})
        recs[1000 + i] = [rec(5000 + i, f"Jane pick {i}")]
    add_movie(1, "Sam's one favourite", {s: 9})        # Sam has one 4½★
    recs[1] = [rec(9000, "Sam pick")]
    got = {m["title"]: m["score"] for m in client.get(f"/api/suggestions?people={j},{s}").get_json()}
    assert got["Sam pick"] == max(got.values())        # his one favourite counts as much as her best
    assert len([t for t in got if t.startswith("Jane pick")]) == 40   # she gets her share of seeds (80 / 2)


def test_claude_prompt_gives_everyone_an_equal_say(client, people, add_movie, monkeypatch):
    j, s = people["Jane"], people["Sam"]
    for i in range(60):                               # Jane has rated loads, Sam only a couple
        add_movie(100 + i, f"Jane film {i}", {j: 10 if i < 30 else 6})
    add_movie(1, "Sam fave", {s: 10})
    add_movie(2, "Sam hate", {s: 2})
    prompts = []

    class Fake:
        def __init__(self, **kw):
            self.beta = self
            self.messages = self

        def create(self, **kw):
            prompts.append(kw["messages"][0]["content"])
            block = type("B", (), {"type": "text", "text": json.dumps({"suggestions": []})})
            return type("R", (), {"stop_reason": "end_turn", "content": [block]})

    monkeypatch.setattr(A, "ANTHROPIC_KEY", "x")
    monkeypatch.setattr(A.anthropic, "Anthropic", Fake)
    client.post("/api/claude-suggestions", json={"people": [j, s]})
    rated = prompts[0]
    assert "Sam fave" in rated and "Sam hate" in rated and "Sam 10/10" in rated
    assert rated.count("Jane 10/10") == 30 and rated.count("Jane 6/10") == 10   # her share: 80 // 2 = 40, loves first
    # Jane's other 20 are listed by title as seen: Claude only picks what nobody in the group has seen
    assert "Also seen by someone here" in rated and "- Jane film 59 (2020)" in rated and "[new to" not in rated
    assert "Sam hate (2020) — Sam 2/10\n" in rated
    assert "equal say" in rated and "Jane 60, Sam 2" in rated
    add_movie(50, "Both saw", {j: 5, s: 5})
    client.post("/api/claude-suggestions", json={"people": [j]})
    assert "Also seen" not in prompts[1] and prompts[1].count("— Jane ") == 61 and "[new to" not in prompts[1]   # alone: everything


# ---------------------------------------------------------------- watch tonight, trailers, top 15
def test_suggestions_show_the_top_15(client, people, add_movie, fake_tmdb):
    recs, _, _ = fake_tmdb
    j = people["Jane"]
    add_movie(1, "Seed", {j: 10})
    recs[1] = [rec(100 + i, f"Pick {i}") for i in range(30)]
    got = titles(client, [j])
    assert len(got) == 15 and got[0] == "Pick 0"


def test_watch_tonight_keeps_library_and_my_services(client, people, add_movie, fake_tmdb, monkeypatch):
    recs, _, _ = fake_tmdb
    j = people["Jane"]
    add_movie(1, "Seed", {j: 10})
    recs[1] = [rec(101, "Nowhere"), rec(102, "On Netflix"), rec(103, "On Hulu"), rec(104, "Downloaded")]
    offers = {102: [{"provider_name": "Netflix Standard with Ads", "logo_path": "/nf.png"}], 103: [{"provider_name": "Hulu"}]}
    inner = A.tmdb_get

    def tmdb_get(path, params=None, cache=True):
        if path.endswith("/watch/providers"):
            return {"results": {"US": {"flatrate": offers.get(int(path.split("/")[2]), [])}}}
        return inner(path, params, cache)
    monkeypatch.setattr(A, "tmdb_get", tmdb_get)
    monkeypatch.setattr(A, "STREAM_SERVICES", ["Netflix"])
    monkeypatch.setattr(A, "RADARR_URL", "http://r")
    monkeypatch.setattr(A, "RADARR_KEY", "k")
    A._radarr_lib.update(at=time.time(), states={104: "downloaded", 101: "wanted"})
    got = client.get(f"/api/suggestions?people={j}&tonight=1").get_json()
    assert [m["title"] for m in got] == ["On Netflix", "Downloaded"]
    assert got[0]["where"] == {"library": False, "stream": ["Netflix"], "logos": {"Netflix": "/nf.png"}}
    assert len(titles(client, [j])) == 4                                   # off: everything
    w = client.get("/api/where?ids=102,104,101").get_json()
    assert w["104"]["library"] and not w["101"]["library"] and w["102"]["stream"] == ["Netflix"]


def test_provider_names_match_loosely_but_not_wildly():
    assert A.provider_matches("Prime Video", "Amazon Prime Video")
    assert A.provider_matches("Disney Plus", "Disney Plus")
    assert not A.provider_matches("Max", "Paramount Plus Apple TV Channel")
    assert not A.provider_matches("", "Netflix")


def test_trailer_prefers_the_official_trailer(client, monkeypatch):
    vids = [{"site": "YouTube", "key": "teaser", "type": "Teaser", "official": True, "iso_639_1": "en"},
            {"site": "YouTube", "key": "fan", "type": "Trailer", "official": False, "iso_639_1": "en"},
            {"site": "Vimeo", "key": "vimeo", "type": "Trailer", "official": True, "iso_639_1": "en"},
            {"site": "YouTube", "key": "real", "type": "Trailer", "official": True, "iso_639_1": "en"}]
    monkeypatch.setattr(A, "tmdb_get", lambda path, params=None, cache=True: {"results": vids if path == "/movie/5/videos" else []})
    assert client.get("/api/trailer/5").get_json()["key"] == "real"
    assert client.get("/api/trailer/6").status_code == 404


def test_library_icon_comes_from_the_media_server(client, monkeypatch):
    class R:
        ok, content, headers = True, b"PNG", {"content-type": "image/png"}
    asked = []
    monkeypatch.setattr(A, "EMBY_URL", "http://emby")
    monkeypatch.setattr(A.requests, "get", lambda url, timeout=5: asked.append(url) or R())
    A._lib_icon.update(at=0.0, img=None)
    r = client.get("/api/library-icon")
    assert r.status_code == 200 and r.data == b"PNG" and r.mimetype == "image/png"
    client.get("/api/library-icon")
    assert asked == ["http://emby/web/images/icon-72x72.png"]          # cached for a day
    monkeypatch.setattr(A, "EMBY_URL", "")
    A._lib_icon.update(at=0.0, img=None)
    assert client.get("/api/library-icon").status_code == 404


def test_school_night_keeps_two_hours_or_less(client, people, add_movie, fake_tmdb, monkeypatch):
    recs, _, _ = fake_tmdb
    j = people["Jane"]
    add_movie(1, "Seed", {j: 10})
    recs[1] = [rec(101, "Epic"), rec(102, "Short one"), rec(103, "No runtime"), rec(104, "Exactly two hours")]
    mins = {101: 169, 102: 95, 104: 120}
    inner = A.tmdb_get

    def tmdb_get(path, params=None, cache=True):
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "movie":
            return {"id": int(parts[1]), "runtime": mins.get(int(parts[1]), 0)}
        return inner(path, params, cache)
    monkeypatch.setattr(A, "tmdb_get", tmdb_get)
    got = client.get(f"/api/suggestions?people={j}&short=1").get_json()
    assert [m["title"] for m in got] == ["Short one", "Exactly two hours"]      # unknown length is left out
    every = {m["title"]: m.get("runtime") for m in client.get(f"/api/suggestions?people={j}").get_json()}
    assert every == {"Epic": 169, "Short one": 95, "No runtime": None, "Exactly two hours": 120}
    assert client.get("/api/where?ids=101").get_json()["101"]["runtime"] == 169


def test_claude_is_told_about_school_night_and_checked(client, people, add_movie, monkeypatch):
    j = people["Jane"]
    add_movie(1, "Seed", {j: 9})
    picks = [{"title": t, "year": 2020, "reason": "r"} for t in ("Long", "Short")]
    ids, mins, prompts = {"Long": 11, "Short": 12}, {11: 150, 12: 90}, []

    class Fake:
        def __init__(self, **kw):
            self.beta = self.messages = self

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
        if path.startswith("/movie/"):
            return {"runtime": mins[int(path.split("/")[2])]}
        raise A.TMDBError(path)

    monkeypatch.setattr(A, "ANTHROPIC_KEY", "x")
    monkeypatch.setattr(A.anthropic, "Anthropic", Fake)
    monkeypatch.setattr(A, "TMDB_KEY", "test")
    monkeypatch.setattr(A, "tmdb_get", tmdb_get)
    got = client.post("/api/claude-suggestions", json={"people": [j], "short": True}).get_json()
    assert [m["title"] for m in got] == ["Short"] and got[0]["runtime"] == 90
    assert "school night" in prompts[0] and "120 minutes" in prompts[0]
    got = client.post("/api/claude-suggestions", json={"people": [j]}).get_json()
    assert [m["title"] for m in got] == ["Long", "Short"] and "school night" not in prompts[1]


# ---------------------------------------------------------------- stats + taste match
def put_cache(key, value):
    conn = A.connect()
    conn.execute("INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?,?,0)", (key, json.dumps(value)))
    conn.commit()
    conn.close()


def test_stats_year_in_review(client, people, add_movie):
    j, s = people["Jane"], people["Sam"]
    add_movie(1, "Up", {j: 10, s: 10}, watched_on="2026-03-05", genres=["Animation", "Family"])
    add_movie(2, "Heat", {j: 9, s: 2}, watched_on="2026-03-20", genres=["Crime"])
    add_movie(3, "Old", {j: 6}, watched_on="2025-07-01", genres=["Drama"])
    put_cache("/movie/1?", {"id": 1, "runtime": 96})          # runtime on hand for one of the two
    st = client.get("/api/stats?year=2026").get_json()
    assert st["year"] == "2026" and st["years"] == ["2026", "2025"]
    assert st["movies"] == 2 and st["minutes"] == 96 and st["runtime_known"] == 1
    assert st["timeline"][2] == {"label": "Mar", "count": 2}
    jane = next(p for p in st["people"] if p["name"] == "Jane")
    assert jane["seen"] == 2 and jane["rated"] == 2 and jane["fives"] == 1 and jane["avg"] == 9.5
    assert jane["favorites"][0]["title"] == "Up"
    assert [m["title"] for m in st["loved"]] == ["Up", "Heat"]
    assert [m["title"] for m in st["argued"]] == ["Heat"] and st["argued"][0]["spread"] == 7
    assert client.get("/api/stats?year=all").get_json()["movies"] == 3
    assert client.get("/api/stats?year=1999").get_json()["year"] == "all"


def test_taste_match_needs_five_shared_ratings(client, people, add_movie):
    j, s, a = people["Jane"], people["Sam"], people["Alex"]
    for i, (rj, rs) in enumerate([(10, 10), (8, 8), (6, 4), (2, 10), (9, 9)]):
        add_movie(100 + i, f"M{i}", {j: rj, s: rs, a: 5} if i < 2 else {j: rj, s: rs})
    taste = client.get("/api/stats?year=all").get_json()["taste"]
    js = next(t for t in taste if {t["a"], t["b"]} == {"Jane", "Sam"})
    # diffs 0,0,2,8,0 -> mean 2 -> 100*(1-2/9) = 78
    assert js["count"] == 5 and js["match"] == 78 and js["exact"] == 3
    assert js["split"]["title"] == "M3" and js["agree"]["title"] == "M0"
    ja = next(t for t in taste if {t["a"], t["b"]} == {"Jane", "Alex"})
    assert ja["match"] is None and ja["count"] == 2
    assert taste[0] is js                       # real matches first


# ---------------------------------------------------------------- finish the series
def collection(cid, name, parts):
    put_cache(f"/collection/{cid}?", {"id": cid, "name": name, "parts": [
        {"id": pid, "title": t, "release_date": d, "vote_count": 500, "vote_average": 7, "genre_ids": []} for pid, t, d in parts]})
    for pid, *_ in parts:
        put_cache(f"/movie/{pid}?", {"id": pid, "belongs_to_collection": {"id": cid, "name": name}})


def test_series_suggests_the_next_unseen_part(client, people, add_movie, fake_tmdb):
    j, s = people["Jane"], people["Sam"]
    collection(10, "Toy Collection", [(1, "Toy 1", "1995-11-22"), (2, "Toy 2", "1999-11-24"),
                                      (3, "Toy 3", "2010-06-18"), (4, "Toy 5", "2099-01-01")])
    collection(20, "Meh Collection", [(5, "Meh 1", "2001-01-01"), (6, "Meh 2", "2003-01-01")])
    add_movie(1, "Toy 1", {j: 9})
    add_movie(5, "Meh 1", {j: 3})              # under 3★: don't push the sequel
    got = client.get(f"/api/series?people={j}").get_json()
    assert [m["title"] for m in got] == ["Toy 2"]
    assert got[0]["series"] == {"id": 10, "name": "Toy Collection", "seen": 1, "total": 3, "avg": 9.0}
    add_movie(2, "Toy 2", {s: 8})              # Sam saw part 2: still next for Jane alone, not for Jane + Sam
    assert [m["title"] for m in client.get(f"/api/series?people={j}").get_json()] == ["Toy 2"]
    assert [m["title"] for m in client.get(f"/api/series?people={j},{s}").get_json()] == ["Toy 3"]
    client.post("/api/dismiss", json={"tmdb_id": 3, "title": "Toy 3"})
    assert client.get(f"/api/series?people={j},{s}").get_json() == []     # unreleased part 4 never shows


def test_series_respects_kid_limits(client, people, add_movie, fake_tmdb):
    _, _, certs = fake_tmdb
    a = people["Alex"]
    collection(30, "Alien Collection", [(7, "Alien", "1979-05-25"), (8, "Aliens", "1986-07-18")])
    add_movie(7, "Alien", {a: 8})
    certs[8] = "R"
    assert client.get(f"/api/series?people={a}").get_json() == []
    certs[8] = "PG"
    assert [m["title"] for m in client.get(f"/api/series?people={a}").get_json()] == ["Aliens"]
