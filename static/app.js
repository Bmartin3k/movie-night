"use strict";
const IMG = "https://image.tmdb.org/t/p/";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

const state = {
  status: {}, people: [], unlinked: [], movies: [], watchlist: [], queue: [],
  suggestions: null, suggestionsKey: "", editing: null, picked: null, draft: {},
  me: Number(lsGet("me")) || null,
  queueShown: 48, watchedShown: 60,
  // big lists render in pages: 1,800 posters at once freezes a phone
  group: (() => { try { return JSON.parse(lsGet("group") || "[]"); } catch { return []; } })(),
};

async function api(path, opts = {}, retried = false) {
  const res = await fetch("/api" + path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  // grown-up only (Settings, age limits...): ask for the settings password once, then try again
  if (res.status === 403 && data.locked && !retried && await askPassword()) return api(path, opts, true);
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// ---------- settings password prompt (only shown when a protected change needs it)
function askPassword() {
  const dlg = $("#unlock-dialog");
  $("#unlock-pw").value = ""; $("#unlock-error").textContent = "";
  dlg.showModal(); $("#unlock-pw").focus();
  return new Promise((resolve) => {
    const done = (ok) => { dlg.close(); $("#unlock-form").onsubmit = null; $("#unlock-cancel").onclick = null; dlg.oncancel = null; resolve(ok); };
    $("#unlock-cancel").onclick = () => done(false);
    dlg.oncancel = () => done(false);
    $("#unlock-form").onsubmit = async (e) => {
      e.preventDefault();
      const res = await fetch("/api/unlock", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: $("#unlock-pw").value }) });
      if (res.ok) return done(true);
      const d = await res.json().catch(() => ({}));
      $("#unlock-error").textContent = d.error || "Wrong password";
      $("#unlock-pw").select();
    };
  });
}

// ---------- people helpers
const person = (id) => state.people.find((p) => p.id === id);
const pname = (id) => { const p = person(id); return p ? `${p.emoji ? p.emoji + " " : ""}${p.name}` : "?"; };
const meP = () => person(state.me);
const myWatch = (m) => m.watches.find((w) => w.person_id === state.me);
function groupIds() {
  const valid = state.group.filter((id) => person(id));
  return valid.length ? valid : state.people.map((p) => p.id);
}
function groupLabel() {
  const ids = groupIds();
  if (ids.length === state.people.length && state.people.length > 1) return "everyone";
  const n = ids.map((id) => person(id).name);
  return n.length > 1 ? n.slice(0, -1).join(", ") + " and " + n.at(-1) : n[0] || "everyone";
}

// ---------- stars (ratings are 1..10 = half stars)
function starsHTML(r) {
  let h = "";
  for (let i = 1; i <= 5; i++) {
    const v = r - (i - 1) * 2;
    h += v >= 2 ? "★" : v === 1 ? '<span class="star-half">★</span>' : '<span class="off">★</span>';
  }
  return `<span class="stars" title="${r / 2} / 5">${h}</span>`;
}
function starSpans(r) {
  return [1, 2, 3, 4, 5].map((i) => {
    const v = (r || 0) - (i - 1) * 2;
    return `<span data-i="${i}" class="${v >= 2 ? "" : v === 1 ? "star-half" : "off"}">★</span>`;
  }).join("");
}
function ratingFromPointer(e) {
  const star = e.target.closest("[data-i]");
  if (!star) return null;
  const rect = star.getBoundingClientRect();
  return (Number(star.dataset.i) - 1) * 2 + (e.clientX - rect.left < rect.width / 2 ? 1 : 2);
}
const fmtR = (r) => `${r / 2}★`;
const fmtStars = (r) => `${Math.floor(r / 2) || ""}${r % 2 ? "½" : ""}★`;   // 9 -> "4½★", 1 -> "½★"

// Hover preview on any clickable star row (To rate cards, the rating dialog): fill to the pointer + show "4½★".
function paintStars(c, r) {
  c.querySelectorAll("[data-i]").forEach((s) => {
    const v = (r || 0) - (Number(s.dataset.i) - 1) * 2;
    s.className = v >= 2 ? "" : v === 1 ? "star-half" : "off";
  });
}
function chosenRating(c) {
  if (c.classList.contains("qstars")) return Number(c.closest(".card")?.dataset.r) || 0;
  const pid = Number(c.closest(".prow")?.dataset.pid);
  return state.draft?.[pid]?.rating || 0;
}
document.addEventListener("mousemove", (e) => {
  const c = e.target.closest(".qstars, .stars.big");
  if (!c) return;
  const r = ratingFromPointer(e);
  if (!r) return;
  paintStars(c, r);
  c.classList.add("hovering");
  c.dataset.tip = fmtStars(r);
});
document.addEventListener("mouseout", (e) => {
  const c = e.target.closest(".qstars, .stars.big");
  if (!c || c.contains(e.relatedTarget)) return;
  c.classList.remove("hovering");
  delete c.dataset.tip;
  paintStars(c, chosenRating(c));
});
const fmtDate = (d) => d ? new Date(d + "T00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";

// ---------- cards
function poster(m, badge) {
  const img = m.poster_path ? `style="background-image:url('${IMG}w342${esc(m.poster_path)}')"` : "";
  const noimg = m.poster_path ? "" : `<div class="noimg">${esc(m.title)}</div>`;
  return `<div class="poster" ${img}>${noimg}${badge ? `<span class="badge">${badge}</span>` : ""}</div>`;
}
const meta = (m) => [m.year, (m.genres || []).slice(0, 2).join(", ")].filter(Boolean).join(" · ");

function whoLine(m) {
  const ws = [...m.watches].sort((a, b) => (b.person_id === state.me) - (a.person_id === state.me) || (b.rating || 0) - (a.rating || 0));
  const unseen = state.people.filter((p) => !m.watches.some((w) => w.person_id === p.id)).map((p) => p.name);
  return `<div class="who">${ws.map((w) => `<span class="${w.person_id === state.me ? "me-r" : ""} ${w.rating ? "" : "seen"}">${esc(pname(w.person_id))} ${w.rating ? fmtR(w.rating) : "· seen"}</span>`).join("")}${unseen.length && unseen.length < state.people.length ? `<span class="seen">Not watched: ${unseen.map(esc).join(", ")}</span>` : ""}</div>`;
}
function watchedCard(m) {
  const mine = myWatch(m);
  const badge = m.avg_rating ? (m.avg_rating / 2).toFixed(1).replace(/\.0$/, "") : "";
  const notes = m.watches.filter((w) => w.notes).map((w) => `<div class="notes">“${esc(w.notes)}” <span class="muted">· ${esc(person(w.person_id)?.name || "")}</span></div>`).join("");
  return `<article class="card" data-id="${m.id}" data-tmdb="${m.tmdb_id ?? ""}">
    ${poster(m, badge)}
    <div class="body">
      <div class="title">${esc(m.title)}</div>
      ${mine && !mine.rating ? `<div class="needs-row"><span class="needs">Needs your rating</span><button type="button" class="linkish" data-act="didnt">I didn't watch it</button></div>` : ""}
      ${whoLine(m)}
      <div class="meta">${esc([meta(m), fmtDate(m.watched_on)].filter(Boolean).join(" · "))}</div>
      ${m.overview ? `<div class="overview" title="Tap to expand">${esc(m.overview)}</div>` : ""}
      ${notes}
    </div></article>`;
}

// who in the group has already seen a pick that's still new to someone else, and how they rated it
const seenLine = (who) => `<div class="seen-by">✓ ${who.map((r) => r.rating ? `${esc(r.name)} rated it ${fmtR(r.rating)}` : `${esc(r.name)} has seen it`).join(", ")}</div>`;
function suggestionCard(m, kind) {
  let why = "";
  if (m.reason) why = `<div class="why">${esc(m.reason)}</div>`;
  if (kind === "watchlist" && m.saved_for_names?.length)
    why = `<div class="loved">Saved for ${m.saved_for_names.length === state.people.length && state.people.length > 1 ? "everyone" : m.saved_for_names.map(esc).join(", ")}</div>` + why;
  else {
    if (m.seen_by?.length) why += seenLine(m.seen_by);
    if (m.loved_by) why += `<div class="loved">${m.loved_by.map((l) => `${esc(l.name)} rated it ${fmtR(l.rating)}`).join(", ")}</div>`;
    if (m.because && m.because.length) {
      const multi = groupIds().length > 1;
      why += `<div class="why">Because of ${m.because.map((b) => `<b>${esc(b.title)}</b> (${multi && b.raters > 1 ? `avg ${(b.avg / 2).toFixed(1).replace(/\.0$/, "")}★` : b.fans && b.fans.length ? b.fans.map(esc).join(", ") : `${(b.avg / 2).toFixed(1).replace(/\.0$/, "")}★`})`).join(", ")}</div>`;
    }
  }
  if (m.rewatch?.length) why = seenLine(m.rewatch) + why;
  const score = m.vote_average ? `★ ${Number(m.vote_average).toFixed(1)}` : "";
  const trailer = m.tmdb_id && state.status?.tmdb ? `<button data-act="trailer" title="Watch the trailer">▶ Trailer</button>` : "";
  const btns = kind === "watchlist"
    ? `<button class="ok" data-act="tonight" title="Mark ${esc(groupLabel())} as having watched it today, then rate it">🍿 We watched it</button>${trailer}<button data-act="unsave" title="Take it off the watchlist for ${esc(groupLabel())}">Remove</button>
       ${state.status?.radarr && m.tmdb_id ? `<button class="radarr" data-act="radarr" disabled>Radarr…</button>` : ""}`
    : `<button class="ok" data-act="watched" title="Already watched it? Add it to Watched with ratings">Already watched</button>
       ${m.tmdb_id ? `<button data-act="save">${m.saved ? "★ Saved" : "☆ Save"}</button>${trailer}<button data-act="dismiss" title="Not interested">✕</button>` : ""}`;
  return `<article class="card" data-tmdb="${m.tmdb_id ?? ""}">
    ${poster(m, score)}
    <div class="body">
      <div class="title">${esc(m.title)}</div>
      <div class="meta">${esc([meta(m), m.cert].filter(Boolean).join(" · "))}</div>
      ${why}
      ${m.overview ? `<div class="overview">${esc(m.overview)}</div>` : ""}
      <div class="btns">${btns}</div>
    </div></article>`;
}

// ---------- header: who's rating
function renderMe() {
  const p = meP();
  const b = $("#me-btn");
  b.textContent = p ? `${p.emoji || "👤"} ${p.name}` : "👤 Who's rating?";
  b.classList.toggle("unset", !p);
  $("#torate-me").textContent = p ? p.name : "you";
  $("#notes-label").textContent = p ? `${p.name}'s notes` : "Notes";
}
function openMe() {
  $("#me-list").innerHTML = state.people.map((p) =>
    `<button class="${p.id === state.me ? "primary" : "ghost"}" data-pid="${p.id}">${esc(p.emoji || "👤")} ${esc(p.name)}${p.pin_set ? " 🔒" : ""}</button>`).join("")
    || `<p class="muted">Nobody yet. Add your household first.</p>`;
  $("#me-dialog").showModal();
}
$("#me-btn").addEventListener("click", openMe);
$("#me-close").addEventListener("click", () => $("#me-dialog").close());
// Who's rating is remembered by the server for this browser; a person with a PIN needs it once per device.
async function setMe(pid) {
  const p = person(pid);
  let pin = null, ask = p?.id !== state.me;
  for (;;) {
    if (p?.pin_set && ask) {
      pin = await askPin(`${p.emoji || "👤"} ${p.name}`, `Enter ${p.name}'s PIN`);
      if (pin === null) return false;
    }
    const res = await fetch("/api/me", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ person_id: pid, pin }) });
    if (res.ok) break;
    const d = await res.json().catch(() => ({}));
    if (!d.pin) { alert(d.error || "Couldn't switch"); return false; }
    pinError = pin === null ? "" : d.error;   // wrong PIN (or this device forgot you): ask again
    ask = true;
  }
  state.me = pid; lsSet("me", pid);
  return true;
}
let pinError = "";
function askPin(title, msg) {
  const dlg = $("#pin-dialog");
  $("#pin-title").textContent = title; $("#pin-msg").textContent = msg;
  $("#pin-in").value = ""; $("#pin-error").textContent = pinError; pinError = "";
  dlg.showModal(); $("#pin-in").focus();
  return new Promise((resolve) => {
    const done = (v) => { dlg.close(); $("#pin-form").onsubmit = null; $("#pin-cancel").onclick = null; dlg.oncancel = null; resolve(v); };
    $("#pin-cancel").onclick = () => done(null);
    dlg.oncancel = () => done(null);
    $("#pin-form").onsubmit = (e) => {
      e.preventDefault();
      const v = $("#pin-in").value.trim();
      if (!/^\d{4}$/.test(v)) { $("#pin-error").textContent = "4 digits"; return; }
      done(v);
    };
  });
}
$("#me-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-pid]");
  if (!b) return;
  $("#me-dialog").close();
  if (!await setMe(Number(b.dataset.pid))) return;
  state.showNotMe = false; state.queueShown = 48;
  loadQueue();
  renderAll();
});
$("#manage-people").addEventListener("click", () => { $("#me-dialog").close(); openPeople(); });
function requireMe() {
  if (meP()) return true;
  openMe();
  return false;
}

// ---------- manage people
async function loadPeople() {
  const d = await api("/people");
  state.people = d.people; state.unlinked = d.unlinked;
  if (state.me && !person(state.me)) { state.me = null; }
}
function renderPeopleDialog() {
  $("#people-rows").innerHTML = state.people.map((p) => `
    <div class="person-edit" data-pid="${p.id}">
      <div class="pe-line">
        <input class="emoji-in" data-f="emoji" value="${esc(p.emoji)}" placeholder="🙂" maxlength="4" aria-label="Emoji">
        <input class="pe-name" data-f="name" value="${esc(p.name)}" aria-label="Name">
        <input class="aka" data-f="aliases" value="${esc(p.aliases)}" placeholder="Also known as (Emby / Jellyfin / Plex / Netflix names)">
        <button type="button" class="danger" data-act="del" title="Remove ${esc(p.name)} and their ratings">✕</button>
      </div>
      <div class="pe-line pe-sub">
        <label>Born <input type="date" data-f="birthdate" value="${esc(p.birthdate || "")}" max="${new Date().toLocaleDateString("en-CA")}"></label>
        <label title="Imported viewings from before this age don't count for ${esc(p.name)}">Count history from age
          <input type="number" class="age-in" data-f="count_from_age" min="0" max="18" value="${p.count_from_age ?? 5}"></label>
        <button type="button" class="ghost small-btn" data-act="pin" title="${p.pin_set ? "Change or remove" : "Set"} ${esc(p.name)}'s 4-digit PIN">🔑 ${p.pin_set ? "PIN set" : "Set PIN"}</button>
        <select class="cert" data-f="max_cert" title="Suggestions that include ${esc(p.name)} stay within this age rating">
          ${[["", "Any rating"], ["PG-13", "Kid: up to PG-13"], ["PG", "Kid: up to PG"], ["G", "Kid: G only"]]
            .map(([v, l]) => `<option value="${v}" ${(p.max_cert || "") === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </div>
      <div class="count">${p.rated} rated${p.unrated ? ` · ${p.unrated} need rating` : ""}${p.birthdate ? ` · viewings before age ${p.count_from_age} ignored` : ""}</div>
    </div>`).join("") || `<p class="muted small">Add each person who'll rate movies, e.g. you, your partner, the kids.</p>`;
  const opts = state.people.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("#unlinked").innerHTML = state.unlinked.map((n) => `
    <div class="unl" data-name="${esc(n)}"><b>${esc(n)}</b>
      ${state.people.length ? `<select><option value="">Link to…</option>${opts}</select>` : ""}
      <button type="button" class="ghost" data-act="new">+ New person</button></div>`).join("");
  $("#unlinked-wrap").hidden = !state.unlinked.length;
}
async function openPeople() {
  await loadPeople();
  $("#people-error").textContent = "";
  renderPeopleDialog();
  $("#people-dialog").showModal();
}
async function peopleChanged() {
  await loadPeople();
  renderPeopleDialog();
  state.suggestionsKey = null;
  await loadQueue();
  renderAll();
}
$("#people-rows").addEventListener("change", async (e) => {
  const row = e.target.closest("[data-pid]");
  if (!row || !e.target.dataset.f) return;
  try {
    await api(`/people/${row.dataset.pid}`, { method: "PUT", body: { [e.target.dataset.f]: e.target.value } });
    $("#people-error").textContent = "";
    await peopleChanged();
  } catch (err) { $("#people-error").textContent = err.message; renderPeopleDialog(); }
});
$("#people-rows").addEventListener("click", async (e) => {
  const pb = e.target.closest("[data-act=pin]");
  if (pb) {
    const p = person(Number(pb.closest("[data-pid]").dataset.pid));
    const pin = await askPin(`🔑 ${p.name}'s PIN`, p.pin_set
      ? `New 4-digit PIN for ${p.name}, or 0000 to remove it. Picking ${p.name} in Who's rating? asks for it.`
      : `Pick a 4-digit PIN for ${p.name} (not 0000). Picking ${p.name} in Who's rating? will ask for it once per device.`);
    if (pin === null) return;
    if (pin !== "0000") {                     // type it twice, so a slip doesn't lock them out
      const again = await askPin(`🔑 ${p.name}'s PIN`, "Type the same PIN again.");
      if (again === null) return;
      if (again !== pin) { $("#people-error").textContent = "The two PINs didn't match, so nothing changed. Try again."; return; }
    }
    try {
      await api(`/people/${p.id}/pin`, { method: "PUT", body: { pin: pin === "0000" ? "" : pin } });
      $("#people-error").textContent = "";
      await peopleChanged();
    } catch (err) { $("#people-error").textContent = err.message; }
    return;
  }
  const b = e.target.closest("[data-act=del]");
  if (!b) return;
  const p = person(Number(b.closest("[data-pid]").dataset.pid));
  if (!confirm(`Remove ${p.name}? Their ${p.rated} ratings are deleted too.`)) return;
  try { await api(`/people/${p.id}`, { method: "DELETE" }); }
  catch (err) { $("#people-error").textContent = err.message; return; }
  await peopleChanged();
  await loadMovies();
});
$("#add-person").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const p = await api("/people", { method: "POST", body: { name: $("#np-name").value, emoji: $("#np-emoji").value } });
    $("#np-name").value = ""; $("#np-emoji").value = "";
    if (!state.me) { await loadPeople(); await setMe(p.id); }
    $("#people-error").textContent = "";
    await peopleChanged();
    $("#np-name").focus();
  } catch (err) { $("#people-error").textContent = err.message; }
});
$("#unlinked").addEventListener("change", async (e) => {
  if (e.target.tagName !== "SELECT" || !e.target.value) return;
  const name = e.target.closest("[data-name]").dataset.name;
  const p = person(Number(e.target.value));
  await api(`/people/${p.id}`, { method: "PUT", body: { aliases: [p.aliases, name].filter(Boolean).join(", ") } });
  await peopleChanged();
});
$("#unlinked").addEventListener("click", async (e) => {
  if (!e.target.closest("[data-act=new]")) return;
  const name = e.target.closest("[data-name]").dataset.name;
  const nice = prompt(`Name for the person Emby/Jellyfin/Plex/Netflix calls "${name}":`, name);
  if (!nice) return;
  try {
    const body = { name: nice.trim() };
    if (nice.trim().toLowerCase() !== name.toLowerCase()) body.aliases = name;
    const p = await api("/people", { method: "POST", body });
    if (!state.me) { await loadPeople(); await setMe(p.id); }
    await peopleChanged();
  } catch (err) { $("#people-error").textContent = err.message; }
});
$("#people-close").addEventListener("click", async () => {
  $("#people-dialog").close();
  await loadMovies();
  if (!meP() && state.people.length) openMe();
});

// ---------- watched tab
function renderWatched() {
  const q = $("#filter").value.trim().toLowerCase();
  const view = $("#view").value, sort = $("#sort").value;
  let list = state.movies.filter((m) => {
    if (q && ![m.title, ...(m.genres || []), ...m.watches.map((w) => w.notes)].join(" ").toLowerCase().includes(q)) return false;
    const mine = myWatch(m);
    if (view === "mine") return mine && mine.rating;
    if (view === "needs") return mine && !mine.rating;
    if (view === "unseen") return !mine;
    return true;
  });
  const myR = (m) => myWatch(m)?.rating || 0;
  const by = {
    date: (a, b) => (b.watched_on || "").localeCompare(a.watched_on || "") || b.created_at - a.created_at,
    mine: (a, b) => myR(b) - myR(a) || a.title.localeCompare(b.title),
    avg: (a, b) => (b.avg_rating || 0) - (a.avg_rating || 0) || a.title.localeCompare(b.title),
    title: (a, b) => a.title.localeCompare(b.title),
    year: (a, b) => (b.year || 0) - (a.year || 0),
  }[sort];
  list = list.sort(by);
  $("#watched-grid").innerHTML = list.slice(0, state.watchedShown).map(watchedCard).join("");
  fillRT($("#watched-grid"));               // critic badges for the cards on screen only
  const more = $("#watched-more");
  more.hidden = list.length <= state.watchedShown;
  more.textContent = `Show more (${list.length - state.watchedShown} left)`;
  const empty = $("#watched-empty");
  empty.hidden = list.length > 0;
  empty.innerHTML = state.movies.length
    ? "Nothing matches."
    : state.people.length ? "Nothing here yet. Hit <b>+ Add movie</b>, or rate what's waiting in <b>To rate</b>." : "First, add who's in the household: tap <b>👤 Who's rating?</b> → Manage people.";
  $("#count-watched").textContent = state.movies.length || "";
  const needs = state.movies.filter((m) => { const w = myWatch(m); return w && !w.rating; }).length;
  $("#view").options[2].textContent = `Needs my rating${needs ? ` (${needs})` : ""}`;
}
const rewatched = () => { state.watchedShown = 60; renderWatched(); };
let filterTimer;
$("#filter").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(rewatched, 300); });
$("#view").addEventListener("change", rewatched);
$("#sort").addEventListener("change", rewatched);
$("#watched-more").addEventListener("click", () => { state.watchedShown += 60; renderWatched(); });
$("#watched-grid").addEventListener("click", async (e) => {
  const card = e.target.closest(".card");
  if (card && e.target.closest("[data-act=didnt]")) {
    await api(`/movies/${card.dataset.id}`, { method: "PUT", body: { ratings: [{ person_id: state.me, remove: true }] } });
    state.suggestionsKey = null;
    await loadMovies();
    return;
  }
  if (e.target.closest(".overview")) { e.target.closest(".overview").classList.toggle("open"); return; }
  if (card) openDialog(state.movies.find((m) => m.id === Number(card.dataset.id)));
});
async function loadMovies() {
  state.movies = await api("/movies");
  renderWatched();
}

// ---------- "To rate": one list per person
// Every imported movie and every movie in Watched shows up for every person until *they* rate it,
// say they didn't watch it, or skip it. Movies the imports credit to them come first.
const MEDIA_NAMES = { emby: "Emby", jellyfin: "Jellyfin", plex: "Plex" };
const srcLabel = (q) => q.kind === "movie" ? "Watched" : q.source === "netflix" ? "Netflix" : MEDIA_NAMES[q.source] || q.source;
function queueCard(q) {
  const pre = q.kind === "queue" && q.favorite ? 9 : 0;
  const notNames = (q.not_watched || []).filter((id) => id !== state.me).map((id) => person(id)?.name).filter(Boolean);
  const others = q.ratings?.length ? `<div class="who">${q.ratings.map((r) => `<span>${esc(r.name)} ${fmtR(r.rating)}</span>`).join("")}</div>` : "";
  const credit = q.credited ? `<span class="needs">${q.kind === "movie" ? "You watched this" : "Imports say you watched"}</span>` : "";
  return `<article class="card" data-key="${esc(q.key)}" data-r="${pre}" data-tmdb="${q.tmdb_id ?? ""}">
    <div style="position:relative">${poster(q)}${pre ? '<span class="fav">❤</span>' : ""}<span class="src">${esc(srcLabel(q))}</span></div>
    <div class="body">
      <div class="title">${esc(q.title)}</div>
      <div class="meta">${esc(meta(q))}</div>
      ${credit}
      ${q.kind === "queue" && q.watched_by ? `<div class="meta">${esc([q.watched_by, fmtDate(q.watched_on)].filter(Boolean).join(" · "))}</div>` : ""}
      ${others}
      ${notNames.length ? `<div class="meta">Not watched by ${esc(notNames.join(", "))}</div>` : ""}
      ${q.too_young?.length ? `<div class="meta">Too young then: ${esc(q.too_young.map((id) => person(id)?.name).filter(Boolean).join(", "))}</div>` : ""}
      ${q.overview ? `<div class="overview" title="Tap to expand">${esc(q.overview)}</div>` : ""}
      <button type="button" class="linkish" data-act="rematch">Wrong movie?</button>
      ${q.declined
        ? `<div class="btns"><button data-act="undo-notme">I did watch it</button></div>`
        : `<div class="qstars" role="slider" aria-label="Rating">${starSpans(pre)}</div>
      <div class="btns">
        <button class="ok" data-act="confirm" ${pre ? "" : "hidden"}>✓ Save ${fmtR(pre || 0)}</button>
        <button data-act="notme" title="You didn't watch this one">Didn't watch</button>
        <button data-act="who" title="Rate it for several people at once">Rate for others</button>
        <button data-act="skip" title="Seen it, don't want to rate it">Skip</button>
      </div>`}
    </div></article>`;
}
function visibleQueue() {
  return state.queue.filter((q) =>
    (state.showNotMe ? q.declined : !q.declined));
}
function renderQueue() {
  const list = visibleQueue();
  const live = state.queue.filter((q) => !q.declined);
  const declinedN = state.queue.length - live.length;
  $("#torate-grid").innerHTML = list.slice(0, state.queueShown).map(queueCard).join("");
  fillRT($("#torate-grid"));                // critic badges for the cards on screen only
  const more = $("#torate-more");
  more.hidden = list.length <= state.queueShown;
  more.textContent = `Show more (${list.length - state.queueShown} left)`;
  $("#torate-empty").hidden = list.length > 0;
  $("#torate-empty").textContent = state.showNotMe ? "Nothing marked as not watched." : meP() ? "All caught up 🎉" : "Pick who's rating (👤) to see your list.";
  $("#count-torate").textContent = live.length || "";
  const t = $("#notme-toggle");
  t.hidden = !declinedN;
  t.textContent = state.showNotMe ? "← Back to my list" : `Show the ${declinedN} I didn't watch`;
  const media = state.status.media || {}, syncs = state.status.syncs || {};
  const on = Object.keys(MEDIA_NAMES).filter((k) => media[k]);
  $("#emby-sync-info").textContent = on.length
    ? on.map((k) => syncs[k]
        ? `${MEDIA_NAMES[k]} last synced ${new Date(syncs[k].at * 1000).toLocaleString()} · ${syncs[k].played} played movies across ${syncs[k].users.join(", ")}`
        : `${MEDIA_NAMES[k]} not synced yet.`).join(" · ")
    : "No media server set up (Emby, Jellyfin or Plex in ⚙ Settings). You can still import Netflix history.";
  $("#emby-sync").hidden = !on.length;
  $("#emby-sync").textContent = `⟳ Sync from ${on.map((k) => MEDIA_NAMES[k]).join(" + ")}`;
}
async function loadQueue() {
  state.queue = meP() ? await api(`/torate?person_id=${state.me}`) : [];
  renderQueue();
}
const findQ = (card) => state.queue.find((x) => x.key === card.dataset.key);
function dropCard(card, q) {
  card.classList.add("done");
  state.queue = state.queue.filter((x) => x !== q);
  setTimeout(renderQueue, 300);
}
async function rateQueued(card, rating) {
  if (!requireMe()) return;
  const q = findQ(card);
  card.classList.add("done");
  try {
    if (q.kind === "queue") await api(`/emby/queue/${encodeURIComponent(q.key)}/rate`, { method: "POST", body: { rating, person_id: state.me } });
    else await api(`/movies/${q.key}`, { method: "PUT", body: { ratings: [{ person_id: state.me, rating }] } });
  } catch (err) { card.classList.remove("done"); alert(err.message); return; }
  dropCard(card, q);
  state.suggestionsKey = null;
  loadMovies(); loadWatchlist();
}
const mark = (q, action) => api("/mark", { method: "POST", body: { tmdb_id: q.tmdb_id, person_id: state.me, action } });
$("#torate-grid").addEventListener("click", async (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  if (e.target.closest(".qstars")) {          // pick (half) stars first, then Save
    const r = ratingFromPointer(e);
    if (!r) return;
    card.dataset.r = r;
    card.querySelector(".qstars").innerHTML = starSpans(r);
    const ok = card.querySelector('[data-act="confirm"]');
    ok.hidden = false; ok.textContent = `✓ Save ${fmtStars(r)}`;
    return;
  }
  if (e.target.closest(".overview")) { e.target.closest(".overview").classList.toggle("open"); return; }
  const btn = e.target.closest("button[data-act]");
  if (!btn || !requireMe()) return;
  const q = findQ(card);
  try {
    if (btn.dataset.act === "confirm") rateQueued(card, Number(card.dataset.r));
    if (btn.dataset.act === "notme") { await mark(q, "not_watched"); q.declined = true; q.credited = false; card.classList.add("done"); setTimeout(renderQueue, 300); state.suggestionsKey = null; }
    if (btn.dataset.act === "undo-notme") { await mark(q, "undo_not_watched"); q.declined = false; renderQueue(); state.suggestionsKey = null; }
    if (btn.dataset.act === "skip") { await mark(q, "skip"); dropCard(card, q); state.suggestionsKey = null; }
    if (btn.dataset.act === "who") {
      if (q.kind === "queue") openDialog(null, null, q);
      else openDialog(state.movies.find((m) => String(m.id) === q.key));
    }
    if (btn.dataset.act === "rematch") {
      if (q.kind === "queue") openRematch({ ...q, emby_id: q.key }, "queue");
      else openRematch(state.movies.find((m) => String(m.id) === q.key), "movie");
    }
  } catch (err) { alert(err.message); }
});
$("#emby-sync").addEventListener("click", async () => {
  const b = $("#emby-sync"); b.disabled = true; b.textContent = "Syncing…";
  try {
    const r = await api("/sync", { method: "POST" });
    const errs = Object.entries(r).filter(([, v]) => v.error).map(([k, v]) => `${MEDIA_NAMES[k]}: ${v.error}`);
    state.status = await api("/status");
    await Promise.all([loadQueue(), loadMovies(), loadPeople()]);
    state.suggestionsKey = null;
    if (errs.length) alert(errs.join("\n"));
  } catch (err) { alert(err.message); }
  finally { b.disabled = false; renderQueue(); }
});
$("#torate-more").addEventListener("click", () => { state.queueShown += 48; renderQueue(); });
$("#notme-toggle").addEventListener("click", (e) => {
  e.preventDefault();
  state.showNotMe = !state.showNotMe; state.queueShown = 48;
  renderQueue();
});

// ---------- Netflix import
$("#netflix-btn").addEventListener("click", () => {
  if (!state.people.length) { alert("Add your household first (👤 → Manage people)."); return; }
  $("#nf-result").innerHTML = ""; $("#nf-file").value = "";
  $("#nf-profile").innerHTML = state.people.map((p) => `<option value="${esc(p.name)}" ${p.id === state.me ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  $("#netflix-dialog").showModal();
});
$("#nf-close").addEventListener("click", () => $("#netflix-dialog").close());
$("#netflix-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("file", $("#nf-file").files[0]);
  fd.append("profile", $("#nf-profile").value);
  const go = $("#nf-go"); go.disabled = true; go.textContent = "Matching titles…";
  try {
    const res = await fetch("/api/import/netflix", { method: "POST", body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || "Import failed");
    const um = d.unmatched.length
      ? `<details><summary>${d.unmatched.length} titles didn't match a movie (usually TV specials or one-off episodes)</summary>
         <ul>${d.unmatched.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>Add any real movies by hand with + Add movie.</details>` : "";
    $("#nf-result").innerHTML = `<div class="sum"><b>${d.added} movies added to To rate.</b><br>
      <span class="muted small">${d.movies} movie titles found · ${d.episodes} TV episodes skipped · ${d.already_rated} already in Watched · ${d.merged} already queued (merged)${d.bad_rows ? ` · ${d.bad_rows} unreadable rows` : ""}</span>${um}</div>`;
    await Promise.all([loadQueue(), loadMovies()]);
    state.suggestionsKey = null;
  } catch (err) {
    $("#nf-result").innerHTML = `<p class="error">${esc(err.message)}</p>`;
  } finally { go.disabled = false; go.textContent = "Import"; }
});

// ---------- "Wrong movie?" picker
let rematchTarget = null;
function openRematch(item, kind) {
  rematchTarget = { item, kind };
  $("#rematch-current").textContent = `Currently: ${item.title}${item.year ? ` (${item.year})` : ""}. Pick the right one. Ratings and who watched it carry over, and future imports will use your pick.`;
  $("#rematch-error").textContent = "";
  $("#rematch-search").value = item.title;
  $("#rematch-dialog").showModal();
  searchRematch();
}
let rmTimer, rmSeq = 0;
async function searchRematch() {
  const q = $("#rematch-search").value.trim();
  if (q.length < 2) { $("#rematch-results").innerHTML = ""; return; }
  const seq = ++rmSeq;
  try {
    const res = await api("/search?q=" + encodeURIComponent(q));
    if (seq !== rmSeq) return;
    state.rmResults = res;
    const cur = rematchTarget.item.tmdb_id;
    $("#rematch-results").innerHTML = res.map((m, i) => `<li data-i="${i}" class="${m.tmdb_id === cur ? "current" : ""}">
      ${m.poster_path ? `<img src="${IMG}w92${esc(m.poster_path)}" alt="">` : `<span class="ph"></span>`}
      <div><div>${esc(m.title)} <span class="muted">${m.year || ""}</span>${m.tmdb_id === cur ? ` <span class="seen">· current</span>` : ""}</div>
      <div class="muted small">${esc((m.overview || "").slice(0, 110))}${(m.overview || "").length > 110 ? "…" : ""}</div></div></li>`).join("")
      || `<li class="muted">No matches.</li>`;
  } catch (err) { $("#rematch-results").innerHTML = `<li class="error">${esc(err.message)}</li>`; }
}
$("#rematch-search").addEventListener("input", () => { clearTimeout(rmTimer); rmTimer = setTimeout(searchRematch, 250); });
$("#rematch-close").addEventListener("click", () => $("#rematch-dialog").close());
$("#rematch-results").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-i]");
  if (!li) return;
  const pick = state.rmResults[Number(li.dataset.i)];
  const { item, kind } = rematchTarget;
  if (pick.tmdb_id === item.tmdb_id) { $("#rematch-dialog").close(); return; }
  try {
    if (kind === "queue") await api(`/emby/queue/${encodeURIComponent(item.emby_id)}/rematch`, { method: "POST", body: { tmdb_id: pick.tmdb_id } });
    else {
      const m = await api(`/movies/${item.id}/rematch`, { method: "POST", body: { tmdb_id: pick.tmdb_id } });
      dlg.close();
      await loadMovies();
      openDialog(state.movies.find((x) => x.id === m.id));
    }
    $("#rematch-dialog").close();
    state.suggestionsKey = null;
    await Promise.all([loadQueue(), loadMovies()]);
  } catch (err) { $("#rematch-error").textContent = err.message; }
});
$("#wrong-movie").addEventListener("click", (e) => {
  e.preventDefault();
  if (state.editing) openRematch(state.editing, "movie");
  else if (state.queueItem) { dlg.close(); openRematch(state.queueItem, "queue"); }
});

// ---------- generic CSV import
let csvFile = null;
function importSummary(d) {
  const um = d.unmatched.length
    ? `<details><summary>${d.unmatched.length} titles didn't match a movie</summary>
       <ul>${d.unmatched.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>Add any real movies by hand with + Add movie.</details>` : "";
  return `<div class="sum"><b>${d.added} movies added to To rate.</b><br>
    <span class="muted small">${d.movies} movie titles found · ${d.episodes} TV episodes skipped · ${d.already_rated} already in Watched · ${d.merged} already queued (merged)${d.bad_rows ? ` · ${d.bad_rows} empty rows` : ""}</span>${um}</div>`;
}
$("#csv-btn").addEventListener("click", () => {
  if (!state.people.length) { alert("Add your household first (👤 → Manage people)."); return; }
  csvFile = null;
  $("#csv-file").value = ""; $("#csv-map").hidden = true; $("#csv-result").innerHTML = ""; $("#csv-go").disabled = true;
  $("#csv-profile").innerHTML = state.people.map((p) => `<option value="${esc(p.name)}" ${p.id === state.me ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  $("#csv-dialog").showModal();
});
$("#csv-close").addEventListener("click", () => $("#csv-dialog").close());
$("#csv-file").addEventListener("change", async () => {
  csvFile = $("#csv-file").files[0];
  $("#csv-result").innerHTML = ""; $("#csv-map").hidden = true; $("#csv-go").disabled = true;
  if (!csvFile) return;
  const fd = new FormData(); fd.append("file", csvFile);
  try {
    const res = await fetch("/api/import/csv/preview", { method: "POST", body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || "Couldn't read that file");
    const opts = (sel, none) => (none ? `<option value="-1">(none)</option>` : "") +
      d.columns.map((c, i) => `<option value="${i}" ${sel === i ? "selected" : ""}>${esc(c || `Column ${i + 1}`)}</option>`).join("");
    $("#csv-title").innerHTML = opts(d.guess.title ?? 0, false);
    $("#csv-year").innerHTML = opts(d.guess.year, true);
    $("#csv-date").innerHTML = opts(d.guess.date, true);
    if (d.guess.year == null) $("#csv-year").value = "-1";
    if (d.guess.date == null) $("#csv-date").value = "-1";
    $("#csv-rows").textContent = `${d.rows} rows. First few:`;
    $("#csv-sample").innerHTML = `<tr>${d.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>` +
      d.sample.map((r) => `<tr>${d.columns.map((_, i) => `<td>${esc(r[i] ?? "")}</td>`).join("")}</tr>`).join("");
    $("#csv-map").hidden = false; $("#csv-go").disabled = false;
  } catch (err) { $("#csv-result").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
});
$("#csv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!csvFile) return;
  const fd = new FormData();
  fd.append("file", csvFile);
  for (const [k, id] of [["title_col", "#csv-title"], ["year_col", "#csv-year"], ["date_col", "#csv-date"], ["profile", "#csv-profile"], ["source", "#csv-source"]]) fd.append(k, $(id).value);
  const go = $("#csv-go"); go.disabled = true; go.textContent = "Matching titles…";
  try {
    const res = await fetch("/api/import/csv", { method: "POST", body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || "Import failed");
    $("#csv-result").innerHTML = importSummary(d);
    await Promise.all([loadQueue(), loadMovies()]);
    state.suggestionsKey = null;
  } catch (err) {
    $("#csv-result").innerHTML = `<p class="error">${esc(err.message)}</p>`;
  } finally { go.disabled = false; go.textContent = "Import"; }
});

// ---------- suggestions tab
const claudeResults = [];
function renderForChips() {
  const sel = new Set(groupIds());
  const everyone = sel.size === state.people.length;
  const chips = (state.people.length > 1 ? `<button class="chip ${everyone ? "on" : ""}" data-all="1">👪 Everyone</button>` : "")
    + state.people.map((p) => `<button class="chip ${sel.has(p.id) && !everyone ? "on" : ""}" data-pid="${p.id}">${esc(p.emoji || "")} ${esc(p.name)}</button>`).join("");
  $$(".for-chips").forEach((el) => (el.innerHTML = chips));   // Suggestions and Watchlist share one selection
  $("#for-label").textContent = groupLabel();
  const order = ["G", "PG", "PG-13", "R", "NC-17"];
  const kids = groupIds().map(person).filter((p) => p && p.max_cert);
  const lim = kids.length ? kids.map((p) => p.max_cert).sort((a, b) => order.indexOf(a) - order.indexOf(b))[0] : null;
  for (const note of [$("#kid-note"), $("#wl-kid-note")]) {
    note.hidden = !lim;
    if (lim) note.textContent = `👶 Kid-safe: only ${lim === "G" ? "G" : `${lim} and under`}, because ${kids.map((p) => p.name).join(" & ")} ${kids.length > 1 ? "are" : "is"} included.`;
  }
}
const onForChip = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.all) state.group = [];
  else {
    const id = Number(b.dataset.pid);
    const everyone = groupIds().length === state.people.length;
    const cur = new Set(everyone ? [] : groupIds());
    cur.has(id) ? cur.delete(id) : cur.add(id);
    state.group = [...cur];
  }
  lsSet("group", JSON.stringify(state.group));
  renderForChips();
  claudeResults.length = 0; renderClaude();
  if (!$("#tab-suggest").hidden) loadSuggestions(true); else state.suggestionsKey = null;
  loadWatchlist();
};
$$(".for-chips").forEach((el) => el.addEventListener("click", onForChip));
function renderSuggestions() {
  const g = $("#genre-filter").value;
  const fb = $("#fresh-only");
  fb.hidden = !state.status.rt;
  const fresh = state.status.rt && state.freshOnly;
  fb.setAttribute("aria-pressed", String(!!fresh));
  const all = state.suggestions || [];
  all.forEach((m) => { if (m.tmdb_id && "mc" in m && !(m.tmdb_id in rtCache)) rtCache[m.tmdb_id] = { rt: m.rt, mc: m.mc }; });
  const unknown = fresh && all.some((m) => m.tmdb_id && !rtSettled(m.tmdb_id));
  const list = all.filter((m) => (!g || (m.genres || []).includes(g)) && (!fresh || isFresh(m)));
  $("#suggest-grid").innerHTML = unknown ? `<div class="spinner">Checking Rotten Tomatoes scores…</div>` : list.map((m) => suggestionCard(m)).join("");
  if (unknown) loadRT(all.map((m) => m.tmdb_id)).then(renderSuggestions);
  else fillRT($("#suggest-grid"));
  fillWhere($("#suggest-grid"));
  const empty = $("#suggest-empty");
  if (!state.status.tmdb) { empty.hidden = false; empty.textContent = "Add a TMDB API key in ⚙ Settings to get these."; }
  else if (state.suggestions && !state.suggestions.length && !tonightOn()) { empty.hidden = false; empty.textContent = `Nothing yet for ${groupLabel()}. Rate a few movies higher than 3 stars.`; }
  else if (!unknown && all.length && !list.length && fresh) { empty.hidden = false; empty.textContent = "None of these are Fresh on Rotten Tomatoes. Turn off 🍅 Fresh only to see them all."; }
  else if (state.suggestions && !all.length && tonightOn()) { empty.hidden = false; empty.textContent = `Nothing for ${groupLabel()} is in the library or on your services right now. Turn off 📺 Watch tonight to see everything.`; }
  else empty.hidden = true;
  const genres = [...new Set((state.suggestions || []).flatMap((m) => m.genres || []))].sort();
  $("#genre-filter").innerHTML = `<option value="">All genres</option>` + genres.map((x) => `<option ${x === g ? "selected" : ""}>${esc(x)}</option>`).join("");
}
function renderClaude() { $("#claude-grid").innerHTML = claudeResults.map((m) => suggestionCard(m)).join(""); fillRT($("#claude-grid")); fillWhere($("#claude-grid")); }

// ---------- watch tonight: in the library (Radarr has the file) or on a streaming service in Settings
const whereCache = {};
let libIcon = true;          // the media server's icon for "in the library"; ▶ if it can't be loaded
fetch("/api/library-icon", { method: "HEAD" }).then((r) => { libIcon = r.ok; }).catch(() => { libIcon = false; });
const tonightOn = () => !!(state.status?.tonight && state.tonight);
function whereBadge(card, w) {
  if (!w || card.querySelector(".where") || (!w.library && !w.stream?.length)) return;
  const lib = libIcon ? `<img src="/api/library-icon" alt="Library" title="In your library (downloaded by Radarr)" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'lib',textContent:'▶',title:this.title}))">`
    : `<span class="lib" title="In your library (downloaded by Radarr)">▶</span>`;
  const tags = [...(w.library ? [lib] : []),
    ...w.stream.map((n) => w.logos?.[n]
      ? `<img src="${IMG}w92${esc(w.logos[n])}" alt="${esc(n)}" title="Streaming on ${esc(n)}">`
      : `<span title="Streaming on ${esc(n)}">${esc(n)}</span>`)];
  card.querySelector(".poster")?.insertAdjacentHTML("beforeend", `<div class="where">${tags.join("")}</div>`);
}
async function loadWhere(ids) {
  const need = [...new Set(ids.map(String).filter((id) => id && id !== "null" && !(id in whereCache)))];
  for (let i = 0; i < need.length; i += 50) {
    try { Object.assign(whereCache, await api(`/where?ids=${need.slice(i, i + 50).join(",")}`)); }
    catch { /* badges are a nice-to-have */ }
  }
}
async function fillWhere(grid) {
  if (!state.status?.tonight || !grid) return;
  const cards = () => grid.querySelectorAll(".card[data-tmdb]");
  await loadWhere([...cards()].map((c) => c.dataset.tmdb));
  cards().forEach((c) => whereBadge(c, whereCache[c.dataset.tmdb]));
}
function renderTonight() {
  $$(".tonight-btn").forEach((b) => { b.hidden = !state.status?.tonight; b.setAttribute("aria-pressed", String(tonightOn())); });
}
state.tonight = lsGet("tonight") === "1";
$$(".tonight-btn").forEach((b) => b.addEventListener("click", () => {
  state.tonight = !state.tonight;
  lsSet("tonight", state.tonight ? "1" : "");
  renderTonight();
  if (!$("#tab-suggest").hidden) loadSuggestions(true); else state.suggestionsKey = null;
  loadWatchlist();
}));

// ---------- trailer
const tdlg = $("#trailer-dialog");
async function openTrailer(m) {
  $("#trailer-title").textContent = `${m.title}${m.year ? ` (${m.year})` : ""}`;
  $("#trailer-msg").textContent = "Finding the trailer…";
  $("#trailer-iframe").src = "about:blank";
  $("#trailer-yt").hidden = true;
  tdlg.showModal();
  try {
    const t = await api(`/trailer/${m.tmdb_id}`);
    $("#trailer-iframe").src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(t.key)}?autoplay=1&rel=0`;
    $("#trailer-yt").href = `https://www.youtube.com/watch?v=${encodeURIComponent(t.key)}`;
    $("#trailer-yt").hidden = false;
    $("#trailer-msg").textContent = t.name;
  } catch (err) {
    $("#trailer-msg").textContent = err.message;
    $("#trailer-yt").href = `https://www.youtube.com/results?search_query=${encodeURIComponent(`${m.title} ${m.year || ""} trailer`)}`;
    $("#trailer-yt").textContent = "Search YouTube"; $("#trailer-yt").hidden = false;
  }
}
$("#trailer-close").addEventListener("click", () => tdlg.close());
tdlg.addEventListener("close", () => { $("#trailer-iframe").src = "about:blank"; $("#trailer-yt").textContent = "Open on YouTube"; });

// Rotten Tomatoes Tomatometer badges (OMDb), fetched after the cards are drawn so they never slow them down
const rtCache = {};
function rtBadge(card, v) {           // v = {rt: "92%" | null, mc: 74 | null}
  if (!v || (!v.rt && v.mc == null) || card.querySelector(".rt")) return;
  const n = parseInt(v.rt, 10);
  const parts = [];
  if (v.rt) parts.push(`<span title="Rotten Tomatoes Tomatometer">${n >= 60 ? "🍅" : "🤢"} ${esc(v.rt)}</span>`);
  if (v.mc != null) parts.push(`<span class="mc ${v.mc >= 61 ? "good" : v.mc >= 40 ? "mixed" : "bad"}" title="Metacritic Metascore">${Number(v.mc)}</span>`);
  card.querySelector(".poster")?.insertAdjacentHTML("beforeend", `<span class="rt">${parts.join(" ")}</span>`);
}
// Scores the server couldn't look up just now (OMDb's daily limit, a network blip) aren't in its answer.
// Remember when we asked, and ask again in 10 minutes instead of on every redraw.
const rtAsked = new Map();
const RT_RETRY_MS = 10 * 60 * 1000;
const rtSettled = (id) => String(id) in rtCache || Date.now() - (rtAsked.get(String(id)) || 0) < RT_RETRY_MS;
async function loadRT(ids, onBatch) {      // fetch scores we don't have yet, 40 at a time
  const need = [...new Set(ids.map(String).filter((id) => id && id !== "null" && !rtSettled(id)))];
  need.forEach((id) => rtAsked.set(id, Date.now()));
  for (let i = 0; i < need.length; i += 40) {
    try { Object.assign(rtCache, await api(`/rt?ids=${need.slice(i, i + 40).join(",")}`)); }
    catch { /* asked: retried after RT_RETRY_MS */ }
    onBatch?.();
  }
}
const paintRT = (grid) => grid.querySelectorAll(".card[data-tmdb]").forEach((c) => c.dataset.tmdb in rtCache && rtBadge(c, rtCache[c.dataset.tmdb]));
async function fillRT(grid) {
  if (!state.status?.rt || !grid) return;
  paintRT(grid);
  await loadRT([...grid.querySelectorAll(".card[data-tmdb]")].map((c) => c.dataset.tmdb), () => paintRT(grid));
}
const isFresh = (m) => parseInt(rtCache[m.tmdb_id]?.rt, 10) >= 60;
async function loadSuggestions(force) {
  renderForChips();
  if (!state.status.tmdb) return renderSuggestions();
  const key = groupIds().join(",") + (tonightOn() ? "&tonight=1" : "");
  if (state.suggestions && !force && state.suggestionsKey === key) return renderSuggestions();
  $("#suggest-grid").innerHTML = `<div class="spinner">Finding movies for ${esc(groupLabel())}${tonightOn() ? " you can watch tonight" : ""}…</div>`;
  try {
    state.suggestions = await api("/suggestions?people=" + key); state.suggestionsKey = key;
    state.suggestions.forEach((m) => { if (m.where && m.tmdb_id) whereCache[m.tmdb_id] = m.where; });
  }
  catch (err) { state.suggestions = []; $("#suggest-empty").textContent = err.message; }
  renderSuggestions();
}
$("#genre-filter").addEventListener("change", renderSuggestions);
state.freshOnly = lsGet("freshOnly") === "1";
$("#fresh-only").addEventListener("click", () => {
  state.freshOnly = !state.freshOnly;
  lsSet("freshOnly", state.freshOnly ? "1" : "");
  renderSuggestions();
});
$("#refresh-suggest").addEventListener("click", () => loadSuggestions(true));
// ---------- dismissed picks: bring back one at a time, or all
let dismissedChanged = false;
async function openDismissed() {
  dismissedChanged = false;
  const ul = $("#dismissed-list");
  ul.innerHTML = `<li class="muted">Loading…</li>`;
  $("#dismissed-dialog").showModal();
  try {
    const rows = await api("/dismissed");
    ul.innerHTML = rows.map((m) => `<li data-tmdb="${m.tmdb_id}">
      ${m.poster_path ? `<img src="${IMG}w92${esc(m.poster_path)}" alt="">` : `<span class="ph"></span>`}
      <div class="grow"><div>${esc(m.title || "?")} <span class="muted">${m.year || ""}</span></div>
        <div class="muted small">Dismissed ${fmtDate(new Date(m.created_at * 1000).toISOString().slice(0, 10))}</div></div>
      <button type="button" class="ghost" data-act="undismiss">Bring back</button></li>`).join("")
      || `<li class="muted">Nothing dismissed.</li>`;
    $("#clear-dismissed").hidden = !rows.length;
  } catch (err) { ul.innerHTML = `<li class="error">${esc(err.message)}</li>`; }
}
$("#open-dismissed").addEventListener("click", (e) => { e.preventDefault(); openDismissed(); });
$("#dismissed-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act=undismiss]");
  if (!btn) return;
  const li = btn.closest("li");
  btn.disabled = true;
  try { await api(`/dismiss/${li.dataset.tmdb}`, { method: "DELETE" }); }
  catch (err) { btn.disabled = false; alert(err.message); return; }
  li.remove(); dismissedChanged = true;
  if (!$("#dismissed-list li")) { $("#dismissed-list").innerHTML = `<li class="muted">Nothing dismissed.</li>`; $("#clear-dismissed").hidden = true; }
});
$("#clear-dismissed").addEventListener("click", async () => {
  if (!confirm("Bring back every dismissed pick?")) return;
  await api("/dismiss", { method: "DELETE" });
  dismissedChanged = true;
  $("#dismissed-list").innerHTML = `<li class="muted">Nothing dismissed.</li>`; $("#clear-dismissed").hidden = true;
});
$("#dismissed-close").addEventListener("click", () => $("#dismissed-dialog").close());
$("#dismissed-dialog").addEventListener("close", () => { if (dismissedChanged) { state.suggestionsKey = null; loadSuggestions(true); } });
$("#claude-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#claude-form button");
  btn.disabled = true;
  $("#claude-grid").innerHTML = `<div class="spinner">Claude is going through ${esc(groupLabel())}'s ratings…</div>`;
  try {
    const res = await api("/claude-suggestions", { method: "POST", body: { request: $("#claude-wish").value, people: groupIds() } });
    claudeResults.splice(0, claudeResults.length, ...res);
    renderClaude();
  } catch (err) {
    $("#claude-grid").innerHTML = `<p class="error">${esc(err.message)}</p>`;
  } finally { btn.disabled = !state.status.claude; }
});

function findSuggestion(tmdb, title) {
  for (const p of [claudeResults, state.suggestions || [], state.watchlist]) {
    const m = p.find((x) => (tmdb && x.tmdb_id === tmdb) || (!tmdb && x.title === title));
    if (m) return m;
  }
}
async function onSuggestionClick(e) {
  const btn = e.target.closest("button[data-act]");
  const card = e.target.closest(".card");
  if (!card) return;
  if (!btn) { card.querySelector(".overview")?.classList.toggle("open"); return; }
  const tmdb = Number(card.dataset.tmdb) || null;
  const m = findSuggestion(tmdb, card.querySelector(".title").textContent);
  if (!m) return;
  try {
    if (btn.dataset.act === "watched" || btn.dataset.act === "tonight") {
      const existing = tmdb && state.movies.find((x) => x.tmdb_id === tmdb);
      openDialog(existing || null, existing ? null : m, null, true);
      if (btn.dataset.act === "tonight") markJustWatched();
    } else if (btn.dataset.act === "trailer") {
      openTrailer(m);
    } else if (btn.dataset.act === "save") {
      if (m.saved) { await api(`/watchlist/${tmdb}?people=${groupIds().join(",")}`, { method: "DELETE" }); m.saved = false; }
      else {
        const reason = m.reason || (m.because?.length ? "Because of " + m.because.map((b) => b.title).join(", ") : "")
          || (m.loved_by ? m.loved_by.map((l) => `${l.name} rated it ${fmtR(l.rating)}`).join(", ") : "");
        await api("/watchlist", { method: "POST", body: { ...m, reason, people: groupIds() } }); m.saved = true;
      }
      btn.textContent = m.saved ? "★ Saved" : "☆ Save";
      if (m.saved && card.closest("#suggest-grid, #claude-grid")) {   // it's on the watchlist now: off Suggestions
        card.classList.add("done");
        state.suggestions = (state.suggestions || []).filter((x) => x.tmdb_id !== tmdb);
        const i = claudeResults.indexOf(m); if (i >= 0) claudeResults.splice(i, 1);
        setTimeout(() => { renderSuggestions(); renderClaude(); }, 300);
      }
      loadWatchlist();
    } else if (btn.dataset.act === "dismiss") {
      await api("/dismiss", { method: "POST", body: { tmdb_id: tmdb, title: m.title } });
      state.suggestions = (state.suggestions || []).filter((x) => x.tmdb_id !== tmdb);
      const i = claudeResults.indexOf(m); if (i >= 0) claudeResults.splice(i, 1);
      renderSuggestions(); renderClaude();
    } else if (btn.dataset.act === "radarr") {
      btn.disabled = true; btn.textContent = "Requesting…";
      try { const r = await api("/radarr/request", { method: "POST", body: { tmdb_id: tmdb } }); setRadarr(btn, r.state); }
      catch (err) { btn.disabled = false; btn.textContent = "⬇ Request in Radarr"; alert(err.message); }
    } else if (btn.dataset.act === "unsave") {      // off the selected people's lists; stays for anyone else it was saved for
      await api(`/watchlist/${tmdb}?people=${groupIds().join(",")}`, { method: "DELETE" });
      state.suggestionsKey = null;
      loadWatchlist();
    }
  } catch (err) { alert(err.message); }
}
["#suggest-grid", "#claude-grid", "#watchlist-grid"].forEach((s) => $(s).addEventListener("click", onSuggestionClick));

// ---------- watchlist tab
// Radarr button on watchlist cards: shows whether Radarr has it, or lets you request it
const RADARR_LABEL = { downloaded: "✓ Downloaded", wanted: "⏳ In Radarr, searching", unmonitored: "⬇ Request in Radarr" };
function setRadarr(btn, st) {
  btn.dataset.state = st || "none";
  btn.textContent = st ? RADARR_LABEL[st] : "⬇ Request in Radarr";
  btn.disabled = st === "downloaded" || st === "wanted";
  btn.title = st === "wanted" ? "Radarr is monitoring it and will grab it when it finds a release" : st === "downloaded" ? "Radarr has the file" : "Add it to Radarr and start a search";
}
async function fillRadarr() {
  const btns = [...$("#watchlist-grid").querySelectorAll("[data-act=radarr]")];
  if (!btns.length) return;
  const ids = btns.map((b) => b.closest(".card").dataset.tmdb);
  try {
    const st = await api(`/radarr/status?ids=${ids.join(",")}`);
    btns.forEach((b, i) => setRadarr(b, st[ids[i]]));
  } catch (err) { btns.forEach((b) => { b.textContent = "Radarr unavailable"; b.title = err.message; }); }
}
async function loadWatchlist() {
  renderTonight();
  state.watchlist = await api("/watchlist?people=" + groupIds().join(","));
  let list = state.watchlist;
  if (tonightOn()) {
    $("#watchlist-grid").innerHTML = `<div class="spinner">Checking what you can watch tonight…</div>`;
    await loadWhere(list.map((m) => m.tmdb_id));
    list = list.filter((m) => { const w = whereCache[m.tmdb_id]; return w && (w.library || w.stream?.length); });
  }
  $("#watchlist-grid").innerHTML = list.map((m) => suggestionCard(m, "watchlist")).join("");
  fillRT($("#watchlist-grid"));
  fillWhere($("#watchlist-grid"));
  fillRadarr();
  $("#watchlist-empty").hidden = list.length > 0;
  $("#watchlist-empty").innerHTML = state.watchlist.length && tonightOn()
    ? `None of ${esc(groupLabel())}'s ${state.watchlist.length} saved movies are in the library or on your services. Turn off <b>📺 Watch tonight</b>, or request one in Radarr.`
    : `Nothing saved for ${esc(groupLabel())} that they haven't seen. Save suggestions here with <b>☆ Save</b>.`;
  $("#count-watchlist").textContent = state.watchlist.length || "";
}

// ---------- add / edit dialog (one row of stars per person)
const dlg = $("#movie-dialog");
function renderPersonRows() {
  const order = [...state.people].sort((a, b) => (b.id === state.me) - (a.id === state.me));
  $("#person-rows").innerHTML = order.map((p) => {
    const d = state.draft[p.id];
    const has = d && d.state !== "none";
    const label = !has ? "" : d.rating ? fmtR(d.rating) : "seen";
    return `<div class="prow ${p.id === state.me ? "me-row" : ""} ${has ? "has" : ""}" data-pid="${p.id}">
      <span class="pname">${esc(p.emoji || "👤")} ${esc(p.name)}</span>
      <span class="stars big">${starSpans(d?.rating)}</span>
      <span class="state">${label}</span>
      <button type="button" class="seen-b ${d?.state === "seen" ? "on" : ""}" title="${esc(p.name)} saw it but hasn't rated it">Seen</button>
      <button type="button" class="nw ${has ? "" : "on"}" title="${esc(p.name)} hasn't watched it">Not watched</button></div>`;
  }).join("") || `<p class="muted small">Add your household first (👤 → Manage people).</p>`;
}
$("#person-rows").addEventListener("click", (e) => {
  const row = e.target.closest(".prow");
  if (!row) return;
  const pid = Number(row.dataset.pid);
  if (e.target.closest(".nw")) state.draft[pid] = { ...state.draft[pid], state: "none", rating: null, explicit: true };
  else if (e.target.closest(".seen-b")) state.draft[pid] = { ...state.draft[pid], state: "seen", rating: null };
  else if (e.target.closest(".stars")) {
    const r = ratingFromPointer(e);
    if (r) state.draft[pid] = { state: "rated", rating: r };
  }
  renderPersonRows();
});
function setPicked(m) {
  state.picked = m;
  $("#search-wrap").hidden = !!m || !state.status.tmdb;
  $("#manual").hidden = !!m || state.status.tmdb;
  $("#picked").hidden = !m;
  if (m) {
    $("#picked-poster").src = m.poster_path ? `${IMG}w154${m.poster_path}` : "";
    $("#picked-title").textContent = `${m.title}${m.year ? ` (${m.year})` : ""}`;
  }
}
// 🍿 "We just watched it": everyone selected on Suggestions/Watchlist is marked Seen, dated today. Each person
// can rate right here or later (it waits in their To rate). Nobody else is touched.
function markJustWatched() {
  for (const id of groupIds()) {
    const d = state.draft[id];
    if (!d || d.state === "none") state.draft[id] = { ...d, state: "seen", rating: null };
  }
  $("#m-date").value = new Date().toLocaleDateString("en-CA");
  renderPersonRows();
}
$("#just-watched").addEventListener("click", markJustWatched);
function openDialog(movie, fromSuggestion, queueItem, fromCard) {
  if (!state.people.length) { openPeople(); return; }
  if (!requireMe()) return;
  state.editing = movie || null;
  state.queueItem = queueItem || null;
  if (queueItem) fromSuggestion = queueItem;
  $("#form-error").textContent = "";
  $("#dialog-title").textContent = movie ? movie.title : queueItem ? `Who watched ${queueItem.title}?` : "Add a movie";
  $("#delete-btn").hidden = !movie;
  $("#search").value = ""; $("#search-results").innerHTML = "";
  $("#m-title").value = ""; $("#m-year").value = "";
  state.draft = {};
  (movie?.watches || []).forEach((w) => { state.draft[w.person_id] = { state: w.rating ? "rated" : "seen", rating: w.rating, orig: true }; });
  (queueItem?.watchers || []).forEach((id) => { state.draft[id] = { state: "seen", rating: null, orig: true }; });
  $("#m-date").value = movie ? movie.watched_on || "" : queueItem?.watched_on || "";   // blank = don't remember
  $("#m-notes").value = (movie && myWatch(movie)?.notes) || "";
  const src = movie || fromSuggestion;
  if (src && (src.tmdb_id || movie)) setPicked(src);
  else if (src) { setPicked(null); $("#search-wrap").hidden = true; $("#manual").hidden = false; $("#m-title").value = src.title; $("#m-year").value = src.year || ""; }
  else setPicked(null);
  $("#picked").hidden = $("#picked").hidden || !!movie;
  $("#repick").hidden = !!movie || !!queueItem;
  $("#wrong-wrap").hidden = !(movie && movie.tmdb_id) && !queueItem;
  $("#just-wrap").hidden = !fromCard;
  $("#just-who").textContent = fromCard ? `marks ${groupLabel()} as seen, today` : "";
  renderPersonRows();
  dlg.showModal();
  if (!src && state.status.tmdb) $("#search").focus();
}
$("#m-date-clear").addEventListener("click", () => { $("#m-date").value = ""; });
let searchTimer, searchSeq = 0;
$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("#search").value.trim();
  if (q.length < 2) { $("#search-results").innerHTML = ""; return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const res = await api("/search?q=" + encodeURIComponent(q));
      if (seq !== searchSeq) return;
      state.searchResults = res;
      $("#search-results").innerHTML = res.map((m, i) => `<li data-i="${i}">
        ${m.poster_path ? `<img src="${IMG}w92${esc(m.poster_path)}" alt="">` : `<span class="ph"></span>`}
        <div><div>${esc(m.title)} <span class="muted">${m.year || ""}</span></div>
        <div class="muted small">${esc((m.genres || []).slice(0, 3).join(", "))}${m.watched ? ` <span class="seen">· already in Watched</span>` : ""}</div></div></li>`).join("")
        || `<li class="muted">No matches. <a href="#" id="go-manual">Enter it by hand</a></li>`;
    } catch (err) { $("#search-results").innerHTML = `<li class="error">${esc(err.message)}</li>`; }
  }, 250);
});
$("#search-results").addEventListener("click", (e) => {
  if (e.target.id === "go-manual") {
    e.preventDefault(); $("#search-wrap").hidden = true; $("#manual").hidden = false;
    $("#m-title").value = $("#search").value; return;
  }
  const li = e.target.closest("li[data-i]");
  if (!li) return;
  const m = state.searchResults[Number(li.dataset.i)];
  const existing = state.movies.find((x) => x.tmdb_id === m.tmdb_id);
  if (existing) { openDialog(existing); return; }
  setPicked(m);
});
$("#search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#search-results li[data-i]")?.click(); }
});
$("#repick").addEventListener("click", (e) => { e.preventDefault(); setPicked(null); $("#search").focus(); });
$("#cancel-btn").addEventListener("click", () => dlg.close());
$("#add-btn").addEventListener("click", () => openDialog());

$("#movie-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#form-error");
  const notes = $("#m-notes").value;
  const ratings = [];
  for (const p of state.people) {
    const d = state.draft[p.id];
    if (!d) continue;
    if (d.state === "none") { if (d.orig !== undefined || state.editing || d.explicit) ratings.push({ person_id: p.id, remove: true }); continue; }
    const entry = { person_id: p.id, rating: d.rating || null };
    if (p.id === state.me) entry.notes = notes;
    ratings.push(entry);
  }
  // notes typed for me without touching my stars on an existing movie
  if (state.editing && !ratings.some((r) => r.person_id === state.me) && notes !== (myWatch(state.editing)?.notes || "")) {
    const mine = myWatch(state.editing);
    if (mine) ratings.push({ person_id: state.me, rating: mine.rating, notes });
  }
  if (!state.editing && !ratings.some((r) => !r.remove)) { err.textContent = "Rate it for at least one person."; return; }
  let body;
  if (state.editing) body = { watched_on: $("#m-date").value, ratings };
  else if (state.picked) {
    const p = state.picked;
    body = { tmdb_id: p.tmdb_id, title: p.title, year: p.year, poster_path: p.poster_path, overview: p.overview, genres: p.genres, watched_on: $("#m-date").value, ratings };
  } else {
    if (!$("#m-title").value.trim()) { err.textContent = "Search for a movie or type a title."; return; }
    body = { title: $("#m-title").value, year: $("#m-year").value, watched_on: $("#m-date").value, ratings };
  }
  if (state.queueItem) {
    const qi = state.queueItem;
    const watches = ratings.filter((r) => !r.remove);
    if (!watches.length) { err.textContent = "Pick at least one person who watched it."; return; }
    const notWatched = [...new Set([...qi.watchers, ...qi.not_watched])].filter((id) => !watches.some((w) => w.person_id === id));
    $("#save-btn").disabled = true;
    try {
      await api(`/emby/queue/${encodeURIComponent(qi.key || qi.emby_id)}/resolve`, { method: "POST", body: { watches, not_watched: notWatched, watched_on: $("#m-date").value } });
      dlg.close(); state.suggestionsKey = null;
      await Promise.all([loadMovies(), loadQueue(), loadWatchlist()]);
    } catch (ex) { err.textContent = ex.message; }
    finally { $("#save-btn").disabled = false; }
    return;
  }
  $("#save-btn").disabled = true;
  try {
    if (state.editing) await api(`/movies/${state.editing.id}`, { method: "PUT", body });
    else await api("/movies", { method: "POST", body });
    dlg.close();
    if (body.tmdb_id) {
      const i = claudeResults.findIndex((x) => x.tmdb_id === body.tmdb_id); if (i >= 0) claudeResults.splice(i, 1);
    }
    state.suggestionsKey = null;              // refetch next time the tab opens...
    if (body.tmdb_id && state.suggestions) {  // ...but drop the rated movie from the grid now
      state.suggestions = state.suggestions.filter((x) => x.tmdb_id !== body.tmdb_id);
      renderSuggestions();
    }
    await Promise.all([loadMovies(), loadWatchlist(), loadQueue()]);
    renderClaude();
  } catch (ex) { err.textContent = ex.message; }
  finally { $("#save-btn").disabled = false; }
});
$("#delete-btn").addEventListener("click", async () => {
  if (!confirm(`Remove “${state.editing.title}” and everyone's ratings for it?`)) return;
  await api(`/movies/${state.editing.id}`, { method: "DELETE" });
  dlg.close(); state.suggestionsKey = null; loadMovies();
});

// ---------- tabs
function showTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => (p.hidden = p.id !== "tab-" + name));
  if (name === "suggest") loadSuggestions(false);
  if (name === "watchlist") renderForChips();
  lsSet("tab", name);
}
$$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

function renderAll() {
  renderMe(); renderWatched(); renderQueue(); renderForChips(); renderTonight();
  if (!$("#tab-suggest").hidden) loadSuggestions(false);
}

// ---------- boot
(async function init() {
  state.status = await api("/status");
  const missing = [!state.status.tmdb && "a TMDB key (search, posters, suggestions)", !state.status.claude && "an Anthropic key (Ask Claude)"].filter(Boolean);
  if (missing.length) {
    const b = $("#banner"); b.hidden = false;
    b.innerHTML = `Not configured yet: ${missing.map(esc).join(" and ")}. <a href="#" id="banner-settings">Add them in ⚙ Settings</a>.`;
    $("#banner-settings").addEventListener("click", (e) => { e.preventDefault(); openSettings(); });
  }
  if (!state.status.claude) $("#claude-form button").disabled = true;
  await loadPeople();
  const srv = (await api("/me")).person_id;      // the server knows who this browser is
  if (srv) { state.me = srv; lsSet("me", srv); }
  else if (state.me && person(state.me) && !person(state.me).pin_set) {       // from before PINs: carry it over
    await api("/me", { method: "POST", body: { person_id: state.me } });
  } else state.me = null;
  await Promise.all([loadMovies(), loadWatchlist(), loadQueue()]);
  renderMe();
  showTab(lsGet("tab") || (state.queue.length ? "torate" : "watched"));
  if (!state.people.length) openPeople();
  else if (!meP()) openMe();
})();

// ---------- settings: API keys (saved in the database, override .env)
const SERVICES = {
  tmdb: ["TMDB", "Needed for search, posters and suggestions.", "https://www.themoviedb.org/settings/api"],
  claude: ["Anthropic (Ask Claude)", "Only used by the Ask Claude button.", "https://console.anthropic.com/settings/keys"],
  omdb: ["OMDb (Rotten Tomatoes + Metacritic)", "Optional: critic scores and the critics boost. Free tier 1,000/day.", "https://www.omdbapi.com/apikey.aspx"],
  radarr: ["Radarr", "Optional: Request in Radarr on watchlist cards. Key: Radarr → Settings → General.", ""],
  emby: ["Emby", "Optional: imports played movies into To rate. Key: Dashboard → Advanced → API Keys.", ""],
  jellyfin: ["Jellyfin", "Optional: imports played movies into To rate. Key: Dashboard → API Keys → +. URL like http://192.168.1.50:8096", ""],
  plex: ["Plex", "Optional: imports every account's watch history (movies) into To rate. Use the server owner's token. URL like http://192.168.1.50:32400", "https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/"],
  streaming: ["Streaming services (📺 Watch tonight)", "Optional: the services you pay for. Cards show where each movie streams, and 📺 Watch tonight keeps only those plus what Radarr has downloaded. Data from TMDB / JustWatch, refreshed weekly. Tap a service below to add or remove it.", ""],
};
let settingsList = [];
const SRC_LABEL = { saved: "saved here", env: "from .env", "": "not set" };
async function openSettings() {
  $("#settings-error").textContent = "";
  $("#settings-body").innerHTML = `<div class="spinner">Loading…</div>`;
  $("#settings-dialog").showModal();
  try { settingsList = await api("/settings"); state.lock = await api("/lock"); }
  catch (err) { $("#settings-dialog").close(); if (!/Locked/.test(err.message)) alert(err.message); return; }
  renderSettings();
}
function passwordSection() {
  const on = state.lock?.password_set;
  return `<div class="set-svc" id="pw-section">
    <h3>Settings password <span class="spacer"></span>${on ? `<button type="button" class="ghost" data-pw="lock">🔒 Lock now</button>` : ""}</h3>
    <div class="muted small note">${on
      ? "On. Settings, age limits, birthdates and removing someone ask for it; everything else stays open. Unlocking lasts 15 minutes from the last change."
      : "Off. Set one so kids can use the app but can't change Settings, age limits or birthdates, or remove anyone."}</div>
    <label>${on ? "New password" : "Password"}
      <span class="set-field"><input id="pw-new" type="password" autocomplete="new-password" placeholder="at least 4 characters"></span>
    </label>
    <label>Type it again
      <span class="set-field"><input id="pw-again" type="password" autocomplete="new-password" placeholder="same password">
        <button type="button" class="ghost" data-pw="set">${on ? "Change" : "Set"}</button>
        ${on ? `<button type="button" class="ghost small-btn" data-pw="off">Turn off</button>` : ""}</span>
    </label><div class="set-test" id="pw-msg"></div></div>`;
}
function renderSettings() {
  $("#settings-body").innerHTML = Object.entries(SERVICES).map(([svc, [name, note, link]]) => {
    const fields = settingsList.filter((f) => f.service === svc).map((f) => `
      <label>${esc(f.label)}
        <span class="set-field">
          <input data-key="${f.key}" ${f.secret ? `type="password" autocomplete="new-password" placeholder="${f.hint ? `${esc(f.hint)} (leave empty to keep)` : "not set"}"`
            : `type="text" value="${esc(f.value || "")}" placeholder="${f.key === "CLAUDE_MODEL" ? "claude-opus-5-5" : ""}"`}>
          <span class="src">${SRC_LABEL[f.source]}</span>
          ${f.source === "saved" ? `<button type="button" class="ghost small-btn" data-forget="${f.key}" title="${f.env ? "Use the .env value again" : "Clear it"}">${f.env ? "Use .env" : "Clear"}</button>` : ""}
        </span>
      </label>`).join("");
    return `<div class="set-svc" data-svc="${svc}">
      <h3>${esc(name)} <span class="spacer"></span><button type="button" class="ghost" data-test="${svc}">Test</button></h3>
      <div class="muted small note">${esc(note)} ${link ? `<a href="${link}" target="_blank" rel="noopener">${svc === "plex" ? "Find your token" : "Get a key"}</a>` : ""}</div>
      ${fields}${svc === "streaming" ? `<div class="chips prov-picks"></div>` : ""}<div class="set-test"></div></div>`;
  }).join("") + passwordSection();
  fillProviders();
}
// streaming picker: TMDB's services for the country, tap to add/remove from the comma list
let providerNames = null;
async function fillProviders() {
  const box = $(".prov-picks"), input = $('input[data-key="STREAM_SERVICES"]');
  if (!box || !input || !state.status.tmdb) return;
  try { providerNames ??= await api("/providers"); } catch { return; }
  const paint = () => {
    const on = new Set(input.value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean));
    box.innerHTML = providerNames.map((n) => `<button type="button" class="chip ${on.has(n.toLowerCase()) ? "on" : ""}" data-prov="${esc(n)}">${esc(n)}</button>`).join("");
  };
  paint();
  input.addEventListener("input", paint);
  box.onclick = (e) => {
    const b = e.target.closest("[data-prov]");
    if (!b) return;
    const cur = input.value.split(",").map((x) => x.trim()).filter(Boolean);
    const i = cur.findIndex((x) => x.toLowerCase() === b.dataset.prov.toLowerCase());
    i >= 0 ? cur.splice(i, 1) : cur.push(b.dataset.prov);
    input.value = cur.join(", ");
    paint();
  };
}
function settingsChanges() {
  const body = {};
  $$("#settings-body input[data-key]").forEach((i) => {
    const f = settingsList.find((x) => x.key === i.dataset.key);
    const v = i.value.trim();
    if (f.secret ? v !== "" : v !== (f.value || "")) body[f.key] = v;
  });
  return body;
}
async function saveSettings(body) {
  if (!Object.keys(body).length) return;
  settingsList = await api("/settings", { method: "PUT", body });
  state.status = await api("/status");
  state.suggestionsKey = null;
}
$("#settings-btn").addEventListener("click", openSettings);
$("#settings-close").addEventListener("click", () => $("#settings-dialog").close());
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#settings-error").textContent = "";
  try { await saveSettings(settingsChanges()); $("#settings-dialog").close(); location.reload(); }
  catch (err) { $("#settings-error").textContent = err.message; }
});
$("#settings-body").addEventListener("click", async (e) => {
  const pw = e.target.closest("[data-pw]");
  if (pw) {
    try {
      if (pw.dataset.pw === "lock") { await api("/lock", { method: "POST" }); $("#settings-dialog").close(); return; }
      if (pw.dataset.pw === "off" && !confirm("Turn the settings password off? Anyone could change Settings and age limits again.")) return;
      const v = pw.dataset.pw === "off" ? "" : $("#pw-new").value;
      const bad = (m) => { $("#pw-msg").className = "set-test bad"; $("#pw-msg").textContent = m; };
      if (pw.dataset.pw === "set" && v.length < 4) return bad("Use at least 4 characters");
      if (pw.dataset.pw === "set" && v !== $("#pw-again").value) { $("#pw-again").value = ""; $("#pw-again").focus(); return bad("The two don't match. Type it again."); }
      state.lock = await api("/password", { method: "PUT", body: { password: v } });
      renderSettings();
      $("#pw-msg").className = "set-test ok";
      $("#pw-msg").textContent = v ? "✓ Password saved. Remember it: resetting it means editing data/settings.json on the server." : "Password turned off.";
    } catch (err) { $("#pw-msg").className = "set-test bad"; $("#pw-msg").textContent = err.message; }
    return;
  }
  const forget = e.target.closest("[data-forget]"), test = e.target.closest("[data-test]");
  try {
    if (forget) { await saveSettings({ ...settingsChanges(), [forget.dataset.forget]: null }); renderSettings(); }
    if (test) {
      const svc = test.dataset.test;
      const pending = settingsChanges();        // test what's typed: save it first
      await saveSettings(pending);
      if (Object.keys(pending).length) renderSettings();
      const box = $(`.set-svc[data-svc="${svc}"] .set-test`);
      box.className = "set-test"; box.textContent = "Testing…";
      const r = await api(`/settings/test/${svc}`, { method: "POST" });
      box.className = "set-test " + (r.ok ? "ok" : "bad");
      box.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
    }
  } catch (err) { $("#settings-error").textContent = err.message; }
});
