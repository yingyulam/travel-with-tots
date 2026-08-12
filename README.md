# Travel with Tots

A web app that builds a **nap-friendly, single-day itinerary** for parents
travelling with young children (ages 0-5), backed by parent accounts and an
AI chatbot that answers questions about how the site works.

A parent enters their day's shape: wake-up time, bedtime, up to four naps
(each with a start time and typical duration), the kid's age, destination,
how they're getting around, and which family-friendly features matter, and
the app arranges a timed list of suitable stops between wake-up and bedtime.
Parents can create an account to
save children's profiles and past trips, and a chatbot widget on every page
can answer questions about how the site works.

The core planning flow is split into **two pages** so planning and doing stay
separate.

### Page 1 - Planning (`/plan`)

- Collects trip details through a clean, mobile-friendly form (times, kid's
  age in years + months, destination, transit, pace, and features). Naps are
  entered via an "+ Add a nap" control (up to 4), each with its own start
  time and typical duration, rather than a fixed pair of time inputs.
- Selects venues that match the parent's chosen features (kid-friendly,
  family room, nursing room, stroller/step-free access).
- Lets the parent pick **1-3 themes** (Outdoorsy / Rainy-day / Culture) via
  checkboxes; picking none defaults to a "Mixed" plan drawing from all
  three. Generates **one candidate plan** shown as a **comparable card**
  (theme label -- "Mixed" or a comma-joined list of the picks -- plus a
  preview of the first 2 stops, with a "+N more" link that expands the rest
  in place, no reload or extra AI calls). The plan draws from whichever
  theme(s) were picked rather than being locked to just one, is short (2-4
  stops, fewer for younger kids and a relaxed pace, more for older kids and
  an adventurous pace), places a **food** venue as close as possible to the
  parent's preferred lunch time (or midday if none was given), and drops a
  **nap-friendly** venue into the nap window so the day keeps flowing
  instead of blocking time.
- The card has a **"Start this day"** button that carries the chosen plan to
  the in-trip page. `generate_plans` produces a `Plan` object; picking it
  creates a `Trip`.
- The card also has a **"✨ Try AI-assisted day"** button that builds an
  AI-generated plan for the **same selected theme(s)**, on demand, so no
  model call is spent unless the parent actually asks for it. It's powered by
  `PlanningAgent` (`src/agents.py`) instead of the rule-based `generate_plans`,
  with a shared model dropdown (free/paid) and a system prompt
  (`src/prompts/planner.txt`, editable from `/settings`). Before calling the
  model, it queries the `venues` table for **curated** venues matching the
  destination, the child's age range, and the requested feature tags, capped
  at around 15-20 candidates so the prompt stays cheap. Without a car
  selected, candidates are narrowed to a single neighbourhood (a proxy for
  "close together," since there's no real location/travel-time data
  anywhere in this app); with a car, all matching neighbourhoods stay in
  play. If "dine out" was chosen, a real restaurant option is guaranteed a
  slot so there's always a venue for the lunch stop. The accommodation, the
  child's nap/sleep-habit notes, and any other free-text notes from the
  parent are passed to the model too, with explicit instructions to actually
  act on them rather than just display them: ordering stops sensibly around
  the accommodation, picking the nap stop to suit the child's specific sleep
  habits (not just general nap-friendliness), and letting other notes change
  venue choice, timing, or pace when they're relevant. A **"Can your child
  nap during transit?"** field (yes/sometimes/no) also shapes the nap stop:
  "yes" lets transit itself cover the nap window, "sometimes" prefers a
  proper nap venue but tolerates transit overlap, and "no" keeps transit and
  high-energy stops out of the nap window entirely, requiring a genuine
  rest-friendly venue instead. The model must choose
  exclusively from those `venue_id`s, and **every stop must cite one**: if
  even one stop cites an id outside the list, repeats an id already used
  elsewhere in the same plan, or is otherwise malformed, the *whole*
  response is rejected and retried once rather than silently trimmed. The
  pace's stop count (2/3/4 for relaxed/balanced/adventurous) is a **ceiling**,
  not a mandate: the model is told to use fewer stops whenever a shorter day
  paces more realistically, not only when the candidate list is thinner than
  that, and validation accepts anywhere from 1 up to that ceiling either way.
  To make "realistic" concrete, the prompt gives the model assumed durations
  for an activity/meal stop, each nap's own real stated duration (rather than
  a flat guess), and a minimum per-transit-mode gap to leave between stops
  (a heuristic placeholder pending a real routing API, in keeping with the
  "no real geodata" limitation above), and treats every given time --
  wake-up, bedtime, each nap, and the parent's **preferred lunch time** --
  as a target window rather than an exact appointment. Both
  planners schedule the lunch stop as close as possible to that preferred
  lunch time when one is given, instead of a fixed clock window. If no
  candidate fits any of the selected themes well, the model drops
  theme-matching for that plan and chooses stops from the trip's other
  constraints instead. If there are no candidate venues at all, the
  model is never called and a clear error is shown right away. The result
  appears as a new card right beside its rule-based counterpart, tagged
  **"✨ AI-suggested plan"** so the two are easy to tell apart. Asking again
  replaces the existing AI card rather than piling up. If a response still
  doesn't validate after the retry, an inline error is shown on that card's
  button and the rule-based card is left untouched.
  Picking an AI plan works exactly like picking a
  rule-based one: same `Plan`/`Trip` shape, no visible difference on the
  in-trip page. Every OpenRouter call (including the retry) prints its
  token counts, time, and estimated cost to the server console, same as the
  chatbot.
  Each AI-suggested plan also gets the same 👍/👎 rating widget as the
  chatbot: clicking one disables both buttons for that card and saves the
  trip input, generated plan, model, timestamp, response time, and token
  counts to `data/results.json` under a "Generated Plan" record, reviewable
  alongside chatbot ratings from `/results`.

### Page 2 - In-trip (`/trip`)

The in-trip page renders the chosen `Trip` (no input form here), top to bottom:

1. A header with destination, transit mode, and the adjustable **current time**.
2. The **live timeline** of the chosen plan: the current stop highlighted as
   *now*, past stops marked *done*, based on the current time.
3. **"Something came up?"** situation buttons.
4. A **"Need something now?"** find-nearby panel.
5. A **version switcher** to toggle between the original plan and any re-planned
   versions.
6. A **"Save this plan"** button (only on a fresh, not-yet-saved trip, and
   only once a child is picked), so a plan doesn't have to be saved from the
   planning page up front. It saves whichever version (original or
   re-planned) is currently on screen.

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
can add, edit, or remove a child's profile, and browse their **saved plans**:
each one shows its date, which child it's for, an expandable preview of its
stops, a link to reopen the full itinerary, and a **Remove** button. A saved
plan belongs to the parent's account, not the child it names -- removing a
child keeps their
past plans (shown with a "child no longer on your account" fallback) instead
of deleting them. An account isn't required to generate a plan, only to save
one.

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
- `/results`: browse every thumbs up/down rated chatbot response and
  AI-generated plan, each in its own session ("Chatbox" and "Generated
  Plan") with its own aggregate stats (total up/down, percent positive) at
  the top. The page polls for new ratings and refreshes itself
  automatically.

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
│   └── results.json          # thumbs up/down ratings, chatbot + generated plans (generated; git-ignored)
├── src/                       # Application logic
│   ├── data_loader.py         # loads venue data, builds Google Maps links
│   ├── db.py                  # SQLite data layer (schema, connection, safe writes)
│   ├── filters.py             # filters venues by selected features
│   ├── models.py               # Plan and Trip domain objects
│   ├── itinerary.py            # generate_plans: themed candidate Plan objects
│   ├── interactions.py         # replan() + find_nearby() placeholders
│   ├── agents.py                # chatbot + PlanningAgent logic, routed through OpenRouter
│   ├── rag.py                   # chunking, embeddings, and retrieval for the chatbot
│   ├── results.py               # saves/reads thumbs up/down ratings, by kind (chatbot/plan)
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
│   ├── results.html              # admin: browse chatbot + generated-plan ratings, stats per session
│   ├── _chatbot_widget.html      # floating chat widget, included on every page
│   ├── _nav.html                 # top-right avatar/login + sidebar, included on every page
│   ├── _stop_preview.html        # shared stop_line() macro, used by plan.html + dashboard.html
│   └── _results_session.html     # shared results_session() macro, used by results.html per kind
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
- **trips**: a single outing's nap schedule and details, owned by the
  parent account (`parent_id`, `NOT NULL`). `child_id` is optional and only
  for display -- removing a child sets it to `NULL` (`ON DELETE SET NULL`)
  rather than deleting the trip.
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
| `/delete-trip/<id>`         | POST     | Remove a saved plan from the account                  |
| `/trip`                    | GET/POST | Page 2: in-trip view for the chosen plan               |
| `/trip/<id>`                | GET      | Reopen a previously saved trip                        |
| `/replan`                   | POST     | Re-plan the rest of the day (JSON in/out)              |
| `/find_nearby`              | POST     | Find 1-2 venues for an immediate need (JSON)          |
| `/chatbot`                  | POST     | Ask the chatbot a question (JSON in/out)              |
| `/feedback`                 | POST     | Save a thumbs up/down rating on a chatbot response or AI-generated plan |
| `/rag/status`               | GET      | Poll-able chatbot indexing status                     |
| `/settings`                 | GET      | Admin: view/edit knowledge base + prompts              |
| `/settings/knowledge-base`, `/settings/prompt`, `/settings/planner-prompt` | POST | Admin: save the knowledge base, chatbot prompt, or planner prompt |
| `/chunks`                   | GET      | Admin: list every chatbot knowledge-base chunk        |
| `/chunks/rerun`             | POST     | Admin: re-chunk and re-embed at a different size      |
| `/results`                  | GET      | Admin: browse rated chatbot responses + generated plans, stats per session |
| `/results/data`             | GET      | Admin: poll-able per-session stats + results, for auto-refresh |

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
