# Travel with Tots

A day planner for parents travelling in Vancouver with children aged 0 to 5.

Given a child's wake, nap and bedtime, how the family is getting around and
where they are staying, it builds a timed itinerary of real places checked
against real opening hours, then helps rearrange it while the day is underway.

Flask, SQLite or Supabase, and LLM calls routed through OpenRouter.

---

## What it does

**For parents**

- **Plan a day** from a form, or by describing it to the chat assistant
- **Plan a visit** of up to 7 days, with no venue repeating across days
- **Run the day** on a live timeline that shifts when a nap runs long or plans change
- **Find somewhere nearby** by sharing location and saying what is needed
- **Ask questions** through a chat assistant that knows how the site works
- **Save** children, trips and places found along the way
- **Contribute** by logging missing places and reporting amenities

**For admins**

- Review queue for proposed venues, parent submissions and disputed hours
- Editable chatbot knowledge base
- Test pages for every AI component and workflow
- Switch the data source between local SQLite and Supabase

---

## Tech stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.13 |
| Web | Flask 3, Jinja templates, blueprints |
| Database | SQLite (default) or Supabase Postgres |
| LLM | OpenRouter, via `langchain-openai` and LangGraph |
| Retrieval | `all-MiniLM-L6-v2` on ONNX Runtime, vectors in a JSON index |
| Maps | Leaflet with OpenStreetMap tiles |
| Server | gunicorn |
| Tests | stdlib `unittest` |

External APIs: OpenRouter (LLM), Tavily (web search), Google Places (place
lookup), OpenStreetMap Overpass and Nominatim (hours and geocoding), City of
Vancouver Open Data.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env
# then add OPENROUTER_API_KEY to .env

python3 app.py
```

Open <http://localhost:8016>. The schema and demo data are created on first
boot, so there is nothing to migrate.

**Demo account:** `demo@travelwithtots.app` / `demo1234`

No admin is seeded. Sign up through the app, then promote yourself:

```bash
python3 scripts/set_admin.py promote you@example.com
```

The first chat message downloads the embedding model (~80MB) and builds the
search index. That happens once.

---

## Configuration

All settings load from `.env`. No key is ever sent to the browser, and no
credential has a default.

**Required**

| Variable | Used for |
| --- | --- |
| `SECRET_KEY` | Signs the session cookie |
| `OPENROUTER_API_KEY` | Every LLM call |

**Optional**

| Variable | Used for |
| --- | --- |
| `TAVILY_API_KEY` | Web search, and the venue proposal agent |
| `GOOGLE_MAPS_API_KEY` | Searching for a place by name |
| `SUPABASE_URL`, `SUPABASE_API_KEY` | Cloning the database to Supabase (REST) |
| `SUPABASE_DB_URL` | Serving pages from Supabase (SQL) |
| `DB_BACKEND` | Pins the data source to `local` or `supabase`, overriding `/settings` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Seeds the first admin on a database that has none |
| `SESSION_COOKIE_SECURE` | Marks the session cookie HTTPS-only |
| `TRUST_PROXY` | Believe `X-Forwarded-For`. Only set behind a proxy |
| `RATE_LIMITS` | Set to `off` to disable request limits |
| `RAG_AUTOBUILD` | Set to `off` to never build the search index at runtime |

Features whose key is missing report that cleanly; the rest keep working.

**Set `DB_BACKEND=local` for development.** With Supabase configured, the
`/settings` dropdown is otherwise the only thing keeping a local run off the
live project.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

1676 tests across 76 files, about 9 seconds, entirely offline.

`-t .` matters. It makes `tests/__init__.py` load, which pins the database to
local SQLite, disables rate limiting, and blocks requests to OpenRouter over
both `requests` and `httpx2` so a stale mock cannot spend money. Every test file
imports the package so those settings hold however the suite is invoked, and
`tests/test_safety_net.py` enforces that.

To let a test really call a model:

```bash
ALLOW_LIVE_AI=1 python3 -m unittest discover -s tests -t .
```

---

## Project structure

```
travel-with-tots/
├── app.py                     # Flask entry point: creates the app, registers blueprints
├── src/
│   ├── web/                   # one blueprint per subject, all routes
│   │   ├── guards.py          # authentication and rate limiting
│   │   ├── auth.py            # signup, login, logout
│   │   ├── account.py         # dashboard, children
│   │   ├── planning.py        # /plan, saving a trip
│   │   ├── trip.py            # in-trip page, replanning
│   │   ├── places.py          # logged places, amenity reports
│   │   ├── chat.py            # chat widget endpoints
│   │   ├── venues.py          # admin review queue
│   │   ├── settings.py        # data source, knowledge base, prompts
│   │   ├── devpages.py        # /components and /workflows test pages
│   │   ├── lookups.py         # map lookups shared by blueprints
│   │   └── ratelimit.py       # per-caller request buckets
│   ├── store/                 # all persistence, the only SQL
│   │   ├── db.py              # every query
│   │   ├── schema.py          # tables and migrations
│   │   ├── postgres.py        # the same SQL in Supabase's dialect
│   │   ├── supabase_sync.py   # clone up, pull down
│   │   ├── candidates.py      # proposed venues awaiting review
│   │   └── results.py         # thumbs up/down ratings
│   ├── ai/                    # what asks a model, and what reads the answer
│   │   ├── agents.py          # OpenRouter transport, plan/replan adjusters
│   │   ├── tool_agent.py      # LangGraph tool-calling agent
│   │   ├── rag.py             # chunking, embeddings, retrieval
│   │   └── intent.py          # cancel words, routing log
│   ├── clients/               # outbound calls to third parties
│   │   ├── osm.py             # OpenStreetMap hours via Overpass
│   │   ├── nominatim.py       # geocoding
│   │   ├── opendata.py        # City of Vancouver Open Data
│   │   └── webpage.py         # plain text from one page
│   ├── components/            # single-purpose capabilities
│   ├── workflows/             # components chained into use cases
│   ├── prompts/               # system prompts, editable at /settings
│   ├── itinerary.py           # builds the day
│   ├── interactions.py        # replanning and find-nearby logic
│   ├── data_loader.py         # venues as plain dicts, hours for a date
│   ├── form_helpers.py        # form parsing and validation, no Flask
│   ├── geo.py                 # distance and reach per transport mode
│   ├── models.py              # Plan and Trip objects
│   ├── memory.py              # what the app already knows about a parent
│   ├── importers.py           # open data onto venue rows
│   ├── plan_diff.py           # what a replan changed
│   └── dates.py               # date and age helpers
├── templates/                 # Jinja, all extending base.html
├── static/                    # CSS, JS, and vendored Leaflet
├── data/
│   ├── app.db                 # SQLite database
│   ├── venues.json            # curated venue seed
│   ├── venue_candidates.csv   # proposals and review decisions
│   ├── knowledge_base.md      # what the chatbot knows
│   ├── rag_index.json         # chunks and vectors (generated)
│   ├── onnx_models/           # embedding model (generated)
│   └── backups/               # Supabase backups (generated)
├── scripts/                   # CLI jobs
├── tests/                     # stdlib unittest
└── render.yaml                # Render blueprint
```

---

## Architecture

Routes hold no business logic. Each layer only knows the one below it.

```
web/          HTTP: routes, auth, form binding
  ↓
components/   single capabilities, each with its own test page
  ↓
workflows/    components chained into multi-turn use cases
  ↓
ai/           the tool-calling agent and the adjusters
  ↓
itinerary.py  the rule-based day builder
  ↓
store/        the only SQL
```

### How a day is planned

Four steps, one of which is AI.

| Step | Where |
| --- | --- |
| 1. Collect the day's shape from the form | `src/web/planning.py` |
| 2. Lay out times, anchor naps, pick venues | `src/itinerary.py` |
| 3. Check every stop against that venue's hours | `src/components/validate_hours.py` |
| 4. Smooth pacing and wording | `src/ai/agents.py` |

The rule-based draft is always valid on its own. If the AI step fails, the
draft ships unchanged.

A visit is `trips` rows sharing a `trip_group_id`, ordered by `day_index`.
Days are planned in sequence and each is told what earlier days used, so no
venue repeats.

### Components

Single-purpose, each with an admin page that runs it in isolation.

| Component | Kind | What it does |
| --- | --- | --- |
| `plan_trip` | AI-backed | A rule-based draft, then smoothing |
| `replan_trip` | AI-backed | The same, for a mid-trip change |
| `extract_form` | AI-backed | A sentence into the planning form |
| `validate_hours` | Deterministic | Every stop against that venue's hours |
| `find_nearby` | Deterministic | Ranks real places by distance, then need |
| `search_web` | API-backed | Tavily, when the database has nothing |
| `place_search` | API-backed | Google Places, by name |
| `geocode` | API-backed | An address into coordinates |

### Workflows

Five use cases chained from components: replan on the go, log a place, fill the
form from a chat message, find a nearby place, and propose new venues. Each is
listed at `/workflows`, and those with a test page run the real chain.

### The agent

A LangGraph tool-calling loop behind the chat bubble, with 5 tools: answering
from the knowledge base, plus one per message-triggered workflow, generated from
the same registry `/workflows` renders. There is no separate intent classifier;
tool selection is that decision.

Models are a per-request choice from the chat widget. The allowed set is
deliberately short, since `/chatbot` is public:

| Model | |
| --- | --- |
| `nvidia/nemotron-3-super-120b-a12b:free` | default |
| `openai/gpt-4o-mini` | paid |

### Retrieval

`data/knowledge_base.md` is chunked, embedded with `all-MiniLM-L6-v2` on ONNX
Runtime, and stored as vectors in `data/rag_index.json`. The index rebuilds when
the knowledge base changes. `chromadb` is a dependency only because it ships the
ONNX embedder.

### Data storage

SQLite at `data/app.db` by default, or Supabase Postgres. `src/store/postgres.py`
translates SQLite's dialect on the way through, so nothing above `store/` knows
which database it is talking to.

Tables: `parents`, `children`, `trips`, `venues`, `venue_reports`,
`venue_hours`, `venue_hours_checks`. Ratings, candidates and routing logs are
flat files in `data/`.

Switching to Supabase takes three steps on `/settings`: run the generated
`CREATE TABLE` statements in Supabase's SQL editor, clone the rows, then run a
second block adding sequences, indexes and foreign keys. The clone is one way
and skips rows already present. `scripts/pull_from_supabase.py` is the reverse,
writing a timestamped SQLite copy into `data/backups/`.

---

## Where venues come from

| Source | Review needed |
| --- | --- |
| City of Vancouver Open Data (parks, community centres, washrooms) | No, authoritative |
| Curated seed in `data/venues.json` | Yes |
| Places logged by parents | Yes, before appearing in plans |
| Amenity reports from parents | No, applied directly |
| Proposal agent (web search, grounded against OSM and Nominatim) | Yes, always |

The proposal agent never adds a venue itself. Approvals happen at
`/venues/review`, grouped by the decision being asked: **Decide** (is this a
venue?), **Finish** (in the database, hours missing or disputed), **Confirm**
(in use, unchecked) and **Set aside** (an archive of rejections, which are
remembered so nothing is re-proposed).

The database holds attractions, not restaurants. Lunch is either a stop that
serves food or a free block with a find-nearby handoff.

Amenities such as nursing rooms and step-free access are stored as individual
reports carrying who said it and when, so "nobody has told us" stays distinct
from "someone looked, and there wasn't one".

---

## Scripts

Jobs too slow or too rude for a web request.

| Script | Purpose |
| --- | --- |
| `set_admin.py` | List, promote, revoke and audit admin accounts |
| `import_open_data.py` | Import City of Vancouver open data. Dry run by default |
| `propose_venues.py` | Run a batch of venue proposals |
| `verify_hours.py` | Compare stored hours against OpenStreetMap |
| `geocode_venues.py` | Fill in coordinates on the curated seed |
| `replay_candidates.py` | Put approved venues back after a rebuild |
| `pull_from_supabase.py` | Back up Supabase into a local SQLite file |

`set_admin.py list` also reports any admin whose password is guessable, worth
running against a cloned database since a clone copies accounts.

---

## Deployment

`render.yaml` is a Render blueprint: point Render at the repo and create the
service from it.

```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
```

- **One worker**, because memory is the binding constraint
- **Threads**, because every slow path waits on a third-party API
- **`--timeout 120`**, because generating a plan outlasts gunicorn's 30s default

**Supabase is required, not optional.** Render's disk is ephemeral, so
`data/app.db` and `data/data_source.json` are wiped on every deploy.
`render.yaml` sets `DB_BACKEND=supabase` to pin the backend regardless.

Set in the Render dashboard (marked `sync: false` so they stay out of git):
`OPENROUTER_API_KEY`, `SUPABASE_URL`, `SUPABASE_API_KEY`, `SUPABASE_DB_URL`,
and optionally `TAVILY_API_KEY` and `GOOGLE_MAPS_API_KEY`. Render generates
`SECRET_KEY`.

**Memory** (measured on macOS; Linux is usually lighter):

| | Resident |
| --- | --- |
| Serving pages, index prebuilt | ~190 MB |
| After a chatbot question reaches retrieval | ~470 MB |
| Rebuilding the index at runtime | ~580 MB |

Render's Free and Starter tiers are both 512 MB, so normal use fits and a
rebuild does not. `render.yaml` builds the index during the build step so a cold
start never rebuilds. Editing the knowledge base on `/settings` does rebuild, so
on 512 MB expect that request to be killed. Edit locally and redeploy, or use a
2 GB instance.

---

## Conventions

- Anything a parent can pick is a closed list, checked on the way in as well as on the way out
- Failures are loud. No silent fallbacks that hide a broken step
- API keys stay on the server. Google Places is proxied
- Third-party JavaScript is vendored into `static/vendor/`, never loaded from a CDN
- SQL exists only in `src/store/`
