# Travel with Tots

A Flask web app that plans a **nap-friendly day out in Vancouver** for parents
of children aged 0 to 5.

A parent describes their day (wake-up, bedtime, naps, how they get around,
where they are staying). The app returns a timed list of real places, checked
against opening hours, and then helps them adjust it while the day is actually
happening.

---

## What it does

- **Plans a day** (`/plan`). A rule-based draft, smoothed by an LLM, from a
  curated venue database.
- **Runs the day** (`/trip`). A live timeline that re-plans when a nap runs
  long, it starts raining, or a stop overruns.
- **Answers questions.** A chat bubble on every page, backed by a
  tool-calling agent with retrieval over the site's own knowledge base.
- **Finds somewhere nearby.** Share a location, name a need (nursing room,
  lunch, quiet spot), get ranked venues with map links.
- **Saves accounts.** Children, past trips, and places a parent logged.
- **Grows its own data.** An agent proposes new venues; a human approves them.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env
# then edit .env and add your API keys

python app.py
```

Open **http://localhost:8016**. The database, schema and demo data are created
on first boot.

**Seeded accounts:**

| Role   | Email                      | Password    |
| ------ | -------------------------- | ----------- |
| Parent | `demo@travelwithtots.app`  | `demo1234`  |
| Admin  | `admin@travelwithtots.app` | `admin1234` |

The first chatbot message downloads an embedding model (~90MB) and builds the
vector index. That takes a few seconds, once.

### Configuration

All keys load from `.env` via `os.environ`. None is ever sent to the browser.

| Variable              | Required | Used for                                  |
| --------------------- | -------- | ----------------------------------------- |
| `SECRET_KEY`          | yes      | Signs the session cookie. No default.     |
| `OPENROUTER_API_KEY`  | yes      | Every LLM call (chatbot, agent, planner). |
| `TAVILY_API_KEY`      | no       | Web search fallback, venue proposals.     |
| `GOOGLE_MAPS_API_KEY` | no       | Place search and geocoding.               |

Without the optional keys those features fail cleanly and the rest of the app
keeps working.

### Tests

```bash
python3 -m unittest discover -s tests
```

About 1100 tests across 56 files. No network calls: every external service is
stubbed.

---

## Stack

| Layer     | Choice                                                       |
| --------- | ------------------------------------------------------------ |
| Backend   | Flask, SQLite (no ORM, `src/db.py` is the only module with SQL) |
| Auth      | Session cookies, Werkzeug password hashing                   |
| LLM       | OpenRouter, so the model is swappable per request            |
| Agent     | LangGraph tool-calling loop                                  |
| Retrieval | `sentence-transformers` (all-MiniLM-L6-v2) + ChromaDB        |
| Frontend  | Jinja templates, vanilla JS, Leaflet maps (vendored, no CDN) |
| Data      | Vancouver Open Data, OpenStreetMap, Nominatim, Google Places, Tavily |

---

## How a day gets planned

1. **The form** collects wake-up, bedtime, naps, transport mode, stop count,
   lunch preference, interests, and an accommodation pin.
2. **The rule-based planner** (`src/itinerary.py`) lays out stop times, anchors
   naps, and picks a venue per slot.
3. **The hours check** (`src/components/validate_hours.py`) tests every stop
   against that venue's hours for the trip date, then swaps or frees the slot.
4. **The AI adjuster** smooths pacing and wording. If it fails, the rule-based
   draft is shown instead, and the parent is not told either way.

### The rules that shape a day

**Preferences sort, they never filter.** Interests, nap-friendliness, shelter
and proximity all reorder the candidate pool. Nothing is excluded. A filter can
empty a day; a sort cannot.

**The nap is a soft constraint.** The planner structures the day around the nap
the parent expects. It does not predict where a child will actually sleep, which
is what in-trip replanning is for.

**Transport mode sets a reach, not a route.** How far apart two consecutive
stops may sit:

| Mode                    | Reach  |
| ----------------------- | ------ |
| `walk`                  | 1.5 km |
| `transit`               | 5 km   |
| `car` (incl. taxi/ride-share) | 8 km |

This is one question: how you get *between* stops. Every family is assumed to
have a stroller *at* a stop. There is deliberately **no travel-time model**;
routing needs schedules and transfers, so the app reports distances and lets the
parent judge.

**The accommodation anchors both ends.** Pin it on the map and the first stop is
chosen from where you wake up, the last from where you have to get back to.
Optional: without a pin the day plans the same way, unanchored.

**Unknown hours mean not schedulable.** A venue whose hours we do not know for
the trip date is never scheduled and never offered as a candidate. On statutory
holidays, only places with no door (parks, beaches, the seawall) keep their
usual hours.

### Running the day (`/trip`)

A timeline with a current-time marker. Six situation buttons re-plan the rest of
the day:

`Nap happened here` · `Need to stay here longer` · `Skip next stop` ·
`Finished this stop early` · `It's raining` · `Do something else`

Each runs the same draft-then-smooth pattern as planning. Parents can also
report amenities they found at a stop.

---

## Venue data

268 venues, from three sources with different trust rules.

| Source                | Rows | How it gets in                          | Needs review? |
| --------------------- | ---: | --------------------------------------- | ------------- |
| `municipal_open_data` |  238 | Imported from the City of Vancouver     | No            |
| `curated`             |   28 | Hand-typed seed (`data/venues.json`)    | Yes           |
| `user_submitted`      |    2 | A parent logged it                      | Yes, before use |

**Trust has two routes: provenance or inspection.** The City is authoritative
about its own parks, so those rows are trusted for where they came from and are
never queued for review. Everything else earns trust by being looked at, which
is what `verified_at` records.

That claim is scoped to what the City publishes: name, location, existence. It
does not cover opening hours, which the City does not publish for parks.

**Agent-proposed venues** follow their own flow. `src/workflows/propose_venues.py`
searches the web, grounds each candidate against Nominatim and OpenStreetMap,
and writes to `data/venue_candidates.csv`. It never writes a venue. A human
approves them at `/venues/review`, which stamps `verified_at`. Rejections are
remembered so the same place is never proposed twice.

**No restaurants.** The table holds attractions. Lunch happens at a stop that
serves food, or it is a free block with a find-nearby handoff. Google has live
hours and reviews and we cannot.

### A venue's fields

| Field           | Meaning                                                    |
| --------------- | ---------------------------------------------------------- |
| `type`          | What the place is, for a human to read. 14 values.          |
| `setting`       | Where the visit is spent: `indoor`, `outdoor`, `both`.      |
| `neighbourhood` | A grouping a parent recognises ("Stanley Park"), stored not derived. |
| `open_time` / `close_time` | One pair per venue. Required before approval.    |
| `hours_note`    | Free text for what a pair cannot hold ("closed Mondays").   |
| `lat` / `lng`   | Used for proximity sorting and distances.                   |
| `can_eat`       | Food on site, so lunch needs no extra travel leg.           |

**Amenities are reports, not columns.** Washrooms, nursing rooms, high chairs
and step-free access live in `venue_reports`, one row per claim with an author
and a date. Newest wins. This keeps "nobody has said" different from "somebody
looked and there was none". Read them with `.get()`; an unreported field is
absent.

---

## AI features

Every LLM call goes through OpenRouter, so the model is a per-request choice
made in the chat widget's dropdown.

### Chatbot (RAG)

`data/knowledge_base.md` is chunked (128 words), embedded with all-MiniLM-L6-v2,
and stored in ChromaDB. The index rebuilds when the knowledge base changes.
Admins edit the source at `/settings` and inspect chunking at `/chunks`.

### Agent

`src/agent.py` routes a chat message either to a workflow or to a LangGraph
tool-calling agent with four tools:

- `answer_faq_tool`, the knowledge base
- `plan_trip_tool`, build a day
- `extract_form_tool`, read a described day into the planning form
- `find_nearby_tool`, somewhere nearby for a need

`src/intent.py` classifies a message to a workflow name or `none`, logging every
decision to `data/intents.jsonl`.

### Components and workflows

**Components** are single-purpose units, each with its own admin test page,
listed at `/components`.

| Component        | Kind          | Job                                        |
| ---------------- | ------------- | ------------------------------------------ |
| `plan_trip`      | AI-backed     | Rule-based draft, then smoothing           |
| `replan_trip`    | AI-backed     | The same, for a mid-trip situation         |
| `extract_form`   | AI-backed     | A sentence into the planning form          |
| `validate_hours` | deterministic | Every stop against that venue's hours      |
| `find_nearby`    | deterministic | Rank venues by real distance, then need    |
| `search_web`     | API-backed    | Tavily, when the venue table has nothing   |
| `place_search`   | API-backed    | Google Places, "which place did you mean"  |
| `geocode`        | API-backed    | An address into coordinates                |

**Workflows** chain components into an end-to-end use case, listed at
`/workflows`, each with a test page that runs the real thing.

| Workflow            | Trigger   | What it does                              |
| ------------------- | --------- | ----------------------------------------- |
| Plan from chat      | message   | Fills the planning form over a few turns  |
| Replan on the go    | message   | Shifts the rest of the day                |
| Find a nearby place | message   | Answers "somewhere nearby" with map links |
| Log a place         | message   | Adds a place the app does not have        |
| Propose new venues  | scheduled | A batch of candidates for review          |

### Memory

`src/memory.py` recalls what the app already knows about a parent (a child's
age, the routine from their last saved trip) so the chat need not ask again.
Read-only, recomputed per call, never a new source of truth. The chat shows what
it remembered and lets the parent reject it.

---

## Admin tools

All behind `@admin_required`.

| Page             | Purpose                                          |
| ---------------- | ------------------------------------------------ |
| `/components`    | Inventory of data sources, agent and components  |
| `/workflows`     | The end-to-end use cases, each runnable          |
| `/venues/review` | Approve proposals, confirm venues, fill hours    |
| `/propose-venues`| Run a proposal batch                             |
| `/settings`      | Edit the knowledge base and system prompts       |
| `/chunks`        | Inspect and re-run chunking                      |
| `/results`       | Thumbs up/down ratings and stats                 |
| `/agent`         | Watch the chat bubble's real traffic             |

---

## Project structure

```
travel-with-tots/
├── app.py                     # Flask entry point: routes, auth, form handling
├── src/
│   ├── db.py                  # the only module with SQL: schema, writes, reports
│   ├── data_loader.py         # venues as plain dicts, hours for a date, constants
│   ├── models.py              # Plan and Trip domain objects
│   ├── itinerary.py           # generate_plans: the rule-based day
│   ├── interactions.py        # replan() + find_nearby() in-trip logic
│   ├── form_helpers.py        # form parsing and validation, no Flask
│   ├── geo.py                 # distance, reach per transport mode, bounds guard
│   ├── dates.py               # date and age utilities
│   ├── memory.py              # recall(): what the app already knows
│   ├── agents.py              # chatbot + plan/replan adjusters over OpenRouter
│   ├── agent.py               # LangGraph tool-calling agent
│   ├── intent.py              # message to workflow name, or none
│   ├── rag.py                 # chunking, embeddings, retrieval
│   ├── results.py             # thumbs up/down ratings by kind
│   ├── candidates.py          # the venue candidate store
│   ├── opendata.py            # Vancouver Open Data client
│   ├── importers.py           # open-data records onto venue rows
│   ├── osm.py                 # OpenStreetMap hours + websites, via Overpass
│   ├── nominatim.py           # keyless geocoding for the proposal path
│   ├── components/            # one file per component (see table above)
│   ├── workflows/             # one file per workflow
│   └── prompts/               # system prompts, editable from /settings
├── templates/                 # Jinja, all extending base.html
├── static/                    # CSS, JS, vendor/ (Leaflet, vendored not CDN)
├── data/
│   ├── app.db                 # SQLite, the runtime source of truth
│   ├── venues.json            # curated seed, copied in on every boot
│   ├── venue_candidates.csv   # agent proposals and human decisions
│   ├── knowledge_base.md      # chatbot facts, edited directly
│   └── chroma/                # vector index (generated, gitignored)
├── scripts/                   # CLI jobs, see below
└── tests/                     # stdlib unittest
```

### Scripts

| Script                 | Job                                             |
| ---------------------- | ----------------------------------------------- |
| `import_open_data.py`  | Import City parks and centres. Dry run by default. |
| `propose_venues.py`    | Run a proposal batch too slow for a browser     |
| `verify_hours.py`      | Flag hours that disagree with OpenStreetMap     |
| `geocode_venues.py`    | Fill missing venue coordinates                  |
| `replay_candidates.py` | Restore approved venues after a rebuild         |

---

## Data model

SQLite at `data/app.db`, created by `src/db.py`. Foreign keys are enforced per
connection; every write is parameterized and transactional.

| Table                | Holds                                                      |
| -------------------- | ---------------------------------------------------------- |
| `parents`            | Accounts: email, password hash, `is_admin`                 |
| `children`           | Name and date of birth. Age is computed, never stored.     |
| `trips`              | A saved day: the form's answers plus `plan_json`           |
| `venues`             | The venue table, with provenance and verification columns  |
| `venue_reports`      | One amenity claim, with its author and date                |
| `venue_hours_checks` | Disagreements with OpenStreetMap, awaiting a decision      |

Ratings live in `data/results.json`, candidates in `data/venue_candidates.csv`,
and intent decisions in `data/intents.jsonl`.

---

## Routes

| Public                 | Logged in            | Admin                                |
| ---------------------- | -------------------- | ------------------------------------ |
| `/` landing            | `/dashboard`         | `/components`, `/workflows`          |
| `/plan` build a day    | `/trip/<id>` saved   | `/venues/review`, `/propose-venues`  |
| `/trip` run a day      | `/log-place`         | `/settings`, `/chunks`, `/results`   |
| `/login`, `/signup`    | `/save-trip`         | Component and workflow test pages    |
| `/chatbot`, `/feedback`| child and place CRUD | `/agent`                             |

---

## Conventions

- **`src/db.py` owns all SQL.** Everything else takes plain dicts.
- **Fail clearly.** No silent fallbacks that hide a broken step.
- **Enums are enforced where a value enters**, not only where it is offered, so
  a stale page or hand-made post cannot introduce one.
- **Keys stay server-side.** Maps are Leaflet with OpenStreetMap tiles; Google
  Places is proxied through the server.
- **Third-party JS is vendored**, never loaded from a CDN.
