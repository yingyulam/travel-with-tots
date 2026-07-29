# Travel with Tots

A proof-of-concept web app that builds a **nap-friendly, single-day itinerary**
for parents travelling with young children (ages 0–5).

A parent enters their day's shape — wake-up time, bedtime, nap time(s), the
kid's age, destination, how they're getting around, and which family-friendly
features matter — and the app arranges a timed list of suitable stops between
wake-up and bedtime.

## What it does

- Collects trip details through a clean, mobile-friendly form.
- Selects venues that match the parent's chosen features (kid-friendly,
  family room, nursing room, stroller/step-free access).
- Arranges the day from wake-up to bedtime: a **rest/nap block** at each nap
  time, a **food** venue around midday, and **activities** in the remaining
  slots.
- Displays the plan as an ordered, timed list. Each stop shows its time, name,
  type, neighbourhood, feature badges, and an **Open in Google Maps** link.
- Shows the chosen transit approach at the top of the plan (it is displayed
  only — no routes are computed).
- Includes an **Ask for help** button backed by a placeholder function that
  returns a generic tip, ready to be swapped for a real AI backend.

The itinerary generator is deliberately simple: it *selects and arranges*
venues between fixed times. It is not a scheduling or routing engine. There is
no database, no external API, and no map SDK.

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
├── app.py               # Flask entry point (routes + form handling)
├── data/
│   └── venues.json      # ~12 hardcoded Vancouver venues
├── src/                 # Application logic
│   ├── data_loader.py   # loads venue data, builds Google Maps links
│   ├── filters.py       # filters venues by selected features
│   ├── itinerary.py     # arranges selected venues across the day
│   └── ai_helper.py     # placeholder "Ask for help" suggestion
├── templates/
│   └── index.html       # form + generated itinerary
├── static/
│   └── style.css         # styling
├── requirements.txt
└── README.md
```

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

A `maps_url` (a Google Maps search link) is generated from the venue name at
load time.

## Designed to grow

The pieces are intentionally modular so the POC can get smarter without a
rewrite:

- **Richer data** — replace `data/venues.json` (and, later, `data_loader.py`)
  with a database or a real venues API.
- **Smarter scheduling** — extend `itinerary.py` with real nap/feed timing,
  travel time, and more inputs.
- **Real AI help** — replace `ai_helper.get_suggestion()` with an LLM call; it
  already receives the plan context and returns a text tip.
