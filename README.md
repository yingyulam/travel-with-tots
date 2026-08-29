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
  the day and leaves it alone, or the call fails. `plan_trip` and `replan_trip`
  report both `adjusted` (did the step run) and `changed` (did it move
  anything); `changed` comes free from the per-stop `adjusted` marks the agent
  already sets. **None of it is shown to a parent.** All three outcomes hand
  them a real plan, so which one happened is a fact about our pipeline, not
  something they can act on: the "✨ adjusted" stop badges and the three status
  messages are gone, and the plan and trip pages `console.debug` the same
  detail instead, so it stays visible in development.
- **Revising is the exception.** There the parent asked for one specific
  change, so silence would read as the button doing nothing, and all three
  outcomes still get a message: *"Your plan has been updated."*, *"This is
  already the best plan for your day. No changes needed."*, or *"We couldn't
  update your plan this time."* Each describes what happened to their plan
  rather than which step of ours produced it.

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
  table, nursing room, other) calls `find_nearby(need)`, which returns 1-2
  matching venues, or escalates to web search when the table cannot answer.
  Restaurant always escalates, because the table holds attractions. "Quiet spot"
  is gone: nobody can reliably report quiet, and it changes with the hour and the
  weather, so a soft guess in answer to a specific request is worse than none.

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
- **Logging the same place twice corrects it, rather than queueing a second
  row.** `db.add_or_update_submission` replaces your own earlier submission of
  the same name, so a parent who thought the first attempt had not worked does
  not leave the reviewer a pile of near-identical rows to sort out. Scoped to
  your own `user_submitted` rows: another parent logging the same place gets
  their own row, and a curated venue of that name is never touched.
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
its name from the post, and "Generate my day" is nothing but its name.

**Generating is opt in, and so is storing a place.** `/plan` builds a day only
for a post carrying `generate`, and `/log-place` writes a row only for one
carrying `store`; anything else fills the form in and stops. Each page's own
form carries its flag as a hidden field, so only the chat's primary button
sends it. This was the other way round, a `prefill` flag that turned the
expensive action *off*, which made a minute-long AI call the default for any
post that lost the flag. A submit button's name is exactly what a post loses:
disable the submitter mid-submit, or serve a cached older script, and the safe
action silently becomes the expensive one. Naming the dangerous button instead
means a lost name costs a filled-in form.

### Find somewhere nearby

Reads the need from the message, searches, and returns each place as a card with
a working **📍 Open in Google Maps** link.

- **The need is read by keyword**, and the order is the point: "a quiet place to
  feed the baby" is a nursing room. Unrecognised, it asks once with the need
  buttons.
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

**The fill-the-form page shows both halves, side by side.** Its whole job is
verifying that what the workflow collected becomes the right form fields, and
only the pair is verifiable: the left panel is the workflow's own state with a
badge saying where each value came from, the right is the real form field names
that collection would post. That right panel is built from the same mapping the
chat's own hand-off performs, not a second copy, so the page cannot agree with
itself while disagreeing with what `/plan` receives. The mapping is where a bug
would hide: naps become parallel `nap_start` and `nap_duration` entries,
a checkbox becomes the literal `on`, and an empty value is dropped so the server
falls back to its own default. Being admin-only and used on a desktop, the panels
sit side by side rather than stacking.

Three badges, not two, since Memory landed: a value came **from your words**, was
**remembered** from the child's record or their last saved day, or is on a
**default**. The middle one is the one worth watching, because in the finished
form a recalled value is indistinguishable from one the parent typed. The
hand-off turn carries its provenance too, since it is the one turn with no state
left to read it from.

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
- **A value the description cannot support is dropped.** `read_form` asks
  whether a value is well formed; grounding asks whether the parent said it, and
  both are needed, because a fabricated time is perfectly in range. Times, ages
  and counts need a number in the description ("up at seven" counts, "money"
  does not); a nap needs a mention of sleep, since a nap may legitimately carry
  no clock time; a city has to be named; free text has to share words with what
  was said. The vocabulary fields are deliberately left alone, because "we'll
  drive" is legitimately `car` without sharing a word with it.

  This came from a measured failure. Asked to fill the form from the three words
  "Plan a trip", the pinned model returned up to ten non-null fields, every one
  lifted from an example in the prompt: an age of 1y6m from "My 18-month-old", a
  13:30 nap from "naps at around 1:30 pm". The schema and `read_form` both
  passed them, and the chat then skipped its own opening guidance because it
  believed the parent had already described a day. The prompt now puts the
  description last, marks its examples as examples, and says that all-null is
  the right answer for a description that says nothing.
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

## Memory

`src/memory.py`, one read-only function. `recall(parent_id)` turns a parent into
the durable facts worth reusing, already in the planning form's own shape, so
the chat stops asking for things the app is already holding. A parent whose
child's date of birth is on file was still being asked how old they are.

- **The chat learns who is asking from the session, and only from the session.**
  `parent_id` is what every recall is scoped by, so a client-supplied one would
  read another parent's children and saved trips. No client change was needed:
  the widget's `fetch` is same origin, so the cookie was already arriving and
  only the server was ignoring it. An anonymous chat recalls nothing and behaves
  exactly as before, which matters because the bubble is on every page.
- **`read_form` decides, and a repaired value is not a memory.** The trip row
  goes through the same validator `/plan` uses, so NULL naps, malformed nap
  JSON and the age cap are handled by code that already exists. A field is only
  *remembered* if its stored value survived that unchanged: the `stop_count`
  column still holds legacy words like `"balanced"`, which clamps to `3`, and
  offering that back as the parent's own answer would be worse than not
  remembering it.
- **An age is recomputed, a routine is dated.** The age comes from the date of
  birth on every call, so it cannot go stale; the routine comes from the last
  saved trip and can be months old. They are shown under separate headings for
  that reason, and past a freshness window the clock fields are asked about
  again rather than recalled, because sleep moves every few months at these
  ages and it is what the whole plan is shaped around.
- **It names the child, not just the age.** `/plan` recomputes the age from
  `plan_child_id` on both its branches and defaults to the youngest child, so an
  age handed over without a child attached is silently replaced. Measured on a
  parent with three children: a remembered 3y3m arrived as 1y2m.
- **Nothing recalled reaches a model.** The seeding is deterministic with no
  model call, `extract_form` still sees only the parent's message, and the AI
  adjuster still sees `age_months` as a number, so no child's name or date of
  birth leaves the app. The two note fields are deliberately never recalled,
  since they *are* fed to the adjuster and would ship a months-old note into a
  new request.

In the conversation this is a third provenance list beside "the parent said it"
and "the parent declined", and the summary gains a bucket per source, because
nobody checks a field they believe came from their own words. Their own words
always win: only fields the extractor reports may overwrite.

**It shows what it remembers, on the turn it first uses it.** The reported bug
was an assistant that said it had filled in what it knew and then asked a
question, leaving no way to see what it thought it knew until the summary several
turns later. Closing the tab did not help, and would not: the transcript is
per-tab, but this memory is the parent's own saved rows. So the values are
itemised where the claim is made, under the source they came from, and the reply
names that source, which is always something already on their dashboard. Nothing
is remembered that they cannot go and look at.

**"Something's changed"** is offered on that same turn rather than only at the
summary, since a recalled value is the one kind never asked about, so the turn
that reveals it is the turn to be able to reject it. It drops everything recalled
and asks properly, and doubles as the only way to retract a field no question
covered. Its matcher runs on every turn, which is why "no" is deliberately not in
its vocabulary: a bare "no" answers whatever was just asked.

**Deliberately not done yet.** Drafts are not memory, so a half-finished form is
not stored. The tool-calling agent gets none of this: its tools take
model-chosen arguments, so a `recall(parent_id)` tool would let the model name
any parent, and it would have to be bound at construction time instead.

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

New coordinates reach an existing database through `db._seed_venues`, which
runs on startup and updates venues already in the table as well as inserting
new ones. A null coordinate in the seed file never overwrites one already
found, so re-running the script is safe and so is re-booting after it.

## Where a venue's data comes from

Three sources, and each is reviewed only where a person adds something.

| Tier | Covers | Review |
| --- | --- | --- |
| **The City** | parks (218), community centres (27), washrooms (147) | none: the City is more reliable about its own parks than any reviewer |
| **The agent** | museums, aquariums, attractions, malls | required, 10 at a time |
| **Parents** | the amenities inside any venue | never gated |

**The boundary follows ownership, not category.** Vancouver Open Data publishes
what the City owns. Science World and the Vancouver Aquarium are non-profits,
Grouse Mountain and Capilano are private, the malls are private. No municipal
dataset will ever list them, so they need a different source by construction
rather than by oversight, and that is what the agent-proposes-person-approves
loop is for.

Both steps are pages in the app:

```
/propose-venues    press Run. The agent searches and writes up to 10 candidates
/venues/review     correct anything wrong, tick what the place offers, approve
```

`scripts/propose_venues.py --batch 30` does the same thing from the command
line, for a one-off larger run: a request that long does not belong in a
browser. Nothing about the normal cadence needs a terminal.

The agent never writes a venue. It writes `data/venue_candidates.csv` and
nothing else, and `/venues/review` is the only path from a candidate to a row.
Rejections are remembered, so a place you turn down is never proposed again.

### Why the proposer does not use Google Places

Google Maps Platform terms allow storing a place **id** but restrict retaining
the content a lookup returns. Everything the proposal path writes lands in
`data/venue_candidates.csv`, which is **tracked in git**, so a public repo was
redistributing Google's addresses and coordinates. Not hypothetical: five rows
carried them until this was fixed.

So the proposer geocodes through **Nominatim** (`src/nominatim.py`) instead.
ODbL, like the OSM hours lookup, so a result can be stored and shown with
attribution. It also gives something Places never could: a stable OSM id, kept
as `external_id`, so a re-proposal of the same venue is recognisable rather than
a second row.

```
Maplewood Farm    49.3088, -123.0193    osm:way/261318457
```

The trade is precision — free-text geocoding with no notion of "the branch
nearest here" — so two guards do the work a paid API would. A bare-name hit is
accepted only when Nominatim puts the result in British Columbia, and every
coordinate is checked against Metro Vancouver's bounds (`geo.in_metro_vancouver`).
`search_places` stays exactly where it was for `/place-search`, `/log-place`,
Log a Place and find-nearby: nothing a parent touches changed, and
`GOOGLE_MAPS_API_KEY` is still required.

**There are two Vancouvers, 500km apart, and search reaches both.** The first
live run of the retargeted queries returned a *Portland* listicle and took two
Washington venues from it. The coordinate guard could not catch them — it only
fires on a located candidate, and a Washington venue is one Nominatim searching
Metro Vancouver never finds, so both arrived with no coordinates and passed the
bounds check untouched. The article is what has to be rejected, not the venue:
a Portland guide is not evidence for a Vancouver outing whatever it names. So a
result whose URL or title names Portland, Oregon or Vancouver WA is dropped
before the model reads it. Snippets are not matched, and "washington" alone is
not a trigger, because Vancouver BC has a Washington Street.

**Where the agent searches** has moved with the database. The two restaurant
queries went when restaurants left the table, and the community-centres query
went because the City publishes all 27 authoritatively — proposing them was
effort against a worse source. What is left points at the ownership gap:
children's museums, aquariums, indoor play, farms, toddler pools.

`gap_queries()` changed what it measures, too. Counting venues per
neighbourhood was right at 38 venues and wrong at 260: every City area has
somewhere outdoors now, and what a family cannot find is somewhere under cover.
It ranks by indoor shortage instead, and iterates every neighbourhood the app
knows rather than only those already in the data — an area with *zero* venues
never entered the old counts, so the biggest gaps were the ones it could not see.

### What the agent fills in before you review it

Review is one person's attention, so anything a reviewer would look up by hand
should already be on the row. Three things arrive filled in, and each carries
where it came from, because a prefilled field nobody can check is worse than a
blank one.

**Hours, from OpenStreetMap.** The prompt still forbids the model from
reporting hours: a listicle does not establish when a museum opens, and a guess
from a model is indistinguishable from a fact. They come from one batched
Overpass query per run instead, and only when OSM says one plain pair. Anything
richer -- a Monday closure, a lunch break, seasonal bands -- leaves the inputs
blank and shows the raw string, because a single open/close cannot hold it and
picking half would be inventing an answer.

Either way the row carries the raw OSM string **and the name of the OSM entry
that answered**, which is what makes a prefill checkable. That mattered: the
loose name matching the hours-verification tool uses returned "The Granville
Island Toy Company" for Granville Island and the Kerrisdale branch for
Vancouver Public Library. Matching on the whole name drops both, and finds more
rather than less -- `Maplewood Farm` was being shadowed by `Maplewood Farm
Livestock Barn`, whichever came back first.

**The official website.** Some citations are somewhere anyone can publish: one
live proposal cited a Facebook group post. Those are not dropped, since a
Facebook post is often how a small venue announces itself, but they are a
reason to go looking for the venue's own site. One search per candidate, then
the answer is read off the **domain**, which is the one part of a result a
venue has to own:

```
Roundhouse Community Centre  ->  roundhouse.ca          found
Maplewood Farm               ->  maplewoodfarm.bc.ca    found
Granville Island             ->  granvilleisland.com    found
Little Nest                  ->  vancouvermom.ca        declined, an article
Treetop Adventures           ->  (nothing)              declined
```

Six of nine on the live queue, nothing wrong accepted. Deterministic, because
there is nothing here for a model to judge that a string comparison cannot.
Approving stores the official site as the venue's `source_url`; the page it was
discovered on stays in `venue_candidates.csv`, which is the durable record of
where a venue came from.

**A type and a neighbourhood the app actually knows.** See below.

### Why a value used to arrive "not known", and what changed

The review page used to show `type: restaurants (not a known value)` and
`neighbourhood: Central Vancouver (not a known value)`. The proposer was
generating them: the response schema typed both fields as free strings and the
prompt described `type` in prose with a list of examples that was not even our
list. A live batch produced `activity` four times, plus `cafe` and `restaurant`
for venues the prompt tells it to skip outright.

The display was the smaller half of the problem. The form rendered the
unrecognised value as the **selected** option, and the approval check only
asked whether the field was non-empty:

```
_cannot_approve(type='activity')     ->  ''      approves fine
is_nap_friendly({'type':'activity'}) ->  False   silently, forever
```

So approving without opening the dropdown wrote `activity` into `venues.type`,
where nothing downstream refuses it -- `is_nap_friendly` does not fail on a type
it does not know, it just answers False. Fixed at all three layers, in that
order:

1. **Generated.** `type` and `neighbourhood` are `enum`s in the JSON schema,
   built from the `data_loader` constants so the schema, the prompt and the
   review dropdowns cannot drift. There is no longer a `restaurant` value for
   the model to reach for.
2. **Stored.** `_grounded` blanks a value outside the list before it is written,
   because a model can ignore a schema. Blank asks the reviewer a question;
   wrong tells them an answer.
3. **Approved.** `_cannot_approve` refuses a `type` or `city` outside the enum,
   and a `neighbourhood` outside it when one is set, so not even a hand-made
   POST can put one in the table.

Only then the display: an unrecognised value is no longer offered back as the
selected option. The dropdown arrives unset and the row says what was proposed,
so it is a question rather than an answer you can approve by inertia.

### Importing what the City publishes

```
python3 scripts/import_open_data.py                        # dry run, prints a report
python3 scripts/import_open_data.py --write
python3 scripts/import_open_data.py --source parks --write
```

218 parks and 27 community centres, straight into the table with `source =
"municipal_open_data"`, a citation, and an `external_id` so a re-run updates
rather than duplicates. No review step, because putting a person in front of
"Trafalgar Park exists at these coordinates" is review as theatre.

**The dry run is not a sketch.** It runs the same two-step match the write does,
so the counts it prints are the counts you get. That mattered immediately: it
said 7 of the 11 seeded parks would be upgraded and 211 rows inserted, and 7 is
not 11. The four it leaves alone are right to leave alone -- Lynn Canyon Park is
North Vancouver's, UBC Botanical Garden is UBC's, Second Beach has no record of
its own, and Stanley Park Seawall is a part of Stanley Park rather than the same
thing. But two of the earlier misses were real duplicates waiting to happen:

```
the City says              the curator says
John Hendry (Trout Lake) Park   Trout Lake (John Hendry Park)
English Bay Beach Park          English Bay Beach
```

Those two are in `importers.CURATED_ALIASES`, checked by hand, and deliberately
not a fuzzy rule: any normalized or prefix match loose enough to accept "English
Bay Beach" as "English Bay Beach Park" would also accept a park as its own
extension, and nobody would notice that merge.

**An import fills blanks and never overwrites a value.** One rule, both match
paths. Everything already on a row was typed by the curator or corrected by an
admin, and no unattended script should undo that. `seed_rank` and `source` are
never touched either: `seed_rank` is the curator's ranking, which is why a plan
still opens on Stanley Park Seawall with 266 venues in the table rather than 28,
and `source` decides which queue a row sits in. The cost, stated plainly: if the
City renames a park, a re-run will not pick it up, and deleting the row is how
you take the correction.

**Washrooms are an attribute, not a venue.** A public toilet is not an outing,
so the 147 washroom rows are read as evidence about a park instead, joined on
`park_name` -- an exact match, and a better key than any radius, since a park's
coordinate is one point and Stanley Park is 400 hectares. 100 of the 134 named
rows match a park outright and most of the rest name a community centre, so the
same join gives the centres their washroom for free.

It lands as a **report by nobody** rather than a column, for two reasons. The
dataset publishes `summer_hours` and `winter_hours` separately, which is the City
telling us these close seasonally, so a Y/N is not true year-round. And a parent
who was there last week has to be able to disagree. Both answers are written,
including "the City says there is none", which is the whole point of the reports
table.

It also lets the City's two datasets be compared, and they disagree: **9 parks
flagged `washrooms = "N"` have a facility in the washrooms dataset named after
them** -- Riley Park, Douglas Park, Kerrisdale Park and six more. Insert order
resolves it, the point-level record last, so the more specific answer wins.

**Community centres arrive without hours, and that is not a bug.** The City
publishes the address, the coordinates and a link to each centre's page, and
does not publish when it opens. A venue whose hours we do not know cannot be
scheduled, so all 27 correctly stay out of every plan -- and they are also kept
out of the candidate list, because a stop the validator can only ever replace
wastes one of an 18-venue budget, and 27 of them would crowd out most of it.
They appear under **Needs hours** on `/venues/review` instead, where reading the
centre's page and typing two times is what finishes the row.

Imported rows carry `verified_at IS NULL`, correctly: nobody checked them. They
stay out of the confirm backlog all the same, which is scoped to `curated` rows,
so that list remains the ~28 things only a person can vouch for rather than 245
parks.

### What a parent asks for, and what a venue is

Three concepts, one dimension each. This replaced a single `theme` control that
was doing all three jobs at once.

| | | |
|---|---|---|
| **`type`** | venue fact | what the place *is*: park, garden, beach, museum, market. Descriptive only |
| **`setting`** | venue fact | where a **visit is spent**: `indoor`, `outdoor`, `both`. Shelter, nothing else |
| **`interest`** | parent preference | which kinds of place they want. Optional, multi-select, sorts only |
| weather | context | attached to a *time*, not to a day |

**Why themes had to go.** "Rainy-day" was a weather condition, "Outdoorsy" a
physical setting, "Culture" an activity interest -- three dimensions in one
control. So a day could not be both outdoor and cultural, and a garden, which
is both, matched none of the three themes at all. Two faults came with it:
selecting no theme applied "Mixed", which was the union of the three type sets
and therefore deprioritised 10 of the 14 types, so *no preference was a
preference*; and asking for Rainy-day and Outdoorsy together produced a
preference for indoor and outdoor equally, which says nothing.

**`interest` is the type list itself, not a grouping over it.** Groups like
"Museums & galleries" were measured and added nothing: a grouped selection gave
results identical to the equivalent types, because an interest only ever
*sorts*. Asking for `museum` still reaches the aquarium at position 6 of 266.
A grouping layer would only have been a second vocabulary to keep in sync with
`VENUE_TYPES`, which is exactly how the themes rotted. The options are read
from the venues that exist, so the form never offers a kind of place there is
nothing behind, and an empty selection sorts nothing at all.

The day that was previously inexpressible:

```
interest = garden
   9:00 AM  Stanley Park Seawall        seawall  outdoor
  11:30 AM  VanDusen Botanical Garden   garden   outdoor   <- what you asked for
   1:00 PM  Bloedel Conservatory        garden   indoor
```

**`setting` exists because `type` cannot carry it.** `attraction` is a
legitimate residual -- somewhere that fits none of the other types -- and its
eight venues split four indoor, four outdoor. Avoiding the field would mean
types like `mountain` and `observation tower`, which is worse.

`both` means either mode is a real visit on its own, **not** that some part of
the venue has a roof. Capilano has a gift shop and a cafe and is plainly
outdoor: nobody goes there in the rain to stand in the shop. Two of 28 curated
venues are `both`, and none of the 249 imported ones.

Two tiers, never three. `SHELTERED` and `OPEN_AIR` both contain `both`, because
ranking it below an exact match measurably drops Grouse Mountain below all 222
imported parks, throwing away the curator's ranking for a weaker heuristic. It
also confines the field's ambiguity to where it cannot matter: `indoor` and
`both` share a tier, so if you cannot decide which the Aquarium is, it makes no
difference -- while the indoor-versus-outdoor call that *does* change a plan is
never the ambiguous one.

**Weather is context, not a day-level preference.** A parent wants to mix indoor
and outdoor stops, and no preference already gives that, because the curated
ranking alternates. So there is no "keep us indoors" setting. Instead:

```
"It's raining"   reads setting for the stops still ahead      available now
a forecast       would mark individual slots wet              not built
```

That replan path used to look for a Rainy-day *theme* whose type set was
`{museum, mall, cafe}`. It could reach **8 of 39 indoor venues** -- it could not
offer the Aquarium, Bloedel Conservatory, the Lookout, or any of the 27 imported
community centres, and one of its three targets was a type that no longer
exists.

Weather only ever pushes *towards* shelter: rain makes indoors better, dry
weather does not make outdoors obligatory. So `suits_weather` treats "dry" and
"no forecast" as the same path, which is what lets a forecast be added later
without changing how a day with no forecast is planned.

### No restaurants

The venue table holds attractions. Restaurants are the data hardest to maintain
and to source: they close constantly, OSM tags a highchair on 9 of 2,643
Vancouver food places (0.34%), Business Licences carries thousands with no
toddler signal, and no open dataset publishes restaurant amenities. Every
restaurant here would be hand-typed and stale from the day it landed, while
Google has live hours and current reviews and is already on the parent's phone.

Lunch keeps its time block, because that is where the value is: 90 minutes, near
the preferred time, before the nap. If a stop already on the day serves food,
lunch is taken there and no travel leg is added. Otherwise the block names
nowhere and offers Find nearby, searching from the previous stop. The planner
never inserts a venue just to have somewhere to eat, which is what it used to do
once the restaurants were gone: a Stanley Park morning was being sent to a mall
seven kilometres south.

**One deliberate exception, not yet built.** The argument above is against
holding *every* restaurant in Vancouver, which is unmaintainable. It is not an
argument against a small curated set of places built for small children -- a
kid cafe with a play area is closer to an indoor playground that serves food
than to a restaurant, and it is exactly the kind of place a parent cannot find
on Google without wading. The intent is to hold a handful of those, and to let
the plan form ask whether lunch should be somewhere designed for children or
anywhere at all, so a general dining preference and a specific request stay
distinguishable. Find Nearby would use the same distinction.

The candidates for it are already on file: the food venues the proposer found
sit under **Set aside** on `/venues/review`, rejected rather than deleted, and
restoring one is a click. Two things will need changing when this is picked up,
and neither is a surprise:

- `data_loader.VENUE_TYPES` has no value for them. Restoring a candidate works
  today, but approving it is refused with `type 'cafe' is not one we know`,
  which is the enum guard doing its job. A `kid cafe` type is the switch.
- `can_eat` currently means "food on site, no travel leg", which is a property
  of a mall or a market. A kid cafe is a venue whose *whole point* is the food
  stop, so the lunch rule in `src/itinerary.py` would need to place one rather
  than only use one it happens to pass.

Until then the pool costs nothing to keep and the proposer will not offer those
names again, because rejections are remembered.

### Amenities are reports, not columns

`venue_reports` holds who said an amenity was there and when. The venues table
still has the columns, but nothing reads them for this, because a claim needs an
author and a date, and **"nobody has said" has to differ from "somebody looked
and there was none"** -- a distinction `INTEGER NOT NULL DEFAULT 0` cannot make.

A field resolves to its newest report, with a real parent outranking a seed
claim of any age. Recency rather than a vote count, because amenities genuinely
change: a change table is removed, a park washroom closes for the winter, and
with a small user base a threshold would leave every field unknown forever.

Parents report from the trip page, per stop, after the visit. Five questions,
all skippable; *not sure* is the default and writes nothing. The highchair
question only appears where the stop serves food.

This exists because of what the seed data was doing: 11 venues asserted a
nursing room and 14 a family room, hand-typed for a demo, never verified, and
**with no path by which a parent could correct one**. Those claims are now
recorded as reports with no author, which keeps plans working while making their
weight visible, and one real report supersedes one.

### Nothing rejected is deleted

A reviewer can be wrong. Rejecting a proposal keeps it on file, and rejecting a
submission records a timestamp rather than deleting the row -- which also
mattered because `venue_reports.venue_id` cascades, so the old DELETE took the
parent's own words and every report about the venue with it. Both appear under
"Set aside" on the review page with a button to put them back.

### What is not stored, and why

| Field | Why not |
| --- | --- |
| `kid_friendly` | True on 37 of 38 rows. An admission rule, not an attribute: non-kid-friendly places do not enter the table |
| `nap_friendly` | Derived from `type`. A stroller nap needs somewhere you can keep walking without paying admission, which the kind of place already tells you |
| `category` | A tautology once the table holds attractions only. `can_eat` marks the ones with food |
| `min_age_months`, `max_age_months` | 0 and 60 on every row ever written; the age clause never excluded a venue. Age paces the day instead |
| `children.gender` | Collected, stored, and read back only by the form that collected it |

### Getting between stops

The form asks one question about transport: **how do you get from one stop to
the next?** Car (covering taxi and ride-share), public transit, or on foot.

It used to ask five things at once — car, bus, stroller, carrier, other — and
the answer changed nothing at all. Five combinations, one plan:

```
['car']                    9:00 11:30 1:00 4:45   8.8 km
['bus']                    9:00 11:30 1:00 4:45   8.8 km
['stroller']               9:00 11:30 1:00 4:45   8.8 km
['car','bus','stroller']   9:00 11:30 1:00 4:45   8.8 km
```

Identical venues, identical times, and a **4.1 km leg** handed to a family on
foot with a toddler.

**Why the list shrank to three.** It was mixing two different questions: how you
travel between venues, and what you have with you at one. A stroller is not a way
of covering three kilometres — it is what you push around a park once you get
there. Every family is now assumed to have one, so it is not asked about;
`stroller` and `carrier` are gone rather than merged, because they were answering
a different question and differed nowhere in the code.

And once it is only about the gap between two venues, **everyone walks**. Walking
is the floor, not one option among several, so what actually varies is the
furthest you can comfortably get — which makes it one choice, not a checklist.

**How far is "reasonable" is a judgment, not a calculation:**

```
on foot           1.5 km      about 26 minutes pushing a stroller
public transit    5 km
car / taxi        8 km
```

There is no routing, no schedules, no transfers, no waiting, and no attempt to
model that a SkyTrain covers more ground than a bus. The reach *is* the transit
model, and it is honest about being a heuristic.

**Proximity sorts the candidates; it never filters them.** Each stop after the
first is chosen with the previous one as an anchor, and venues within reach go to
the front — so the curator's ranking and what the parent asked for still decide
*within* reach, and a distant venue stays reachable when nothing nearer is open.
A filter would have emptied a day, which this codebase has been bitten by twice.

The effect, measured over nine plans per mode:

```
driving    62.4 km -> 62.4 km    every plan byte-identical
transit    62.4 km -> 51.2 km    changed only where a leg exceeded 5 km
walking    62.4 km -> 19.7 km    68% less, no leg over 1.3 km
```

A walking day went from *Seawall → Science World → Queen Elizabeth Park →
Oakridge Mall* to *Seawall → English Bay → Second Beach → Alexandra Park* — all
four stops kept, and a real West End afternoon.

Every stop now tells you how far it is: *"A beach in West End. 1.0 km from your
last stop."* That is the only thing a parent can see that proves the mode was
read, and it is how they judge whether a day is walkable.

**No travel-time model, deliberately.** Stops are spread across the whole day, so
gaps are hours wide and travel time disappears into them. At a 1.5 km walking
reach the longest leg is about 26 minutes, which fits even the tightest gap the
nap anchoring produces — so fixing *which venues are chosen* fixed the timing for
free. A real estimate needs routing, and the honest version of that is the Google
Maps link already on every stop.

### Hours, and the day being planned

The plan form carries a **date**, because which hours apply depends on it. A
venue has **one** open/close pair, required before it can be approved, and
`data_loader.get_venues(on_date=...)` resolves it for that day so every caller
stays date-unaware and reads the same two keys it always did.

```
2026-09-15  weekday        -> the default pair
2026-12-25  holiday, park  -> the default pair. Nothing is locked
2026-12-25  holiday, museum-> unknown. A pair says nothing about Christmas
any date    no pair at all -> unknown, so not schedulable
```

**A holiday depends on whether there is a door.** This used to refuse every
venue on all 11 BC statutory holidays, so Canada Day produced a plan with *zero
stops* while every park in the city sat there open. It also contradicted the
importer, which writes 06:00–22:00 for all 218 City parks precisely because a
park has no door. `data_loader.HOURS_ARE_A_CONVENTION` — `park`, `beach`,
`seawall` — names that assumption once and both readers share it. `garden` is
deliberately excluded: all four of ours are gated and ticketed, and shut on
Christmas like any other paid attraction.

### One pair, and a note for the rest

There used to be a `venue_hours` table keyed on (season, day type): six slots
per venue, 12 columns on every candidate row, six time-input pairs on every
review. **It never held a single row**, and the real data showed why it never
would. Of seven venues OpenStreetMap disagreed with us about, it could express
one:

```
Science World      Mo-Su 10:00-17:00                 a plain pair, no slot needed
Marine Gateway     Mo-Su 09:00-23:00                 a plain pair, ours was wrong
Vancouver Art Gallery  Sa-Th 10:00-17:00; Fr 10:00-20:00   one late weekday
Maritime Museum    Sep-May: Mo off                   closed one weekday, seasonally
Capilano           four date-range bands             finer than two seasons
Grouse Mountain    Dec 24, Dec 25 of its own         specific dates
Pacific Centre     Mo-Fr / Sa-Su                     the one case that fitted
```

`weekday` meant Monday to Friday as one thing, so "closed Mondays" — the
commonest real museum pattern — had no shape in the model at all. So the table
is gone, and what a single pair cannot hold goes in **`hours_note`**, in words a
parent reads, shown on the plan and trip pages:

> Closed Mondays September to May — check before you go.

The planner does not parse it, deliberately. It does not prevent the Monday
mistake, it tells the parent about it, and that is the honest trade: no small
model expresses season × weekday, and a note somebody reads beats a slot nobody
fills. Migrating the tracked candidate file confirmed the diagnosis — of the 12
columns dropped, **zero rows had a value in any of them**.

### Hours are entered once, and nothing may quietly undo it

`set_venue_default_hours` is the only path by which an approved venue's hours
change. `_seed_venues` used to write `open_time`/`close_time` unconditionally on
every startup, so it silently reverted that path. It really happened: the
Vancouver Aquarium was corrected from 09:30 to 10:00 through the review page,
after OSM showed the app was sending families half an hour before it opened, and
the next boot put 09:30 back. Nobody was told.

Hours now join `lat`/`lng` in the fill-only group — the seed supplies them to a
new row and never overwrites an existing one. Between a static file and a
decision somebody made against outside evidence, the decision wins.

### The check before a plan is shown

`src/components/validate_hours.py` runs after the draft and after the AI
adjuster, and nothing reaches a parent without passing it. For each stop it asks
whether that venue is open at that time on that date, and then:

- **open** -> the stop stands.
- **closed** -> swapped for an open venue, using the same `open_alternative` the
  in-trip replan uses, with the reason written into the stop.
- **hours unknown** -> treated exactly like closed. A venue nobody has given
  hours for is not schedulable: `venue_open_for` returns False, where it used to
  return True. Not knowing is a reason to leave a place out, never to include it.
- **nothing available** -> the slot is left free and says so. A slot the parent
  can fill beats a confident wrong answer.

It is deterministic. Comparing a stop time against stored hours is arithmetic,
and the rule in CLAUDE.md is not to ask a model to do what code already does.
What no model should be trusted with is whether a venue is open on Christmas
Day, so this never guesses.

**Holidays are their own day type.** `dates.bc_holidays` computes British
Columbia's statutory holidays from their rules, including Good Friday via the
Easter computus, so the calendar cannot go stale. A venue with no holiday hours
recorded has *unknown* hours for that date rather than inheriting its weekday
pair, because a default pair is a statement about ordinary days.

The consequence is deliberate and visible: with no holiday hours in the
database, a Christmas Day plan comes back empty, and says so rather than
offering a day built on a guess. The fix is data, and the report names exactly
which venue needs which day's hours.

### Keeping hours from going stale

Hours are typed in once when a venue is approved, and nothing else ever writes
them: `EDITABLE_VENUE_FIELDS` deliberately excludes them, so not even the parent
who submitted a place could fix them. Without a step like this a venue's hours
are frozen at whatever was entered that day, while the planner trusts them
completely.

```
python3 scripts/verify_hours.py            # dry run, prints a report
python3 scripts/verify_hours.py --write    # flags findings for review
```

It compares our stored pair against OpenStreetMap and sends disagreements to a
"Hours to check" section on `/venues/review`, where you correct the hours or keep
them. It never changes anything itself, because about half of what it finds needs
judgment: a mall tagged as closing at half four is more likely a mis-tagged
building than a mall that closes at half four.

OSM rather than a commercial API for two reasons: it is openly licensed, so a
result can be stored and shown with attribution, which is the restriction that
took Google Places out of the proposal path; and it is free, which matters for
something meant to run repeatedly. The trade is coverage, measured at about half
of Vancouver's museums. A venue OSM knows nothing about is reported as
**unverifiable**, never as agreeing.

The first real run found, among 17 venues: the Vancouver Aquarium opening at
10:00 where we held 09:30, so a family would have arrived half an hour early; the
Maritime Museum closed on Mondays from September; Grouse Mountain with its own
Christmas Eve hours; and Capilano's seasonal bands. Nine were unverifiable.

**Not this tool's job: same-day closures.** A private event or a burst pipe needs
a live call, which at three to five stops a plan costs about two orders of
magnitude more than the whole AI step. Every stop carries a Google Maps link
instead, and both the plan and trip pages say plainly that hours can change.

### Two known gaps

- **Boxing Day and Easter Monday** are not statutory in BC and are not treated
  as holidays, though many attractions keep special hours. A venue that closes
  on one needs a date-specific entry, which the model does not support yet.
- **Nothing records whether a place costs money.** A free park and a $30
  aquarium plan very differently, and it is part of why a paid attraction makes
  a poor nap stop.

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

Venues live in the `venues` table in `data/app.db`, which is what the app
reads at runtime. `data/venues.json` is its seed: `db._seed_venues` copies the
file into the table on every boot, inserting new entries and updating existing
ones, so the file stays hand-editable and remains the version-controlled record
of the curated set. Its order matters -- the rule-based planner takes the first
venue that fits a slot, so position in the file is a priority ranking, carried
into the table as `seed_rank`.

Each venue in `data/venues.json` has:

| Field                 | Meaning                                               |
| ---------------------- | -------------------------------------------------------- |
| `name`                 | Venue name                                                |
| `type`                 | park, mall, museum, attraction, garden, beach              |
| `neighbourhood`        | Vancouver neighbourhood                                   |
| `has_family_room`      | true/false, seeded then superseded by reports              |
| `has_nursing_room`     | true/false, same                                          |
| `stroller_accessible`  | true/false, same                                          |
| `can_eat`              | true/false: a meal can happen here without travelling      |
| `open`, `close`        | representative daily hours (`HH:MM`)                       |

The table adds `city`, `lat`/`lng`, `has_washroom`, `has_highchair`, `source`,
`rejected_at`/`rejected_by`, and for provenance `source_url` (the page it was
taken from), `external_id` (its id at that source, namespaced, e.g.
`vanopendata:parks/17`), and `verified_at`/`verified_by`.

The amenity columns are still written, by the review queue and the import seed,
but nothing reads them: `venue_reports` is what an amenity resolves from. See
"Amenities are reports, not columns" above.

`data_loader.get_venues()` is the boundary between the table and the planners.
It reads verified venues only (`source` in `curated`/`municipal_open_data`, so
unreviewed submissions never reach a plan), returns plain dicts rather than
database rows, generates each venue's `maps_url` from its name, and keeps the
seed file's ordering. It reads per call, so a venue added to the table shows up
in the next plan without a restart.

## Designed to grow

The pieces are intentionally modular:

- **Richer data**: the venues table is the source of truth and carries
  provenance columns, so open-data importers (Vancouver Open Data, OSM
  Overpass) and an admin review queue for user submissions can be added
  without touching the planners.
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
