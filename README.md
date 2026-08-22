# Travel with Tots

A web app that builds a **nap-friendly, single-day itinerary** for parents
travelling with young children (ages 0-5), backed by parent accounts and an
AI chatbot that answers questions about how the site works.

A parent enters their day's shape (wake-up/bedtime, naps, destination,
transport, pace, dining, features) and the app arranges a timed list of
suitable stops. Two engines can build that itinerary: a fast rule-based
planner, and an on-demand AI planner grounded in the same venue data, shown
side by side for comparison.

## Features

- **Two-page planning flow**: build a day plan (`/plan`), then live-execute
  it (`/trip`) with re-planning as the day changes.
- **Rule-based and AI-generated plans**, compared side by side.
- **Parent accounts**: save children's profiles, past trips, and places.
- **Admin tools**: edit the chatbot's knowledge base/prompts, inspect
  chunking, review AI ratings and stats.
- **Find a place nearby**: share your location and get kid-friendly venues
  matching an immediate need, from the curated venues or a live web search.
- **Site-help chatbot** everywhere, grounded with retrieval-augmented
  generation (RAG) over the site's own knowledge base.
- **Thumbs up/down feedback**, with aggregate stats, on both chatbot replies
  and AI-generated plans.

## Quick start

```bash
# 1. (optional) create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. add your OpenRouter API key
cp .env.example .env
# then edit .env and set OPENROUTER_API_KEY

# 4. run the app
python app.py
```

Open **http://localhost:8016**. Two accounts are seeded automatically:

| Role   | Email                        | Password    |
| ------ | ----------------------------- | ----------- |
| Parent | `demo@travelwithtots.app`     | `demo1234`  |
| Admin  | `admin@travelwithtots.app`    | `admin1234` |

The first chatbot use downloads the embedding model (~90MB) and builds the
chunk index, which can take a few seconds.

## Planning flow

### Page 1 - Planning (`/plan`)

- Collects trip details through a mobile-friendly form: wake-up/bedtime,
  naps (start time + duration, up to 4), destination, transport, pace,
  dining preference and preferred lunch time, desired features, and
  free-text notes (accommodation, sleep habits, etc).
- Picking **1-3 themes** (Outdoorsy / Rainy-day / Culture), or none for a
  "Mixed" day, generates a **rule-based candidate plan** as a card with a
  stop preview.
- **"✨ Try AI-assisted day"** builds an AI-generated alternative for the same
  theme(s) on demand, grounded only in real venues and shaped by the whole
  trip form, aiming for a realistically paced day rather than one that just
  matches input times literally. It appears as a second, clearly labeled
  card for easy comparison.
- Either card's **"Start this day"** carries that plan to the in-trip page;
  **"💾 Save this plan"** (logged in, child picked) saves it without starting.
- Every AI-generated plan (and every chatbot reply) gets a 👍/👎 rating,
  reviewable with stats from `/results`.

### Page 2 - In-trip (`/trip`)

Renders the chosen `Trip`, top to bottom:

1. Header: destination, transit mode, adjustable **current time**.
2. **Live timeline**: current stop marked *now*, past stops marked *done*.
3. **"Something came up?"** situation buttons.
4. **"Need something now?"** find-nearby panel.
5. **Version switcher** between the original plan and any re-planned versions.
6. **"Save this plan"** (fresh trip, child picked): saves whichever version
   is on screen.

Each stop shows its time, name, type, neighbourhood, feature badges, and an
**Open in Google Maps** link. Transit is displayed only; no routes are computed.

**In-trip interactions:**

- Situation buttons (`Nap happened here`, `Running behind`, `Skip next stop`,
  `Finished this stop early`) call `replan(plan, situation, current_time)`,
  which keeps current/past stops fixed and re-decides the rest of the day.
  The result is a **new** version on the `Trip`; the original is never
  overwritten.
- **"Need something now?"** (kid-friendly restaurant, family room, changing
  table, nursing room, quiet spot, other) calls `find_nearby(need)`, which
  returns 1-2 matching venues.

`replan` and `find_nearby` are deterministic placeholders in one small
module, kept swappable for real AI/location calls later without changing
the UI. The plan generator itself is deliberately simple: it *selects and
arranges* venues between fixed times, not a scheduling or routing engine.

## Accounts and dashboard

Session-based auth (Werkzeug password hashing, no third-party provider).
From `/dashboard`, a logged-in parent can manage children's profiles and
browse saved plans (date, child, stop preview, reopen link, remove button).
Saved plans belong to the account, not the child: removing a child keeps
their past plans. An account is only required to *save* a plan, not to
generate one.

Every page's top-right corner shows login status (a "Log in" button, or an
avatar when signed in). Signed-in users get a collapsible sidebar with
navigation to every page, including admin-only pages when applicable.

Admin accounts (`is_admin` flag on `parents`) get extra pages:

| Page         | Purpose                                                                 |
| ------------ | ------------------------------------------------------------------------ |
| `/settings`  | Edit the chatbot's knowledge base and system prompt from the browser; saving the knowledge base re-indexes it in the background. |
| `/chunks`    | Inspect how the knowledge base was chunked; re-run chunking at a different size. |
| `/results`   | Browse rated chatbot responses and AI-generated plans, each in its own session ("Chatbox" / "Generated Plan") with its own stats, auto-refreshing. |
| `/components` | Inventory of the app's building blocks, each with its own isolated test page. |
| `/workflows` | End-to-end use cases, each one a chain of those components. |

## AI chatbot

A floating widget on every page answers questions about how the site works,
via [OpenRouter](https://openrouter.ai) (model swappable by changing a
string; a dropdown offers a free model and a couple of paid ones). Every
OpenRouter call (chatbot and AI planner alike) has a timeout and retries
once if the provider returns an empty or malformed reply, which free-tier
models occasionally do under load.

Answers are grounded with retrieval-augmented generation (RAG) instead of
the model's own guesses:

1. `data/knowledge_base.md` is split into ~128-token chunks, keeping related
   sentences together.
2. Each chunk is embedded with `sentence-transformers` (`all-MiniLM-L6-v2`)
   and stored in a local [ChromaDB](https://www.trychroma.com) index
   (`data/chroma/`, rebuildable, git-ignored).
3. Each question retrieves the top 3 most similar chunks (with scores),
   included in the prompt; the model answers only from those chunks and
   cites them inline as `[Source N]` (clickable, shows the chunk text).
4. First-time indexing (or after an admin edits/re-chunks the knowledge
   base) runs in the background with a progress animation.

Every reply gets a 👍/👎 rating; clicking one saves the question, answer,
model, timestamp, response time, and token counts to `data/results.json`
(git-ignored runtime data). Closing the widget keeps the conversation for
next time; "End chat" clears it on purpose.

## AI Agent

An admin-only, isolated test page (`/agent`, linked from `/components`) for
a genuine tool-calling agent, built with
[LangGraph](https://langchain-ai.github.io/langgraph/)'s `create_react_agent`
over an OpenRouter-backed model (`src/llms.py`). Unlike the chatbot above
(which only ever answers a question), this agent decides *what to do*: given
a free-text message, it picks between two tools -- planning a full day trip,
or finding a nearby kid-friendly place -- or just replies directly if
neither fits. Each tool is a thin wrapper around code that already powers
the rest of the site (the rule-based planner + `PlanningAgent`'s AI
adjustment, and `find_nearby`), not new planning logic.

It's deliberately isolated: the site-wide chat bubble is untouched and still
only answers FAQ questions. This page exists to test the tool-calling agent
on its own before deciding whether it should ever replace or extend the main
chatbot.

**Getting an OpenRouter API key** (also needed for the chatbot and AI
planner above -- one key covers all of it):

- Go to [openrouter.ai](https://openrouter.ai) and sign up (free).
- Open [openrouter.ai/keys](https://openrouter.ai/keys) and click **Create
  Key**. Give it a name and copy the value -- it's only shown once.
- Paste it into `.env` as `OPENROUTER_API_KEY=<your key>` (copy
  `.env.example` to `.env` first if you haven't already).
- The default model (`google/gemma-4-26b-a4b-it:free`) doesn't require
  adding credit. Switching to a paid model (GPT-4o mini, Claude Sonnet 5)
  needs credit added under
  [openrouter.ai/settings/credits](https://openrouter.ai/settings/credits).
- Never commit `.env` or paste a real key into a prompt, screenshot, or
  commit message -- `_call_openrouter`/`src/llms.py` only ever read it from
  `os.environ`, and it's never logged or printed.

## Web Search

Another admin-only, isolated component test page (`/search-web`, linked from
`/components`): a query box and a **Run** button that call the
[Tavily Search API](https://tavily.com) and display the top 5 results
(title, URL, snippet). `src/components/search_web.py` is self-contained, one
file for the whole component, matching the "isolate and test each piece on
its own" pattern the Components page exists for. Also used as Find Nearby's
fallback (see below) when the curated venue table has nothing to offer.

Tavily, not Brave: Brave killed its free Search API tier in February 2026 --
the "identity verification" card is now an active billing instrument,
charged automatically past $5 of usage/month with no cap. Tavily's free
tier has no such trap.

**Getting a Tavily API key:**

- Go to [tavily.com](https://tavily.com) and sign up -- no credit card
  required.
- Open your [dashboard](https://app.tavily.com/home) and copy your API key.
- Paste it directly into the Web Search page and click **Save Key** -- this
  writes it into `.env` for you (via `python-dotenv`'s `set_key`) and it's
  usable immediately, no restart needed. You can also edit `.env` by hand as
  `TAVILY_API_KEY=<your key>`, same as any other key in this project.
- The free plan includes 1,000 search credits per month, resetting monthly.
  Requests simply stop once exhausted, they never bill you.
- Same rule as above: never commit `.env` or share the key -- it's only
  ever read from `os.environ`, never logged, printed, or sent back to the
  browser once saved.

## Find Nearby

"Find a kid-friendly place near us, right now", available both on its own
admin test page (`/find-nearby`, linked from `/components`) and behind the
live in-trip page's **Need something now?** panel.

Two components, one job each:

- `src/components/geocode.py` turns a location into a place name, via the
  Google Geocoding API. Since venues now carry coordinates, this is optional
  for the "use my location" path: the browser's own free
  `navigator.geolocation` gives coordinates (no key, no Google script in the
  page) and distances are computed against the venues directly, so geocoding
  only adds a human-readable place name. It is genuinely required for a typed
  address, which has no coordinates to work from.
- `src/components/find_nearby.py` does the matching. It narrows the venue
  table to the resolved city (or searches every city when only coordinates
  are known), ranks by real straight-line distance
  (`src/geo.py`) and reports each result's `distance_km`, then calls the
  app's existing `interactions.find_nearby()` for the actual need matching
  rather than reimplementing it. When curated has nothing, it falls back to a
  live Tavily web search, tagging the result `source: "search"` so the UI can
  say where the answer came from.

Not every venue has coordinates (see Venue coordinates below), so venues
without them fall back to same-neighbourhood-first ordering and report no
distance. That fallback is permanent, not transitional: user-submitted venues
never get coordinates from a source.

Curated venues are Vancouver-only today, so a location elsewhere legitimately
returns zero curated matches and falls through to search. Location is always
optional: with none shared, the panel keeps its original behaviour of
matching the need across all venues.

**Optional: a Google Maps API key for address search.**

No key is needed to share your location: the browser supplies coordinates and
the venues carry their own, so distance ranking works out of the box. A key is
only needed for the "set a location by hand" box, since turning typed text
into coordinates is exactly what geocoding does and there is nothing else to
compute it from. Without a key that one input is disabled and says so.

- Open the [Google Cloud console](https://console.cloud.google.com/google/maps-apis/api-list)
  and create or pick a project.
- Enable the **Geocoding API**. That single API is all this component uses.
- Under **Credentials**, create an API key, then restrict it: *API
  restrictions* to the Geocoding API only, and *Application restrictions* to
  IP addresses.
- Paste it into the Find Nearby page and click **Save Key** (writes `.env`
  via `set_key`, usable immediately, no restart), or set
  `GOOGLE_MAPS_API_KEY=<your key>` in `.env` by hand.
- Google gives a recurring monthly credit that covers well beyond this app's
  usage, but the Geocoding API does require billing enabled on the project.
- The key is server-side only: it is never sent to the browser, logged, or
  printed, and `.env` is git-ignored.

## Venue coordinates

Venues carry `lat`/`lng`, populated from open data rather than a paid
geocoder. `scripts/geocode_venues.py` fills them in and is re-runnable:

```bash
python3 scripts/geocode_venues.py            # dry run, prints a report
python3 scripts/geocode_venues.py --write    # also updates data/venues.json
```

It asks the source that is actually authoritative for each kind of venue, all
keyless: the City of Vancouver **parks** dataset for parks and beaches, the
**business licences** dataset for restaurants and cafes, and Nominatim
(OpenStreetMap) for landmarks, museums, and anything outside city limits.

It is deliberately conservative, because a wrong coordinate is worse than a
missing one: a missing one falls back to neighbourhood matching, while a wrong
one silently mis-ranks "what's near me". So it matches on near-exact names
only, requires a restaurant's licence to sit in the venue's own neighbourhood
(this is what picks the right branch of a chain), sanity-checks every result
against a Metro Vancouver bounding box, and leaves anything uncertain null and
listed in its report rather than guessing.

That currently resolves 27 of 38 venues. The rest are mostly independent
restaurants absent from open data, plus venues outside the City of Vancouver's
datasets (Tomahawk Restaurant is in North Vancouver). Add those by hand if you
want them, or leave them: the code degrades to neighbourhood matching for any
venue without coordinates.

New coordinates reach an existing database through
`db._backfill_venue_coordinates`, which runs on startup. It is a separate step
because `_seed_venues` only ever inserts and skips names already present, so
edits to `venues.json` would otherwise never reach a populated `app.db`.

## Project structure

```
travel-with-tots/
├── app.py                        # Flask entry point (routes + form handling)
├── data/
│   ├── venues.json               # curated Vancouver venues
│   ├── knowledge_base.md         # chatbot facts, editable from /settings
│   ├── app.db                    # SQLite database (generated; git-ignored)
│   ├── chroma/                   # chatbot's vector index (generated; git-ignored)
│   ├── rag_config.json           # current chunk size + KB hash (generated; git-ignored)
│   └── results.json              # thumbs up/down ratings, chatbot + plans (generated; git-ignored)
├── src/                           # Application logic
│   ├── data_loader.py             # loads venue data, builds Google Maps links
│   ├── db.py                      # SQLite data layer (schema, connection, safe writes)
│   ├── dates.py                   # date/age utilities, independent of storage
│   ├── geo.py                     # straight-line distance between coordinates
│   ├── form_helpers.py            # trip-planning form parsing/validation, no Flask dependency
│   ├── filters.py                 # filters venues by selected features
│   ├── models.py                  # Plan and Trip domain objects
│   ├── itinerary.py               # generate_plans: rule-based candidate Plan objects
│   ├── interactions.py            # replan() + find_nearby() in-trip logic
│   ├── agents.py                  # chatbot + AI plan/replan adjuster logic, routed through OpenRouter
│   ├── llms.py                    # AI Agent: LangGraph tool-calling agent over OpenRouter
│   ├── components/
│   │   ├── plan_trip.py           # Plan Trips component: rule-based draft + AI smoothing
│   │   ├── replan_trip.py         # Replan a Trip component: rule-based replan + AI smoothing
│   │   ├── find_nearby.py         # Find Nearby component: location-narrowed venues, search fallback
│   │   ├── geocode.py             # Geocode component: coordinates/address to city + neighbourhood
│   │   └── search_web.py          # Web Search component: Tavily Search API
│   ├── rag.py                     # chunking, embeddings, and retrieval for the chatbot
│   ├── results.py                 # saves/reads thumbs up/down ratings, by kind (chatbot/plan/replan)
│   ├── workflows/                 # one file per workflow, each chaining components
│   │   ├── nap_time_rescue.py     # replan around a long nap, substitute closed stops
│   │   ├── answer_with_web_fallback.py  # knowledge base, then live web search
│   │   └── plan_from_chat.py      # describe a day in chat, get a plan back
│   └── prompts/
│       ├── website_chatbot.txt    # chatbot system prompt
│       ├── plan_adjust.txt        # AI plan adjuster system prompt
│       └── replan_adjust.txt      # AI replan adjuster system prompt
├── templates/
│   ├── index.html                 # marketing landing page
│   ├── plan.html                  # Page 1: planning form + comparison cards
│   ├── trip.html                  # Page 2: in-trip timeline + interactions
│   ├── base.html                  # shared layout for account/admin pages
│   ├── login.html, signup.html    # auth pages
│   ├── dashboard.html             # saved children + trips
│   ├── settings.html              # admin: edit knowledge base + chatbot prompt
│   ├── chunks.html                # admin: view and re-run chunking
│   ├── results.html               # admin: browse ratings, stats per session
│   ├── components.html            # admin: inventory of the app's components
│   ├── workflows.html             # admin: use cases chaining those components
│   ├── ai_agent.html              # admin: isolated AI Agent test page (/agent)
│   ├── search_web.html            # admin: isolated Web Search test page (/search-web)
│   ├── plan_trip.html             # admin: isolated Plan Trips test page (/plan-trip)
│   ├── replan_trip.html           # admin: isolated Replan a Trip test page (/replan-trip)
│   ├── find_nearby.html           # admin: isolated Find Nearby test page (/find-nearby)
│   ├── _chatbot_widget.html       # floating chat widget, included on every page
│   ├── _nav.html                  # avatar/login + sidebar, included on every page
│   ├── _stop_preview.html         # shared stop_line() macro (plan.html + dashboard.html)
│   └── _results_session.html      # shared results_session() macro (results.html, per kind)
├── static/
│   ├── style.css                  # planner / in-trip / account styling
│   ├── landing.css                # landing-page styling
│   ├── nav.css                    # avatar/login + sidebar styling
│   ├── chatbot.css, chatbot.js    # chat widget styling + behaviour (incl. ratings)
│   ├── agent-chat.js              # AI Agent test page's minimal chat behaviour
│   ├── search-web.js              # Web Search test page's key-save + run behaviour
│   ├── plan-trip.js               # Plan Trips test page's run behaviour
│   ├── replan-trip.js             # Replan a Trip test page's run behaviour
│   ├── find-nearby.js             # Find Nearby test page's geolocation + run behaviour
│   ├── stop-render.js             # shared stop-list rendering for plan-trip.js/replan-trip.js
│   ├── rag-status.js              # shared polling helper for indexing progress
│   ├── chunks.js                  # Chunks page re-run behaviour
│   └── results.js                 # Results page auto-refresh polling
├── scripts/
│   └── geocode_venues.py          # one-time: fill venue lat/lng from open data
├── tests/
│   ├── test_agents.py             # smoke test for the OpenRouter connection
│   ├── test_planning_agent.py     # unit tests for the live plan adjuster
│   ├── test_replanning_agent.py   # unit tests for the live replan adjuster
│   ├── test_components_plan_trip.py    # unit tests for the Plan Trips component
│   ├── test_components_replan_trip.py  # unit tests for the Replan a Trip component
│   ├── test_components_find_nearby.py  # unit tests for Find Nearby + Geocode
│   ├── test_form_helpers.py       # unit tests for form parsing/child resolution
│   ├── test_interactions.py       # unit tests for replan()/find_nearby()
│   ├── test_dates.py              # unit tests for compute_age
│   ├── test_geo.py                # unit tests for haversine distance
│   ├── test_workflows.py          # unit tests for workflow declarations + page
│   ├── test_db.py                 # unit tests for get_candidate_venues
│   └── test_results.py            # unit tests for results.py's kind-filtering and stats
├── requirements.txt
└── README.md
```

### Database

SQLite (`data/app.db`), created automatically by [`src/db.py`](src/db.py).
Foreign keys are enforced per connection; all writes are parameterized and
transactional.

| Table      | Key fields                                                    | Notes                                                                 |
| ---------- | --------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `parents`  | email, password hash, `is_admin`                                 | One row per account.                                                   |
| `children` | name, gender, date of birth                                      | Age is computed from DOB (`compute_age`), never stored.               |
| `trips`    | nap schedule, trip details, `parent_id` (NOT NULL), `child_id`   | Owned by the account; `child_id` is display-only (`ON DELETE SET NULL`), so removing a child doesn't delete their trips. |
| `venues`   | name, type, hours, age range, `source` (CHECK: `municipal_open_data` / `user_submitted` / `curated`) | Seeded from `venues.json`, open to user submissions; curated rows drive AI-planner candidate selection. |

### Routes

| Route                        | Method   | Purpose                                                    |
| ----------------------------- | -------- | ------------------------------------------------------------ |
| `/`                            | GET      | Marketing landing page                                       |
| `/signup`, `/login`            | GET/POST | Create or log in to an account                               |
| `/logout`                      | GET      | Clear the session                                             |
| `/dashboard`                   | GET      | Logged-in parent's children, trips, and places                |
| `/add-child`, `/edit-child/<id>`, `/delete-child/<id>` | POST | Manage saved children |
| `/log-place`                   | POST     | Save a user-submitted venue                                   |
| `/plan`                        | GET/POST | Page 1: trip form and candidate plan cards                    |
| `/save-trip`                   | POST     | Save a generated plan to the account                           |
| `/delete-trip/<id>`             | POST     | Remove a saved plan from the account                           |
| `/trip`                        | GET/POST | Page 2: in-trip view for the chosen plan                        |
| `/trip/<id>`                    | GET      | Reopen a previously saved trip                                  |
| `/replan`                       | POST     | Re-plan the rest of the day, rule-based (JSON in/out)            |
| `/replan/adjust`                | POST     | Re-plan, then let the AI adjuster smooth it (JSON in/out)        |
| `/find_nearby`                  | POST     | Find 1-2 venues for an immediate need (JSON)                    |
| `/chatbot`                      | POST     | Ask the chatbot a question (JSON in/out)                        |
| `/feedback`                     | POST     | Save a thumbs up/down rating (chatbot response or AI plan)      |
| `/rag/status`                   | GET      | Poll-able chatbot indexing status                               |
| `/settings`                     | GET      | Admin: view/edit knowledge base + chatbot prompt                |
| `/settings/knowledge-base`, `/settings/prompt` | POST | Admin: save the knowledge base or chatbot prompt |
| `/chunks`                       | GET      | Admin: list every chatbot knowledge-base chunk                  |
| `/chunks/rerun`                 | POST     | Admin: re-chunk and re-embed at a different size                |
| `/results`                      | GET      | Admin: browse rated chatbot responses + generated plans, stats per session |
| `/results/data`                 | GET      | Admin: poll-able per-session stats + results, for auto-refresh  |
| `/components`                   | GET      | Admin: inventory of components, each with its own test page     |
| `/workflows`                    | GET      | Admin: end-to-end use cases, each a chain of components         |
| `/agent`, `/agent/chat`         | GET, POST | Admin: isolated AI Agent test page + chat (JSON in/out)         |
| `/search-web`, `/search-web/run`, `/search-web/key` | GET, POST | Admin: isolated Web Search test page, run a query, save the API key |
| `/plan-trip`, `/plan-trip/run`  | GET, POST | Admin: isolated Plan Trips component test page + run (JSON out) |
| `/replan-trip`, `/replan-trip/run` | GET, POST | Admin: isolated Replan Trip component test page + run (JSON out) |
| `/find-nearby`, `/find-nearby/run`, `/find-nearby/key` | GET, POST | Admin: isolated Find Nearby test page, resolve a location + find places, save the Maps key |

## Data model

Each venue in `data/venues.json` has:

| Field                 | Meaning                                               |
| ---------------------- | -------------------------------------------------------- |
| `name`                 | Venue name                                                |
| `type`                 | e.g. restaurant, cafe, park, mall, museum, attraction     |
| `category`             | `food` or `activity`: decides its time slot                |
| `neighbourhood`        | Vancouver neighbourhood                                   |
| `kid_friendly`         | true/false                                                |
| `has_family_room`      | true/false                                                |
| `has_nursing_room`     | true/false                                                |
| `stroller_accessible`  | true/false                                                |
| `nap_friendly`         | true/false: suitable for a nap-on-the-go stop              |
| `can_eat`              | true/false: food is available at this stop                 |
| `open`, `close`        | representative daily hours (`HH:MM`)                       |

A `maps_url` (Google Maps search link) is generated from the venue name at
load time.

## Designed to grow

The pieces are intentionally modular:

- **Richer data**: replace `data/venues.json` (and `data_loader.py`) with a
  database or a real venues API.
- **Real routing**: venues carry lat/lng now, but no routing API does, so
  travel time between stops is still a soft, LLM-judgment heuristic in the AI
  planner and a flat per-mode buffer in the rule-based one
  (`itinerary.TRANSIT_BUFFER_MIN`). Both are structured so a real routing API
  can slot in later without changing their shape, and the coordinates are the
  groundwork for it.
- **Real re-planning & help**: swap `interactions.replan()` and
  `interactions.find_nearby()` for real AI/location-service calls without
  changing their signatures or the UI. The chatbot and AI planner already
  show what this looks like end to end: real AI calls, grounded in real
  data, behind a small, swappable module (`src/agents.py`, `src/rag.py`).
