"""
sportmonks_season_app.py
=========================

Pulls HISTORICAL match stats for EVERY fixture in a chosen league season
(e.g. all ~90 games in a 10-team double round-robin) - not head-to-head
between two teams, not live match data.

VERIFIED AGAINST SPORTMONKS' OWN DOCS (docs.sportmonks.com) - endpoint
paths, type_ids, and response shapes below are copied from their current
documentation, not guessed from memory:

  1. Resolve league name -> league_id, with its seasons attached:
        GET /v3/football/leagues/search/{name}?include=seasons

  2. Get every fixture in a season in one call (cheap - no per-fixture
     stats yet), nested by stage -> round -> fixture:
        GET /v3/football/schedules/seasons/{season_id}
     This endpoint returns participants and scores for free, but NOT
     statistics (confirmed against SportMonks' own docs and glossary -
     "if you want more fixture details than the schedule response
     provides, such as advanced stats, use the fixture ID from the
     schedule response to request specific data from the fixtures
     endpoint"). So statistics still need one call per fixture:

  3. Per fixture, pull just the 5 stat types we need:
        GET /v3/football/fixtures/{id}
        ?include=participants;scores;statistics
        &filters=fixtureStatisticTypes:86,1605,83,580,581

  - Team names + home/away come from `participants[].meta.location`
  - Goals come from `scores[]` where description == "CURRENT"
  - Only fixtures with a CURRENT score for both sides are pulled -
    that's what excludes not-yet-played fixtures from a season in
    progress (this is what makes the output "historical", not live).
  - Season label is whatever season you picked in step 1/2 - every row
    in one run belongs to the same season by construction.

TWO HONEST LIMITATIONS (checked against SportMonks' full statistics-type
reference - these aren't oversights, the data just isn't there):
  1. "Box touches" (touches inside the penalty area) does not exist as a
     SportMonks statistic at team OR player level. NOT included.
  2. "Big chances scored" is not a SportMonks field - only
     `big_chances_created` and `big_chances_missed` exist. Computed here
     as created - missed and clearly labeled as derived.

RATE LIMITS: pulling a full season means one API call per fixture for
stats (roughly 90 calls for a 10-team double round-robin). This app
paces requests and shows live progress rather than firing everything at
once - adjust the requests-per-minute setting in the sidebar to match
your SportMonks plan.

RUN:
    streamlit run sportmonks_season_app.py

API KEY: put SPORTMONKS_API_KEY in a .env file, in Streamlit secrets, or
paste it into the sidebar for a quick local try-out.
"""

import csv
import io
import os
import time

import requests
import streamlit as st

st.set_page_config(page_title="SportMonks Season Puller", page_icon="⚽", layout="wide")

BASE_URL = "https://api.sportmonks.com/v3/football"

# Verified type_ids (see docstring above for sources)
TYPE_SHOTS_ON_TARGET = 86
TYPE_SUCCESSFUL_DRIBBLES_PCT = 1605
TYPE_RED_CARDS = 83
TYPE_BIG_CHANCES_CREATED = 580
TYPE_BIG_CHANCES_MISSED = 581
STAT_TYPE_IDS = [
    TYPE_SHOTS_ON_TARGET,
    TYPE_SUCCESSFUL_DRIBBLES_PCT,
    TYPE_RED_CARDS,
    TYPE_BIG_CHANCES_CREATED,
    TYPE_BIG_CHANCES_MISSED,
]

CSV_COLUMNS = [
    "fixture_id",
    "season",
    "date",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "home_shots_on_target",
    "away_shots_on_target",
    "home_successful_dribbles_pct",
    "away_successful_dribbles_pct",
    "home_red_cards",
    "away_red_cards",
    "home_big_chances_created",
    "away_big_chances_created",
    "home_big_chances_scored",  # derived: created - missed (see docstring)
    "away_big_chances_scored",
]


def get_api_key():
    try:
        if "SPORTMONKS_API_KEY" in st.secrets:
            return st.secrets["SPORTMONKS_API_KEY"]
    except Exception:
        pass
    return os.environ.get("SPORTMONKS_API_KEY")


def search_leagues(api_key, name):
    """GET /leagues/search/{name} with seasons attached, so we can offer
    a season picker right after choosing the league."""
    url = f"{BASE_URL}/leagues/search/{name}"
    resp = requests.get(url, params={"api_token": api_key, "include": "seasons"}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_season_schedule(api_key, season_id):
    """GET /schedules/seasons/{id} - the full season structure
    (stages -> rounds -> fixtures), with participants/scores already
    attached for free but NOT statistics. Not paginated (per SportMonks'
    docs), so this is always a single call."""
    url = f"{BASE_URL}/schedules/seasons/{season_id}"
    resp = requests.get(url, params={"api_token": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def flatten_fixtures(schedule_stages):
    """The schedule response nests fixtures inside stages -> rounds.
    Flatten that into a plain list of fixture dicts."""
    fixtures = []
    for stage in schedule_stages:
        for round_ in stage.get("rounds", []):
            fixtures.extend(round_.get("fixtures", []))
    return fixtures


def get_fixture_stats(api_key, fixture_id):
    """GET /fixtures/{id} with participants, scores, and only the 5
    statistic types we need."""
    url = f"{BASE_URL}/fixtures/{fixture_id}"
    params = {
        "api_token": api_key,
        "include": "participants;scores;statistics",
        "filters": f"fixtureStatisticTypes:{','.join(str(t) for t in STAT_TYPE_IDS)}",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", {})


def extract_team_names(fixture):
    """Returns (home_name, away_name) using participants[].meta.location -
    never assume array order, per SportMonks' own guidance."""
    home_name = away_name = None
    for p in fixture.get("participants", []):
        location = (p.get("meta") or {}).get("location")
        if location == "home":
            home_name = p.get("name")
        elif location == "away":
            away_name = p.get("name")
    return home_name, away_name


def extract_goals(fixture):
    """Returns (home_goals, away_goals) from scores[] where
    description == 'CURRENT'. Returns (None, None) if there's no CURRENT
    entry yet - i.e. the match hasn't been played."""
    home_goals = away_goals = None
    for s in fixture.get("scores", []):
        if s.get("description") != "CURRENT":
            continue
        score = s.get("score", {})
        if score.get("participant") == "home":
            home_goals = score.get("goals")
        elif score.get("participant") == "away":
            away_goals = score.get("goals")
    return home_goals, away_goals


def extract_stat(fixture, type_id, location):
    for stat in fixture.get("statistics", []):
        if stat.get("type_id") == type_id and stat.get("location") == location:
            return (stat.get("data") or {}).get("value")
    return None


def is_finished(fixture):
    home_goals, away_goals = extract_goals(fixture)
    return home_goals is not None and away_goals is not None


def build_row(fixture, season_name):
    home_name, away_name = extract_team_names(fixture)
    home_goals, away_goals = extract_goals(fixture)

    home_bcc = extract_stat(fixture, TYPE_BIG_CHANCES_CREATED, "home")
    away_bcc = extract_stat(fixture, TYPE_BIG_CHANCES_CREATED, "away")
    home_bcm = extract_stat(fixture, TYPE_BIG_CHANCES_MISSED, "home")
    away_bcm = extract_stat(fixture, TYPE_BIG_CHANCES_MISSED, "away")

    def derive_scored(created, missed):
        if created is None or missed is None:
            return None
        return created - missed

    return {
        "fixture_id": fixture.get("id"),
        "season": season_name,
        "date": (fixture.get("starting_at") or "").split(" ")[0] or fixture.get("starting_at"),
        "home_team": home_name,
        "away_team": away_name,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "home_shots_on_target": extract_stat(fixture, TYPE_SHOTS_ON_TARGET, "home"),
        "away_shots_on_target": extract_stat(fixture, TYPE_SHOTS_ON_TARGET, "away"),
        "home_successful_dribbles_pct": extract_stat(fixture, TYPE_SUCCESSFUL_DRIBBLES_PCT, "home"),
        "away_successful_dribbles_pct": extract_stat(fixture, TYPE_SUCCESSFUL_DRIBBLES_PCT, "away"),
        "home_red_cards": extract_stat(fixture, TYPE_RED_CARDS, "home"),
        "away_red_cards": extract_stat(fixture, TYPE_RED_CARDS, "away"),
        "home_big_chances_created": home_bcc,
        "away_big_chances_created": away_bcc,
        "home_big_chances_scored": derive_scored(home_bcc, home_bcm),
        "away_big_chances_scored": derive_scored(away_bcc, away_bcm),
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("⚽ SportMonks Season Puller")
st.caption(
    "Pulls HISTORICAL stats for every finished fixture in a chosen league "
    "season - not head-to-head between two teams, not live match data."
)

st.info(
    "Two things SportMonks doesn't provide, verified against their "
    "documentation rather than assumed: **box touches** isn't a field at "
    "all (only a generic player 'touches' count, no penalty-box "
    "breakdown) so it's left out entirely; **big chances scored** isn't a "
    "raw field either - it's computed here as `big_chances_created - "
    "big_chances_missed` and is clearly labeled as derived, not a value "
    "SportMonks reports directly."
)

configured_key = get_api_key()
with st.sidebar:
    st.header("API key")
    if configured_key:
        st.success("API key loaded from secrets/environment.")
        api_key = configured_key
    else:
        st.warning("No SPORTMONKS_API_KEY found in secrets or environment.")
        api_key = st.text_input("Paste your SportMonks API token", type="password")
        st.caption("Kept only for this browser session.")

    st.header("Rate limit")
    requests_per_minute = st.number_input(
        "Requests per minute", min_value=10, max_value=1000, value=120, step=10,
        help="Pulling a full season makes one request per fixture for stats. "
             "Match this to your SportMonks plan's hourly call limit.",
    )
    sleep_between = 60.0 / requests_per_minute

if "league_matches" not in st.session_state:
    st.session_state.league_matches = []
if "season_rows" not in st.session_state:
    st.session_state.season_rows = []

st.subheader("1. Find the league")
league_name = st.text_input("League name", placeholder="Premier League")
if st.button("Search league", disabled=not api_key or not league_name):
    try:
        st.session_state.league_matches = search_leagues(api_key, league_name)
        if not st.session_state.league_matches:
            st.warning(f"No league found matching '{league_name}'.")
    except requests.HTTPError as exc:
        st.error(f"SportMonks returned an error: {exc}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Request failed: {exc}")

season_id = season_name = None
if st.session_state.league_matches:
    st.subheader("2. Confirm league and pick a season")
    league_options = {f"{lg['name']} (id {lg['id']})": lg for lg in st.session_state.league_matches}
    chosen_league_label = st.selectbox("League", list(league_options.keys()))
    chosen_league = league_options[chosen_league_label]

    seasons = chosen_league.get("seasons", [])
    if not seasons:
        st.warning("This league has no seasons available on your plan.")
    else:
        season_options = {s["name"]: s for s in seasons}
        default_index = next(
            (i for i, s in enumerate(seasons) if s.get("is_current")), 0
        )
        chosen_season_label = st.selectbox(
            "Season", list(season_options.keys()), index=default_index
        )
        chosen_season = season_options[chosen_season_label]
        season_id = chosen_season["id"]
        season_name = chosen_season["name"]

        st.subheader("3. Pull every fixture's stats for this season")
        if st.button("Get season fixtures & stats"):
            try:
                with st.spinner("Fetching the season schedule..."):
                    schedule = get_season_schedule(api_key, season_id)
                    all_fixtures = flatten_fixtures(schedule)
                    finished_fixtures = [f for f in all_fixtures if is_finished(f)]

                total = len(finished_fixtures)
                not_played = len(all_fixtures) - total
                st.write(
                    f"{len(all_fixtures)} fixture(s) in this season - "
                    f"{total} finished, {not_played} not yet played (skipped)."
                )

                progress = st.progress(0.0)
                log_box = st.empty()
                log_lines = []
                rows = []

                for i, fixture in enumerate(finished_fixtures, start=1):
                    try:
                        detail = get_fixture_stats(api_key, fixture["id"])
                        rows.append(build_row(detail, season_name))
                    except Exception as exc:  # noqa: BLE001
                        log_lines.append(f"  ERROR on fixture {fixture.get('id')}: {exc}")
                    if i % 5 == 0 or i == total:
                        log_lines.append(f"  ...{i}/{total} fixtures pulled")
                        log_box.code("\n".join(log_lines[-20:]))
                    progress.progress(i / total if total else 1.0)
                    time.sleep(sleep_between)

                st.session_state.season_rows = rows
                st.success(f"Done - pulled stats for {len(rows)} finished fixture(s).")
            except requests.HTTPError as exc:
                st.error(f"SportMonks returned an error: {exc}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Request failed: {exc}")

st.subheader("4. Results")
if not st.session_state.season_rows:
    st.caption("No fixtures pulled yet.")
else:
    st.dataframe(st.session_state.season_rows, use_container_width=True, hide_index=True)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(st.session_state.season_rows)

    st.download_button(
        "⬇️ Download CSV",
        data=buffer.getvalue().encode("utf-8"),
        file_name="sportmonks_season.csv",
        mime="text/csv",
    )

    if st.button("Clear results"):
        st.session_state.season_rows = []
        st.rerun()
