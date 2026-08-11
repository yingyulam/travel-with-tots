# Travel with Tots

A web app that builds a **nap-friendly, single-day itinerary** for parents
travelling with young children (ages 0-5), backed by parent accounts and an
AI chatbot that answers questions about how the site works.

A parent enters their day's shape: wake-up time, bedtime, nap time(s),
feeding time(s), the kid's age, destination, how they're getting around, and
which family-friendly features matter, and the app arranges a timed list of
suitable stops between wake-up and bedtime. Parents can create an account to
save children's profiles and past trips, and a chatbot widget on every page
can answer questions about how the site works.

The core planning flow is split into **two pages** so planning and doing stay
separate.

### Page 1 - Planning (`/plan`)

- Collects trip details through a clean, mobile-friendly form (times, kid's
  age in years + months, destination, transit, pace, and features).
- Selects venues that match the parent's chosen features (kid-friendly,
  family room, nursing room, stroller/step-free access).
- Generates **3 themed candidate plans** (Outdoorsy / Rainy-day / Culture)
  shown as **comparable cards** (theme label + a short preview of stops). Each
  plan is short (2-4 stops, fewer for younger kids and a relaxed pace, more for
  older kids and an adventurous pace), places a **food** venue around midday,
  and drops a **nap-friendly** venue into the nap window so the day keeps
  flowing instead of blocking time.
- Each card has a **"Start this day"** button that carries the chosen plan to
  the in-trip page. `generate_plans` produces `Plan` objects; picking one
  creates a `Trip`.
- Each themed card also has a **"✨ Try AI-assisted day"** button that builds
  an AI-generated plan **for that topic only**, on demand, so no model call is
  spent on a topic the parent isn't interested in. It's powered by
  `PlanningAgent` (`src/agents.py`) instead of the rule-based `generate_plans`,
  with a shared model dropdown (free/paid) and a system prompt
  (`src/prompts/planner.txt`, editable from `/settings`). Before calling the
  model, it queries the `venues` table for **curated** venues matching the
  destination and the child's age range, and passes only that list into the
  prompt; the model must choose exclusively from those `venue_id`s, and any
  stop referencing one outside the list is dropped rather than shown, so it can
  never surface a venue that doesn't exist. The result appears as a new card
  right beside its rule-based counterpart, tagged **"✨ AI-suggested plan"** so
  the two are easy to tell apart. Asking again for the same topic replaces that
  topic's AI card rather than piling up. If a response doesn't validate even
  after one corrective retry, an inline error is shown on that card's button
  and every other card is left untouched. Picking an AI plan works exactly like
  picking a rule-based one: same `Plan`/`Trip` shape, no visible difference on
  the in-trip page.

### Page 2 - In-trip (`/trip`)

The in-trip page renders the chosen `Trip` (no input form here), top to bottom:

1. A header with destination, transit mode, and the adjustable **current time**.
2. The **live timeline** of the chosen plan: the current stop highlighted as
   *now*, past stops marked *done*, based on the current time.
3. **"Something came up?"** situation buttons.
4. A **"Need something now?"** find-nearby panel.
5. A **version switcher** to toggle between the original plan and any re-planned
   versions.

Each stop shows its time, name, type, neighbourhood, feature badges, and an
**Open in Google Maps** link. Transit is displayed only; no routes are computed.

### In-trip interactions

- An adjustable **current time** field (defaults to now) marks which stop is
  *now* vs. already *done*.
- Tap-able **situation buttons** ("Nap happened here", "Running behind",
  "Skip next stop", "Finished this stop early") call `replan(plan, situation,
  current_time)`, which keeps the current and past stops fixed and re-decides
  the rest of the day. The result is a **new** version added to the `Trip`
  (labelled with the time it was generated from); the original is never
  overwritten, and the switcher lets you move freely between versions.
- A tap-first **"Need something now?"** panel (kid-friendly restaurant,
  family room, changing table, nursing room, quiet spot, and "Other") calls
  `find_nearby(need)`, which returns 1-2 matching venues.

`replan` and `find_nearby` are deterministic placeholders kept in one small
module so they can later become real AI / location calls without changing the
UI. The plan generator is likewise deliberately simple: it *selects and
arranges* venues between fixed times, not a scheduling or routing engine.

## Accounts and dashboard

Parents can sign up and log in (session-based auth, Werkzeug password
hashing, no third-party auth provider). From `/dashboard` a logged-in parent
can add, edit, or remove a child's profile and reopen any previously saved
itinerary. An account isn't required to generate a plan, only to save one.

The top-right corner of every page shows login status: a "Log in" button
when signed out, or an avatar (the user's first-letter initial as a
placeholder; a future upload flow can swap in a real photo) when signed in.
Signed-in users also get a collapsible left sidebar (toggled with the ☰
icon, state remembered across page loads) holding every page's navigation:
Home, Planning, and, for admins, Settings, Chunks, and Results below.

Admin accounts (an `is_admin` flag on the `parents` table) get three extra
pages, shown in the sidebar only when logged in as an admin:

- `/settings`: edit the chatbot's knowledge base and system prompt directly
  from the browser, with a save confirmation. Saving the knowledge base
  automatically re-indexes it for the chatbot in the background.
- `/chunks`: see exactly how the knowledge base was split into chunks, and
  re-run chunking with a different chunk size.
- `/results`: browse every thumbs up/down rated chatbot response, with
  aggregate stats (total up/down, percent positive) at the top. The page
  polls for new ratings and refreshes itself automatically.

## AI chatbot

A floating chat widget appears on every page and answers questions about how
the site works. It's powered by [OpenRouter](https://openrouter.ai) so the
underlying model can be swapped by changing a string; a dropdown in the
widget lets a visitor pick between a free model and a couple of paid ones,
with a link to browse all models OpenRouter offers.

Answers are grounded with retrieval-augmented generation (RAG) instead of the
model's own guesses:

1. `data/knowledge_base.md` is split into chunks of about 128 tokens each,
   keeping related sentences together.
2. Each chunk is embedded with `sentence-transformers` (`all-MiniLM-L6-v2`)
   and stored in a local [ChromaDB](https://www.trychroma.com) index
   (`data/chroma/`, rebuildable and git-ignored).
3. On each question, the top 3 most similar chunks (with similarity scores)
   are retrieved and included in the prompt; the model answers only from
   those chunks and cites which one(s) it used inline as `[Source N]`.
   Clicking a citation in the chat shows the actual chunk text and score.
4. The first time the index is built (or after an admin edits the knowledge
   base or re-chunks it), embedding runs in the background with a progress
   animation in the widget instead of freezing the page.

Every response gets a 👍/👎 rating in the widget. Clicking one disables both
buttons for that message and saves the question, answer, model, timestamp,
response time, and token counts to `data/results.json` (git-ignored runtime
data, not seed content). An admin can review every rated response, and the
aggregate stats, from `/results`.

Closing the widget (via the chat bubble) keeps the conversation, so
reopening it picks up where you left off. An "End chat" button in the
widget header clears the conversation on purpose, for when you're done.

## Running locally

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

Then open **http://localhost:8016** in your browser. A demo parent account
(`demo@travelwithtots.app` / `demo1234`) and a demo admin account
(`admin@travelwithtots.app` / `admin1234`) are seeded automatically on first
run. The first time the chatbot is used, it downloads the embedding model
(about 90MB) and builds the chunk index, which can take a few seconds.

## Project structure

```
travel-with-tots/
├── app.py                    # Flask entry point (routes + form handling)
├── data/
│   ├── venues.json           # curated Vancouver venues
│   ├── knowledge_base.md     # chatbot facts, editable from /settings
│   ├── app.db                # SQLite database (generated on first run; git-ignored)
│   ├── chroma/                # chatbot's vector index (generated; git-ignored)
│   ├── rag_config.json       # current chunk size + knowledge-base hash (generated; git-ignored)
│   └── results.json          # thumbs up/down ratings (generated; git-ignored)
├── src/                       # Application logic
│   ├── data_loader.py         # loads venue data, builds Google Maps links
│   ├── db.py                  # SQLite data layer (schema, connection, safe writes)
│   ├── filters.py             # filters venues by selected features
│   ├── models.py               # Plan and Trip domain objects
│   ├── itinerary.py            # generate_plans: themed candidate Plan objects
│   ├── interactions.py         # replan() + find_nearby() placeholders
│   ├── agents.py                # chatbot + PlanningAgent logic, routed through OpenRouter
│   ├── rag.py                   # chunking, embeddings, and retrieval for the chatbot
│   ├── results.py               # saves/reads thumbs up/down chatbot ratings
│   └── prompts/
│       ├── website_chatbot.txt  # chatbot system prompt
│       └── planner.txt          # AI itinerary planner system prompt
├── templates/
│   ├── index.html              # marketing landing page
│   ├── plan.html                # Page 1: planning form + comparison cards
│   ├── trip.html                # Page 2: in-trip timeline + interactions
│   ├── base.html                 # shared layout for account/admin pages
│   ├── login.html, signup.html   # auth pages
│   ├── dashboard.html            # saved children + trips
│   ├── settings.html             # admin: edit knowledge base + prompts
│   ├── chunks.html               # admin: view and re-run chunking
│   ├── results.html              # admin: browse chatbot ratings + stats
│   ├── _chatbot_widget.html      # floating chat widget, included on every page
│   └── _nav.html                 # top-right avatar/login + sidebar, included on every page
├── static/
│   ├── style.css                # planner / in-trip / account styling
│   ├── landing.css               # landing-page styling
│   ├── nav.css                   # top-right avatar/login + sidebar styling
│   ├── chatbot.css, chatbot.js   # chat widget styling + behaviour (incl. ratings)
│   ├── rag-status.js              # shared polling helper for indexing progress
│   ├── chunks.js                  # Chunks page re-run behaviour
│   └── results.js                 # Results page auto-refresh polling
├── tests/
│   └── test_agents.py            # smoke test for the OpenRouter connection
├── requirements.txt
└── README.md
```

### Database

A small SQLite database (`data/app.db`) is created automatically on start-up by
[`src/db.py`](src/db.py), a self-contained data layer kept separate from the
routes. Tables (created only if missing, with columns added to existing
databases via a small in-code migration):

- **parents**: one row per account (email login, password hash, `is_admin` flag).
- **children**: name, gender, and **date of birth** (age is computed from the
  DOB via `compute_age`, never stored). References `parents`.
- **trips**: a single outing's nap/feeding schedule and details. References `children`.
- **venues**: kid-friendly places, seeded from `venues.json` and open to
  user submissions. A `source` column is constrained by a `CHECK` to
  `municipal_open_data` | `user_submitted` | `curated`. Curated rows also
  carry `city`, `category`, hours, and an age range (`min_age_months` /
  `max_age_months`), used to pick candidate venues for the AI itinerary
  planner.

Parent, child, and trip relationships use `FOREIGN KEY` constraints with
`PRAGMA foreign_keys = ON` (enabled per connection). All writes are
parameterized and run inside a transaction.

### Routes

| Route                     | Method   | Purpose                                             |
| ------------------------- | -------- | ---------------------------------------------------- |
| `/`                       | GET      | Marketing landing page                                |
| `/signup`, `/login`       | GET/POST | Create or log in to an account                        |
| `/logout`                 | GET      | Clear the session                                     |
| `/dashboard`              | GET      | Logged-in parent's children, trips, and places        |
| `/add-child`, `/edit-child/<id>`, `/delete-child/<id>` | POST | Manage saved children |
| `/log-place`              | POST     | Save a user-submitted venue                            |
| `/plan`                   | GET/POST | Page 1: trip form and candidate plan cards             |
| `/plan/ai`                 | POST     | Page 1: AI-assisted plan for one theme (JSON out)     |
| `/save-trip`               | POST     | Save a generated plan to the account                   |
| `/trip`                    | GET/POST | Page 2: in-trip view for the chosen plan               |
| `/trip/<id>`                | GET      | Reopen a previously saved trip                        |
| `/replan`                   | POST     | Re-plan the rest of the day (JSON in/out)              |
| `/find_nearby`              | POST     | Find 1-2 venues for an immediate need (JSON)          |
| `/chatbot`                  | POST     | Ask the chatbot a question (JSON in/out)              |
| `/feedback`                 | POST     | Save a thumbs up/down rating on a chatbot response    |
| `/rag/status`               | GET      | Poll-able chatbot indexing status                     |
| `/settings`                 | GET      | Admin: view/edit knowledge base + prompts              |
| `/settings/knowledge-base`, `/settings/prompt`, `/settings/planner-prompt` | POST | Admin: save the knowledge base, chatbot prompt, or planner prompt |
| `/chunks`                   | GET      | Admin: list every chatbot knowledge-base chunk        |
| `/chunks/rerun`             | POST     | Admin: re-chunk and re-embed at a different size      |
| `/results`                  | GET      | Admin: browse rated chatbot responses + stats         |
| `/results/data`             | GET      | Admin: poll-able stats + full results list, for auto-refresh |

## Data model

Each venue in `data/venues.json` has:

| Field                 | Meaning                                             |
| --------------------- | ---------------------------------------------------- |
| `name`                | Venue name                                            |
| `type`                | e.g. restaurant, cafe, park, mall, museum, attraction |
| `category`            | `food` or `activity`: decides its time slot           |
| `neighbourhood`       | Vancouver neighbourhood                               |
| `kid_friendly`        | true/false                                            |
| `has_family_room`     | true/false                                            |
| `has_nursing_room`    | true/false                                            |
| `stroller_accessible` | true/false                                            |
| `nap_friendly`        | true/false: suitable for a nap-on-the-go stop         |
| `can_eat`             | true/false: food is available at this stop            |
| `open`, `close`       | representative daily hours (`HH:MM`)                  |

A `maps_url` (a Google Maps search link) is generated from the venue name at
load time.

## Designed to grow

The pieces are intentionally modular so the app can get smarter without a
rewrite:

- **Richer data**: replace `data/venues.json` (and, later, `data_loader.py`)
  with a database or a real venues API.
- **Smarter planning**: replace the body of `itinerary.generate_plans()` with
  an AI planner; the inputs and returned shape stay the same.
- **Real re-planning & help**: swap `interactions.replan()` and
  `interactions.find_nearby()` for real AI / location-service calls. Their
  signatures (and the UI that calls them) don't need to change. The chatbot
  already shows what this looks like end to end: real AI calls, grounded in
  real data, with the same kind of small, swappable module (`src/agents.py`,
  `src/rag.py`) behind a stable interface.
