from datetime import date

import pytest

import app as A


@pytest.mark.parametrize("scores,strength,expected", [
    ({"rt": None, "mc": None}, 1.0, 1.0),          # no scores: no effect
    ({"rt": "60%", "mc": 60}, 1.0, 1.0),           # 60 is neutral
    ({"rt": "100%", "mc": 100}, 1.0, 1.2),
    ({"rt": "0%", "mc": 0}, 1.0, 0.7),
    ({"rt": "100%", "mc": None}, 1.0, 1.2),        # one score is enough
    ({"rt": None, "mc": 0}, 1.0, 0.7),
    ({"rt": "100%", "mc": 100}, 0.5, 1.1),         # kids' lists: half strength
    ({"rt": "0%", "mc": 0}, 0.5, 0.85),
    ({"rt": "90%", "mc": 70}, 1.0, 1.1),           # averaged: 80
])
def test_critic_factor(scores, strength, expected):
    assert A.critic_factor(scores, strength) == pytest.approx(expected)


def test_dismiss_weight_is_a_quarter_of_one_star():
    assert A.DISMISS_WEIGHT == pytest.approx(0.25 * (2 - 5.5))


@pytest.mark.parametrize("raw,expected", [
    ("The Lord of the Rings", "lord of the rings"),
    ("WALL·E", "wall e"),
    ("WALL-E", "wall e"),
    ("Amélie", "amelie"),
    ("Fast & Furious", "fast and furious"),
    ("A Quiet Place", "quiet place"),
])
def test_norm_title(raw, expected):
    assert A.norm_title(raw) == expected


def test_merge_names_keeps_first_spelling_and_dedupes():
    assert A.merge_names("Jess, bmartin", "BMartin, Kilo") == "bmartin, Jess, Kilo"


def test_netflix_csv_separates_movies_from_episodes():
    text = "\n".join([
        "Title,Date",
        '"Dune: Part Two","3/1/24"',                          # a movie with a colon
        '"Kill Bill: Vol. 1","2/20/24"',
        '"Paddington 2","2/14/24"',
        '"Paddington 2","1/1/24"',                            # watched twice: latest date wins
        '"Stranger Things: Season 1: Chapter One","2/2/24"',  # season marker
        '"Bluey: Camping","1/5/24"',                          # same prefix, several suffixes = a show
        '"Bluey: Sleepytime","1/6/24"',
        '"Broken Row","not a date"',
    ])
    movies, episodes, bad = A.parse_netflix_csv(text)
    assert movies == {"Dune: Part Two": date(2024, 3, 1), "Kill Bill: Vol. 1": date(2024, 2, 20),
                      "Paddington 2": date(2024, 2, 14)}
    assert episodes == 3
    assert bad == 1


def test_netflix_csv_detects_day_first_dates():
    movies, _, bad = A.parse_netflix_csv('Title,Date\n"Up","25/12/23"\n"Coco","01/02/23"\n')
    assert movies == {"Up": date(2023, 12, 25), "Coco": date(2023, 2, 1)}
    assert bad == 0


def test_netflix_csv_rejects_other_files():
    with pytest.raises(ValueError):
        A.parse_netflix_csv("Name,When\nUp,2024-01-01\n")


@pytest.mark.parametrize("raw,expected", [
    ("Dune: Part Two (4K UHD) (2024) - Rental", ("Dune: Part Two", 2024)),
    ("RoboCop (1987)", ("RoboCop", 1987)),
    ("Paddington 2", ("Paddington 2", None)),
])
def test_clean_rental_title(raw, expected):
    assert A.clean_rental_title(raw) == expected


def test_too_young_uses_the_count_from_birthday(client):
    kid = client.post("/api/people", json={"name": "Kid"}).get_json()["id"]
    client.put(f"/api/people/{kid}", json={"birthdate": "2015-06-01", "count_from_age": 5})
    conn = A.connect()
    cut = A.age_cutoffs(conn)
    conn.close()
    assert cut == {kid: "2020-06-01"}
    assert A.too_young({kid}, "2020-05-31", cut) == {kid}
    assert A.too_young({kid}, "2020-06-01", cut) == set()
    assert A.too_young({kid}, None, cut) == set()


def test_wrong_movie_fixes_chain_and_undo():
    conn = A.connect()
    A.remember_remap(conn, 1, 2)
    A.remember_remap(conn, 2, 3)            # 1 -> 2 then 2 -> 3 means 1 -> 3
    assert A.remapped(conn, 1) == 3 and A.remapped(conn, 2) == 3
    A.remember_remap(conn, 3, 2)            # switching back undoes the 2 -> 3 fix
    assert A.remapped(conn, 2) == 2 and A.remapped(conn, 1) == 2
    conn.close()
