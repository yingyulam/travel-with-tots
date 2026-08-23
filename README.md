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

# 3. set a session secret and your OpenRouter API key
cp .env.example .env
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env
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
- Naps take **any** number of minutes between 15 and 180. The input used to
  step in quarter hours, so the browser refused 40, and 20, and 50: values the
  server had always accepted. Its bounds now come from the same
  `form_helpers` constants the server clamps with, so the two cannot drift.
- Submitting shows **"Building your day…"**, because the page stays on screen
  for the whole AI call and silence there reads as a button that did nothing.

**One model choice, everywhere.** The chat widget's dropdown is the only place
a model is picked, and planning and replanning now read that choice instead of
each falling back to a default nobody can see. The page sends it with the form,
`/plan` and `/replan/adjust` check it against the same `ALLOWED_CHAT_MODELS`
the chat uses, and it reaches `PlanningAgent`/`ReplanningAgent` from there. A
value the app does not offer falls back to the default rather than being
passed on, since the field is client-supplied.

This matters for speed, because the AI call *is* the wait. The rule-based
draft takes 0.0003s; everything after it is the model. Measured on one
identical day, end to end through `/plan`:

| Model | Time |
| --- | --- |
| `openrouter/free` (the dropdown default) | 9.6s, 18s, 22s, 31s |
| `openai/gpt-4o-mini` | 2.1s, 4.0s, 4.1s, 5.1s |

The free option is OpenRouter's auto-router, which picks a different model per
request, so its latency is not just high but unpredictable. Picking a paid
model in the dropdown is now the way to a fast plan.

### Page 2 - In-trip (`/trip`)

Renders the chosen `Trip`, top to bottom:

1. Header: destination, transit mode, adjustable **current time**.
2. **Live timeline**: current stop marked *now*, past stops marked *done*.
3. **"Do you need to change your plan?"** situation buttons, headed by the
   stop the replan is anchored to ("You're at Science World").
4. **"Need something now?"** find-nearby panel.
5. **Version switcher** between the original plan and any re-planned versions.
6. **"Save this plan"** (fresh trip, child picked): saves whichever version
   is on screen.

Each stop shows its time, name, type, neighbourhood, feature badges, and an
**Open in Google Maps** link. Transit is displayed only; no routes are computed.

**In-trip interactions:**

- Situation buttons (`Nap happened here`, `Need to stay here longer`, `Skip
  next stop`, `Finished this stop early`, `It's raining`, `Change the theme`)
  call `replan(plan, situation, current_time)`, which keeps
  current/past stops fixed and re-decides the rest of the day. The result is a
  **new** version on the `Trip`; the original is never overwritten.
- The two duration situations take either a preset or a **typed number of
  minutes**, clamped server-side to `MIN_REPLAN_MINUTES`..`MAX_REPLAN_MINUTES`
  in one place so no caller can shift the day backwards or wrap it past
  midnight.
- A panel-level **free-text box** rides along with every replan, not just some
  of them, and has its own **Replan** button for going on the note alone. That
  route leaves the remaining stops' times untouched and lets the AI adjuster act
  on the words. It is deliberately not also a chip: a button and a box for the
  same request is one control too many.
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

## Log a Place (`/log-place`)

Found somewhere the venue table doesn't have? Search for it by name, or drop a
pin on the map yourself, then tick what it offers and describe anything else
worth knowing. The dashboard lists your submissions, with edit and remove.

**Searching by name uses Google Places, not geocoding.** Geocoding is
address-shaped and answers a cafe's name with a street; Places answers "which
place did you mean". Picking a result fills the name, the kind of place, the
area and the pin from one choice. It needs the **Places API** enabled on the
same Google project as the Geocoding API; without a key the search says so when
you try it, and pinning by hand still works.

The page is never disabled based on whether a key is configured. That flag was
read from `os.environ`, which is fixed when the process starts, so adding a key
to `.env` without restarting left the page insisting there was none while
locking a search box that would have worked. The route answers the question at
the moment it is asked, which is the only answer that can be right.

**Submitting comes back here, showing what was stored** rather than redirecting
away: the name, the address the geocoder resolved, the coordinates, the
amenities and the pending badge. A chain is only observable if its output
appears where it was run. Also testable in isolation: the search half has its
own admin page at `/place-search`, so a wrong address can be pinned on the
search or on the form rather than guessed at.

Two things worth knowing about how it works:

- **A submission never becomes searchable on its own.** It is stored with
  `source="user_submitted"`, and `db.VERIFIED_SOURCES` covers only `curated`
  and `municipal_open_data`, so it appears on your own dashboard and in no
  search or plan until an admin promotes it. Editing your own entry cannot
  change `source`, so a parent can't publish their own guess. The admin page
  for reviewing the queue does not exist yet.
- **The map uses Leaflet with OpenStreetMap tiles, not Google.** Every Google
  embedding option needs the API key in the browser, and this app keeps all its
  keys server-side. The pin's coordinates are something the browser already
  has; turning them into an area name still goes through the server, so the
  Google Geocoding key never moves. Leaflet is vendored in
  `static/vendor/` rather than loaded from a CDN, keeping the property that
  every script the app serves is its own.

A pinned location beats geocoding the name, and not as an optimisation: a
playground or a park building has no address to look up, so its coordinates are
the only thing that locates it.

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
(git-ignored runtime data).

**The conversation follows you around the site.** The widget is on every page,
so navigating used to destroy it: it is a script, and a page load starts a new
one. The transcript is mirrored into `sessionStorage` and replayed on load, so
closing the panel, moving to another page and reloading all keep it, and
**"End chat" is the only thing that clears it**.

It is replayed as data through the same render functions that drew it the first
time, not as saved markup: restored HTML would arrive without its citation and
button listeners, and putting stored text back through `innerHTML` is exactly
the shape the trip page was rewritten to remove. So a restored answer still
opens its sources and its buttons still work. A row of choices already clicked
is remembered as answered and not offered again, while one never answered comes
back live, which is what it was on the page.

`sessionStorage`, not `localStorage`, because the workflow state in there
belongs to one transcript: shared between tabs, two half-filled forms would
answer each other's questions. The cost is that closing the tab ends the chat.

## AI Agent

**The chat bubble is this agent's interface.** A message from the bubble goes
to a tool-calling agent built with
[LangGraph](https://langchain-ai.github.io/langgraph/)'s `create_react_agent`
over an OpenRouter-backed model (`src/agent.py`), which decides *what to do*
with it. It picks between four tools, each a thin wrapper around code that
already powers the rest of the site rather than new logic:

| Tool | Wraps | For |
| --- | --- | --- |
| `answer_faq_tool` | `ask_website_chatbot` | questions about the site, with `[Source N]` citations |
| `extract_form_tool` | the Form Extractor | a described day, turned into the planning form |
| `plan_trip_tool` | the Plan Trips component | building an itinerary, only when explicitly asked |
| `find_nearby_tool` | `find_nearby` | somewhere kid-friendly right now |

The last two overlap, so the system prompt gives the extractor priority: a
parent describing a day they want always fills the form first, even when it
sounds like a request for a plan, because the point is that they see what was
read from their words before a day is built on it. `plan_trip_tool` fires only
when they ask for the itinerary outright. It is a prompt-level rule rather than
a guarantee, so the workflow test page reports which tool actually ran.

Answering questions is now one tool among several rather than a separate code
path, so there is a single implementation behind the bubble and the admin test
page. Each tool hands back both a short line for the model and the real
structured result for the caller, because LangGraph otherwise JSON-stringifies
a returned dict and the caller only gets text.

The test page at `/agent` therefore has **no chat of its own**: it watches real
bubble traffic and shows which tool ran, what it returned, and the tokens and
timing. What is tested is what a parent gets. Its model dropdown is the
bubble's, which makes it the place to check whether a given model can call
tools at all.

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
  commit message -- `call_openrouter`/`src/agent.py` only ever read it from
  `os.environ`, and it's never logged or printed.

## Intent routing

Before the agent sees a message, a small classifier (`src/intent.py`) checks it
against the workflows a chat message could actually trigger, and returns one
name or `none`. A match runs that workflow; everything else falls through to
the tool-calling agent above, unchanged. The classifier is a cheap pinned model
with a strict enum schema, and its answer is re-checked against the offered
names, so a hallucinated workflow becomes `none` rather than a crash.

Every decision is appended to `data/intents.jsonl` (git-ignored) and the reply
carries the name that ran, which the bubble shows as a badge: `⚙️ <workflow>`
or `💬 no workflow`. Two routers coexist deliberately: the classifier owns
workflows and runs first, the agent owns everything else.

A workflow that raises does not cost the parent their turn. It is logged as
routed-but-not-run and the message falls through to the agent, so the trace can
show routing that was right where execution was not.

## Filling the form by talking

Saying "plan a trip" in the bubble starts a conversation rather than a single
extraction. The assistant offers the two ways to plan, and if the parent picks
chat it **asks for everything at once**:

> Tell me about your day, whatever you know: which city, how old your little
> one is, what time their day starts and bedtime, nap time and how long it
> lasts. All in one message is fine.

One open question, not the first of five. Interviewing a parent field by field
is the form again, only slower, and a day is something they can describe in a
sentence. **Only what is missing gets a follow-up**, so "Vancouver, she's 2, up
at 7 and bed at 7:30" is answered with the nap question alone. It finishes with
**"Is there anything else we need to know?"**, because the useful things a
parent knows about their own child are the ones no field thought to ask for.

The follow-ups are shaped by what their answer can be. The city offers
**Vancouver** as a button, taken from the venue data rather than written as a
literal, since that is the only city the app has anything to plan in. Naps ask
for the time and the length together: a nap time with no length is half an
answer, and asking twice for one fact is worse than asking once for both.

Any question can be **declined**, which marks it asked and moves on. Without
that, a question the parent cannot answer repeats forever in the same words:
the extractor finds nothing, so the field stays missing, so it is asked again.
Naps are the field this is really for, since a child can genuinely not have
one, so "she doesn't nap anymore" is recognised as an answer and not just a
plain "no".

**The extractor runs on every message**, not once. Each turn it reads whatever
was just said and merges it into the form built up so far. The merge is the part
that matters: `extract_form` returns a *complete* form plus a `found` list of
what that message actually supplied, so only fields in `found` may overwrite.
A plain dict merge would let the second answer reset the first one's
destination back to its default.

**Notes are the exception: they accumulate rather than replace.** Every other
field holds one value a later answer corrects, but a note is something a parent
adds to, and "she needs a highchair" does not retract "she hates loud places".
Since the extractor reads one message at a time and cannot know what was said
earlier, `nap_notes` and `extra_notes` are appended to, with a repeat of
something already in there dropped. The fragments come back sentence-shaped, so
joining them reads as prose without reformatting. `accommodation` is free text
too but is deliberately left out: it is a value, and saying where you are
staying twice is a correction.

Once the four are there, the whole form is shown split into what came from the
parent's words and what is riding on a default, values included, so nothing
reaches the planner unseen. Anything other than "yes" is treated as a
correction and goes back to collecting, so "make it four stops" edits the form
rather than ending the conversation. Nothing is handed over before they confirm.

**The chatbot never generates the day.** The confirmed form is POSTed to
`/plan`, which plans it exactly as it always has: either prefilled for checking
(a `prefill` marker tells the route to fill the boxes and stop) or straight to
Generate. That keeps one planner rather than two: a generated plan is 2.5-4.5KB
and would not survive Flask's ~4KB session cookie, the AI adjuster is not
deterministic so the chat and the page would show different days, and generating
here would mean duplicating a sixteen-argument call.

Both buttons post in **this** tab, not a new one, and say that they are working
while they do. Generating is a real AI call of ten seconds and up: opened in a
background tab that is a blank page with nothing to explain itself, which is
indistinguishable from a button that did nothing. Here the browser's own
loading indicator does the explaining, and leaving the page costs nothing now
that the transcript survives navigation. The buttons are locked with a class
rather than `disabled`, because disabling the submitter mid-submit can drop its
name from the post, and "Open the form" is nothing but its name.

While a flow is in progress **the classifier is skipped entirely**. Mid-flow,
"two year old" and "yes" are answers to the question just asked, not new
intents, and routing them would derail the conversation. The state travels with
the transcript in the browser, the same grain as the chat history: no cookie
ceiling, no clash between tabs, works for a visitor who is not logged in, and
it dies with "End chat" rather than outliving it.

The widget carries the flow: an avatar beside each assistant message, a greeting
on first open with **"What's Travel with Tots?"** and **"Plan a trip"** as
one-tap openers, and offered choices rendered as buttons that send the same text
a parent would have typed, so both take one path through the server. "Plan a
trip" stays on offer under each answer until the form-filling flow has actually
run, since planning is what most parents come for and a greeting-only chip
scrolls away after a question or two.

## Finding somewhere nearby, from the chat

Asking the bubble "find the nearest nursing room" runs the **Find a nearby
place** workflow: the need is read from the message, the Find Nearby component
searches, and each place comes back as a card with a working
**📍 Open in Google Maps** link.

It used to answer **💬 no workflow**, and that badge was correct rather than
broken. Nothing nearby-shaped was registered, so the classifier was offered a
one-item menu, rightly said `none`, and the message fell through to the agent's
`find_nearby_tool`, which called `interactions.find_nearby`, the deterministic
placeholder. No location, no distance, no web fallback, and the real component
never ran.

**The need is read by keyword, not by a model.** Six fixed categories with
distinctive words is work code does, and the order is the whole point: "a quiet
place to feed the baby" is a nursing room, not a quiet spot. When the words
match nothing it asks, offering the six need buttons, and it asks only once.

**Location is offered, never demanded.** The widget attaches coordinates to a
message only when permission has already been granted, checked through the
Permissions API, which reports the state without prompting. Opening a page
therefore never raises a location prompt. Without coordinates it still answers
from the curated Vancouver venues and adds a **📍 Use my location** button that
re-asks the same question, this time with somewhere to measure from. With them,
the component ranks by real distance.

**Links are rendered from the place records, not from the reply text.** Nothing
parses model prose for URLs; the `href` is always a value this app produced.
The widget checks it against an `^https?://` allowlist first, the same rule
`templates/trip.html` uses, and a URL that fails **loses its link rather than
being rendered**, which matters because a web-fallback result carries a URL
nobody here chose. A web result is labelled `🔗 Open result` rather than
claiming to be a place on a map.

**One implementation behind all three entry points.** The workflow, the agent's
tool (the safety net for a phrasing the classifier misses) and the trip page's
need panel all call the same component. The tool returns its places as a
LangGraph artifact, so the agent's answer renders the same cards the workflow's
does. The trip page's no-location branch used to report `source: "curated"`
without having consulted anything; it now reports the source it actually used.

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

## Form Extractor

An admin-only, isolated test page (`/extract-form`, linked from `/components`)
for reading a parent's own words into the planning form, so they can describe
their day instead of filling in boxes. `src/components/extract_form.py` is
self-contained, one file for the whole component.

The model proposes and the real validator decides: every value it returns goes
through the same `form_helpers.read_form` the `/plan` route uses, so the
clamps, the five-years-zero-months age cap, and the four-nap ceiling are
enforced once, in one place. A model answering `stop_count: 40` yields `6`
rather than reaching the planner. Values outside a fixed vocabulary (transit,
dining, features, themes, transit_nap) are dropped rather than passed on.

It reports which fields the description actually supplied, and the page marks
everything else as a default. That is deliberate: a form quietly filled with
guesses is worse than a form you can see is incomplete, because nobody checks
a field they think came from what they wrote.

Free text is a first-class part of the job, not an afterthought. Anything a
parent said that no structured field can hold goes into `extra_notes`, and
anything about sleep goes into `nap_notes`, both of which already render into
the planner's prompt. Prose a structured field already captured is *not*
repeated there, so the planner never reads the same constraint twice.

Reachable from the chat bubble: this component is what the "Fill the form from
a chat message" workflow calls on every turn (see `/workflows`). That chain ends
at the filled form on purpose, so a description never becomes a finished
itinerary without the parent seeing what was read from it.

It pins its own model rather than using the app default, which is OpenRouter's
free auto-router: the router advertises structured outputs but picks a
different model per request, and measured live it honoured the schema only
about half the time.

The pin is a paid non-reasoning model, chosen by measurement. A free reasoning
model was tried first and replaced: on the same description it spent 3.2k-4.5k
tokens, mostly reasoning, over 25-75s, and found fewer fields than the current
model does in about 2s on roughly 130 tokens. It also failed outright near the
free-tier ceiling, where the reasoning consumed the whole reply and the content
came back empty. At about $0.0003 a call, the paid model buys latency a parent
will wait through and a result that does not change between identical requests.

Naps are the field this component has to get right, and it used to invent their
length: the schema required `duration_min` as a plain integer, so a model under
strict mode had to supply a number even when the parent gave none, producing 15
minutes one run and an hour the next. It is nullable now, so "they didn't say"
is expressible, and the assumed hour comes from
`form_helpers.ASSUMED_NAP_DURATION_MIN` instead of from the model's guess.

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
│   ├── agent.py                   # AI Agent: intent routing + LangGraph tool-calling over OpenRouter
│   ├── intent.py                  # intent classifier: a message to a workflow name, or none
│   ├── components/
│   │   ├── plan_trip.py           # Plan Trips component: rule-based draft + AI smoothing
│   │   ├── replan_trip.py         # Replan a Trip component: rule-based replan + AI smoothing
│   │   ├── extract_form.py        # Form Extractor component: a description into the planning form
│   │   ├── find_nearby.py         # Find Nearby component: location-narrowed venues, search fallback
│   │   ├── geocode.py             # Geocode component: coordinates/address to city + neighbourhood
│   │   ├── place_search.py        # Place Search component: Google Places text search
│   │   └── search_web.py          # Web Search component: Tavily Search API
│   ├── rag.py                     # chunking, embeddings, and retrieval for the chatbot
│   ├── results.py                 # saves/reads thumbs up/down ratings, by kind (chatbot/plan/replan)
│   ├── workflows/                 # one file per workflow, each chaining components
│   │   ├── nap_time_rescue.py     # replan around a long nap, substitute closed stops
│   │   ├── log_a_place.py         # log a place from the map, held for admin verification
│   │   ├── plan_from_chat.py      # fill the planning form over a few chat messages
│   │   └── find_nearby_place.py   # answer "somewhere nearby" from the chat, with Maps links
│   └── prompts/
│       ├── website_chatbot.txt    # chatbot system prompt
│       ├── extract_form.txt       # form extractor system prompt
│       ├── intent.txt             # intent classifier system prompt
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
│   ├── plan_from_chat.html        # admin: fill-the-form-from-chat workflow test page
│   ├── ai_agent.html              # admin: isolated AI Agent test page (/agent)
│   ├── search_web.html            # admin: isolated Web Search test page (/search-web)
│   ├── plan_trip.html             # admin: isolated Plan Trips test page (/plan-trip)
│   ├── replan_trip.html           # admin: isolated Replan a Trip test page (/replan-trip)
│   ├── find_nearby.html           # admin: isolated Find Nearby test page (/find-nearby)
│   ├── extract_form.html          # admin: isolated Form Extractor test page (/extract-form)
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
│   ├── extract-form.js            # Form Extractor test page's run behaviour
│   ├── plan-from-chat.js          # fill-the-form page: watches chat replies
│   ├── stop-render.js             # shared stop-list rendering for plan-trip.js/replan-trip.js
│   ├── geolocate.js               # shared browser-geolocation request, with its guards
│   ├── log-a-place.js             # Log a Place page: the pin map and its form
│   ├── place-search.js            # Place Search test page's run behaviour
│   ├── vendor/leaflet.js          # Leaflet 1.9.4 (BSD-2-Clause), vendored not CDN
│   ├── vendor/leaflet.css         # Leaflet's stylesheet
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
│   ├── test_components_extract_form.py # unit tests for the Form Extractor
│   ├── test_form_helpers.py       # unit tests for form parsing/child resolution
│   ├── test_interactions.py       # unit tests for replan()/find_nearby()
│   ├── test_dates.py              # unit tests for compute_age
│   ├── test_geo.py                # unit tests for haversine distance
│   ├── test_workflows.py          # unit tests for workflow declarations + page
│   ├── test_llms.py               # unit tests for the agent's tools + chat contract
│   ├── test_workflow_plan_from_chat.py # unit tests for the fill-the-form workflow
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
| `/log-place`                   | GET, POST | Log a Place page (map + form), and the submission             |
| `/log-place/area`              | POST     | Pin coordinates to a readable area (server-side geocoding)    |
| `/log-place/search`            | POST     | Find a place by name (server-side Google Places)              |
| `/place-search`, `/place-search/run` | GET, POST | Admin: isolated Place Search component test page + run |
| `/edit-place/<id>`, `/delete-place/<id>` | POST | Correct or remove one of your own logged places       |
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
| `/agent`                        | GET      | Admin: AI Agent test page, watches real chat-bubble traffic      |
| `/workflows/plan-from-chat`     | GET      | Admin: fill-the-form-from-chat workflow test page               |
| `/search-web`, `/search-web/run`, `/search-web/key` | GET, POST | Admin: isolated Web Search test page, run a query, save the API key |
| `/plan-trip`, `/plan-trip/run`  | GET, POST | Admin: isolated Plan Trips component test page + run (JSON out) |
| `/replan-trip`, `/replan-trip/run` | GET, POST | Admin: isolated Replan Trip component test page + run (JSON out) |
| `/find-nearby`, `/find-nearby/run`, `/find-nearby/key` | GET, POST | Admin: isolated Find Nearby test page, resolve a location + find places, save the Maps key |
| `/extract-form`, `/extract-form/run` | GET, POST | Admin: isolated Form Extractor test page, a description in, a filled form out |

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
