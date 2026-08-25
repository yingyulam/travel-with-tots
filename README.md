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
- The AI adjuster has **three** outcomes, not two: it improves the day, it reads
  the day and leaves it alone, or the call fails. The middle one used to be
  reported as *"couldn't fine-tune it right now"*, which made an adjuster that
  agreed with the plan sound broken. It now says **"This is already the best
  plan for your day. No changes needed."** A real failure still says so, because
  what is shown then is the rule-based plan and claiming the AI approved it
  would be a lie. `plan_trip` and `replan_trip` report both `adjusted` (did the
  step run) and `changed` (did it move anything); `changed` comes free from the
  per-stop `adjusted` marks the agent already sets.

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

Found somewhere the venue table doesn't have? Search by name or drop a pin, tick
what it offers, describe anything else worth knowing. The dashboard lists your
submissions, with edit and remove. Reachable from the chat too, via the "Log a
place" workflow.

- **Search uses Google Places, not geocoding.** Geocoding is address-shaped and
  answers a cafe's name with a street; Places answers "which place did you
  mean", and picking a result fills the name, kind, area and pin from one
  choice. It needs the **Places API** on the same Google project as Geocoding;
  without a key the search says so and pinning by hand still works.
- **A pinned location beats geocoding the name**, and not as an optimisation: a
  playground or a park building has no address to look up, so its coordinates
  are the only thing that locates it.
- **Submitting comes back here showing what was stored**, rather than
  redirecting away: the name, the resolved address, the coordinates, the
  amenities and the pending badge. A chain is only observable if its output
  appears where it was run.
- **A submission never becomes searchable on its own.** Stored with
  `source="user_submitted"`, and `db.VERIFIED_SOURCES` covers only `curated` and
  `municipal_open_data`, so it shows on your own dashboard and in no search or
  plan until an admin promotes it. Editing your own entry cannot change
  `source`, so nobody publishes their own guess. The review queue page does not
  exist yet.
- **The map is Leaflet with OpenStreetMap tiles, not Google.** Every Google
  embedding option needs the key in the browser, and this app keeps all keys
  server-side; turning a pin into an area name still goes through the server.
  Leaflet is vendored in `static/vendor/` rather than loaded from a CDN, so
  every script the app serves is its own.

The page is never disabled based on whether a key is configured. That flag came
from `os.environ`, fixed when the process starts, so adding a key to `.env`
without restarting left the page insisting there was none while locking a search
box that would have worked. The route answers at the moment it is asked, which
is the only answer that can be right.

Admin accounts (`is_admin` on `parents`) get extra pages:

| Page | Purpose |
| --- | --- |
| `/settings` | Edit the chatbot's knowledge base and system prompt; saving re-indexes in the background. |
| `/chunks` | Inspect how the knowledge base was chunked; re-run at a different size. |
| `/results` | Browse rated chatbot replies and AI plans, each session with its own stats. |
| `/components` | Inventory of the building blocks, each with an isolated test page. |
| `/workflows` | End-to-end use cases, each chaining those components. |

## AI chatbot

A floating widget on every page answers questions about how the site works, via
[OpenRouter](https://openrouter.ai). Every OpenRouter call in the app has a
timeout and retries once on an empty or malformed reply, which free-tier models
occasionally return under load.

Answers are grounded with retrieval-augmented generation rather than the model's
own guesses:

1. `data/knowledge_base.md` is split into ~128-token chunks, keeping related
   sentences together.
2. Each chunk is embedded with `sentence-transformers` (`all-MiniLM-L6-v2`) into
   a local [ChromaDB](https://www.trychroma.com) index (`data/chroma/`,
   rebuildable, git-ignored).
3. A question retrieves the top 3 chunks with scores; the model answers only
   from those and cites them inline as `[Source N]`, clickable to show the text.
4. First-time indexing, or a re-chunk from the admin page, runs in the
   background behind a progress animation.

Every reply takes a 👍/👎, which saves the question, answer, model, timestamp,
response time and token counts to `data/results.json` (git-ignored).

## The chat widget

- **The conversation follows you around the site.** The widget is a script, so
  a page load used to start a new one and lose everything. The transcript is
  mirrored into `sessionStorage` and replayed on load; closing the panel,
  navigating and reloading all keep it, and **"End chat" is the only thing that
  clears it**.
- **Replayed as data, not as saved markup.** Restored HTML would arrive without
  its citation and button listeners, and putting stored text back through
  `innerHTML` is the shape the trip page was rewritten to remove. So a restored
  answer still opens its sources and its buttons still work. A choice row
  already clicked is remembered as answered; one never answered comes back live.
- **`sessionStorage`, not `localStorage`**, because the workflow state in there
  belongs to one transcript: shared between tabs, two half-filled forms would
  answer each other's questions. The cost is that closing the tab ends the chat.
- **The panel resizes.** Drag the grip in its **top-left** corner, the one with
  room to move, since the panel is anchored bottom-right and the opposite corner
  sits under the bubble. Arrow keys do it without a pointer, and double-click
  resets. The size is a preference rather than conversation state, so it lives in
  `localStorage`, and it is clamped both when set and when restored: a floor so
  the input row stays usable, a ceiling of the current window so a size dragged
  on a wide screen does not reopen off the edge of a narrow one.
- **The message box grows with the message**, one line to start, then it stops
  and scrolls so a long paste cannot push Send off the panel. Being a textarea,
  **Enter sends and Shift+Enter starts a new line**, which the placeholder says
  because a textarea's Enter is a newline by default. The height is cleared
  before it is measured, or it could only ever grow: `scrollHeight` reports the
  box it is already in, not the text inside it.

## AI Agent

**The chat bubble is this agent's interface.** A message goes to a tool-calling
agent built with [LangGraph](https://langchain-ai.github.io/langgraph/)'s
`create_react_agent` over an OpenRouter model (`src/agent.py`), which decides
*what to do* with it. Its four tools are thin wrappers around code that already
powers the rest of the site:

| Tool | Wraps | For |
| --- | --- | --- |
| `answer_faq_tool` | `ask_website_chatbot` | questions about the site, with `[Source N]` citations |
| `extract_form_tool` | the Form Extractor | a described day, turned into the planning form |
| `plan_trip_tool` | the Plan Trips component | building an itinerary, only when explicitly asked |
| `find_nearby_tool` | `find_nearby` | somewhere kid-friendly right now |

The last two overlap, so the prompt gives the extractor priority: a described day
fills the form first, even when it sounds like a request for a plan, because the
point is seeing what was read from your words before a day is built on it.
`plan_trip_tool` fires only when asked outright. That is a prompt-level rule
rather than a guarantee, which is why the test page reports the tool that ran.

Each tool hands back both a short line for the model and the real structured
result for the caller, because LangGraph otherwise JSON-stringifies a returned
dict and the caller gets only text.

`/agent` has **no chat of its own**: it watches real bubble traffic and shows
which tool ran, what it returned, and the tokens and timing. What is tested is
what a parent gets. Its model dropdown is the bubble's, which makes it the place
to check whether a given model can call tools at all.

**Getting an OpenRouter API key** (one key covers the chatbot, the agent and the
AI planner):

- Sign up free at [openrouter.ai](https://openrouter.ai), then create a key at
  [openrouter.ai/keys](https://openrouter.ai/keys). It is shown once.
- Paste it into `.env` as `OPENROUTER_API_KEY=<your key>`, copying
  `.env.example` first if you have not already.
- The default free model needs no credit. A paid model (GPT-4o mini, Claude
  Sonnet 5) needs credit added under
  [openrouter.ai/settings/credits](https://openrouter.ai/settings/credits).
- Never commit `.env` or paste a real key into a prompt, screenshot or commit
  message. `call_openrouter` only ever reads it from `os.environ`, and it is
  never logged or printed.

## Chat workflows

Four things the chat can do beyond answering questions. Each collects a request
over a few messages and then hands it to whatever already does the work, rather
than doing it a second time.

| Workflow | Started by | Ends at |
| --- | --- | --- |
| Plan a day | "plan a trip" | `/plan`, prefilled or straight to Generate |
| Find somewhere nearby | "find the nearest nursing room" | answered in the chat |
| Replan on the go | "we need to replan" | the in-trip page's own replan |
| Log a place | "log this place" | `/log-place`, prefilled or stored |

### Rules they all share

- **Routing.** A cheap pinned classifier (`src/intent.py`) returns one workflow
  name or `none` from a strict enum, re-checked against the names actually
  offered so a hallucinated one becomes `none` rather than a crash. Every
  decision is appended to `data/intents.jsonl`, and the reply carries the name
  that ran, shown as `⚙️ <workflow>` or `💬 no workflow`.
- **A workflow that raises does not cost a turn.** It is logged as
  routed-but-not-run and falls through to the agent, so the trace can show
  routing that was right where execution was not.
- **Mid-flow the classifier is skipped.** "Two year old" and "yes" are answers
  to the question just asked, not new intents. The state travels with the
  transcript in the browser: no cookie ceiling, no clash between tabs, it works
  for a visitor who is not logged in, and it dies with "End chat".
- **Leaving is one tap.** Every open turn offers **✕ Cancel**, and "cancel" or
  "actually never mind" do the same. Checked *before* dispatch, so every
  workflow gets it without remembering to, and it reaches neither the workflow
  nor the classifier. Matched against the whole message, so "stop by the park
  at 3" stays a description of a day.
- **A button is a message.** An offered choice sends exactly its own label, so
  a tap and a typed answer take one path through the server. Labels come from
  the server, because a label it cannot parse is a button that does nothing.
- **Nothing is asked of a model that code can answer.** Needs, features,
  situations and durations are fixed vocabularies matched by keyword; the model
  decides the routing and writes the prose.

### Plan a day

Asks for **everything at once**, then follows up only on what is missing, so
"Vancouver, she's 2, up at 7 and bed at 7:30" is answered with the nap question
alone. Interviewing field by field is the form again, only slower.

**Including the very first message.** It used to be skipped, on the reasoning
that an opening message is only ever an intent and extracting would waste a
call. A parent who opens with their whole day had all of it discarded and had to
type it again. Now the opening turn reads the message: nothing usable in it
still gets the two ways to plan, and a described day skips the offer and carries
on from what was said.

Replan on the go does the same, but only when a *specific* situation was named:
"she napped 90 minutes" is read, "we need to replan" still gets the six chips.
**Log a place deliberately does not.** Its reader is a comma split with no way to
tell a place name from a sentence about wanting to log one, so reading the
opening message would store "I want to log a place" as a venue. The planning chat
can only do this because an extractor decides what is a destination and what is
noise; one extra question is cheaper than a junk row.

- **Asked for:** city, age, wake-up, bedtime, naps. Everything else rides on a
  default and is shown before anything is handed over.
- **The city offers Vancouver as a button**, taken from the venue data rather
  than written as a literal, since it is the only city there is anything to
  plan in.
- **Naps ask for the time and the length together**, because a nap time with no
  length is half an answer.
- **Any question can be declined**, which marks it asked and moves on. Without
  that, a question the parent cannot answer repeats forever in the same words,
  so "she doesn't nap anymore" is a real answer rather than a non-answer.
- **Ends with "Is there anything else we need to know?"**, because the useful
  things a parent knows are the ones no field thought to ask for.

**The extractor runs on every message**, and the merge is the part that matters:
`extract_form` returns a *complete* form plus a `found` list of what that
message supplied, so **only fields in `found` may overwrite**. A plain dict
merge would let the second answer reset the first one's destination.

**Notes accumulate rather than replace.** A note is something a parent adds to:
"she needs a highchair" does not retract "she hates loud places". Repeats are
dropped. `accommodation` is free text but excluded, being a value rather than a
note.

**It never generates the day.** The confirmed form is POSTed to `/plan`, which
plans it as it always has. One planner rather than two, because a generated plan
is 2.5-4.5KB and would not survive Flask's ~4KB session cookie, the AI adjuster
is not deterministic so the two would disagree, and it would mean duplicating a
sixteen-argument call.

Both buttons post in **this** tab and say they are working while they do:
generating is a real AI call of ten seconds and up, and a blank background tab
is indistinguishable from a button that did nothing. They are locked with a
class rather than `disabled`, since disabling the submitter mid-submit can drop
its name from the post, and "Open the form" is nothing but its name.

### Find somewhere nearby

Reads the need from the message, searches, and returns each place as a card with
a working **📍 Open in Google Maps** link.

- **The need is read by keyword**, and the order is the point: "a quiet place to
  feed the baby" is a nursing room, not a quiet spot. Unrecognised, it asks once
  with the six need buttons.
- **Location is offered, never demanded.** Coordinates ride along only when
  permission was already granted, checked through the Permissions API, which
  reports the state without prompting, so opening a page never raises one.
  Without them it still answers from the curated venues and offers **📍 Use my
  location**, which re-asks the same question with somewhere to measure from.
- **Links come from the place records, never from model prose.** The `href` is
  always a value this app produced, checked against an `^https?://` allowlist
  first, and a URL that fails **loses its link rather than being rendered**,
  because a web result carries a URL nobody here chose. Those are labelled
  `🔗 Open result` rather than claiming to be a place on a map.
- **One implementation behind three entry points:** this workflow, the agent's
  tool (the safety net for a phrasing the classifier misses) and the trip page's
  need panel all call the same component.

Two bugs worth remembering, both from the same root. The chat once skipped the
Geocode component and passed the supported city whatever the coordinates said,
so a parent in Richmond got Vancouver venues described as "near you" and the web
fallback could never fire; `resolve_location` now lives in the Geocode component
and every caller uses it. And a location that resolves to **nothing** means the
city this app covers, not the whole web: asked for a restaurant with no
location, the test page once answered with Austin, Texas.
`find_nearby.searchable()` applies that default in one place.

### Replan on the go

Offers the six situations the in-trip page already has as buttons, tapped or
described: "she napped 90 minutes" reads as a long nap with the duration lifted
out of the sentence. Anything unrecognised still replans, carrying what was
typed as the note, exactly as that page's free-text box does.

- **It does not replan.** The in-trip page holds the plan, its versions and the
  clock, and `runReplan` there is the one implementation. The request travels as
  a `twt:replan-request` event, the mirror of the `twt:chat-reply` event going
  the other way, so the new version lands in that page's version switcher.
- **It asks whether a trip is open**, through `on_trip` in the request context.
  Without a started day there is nothing to reshape, so it says where to find
  one rather than collecting a situation it cannot act on.
- **One button, not two.** An earlier draft confirmed with a chip and *then*
  offered a Replan button, which is two controls for one decision. Restoring the
  transcript brings the button back but fires nothing, so a navigation cannot
  replay a stale replan.

This was **"Nap-time rescue"**, a declaration with no `run()` naming one
situation out of seven, when a closed stop, rain or wanting to stay put are the
same request and the component already handled all of them.

### Log a place

Collects what it is called and roughly where, what it offers, and anything else
worth knowing. Only the name is required, matching the single thing the storage
path validates.

- **A place is several things at once**, so the features question takes as many
  as apply. "Family room and nursing room" ticks both: the matcher collects
  every label it finds rather than stopping at the first. The chips **toggle
  rather than send**, and ✓ Done sends the picked labels as one message, so a
  tapped answer reads identically to a typed one.
- **The chat does not store it.** The values are posted to `/log-place`, with
  **📝 Open the form** to check the map pin or **📌 Log it** to submit. The
  bubble is on every page and is not logged in, while a submission needs an
  owner to be editable and to appear on a dashboard, and only the real page has
  the map. A form post carries the session, so an anonymous visitor is sent to
  log in rather than losing what they typed.

It used to answer **💬 no workflow**, and not because the classifier misjudged
it: `runnable_message_workflows()` filters on `trigger == "message"` and this
one was `"event"`, so its name was never in the enum at all. Flipping that alone
would have crashed, since `run(parent_id, values)` is not the message contract.
The storage path is now **`store()`**, which is what it does, and `run` is the
conversation.

### Watching one run

Every workflow has a test page with the same two controls. **▶ Run once** arms
the page so the next message sent in the chat bubble is captured and shown, then
disarms itself; **👂 Listen** keeps capturing until stopped. The bubble stays
the input on purpose: what the page shows is then what a parent really gets,
rather than a canned sample travelling a code path nobody uses.

**Arming a page routes messages to its workflow.** ▶ Run and 👂 Listen both do
that, so a page can always exercise the thing it tests. Without it a page could
not reach its own workflow at all when the classifier preferred another, which is
a problem the course's example never has: MoneyClaw reads Gmail, so its data
source and its agent interface are different objects. Here the chat is both.

Forcing grants nothing new. A parent can already trigger any workflow by typing
the right words, and the name is re-checked against the registry the same way the
classifier's own answer is. Precedence is **cancel, then in-flight, then forced,
then classify**: mid-conversation "Vancouver" is an answer to the question just
asked, so forcing must not restart the flow every turn. Forced turns are flagged
in `data/intents.jsonl`, because that file is what classifier accuracy is
measured from and these never went near the classifier.

**"Once" is one execution, not one turn.** A conversational workflow asks
follow-up questions, so Run holds through them and lets go when the workflow
finishes; Listen carries straight into the next run and stops only on a click.
`conversation` going null is the signal, and it covers an abandoned run too. For
a single-turn workflow like Find a nearby place, Run captures one answer while
Listen keeps answering, so the two stay distinct everywhere.

A captured turn shows what was said, the reply, and whatever that workflow
collected. **A page only ever shows its own workflow's turns**, so a foreign one
leaves a single line naming where it went rather than another workflow's
conversation. The machine behind both buttons lives once, in
`static/workflow-watch.js`; each page keeps only its own rendering.

## Web Search

`src/components/search_web.py`, one file, with its own admin page at
`/search-web`: a query box, a **Run** button, and the top 5
[Tavily](https://tavily.com) results as title, URL and snippet. Also Find
Nearby's fallback when the curated venue table has nothing.

Tavily, not Brave: Brave killed its free Search API tier in February 2026, and
its "identity verification" card is now an active billing instrument, charged
automatically past $5 a month with no cap.

**Getting a Tavily API key:**

- Sign up at [tavily.com](https://tavily.com), no credit card, and copy the key
  from your [dashboard](https://app.tavily.com/home).
- Paste it into the Web Search page and click **Save Key**: that writes `.env`
  via `python-dotenv`'s `set_key` and works immediately, no restart. Editing
  `.env` by hand as `TAVILY_API_KEY=<your key>` does the same.
- The free plan gives 1,000 searches a month. Requests stop when exhausted;
  they never bill you.
- Never commit `.env` or share the key. It is only read from `os.environ`, and
  never logged, printed, or sent to the browser once saved.

## Form Extractor

`src/components/extract_form.py`, one file, with its own admin page at
`/extract-form`: a parent's own words into the planning form, so they can
describe a day instead of filling in boxes. It is what the "Plan a day"
workflow calls on every turn.

- **The model proposes, the real validator decides.** Every value goes through
  the same `form_helpers.read_form` the `/plan` route uses, so the clamps, the
  five-years-zero-months age cap and the four-nap ceiling are enforced once. A
  model answering `stop_count: 40` yields `6`. Values outside a fixed vocabulary
  are dropped rather than passed on.
- **It reports which fields the description actually supplied**, and everything
  else is shown as a default. A form quietly filled with guesses is worse than
  one you can see is incomplete, because nobody checks a field they believe came
  from their own words.
- **Free text is part of the job.** Whatever no structured field can hold goes
  to `extra_notes`, anything about sleep to `nap_notes`, both of which reach the
  planner's prompt. Prose a structured field already captured is not repeated
  there, so the planner never reads one constraint twice.

**It pins its own model**, and the pin was chosen by measurement:

- The app default is OpenRouter's free auto-router, which advertises structured
  outputs but picks a different model per request, and honoured the schema
  about half the time when measured.
- A free reasoning model was tried and replaced: on the same description it
  spent 3.2k-4.5k tokens over 25-75s and found *fewer* fields than the current
  pin does in ~2s on ~130 tokens, and near the free-tier ceiling the reasoning
  consumed the whole reply and the content came back empty.
- At ~$0.0003 a call, the paid non-reasoning pin buys latency a parent will wait
  through and a result that does not change between identical requests.

**Naps are the field it had to stop guessing at.** `duration_min` was a plain
integer, so under strict mode a model had to invent a number when the parent
gave none: 15 minutes one run, an hour the next. It is nullable now, so "they
didn't say" is expressible, and the assumed hour comes from
`form_helpers.ASSUMED_NAP_DURATION_MIN`.

## Find Nearby

"Somewhere kid-friendly near us, right now", on its own admin page
(`/find-nearby`) and behind the in-trip page's **Need something now?** panel.
Two components, one job each:

- **`geocode.py`** turns a location into a place name via the Google Geocoding
  API. Optional for "use my location", since the browser's free
  `navigator.geolocation` gives coordinates and the venues carry their own, so
  geocoding only adds a readable name. Genuinely required for a typed address,
  which has no coordinates to work from.
- **`find_nearby.py`** does the matching: narrow to the resolved city, or search
  every city when only coordinates are known, rank by real straight-line
  distance (`src/geo.py`), report each `distance_km`, and delegate the need
  matching itself to `interactions.find_nearby()` rather than reimplementing it.
  When curated has nothing it falls back to a Tavily search, tagged
  `source: "search"` so the UI can say where the answer came from.

Two limits that are permanent rather than transitional. Venues without
coordinates fall back to same-neighbourhood-first ordering and report no
distance, and user-submitted venues never get coordinates from a source. And
curated venues are Vancouver-only, so a location elsewhere legitimately returns
nothing curated and falls through to search.

**Optional: a Google Maps API key for address search.** No key is needed to
share your location. It is only needed for the "set a location by hand" box,
since turning typed text into coordinates is exactly what geocoding does.

- In the [Google Cloud console](https://console.cloud.google.com/google/maps-apis/api-list),
  create or pick a project and enable the **Geocoding API**. That one API is all
  this component uses.
- Create a key under **Credentials**, then restrict it: *API restrictions* to
  Geocoding only, *Application restrictions* to IP addresses.
- Paste it into the Find Nearby page and click **Save Key**, or set
  `GOOGLE_MAPS_API_KEY=<your key>` in `.env` by hand.
- Google's recurring monthly credit covers well beyond this app's usage, but the
  Geocoding API does require billing enabled on the project.
- The key is server-side only: never sent to the browser, logged or printed.

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
│   │   ├── replan_on_the_go.py    # shift the rest of the day when something changes
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
│   ├── find_nearby_place.html     # admin: find-a-nearby-place workflow test page
│   ├── log_place_from_chat.html   # admin: log-a-place-from-chat workflow test page
│   ├── replan_on_the_go.html      # admin: replan-on-the-go workflow test page
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
| `/workflows/find-nearby-place`  | GET      | Admin: find-a-nearby-place workflow test page                   |
| `/workflows/log-a-place`        | GET      | Admin: log-a-place-from-chat workflow test page                 |
| `/workflows/replan-on-the-go`   | GET      | Admin: replan-on-the-go workflow test page                      |
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
