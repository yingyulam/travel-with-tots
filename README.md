# Travel with Tots

A proof-of-concept web app that builds a **nap-friendly, single-day itinerary**
for parents travelling with young children (ages 0–5).

A parent enters their day's shape — wake-up time, bedtime, nap time(s), the
kid's age, destination, how they're getting around, and which family-friendly
features matter — and the app arranges a timed list of suitable stops between
wake-up and bedtime.

The app is split into **two pages** so planning and doing stay separate.

### Page 1 — Planning (`/plan`)

- Collects trip details through a clean, mobile-friendly form (times, kid's
  age in years + months, destination, transit, pace, and features).
- Selects venues that match the parent's chosen features (kid-friendly,
  family room, nursing room, stroller/step-free access).
- Generates **2–3 themed candidate plans** (Outdoorsy / Rainy-day / Culture)
  shown as **comparable cards** (theme label + a short preview of stops). Each
  plan is short (2–4 stops — fewer for younger kids and a relaxed pace, more for
  older kids and an adventurous pace), places a **food** venue around midday,
  and drops a **nap-friendly** venue into the nap window so the day keeps
  flowing instead of blocking time.
- Each card has a **"Start this day"** button that carries the chosen plan to
  the in-trip page. `generate_plans` produces `Plan` objects; picking one
  creates a `Trip`.

### Page 2 — In-trip (`/trip`)

The in-trip page renders the chosen `Trip` (no input form here), top to bottom:

1. A header with destination, transit mode, and the adjustable **current time**.
2. The **live timeline** of the chosen plan — the current stop highlighted as
   *now*, past stops marked *done*, based on the current time.
3. **"Something came up?"** situation buttons.
4. A **"Need something now?"** find-nearby panel.
5. A **version switcher** to toggle between the original plan and any re-planned
   versions.

Each stop shows its time, name, type, neighbourhood, feature badges, and an
**Open in Google Maps** link. Transit is displayed only — no routes are computed.

### In-trip interactions

- An adjustable **current time** field (defaults to now) marks which stop is
  *now* vs. already *done*.
- Tap-able **situation buttons** — "Nap happened here", "Running behind",
  "Skip next stop", "Finished this stop early" — call `replan(plan, situation,
  current_time)`, which keeps the current and past stops fixed and re-decides
  the rest of the day (e.g. finishing a stop early pulls the remaining stops
  earlier and opens a bonus slot). The result is a **new** version added to the
  `Trip` (labelled with the time it was generated from); the original is never
  overwritten, and the switcher lets you move freely between versions.
- A tap-first **"Need something now?"** panel — kid-friendly restaurant,
  family room, changing table, nursing room, quiet spot, and "Other" — calls
  `find_nearby(need)`, which returns 1–2 matching venues and the time of the
  request.

`replan` and `find_nearby` are deterministic placeholders kept in one small
module so they can later become real AI / location calls without changing the
UI. The plan generator is likewise deliberately simple: it *selects and
arranges* venues between fixed times, not a scheduling or routing engine. There
is no database, no external API, and no map SDK.

## Running locally

```bash
# 1. (optional) create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. run the app
python app.py
```

Then open **http://localhost:8016** in your browser.

## Project structure

```
travel-with-tots/
├── app.py                # Flask entry point (routes + form handling)
├── data/
│   ├── venues.json       # ~12 hardcoded Vancouver venues
│   └── app.db            # SQLite database (generated on first run; git-ignored)
├── src/                  # Application logic
│   ├── data_loader.py    # loads venue data, builds Google Maps links
│   ├── db.py             # SQLite data layer (schema, connection, safe writes)
│   ├── filters.py        # filters venues by selected features
│   ├── models.py         # Plan and Trip domain objects
│   ├── itinerary.py      # generate_plans: themed candidate Plan objects
│   └── interactions.py   # replan() + find_nearby() placeholders
├── templates/
│   ├── index.html        # marketing landing page
│   ├── plan.html         # Page 1 — planning form + comparison cards
│   └── trip.html         # Page 2 — in-trip timeline + interactions
├── static/
│   ├── style.css         # planner / in-trip styling
│   └── landing.css       # landing-page styling
├── requirements.txt
└── README.md
```

### Database

A small SQLite database (`data/app.db`) is created automatically on start-up by
[`src/db.py`](src/db.py) — a self-contained data layer kept separate from the
routes. Tables (created only if missing):

- **parents** — one row per account (email login).
- **children** — name, gender, and **date of birth** (age is computed from the
  DOB via `compute_age`, never stored). References `parents`.
- **trips** — a single outing's nap schedule and details. References `children`.
- **venues** — kid-friendly places, seeded from `venues.json` and open to
  user submissions. A `source` column is constrained by a `CHECK` to
  `municipal_open_data` | `user_submitted` | `curated`.

Parent→child→trip relationships use `FOREIGN KEY` constraints with
`PRAGMA foreign_keys = ON` (enabled per connection). All writes are
parameterized and run inside a transaction. Itinerary/stop tables are
deliberately deferred to a later stage.

### Routes

| Route          | Method   | Purpose                                          |
| -------------- | -------- | ------------------------------------------------ |
| `/`            | GET      | Marketing landing page                           |
| `/plan`        | GET/POST | Page 1 — trip form and candidate plan cards      |
| `/trip`        | POST     | Page 2 — in-trip view for the chosen plan        |
| `/replan`      | POST     | Re-plan the rest of the day (JSON in/out)        |
| `/find_nearby` | POST     | Find 1–2 venues for an immediate need (JSON)     |

## Data model

Each venue in `data/venues.json` has:

| Field                 | Meaning                                             |
| --------------------- | --------------------------------------------------- |
| `name`                | Venue name                                          |
| `type`                | e.g. restaurant, cafe, park, mall, museum           |
| `category`            | `food` or `activity` — decides its time slot        |
| `neighbourhood`       | Vancouver neighbourhood                             |
| `kid_friendly`        | true/false                                          |
| `has_family_room`     | true/false                                          |
| `has_nursing_room`    | true/false                                          |
| `stroller_accessible` | true/false                                          |
| `nap_friendly`        | true/false — suitable for a nap-on-the-go stop      |

A `maps_url` (a Google Maps search link) is generated from the venue name at
load time.

## Designed to grow

The pieces are intentionally modular so the POC can get smarter without a
rewrite:

- **Richer data** — replace `data/venues.json` (and, later, `data_loader.py`)
  with a database or a real venues API.
- **Smarter planning** — replace the body of `itinerary.generate_plans()` with
  an AI planner; the inputs and returned shape stay the same.
- **Real re-planning & help** — swap `interactions.replan()` and
  `interactions.find_nearby()` for real AI / location-service calls. Their
  signatures (and the UI that calls them) don't need to change.
