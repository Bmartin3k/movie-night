# Movie Night

Log the movies the household has watched. Each person rates separately (1–5 stars, half stars allowed),
then get suggestions for one person or any group watching together.

- **People** (👤 button → Manage people): add each household member. "Also known as" holds their Emby user /
  Netflix profile name (e.g. Jane = `jsmith`) so imported history is credited to them. With a **birthdate**, imported viewings from before
  "count history from age" (default 5) don't count for that person: hidden from their To rate, and not "seen" for suggestions. Each device remembers
  who's rating; tap 👤 to switch. A **kid setting** per person (up to PG-13 / PG / G) keeps every suggestion
  list that includes them within that US rating (from TMDB; movies with no US rating are left out).

- **Watched**: each card shows everyone's rating, who hasn't watched it and the family average. Filter by
  "Rated by me / Needs my rating / I haven't seen"; sort by my rating or family average. Click a card to
  rate it for anyone (one row of stars per person; everyone starts as "Not watched").
- **Suggestions**
  - Pick **who it's for**: Everyone, one person, or any mix. Nothing anyone in that group has seen is suggested.
  - *Because you liked…*: TMDB recommendations of every movie the group rated, weighted by the group's
    **average** rating (4+★ pulls up, 2.5★ and under pushes down, so one person's dislike counts against it).
    For one person, movies another family member rated 4+★ that they haven't seen are included too.
  - *Ask Claude*: sends the whole rated list (with notes) plus an optional mood ("something light for Friday")
    to Claude, which returns 8 picks with a reason for each. For a group it looks for common ground. Uses `claude-opus-5-5` (override with `CLAUDE_MODEL`).
    Each click costs a few cents.
- **To rate** is per person: only what that person could have watched and hasn't rated. That's movies credited to them
  (their Netflix profile, imports under their name, Watched movies marked seen), plus movies from shared accounts
  (Emby, rentals) watched after their count-from age. Shared-account movies come first, then ones credited to them, newest first.
  - Every movie marked played in Emby lands here, synced every 6 h and from **⟳ Sync from Emby**. "Watched by" and the date
    come from Emby; already-rated or skipped movies aren't re-queued.
  - Tap the stars, then **✓ Save** to rate it into Watched for whoever is rating on that device. A ❤ Emby favorite starts at 4.5★.
  - **Didn't watch** takes it off your list and is remembered across syncs. **Rate for others** rates it for several
    people at once. **Skip** still counts as seen, so it's never suggested.
  - Tap the summary to expand it. **Wrong movie?** fixes a bad match (see below).
- **Netflix import** (To rate tab → ⇪ Import Netflix history): on netflix.com, Account → Profiles → a profile →
  Viewing activity → *Download all* gives `NetflixViewingHistory.csv`. Upload it per profile and say whose it is.
  TV episodes are skipped; movies are matched to TMDB on exact title (released before the watch date, most-voted
  wins) and queued with the latest watch date. Titles that don't match are listed so you can add them by hand.
  Re-importing is safe, and movies already queued from Emby are merged rather than duplicated.
- **Already watched** on any suggestion adds it to Watched with that movie filled in; it then stops being suggested.
- **CSV import** (To rate → ⇪ Import CSV): any list of movies, e.g. rental/purchase history. Upload, check the
  guessed Title / Year / Date columns, pick whose list it is and a label (shown on the cards). Format tags like
  "(4K UHD)", "[HD]", "- Rental" are stripped; a year in the title or a year column picks the right remake.
- **Wrong movie?** (on To rate cards, and in a Watched movie's dialog): search and pick the right one, e.g.
  RoboCop 1987 instead of 2014. Ratings and watchers carry over (merging if the right one is already rated),
  and the fix is remembered so later Emby syncs / Netflix / CSV imports use it. Switching back undoes it.
- **Watchlist**: suggestions saved with ☆ Save. Marking one "Watched it" moves it to Watched.

## Set up

You need a machine with Docker (with the Compose plugin) and a free TMDB API key. Emby and Anthropic keys are optional.

    git clone <repo-url> movie-night && cd movie-night
    cp .env.example .env && chmod 600 .env   # then add your keys (see below)
    docker compose up -d --build              # open http://<this-machine>:8095

Then, in the app:
1. Tap **👤 Who's rating?** → **Manage people** and add everyone in the household. Add an "also known as" name for
   anyone whose Emby user or Netflix profile has a different name, plus a birthdate / kid setting for kids if you like.
2. Pick who's rating on this device, and start adding movies: search in **Watched**, sync Emby, or import
   Netflix / CSV history from the **To rate** tab.
3. Once a few movies are rated, try **Suggestions**.

Port 8095 already taken? Set `PORT=` in `.env`. The app has **no login**: keep it on your home network
(or behind a reverse proxy with authentication), not open to the internet.

After editing `.env` or the code: `docker compose up -d --build`. Data lives in `data/movies.db` (SQLite). Back it up with the folder.

Keys:
- `TMDB_API_KEY`: free at https://www.themoviedb.org/settings/api. Either the v3 API key or the v4 read token works.
  Without it you can still add movies by hand, but there's no search, posters or "Because you liked".
- `EMBY_URL` / `EMBY_API_KEY`: Emby Dashboard → Advanced → API Keys. Optional `EMBY_USERS=Ben,Jess` to limit
  which Emby users are imported, `EMBY_SYNC_HOURS=0` for button-only.
- `ANTHROPIC_API_KEY`: https://console.anthropic.com/settings/keys. Only needed for Ask Claude.

TMDB responses are cached in the DB for 7 days, so the Suggestions tab is fast after the first load.

## Files
- `app.py`: Flask API, scoring (`suggestions()`), Claude prompt (`SYSTEM_PROMPT`, `claude_suggestions()`)
- `static/`: the page (`index.html`, `style.css`, `app.js`)
