# Travel with Tots: Knowledge Base

This file is the source material for the chatbot's retrieval-augmented
answers (see `src/rag.py` and `src/prompts/website_chatbot.txt`). It gets
split into chunks, embedded, and stored in ChromaDB; the chatbot only ever
sees the top few chunks relevant to a given question, not this whole file
at once. Edit the sections below to add or update what the chatbot can
learn. Saving this file from the Settings page automatically re-chunks
and re-indexes it.

## About

Travel with Tots plans realistic, low-stress single-day outings in
Vancouver for parents travelling with kids aged 0-5. It generates a few
paced itinerary options sequenced around nap and feeding windows, then
lets parents adjust on the fly when the day doesn't go to plan. The whole
idea is to remove the mental load of trip planning with a small child:
parents answer a short form once, and the app does the sequencing,
pacing, and backup planning.

Who it's for: parents of young children, especially those visiting
Vancouver and unfamiliar with the city. It's equally useful for a local
parent who just wants an easier Saturday than trying to plan one from
scratch.

## Features

1. Itinerary generation: parents enter their child's age and trip
   details, and the site generates a few candidate day plans paced
   around naps and feedings.
2. Re-planning mid-day: if something changes (a nap runs long, the
   family falls behind schedule), the app proposes a new plan for the
   rest of the day. The original plan isn't lost; parents can switch
   back to it anytime.
3. Tap-first nearby search: while out and about, parents can tap a
   button for an immediate need (kid-friendly restaurant, family room,
   changing table) and get quick suggestions nearby.
4. Google Maps hand-off: once a destination is chosen, one tap opens
   directions in Google Maps.

## How itinerary pacing works

The planner picks a number of stops based on two things: the pace the
parent chose, and the child's age. Pace has three settings: "relaxed"
plans 2 stops, "balanced" plans 3 stops, and "adventurous" plans 4 stops.
That count is then nudged for age: children under 24 months get one
fewer stop than the pace would normally suggest (younger kids tire out
faster and need more downtime between activities), and children 48
months or older get one extra stop (more stamina, less nap dependency).
The final stop count is always clamped between 2 and 4, no matter what
the pace and age math works out to, so no plan is ever overloaded or
empty.

The day always starts with a two-hour morning buffer after wake-up time,
covering breakfast and getting out the door, before the first stop is
scheduled. Parents also choose a dining style: "dine out" builds a
dedicated midday food stop into the plan, while "on the go" skips a
separate food stop and assumes the family eats during transit or at an
activity instead.

When a parent enters a nap time on the trip form, that window isn't
skipped or left empty. The planner schedules a real stop right at that
time, at a calm, stroller-friendly spot (like a park or garden) where
the child can nap on the go. So the family still has somewhere to be
during the nap; it's just a venue chosen so the child can sleep through
it undisturbed, keeping the day moving instead of standing still.

Every request generates three themed plans at once, so parents can
compare options rather than getting a single locked-in answer: an
"Outdoorsy" plan (parks and fresh air, stroller-friendly), a "Rainy-day"
plan (indoor stops that stay dry and cozy: museums, malls, cafes), and a
"Culture" plan (museums and sights for curious little minds). All three
respect the same stop count, timing, and dining choice; they just pick
different venues for the activity slots.

## Account & Data

Parents sign up or log in to save their info. From the dashboard, they
can add, edit, or remove a child's profile (name, date of birth; age is
always computed from date of birth, never stored directly), and reopen
any previously saved trip itinerary. The trip-planning form itself also
lets a parent enter a child's age on the spot if they haven't saved a
profile yet, so an account isn't required just to try generating a plan.
It's only required to save one for later or to keep a child's info
around between visits.

A parent can plan a trip for multiple saved children at once (all of
them show up as chips to pick from on the planning form), but the actual
pacing of stops and timing is always built around one chosen child at a
time, since nap schedules and stamina differ per child.

## Re-planning in detail

Re-planning never throws away the whole day and starts over. It keeps
every stop that's already happened, and the stop currently in progress,
exactly as they were; only the stops still ahead on the clock get
re-decided. The panel names the stop it is
anchored to, so a parent can see what "here" refers to before choosing.
A parent picks one of these situations to describe what's going on: "Nap
happened here" (an unplanned nap ate into the schedule), "Need to stay
here longer" (they are not ready to move on yet), "Skip next stop" (drop
the very next planned stop and move on), "Finished this stop early"
(there's now spare time before the next stop was supposed to start),
"It's raining", "Change the theme", or "Anything else". For the two
situations involving a duration, the parent either taps a preset or
types the exact number of minutes, and the app shifts the remaining
schedule accordingly. A free-text box sits alongside the buttons and is
sent with every re-plan, so a parent can add "somewhere indoors nearby"
to any of them; "Anything else" is for when the words are the whole
request and no button fits. If a situation frees up time in the day, the app
will try to fill it with a real venue that matches the trip's chosen
features and isn't already elsewhere in the plan, rather than just
leaving a gap. The original plan a parent started with is never mutated
by this process; it stays saved and selectable, so switching back to
"the plan as it was" is always one tap away.

## Tap-first nearby search, in detail

While a trip is in progress, the "need something now?" buttons cover six
categories: kid-friendly restaurant, family room, changing table,
nursing room, quiet spot, and a free-text "other" option for anything
not covered by the first five. Each of the first five maps to a specific
attribute on the venue data; for example "family room" looks for venues
flagged as having a family washroom, and "nursing room" looks for venues
flagged as having a nursing room, so the suggestions are always venues
that are actually known to have that amenity, not just nearby venues in
general. The app returns one or two matching suggestions at a time,
deliberately kept short so a stressed parent can make a quick decision
rather than scroll through a long list.

## Product Facts

- Coverage area: Vancouver only, single-day trips only (no multi-day, no other cities yet)
- Child age range supported: 0-5 years old
- Trip pacing: itineraries are paced around one child per trip, though multiple children can be included on the same trip
- Account required: yes, to save child profiles and reopen past itineraries; not required to try generating a plan
- Cost to use: free during MVP (no billing feature yet)
- Number of plan options generated: 3 themed options per request (Outdoorsy, Rainy-day, Culture)
- Nearby search categories: kid-friendly restaurant, family room, changing table, nursing room, quiet spot, other
- Navigation: hands off to Google Maps, no in-app turn-by-turn
- Pace settings: relaxed (2 stops), balanced (3 stops), adventurous (4 stops), adjusted by the child's age
- Dining styles: "dine out" (dedicated midday food stop) or "on the go" (no dedicated food stop)

## Packing and travel-day tips

A few general tips the chatbot can pass along when asked: build in extra
buffer time beyond what the itinerary shows, since gear changes and
diaper stops always take longer than expected with a toddler in tow.
Bring one spare outfit per child even for a short outing. If a stop
involves a stroller and transit, check the transit modes chosen on the
trip form ahead of time; the itinerary planner takes transit mode into
account when it sequences stops, but it can't account for elevator
outages or last-minute accessibility issues at a specific station.
Snacks that don't need refrigeration travel best between stops. For nap
timing specifically, it's worth entering both nap windows on the
trip-planning form if a child still naps twice a day, since the planner
uses both to place a nap-friendly stop at each one.

## Troubleshooting

If a generated plan looks off (e.g. a stop scheduled too close to a nap
window), double-check the wake-up time, bedtime, and nap times entered
on the planning form; the pacing logic depends entirely on those
values being accurate. If a saved trip isn't showing up on the
dashboard, confirm the plan was actually saved (there's a "Save this
plan" step after picking one of the three themed options; generating
plans alone doesn't save anything until a parent chooses one). If
logging in fails, the most common cause is a typo in the email used at
signup; there's currently no self-serve "forgot password" flow, so a
parent locked out of an account should use the contact email in this
document to ask for help. If the chatbot itself seems to be giving an
unrelated or generic answer, it usually means the question fell outside
what this knowledge base actually covers; try rephrasing to be more
specific about the feature in question.

## Browser and device notes

The site is a standard responsive web app: it works in any modern
desktop or mobile browser (Chrome, Safari, Firefox, Edge) with no app
install required. There's no dedicated native mobile app at this time.
The interface is designed mobile-first for the in-trip pages (the tap-first
nearby search and re-planning buttons), since that's realistically used
on a phone while out with a child, while the planning form works
comfortably on both desktop and mobile.

## Hours

This is a self-serve web app with no live support hours: it's available
anytime, but questions sent to the contact email below aren't answered
24/7. We try to reply to emails within 2 business days.

## Contact Info

yingyu.rain.lin@gmail.com

## Policies

Free to use during the MVP phase; there is no billing or subscription
feature yet, and none is planned to launch alongside the current
feature set. No trip or child data is sold or shared with third parties;
it's used only to generate and save itineraries for the account that
entered it. Deleting a child's profile from the dashboard also deletes
that child's saved trips.

## FAQs

Q: What is this website for?
A: Travel with Tots plans realistic, low-stress single-day outings in
Vancouver for parents travelling with kids aged 0-5, pacing the day around
naps and feedings and letting parents adjust on the fly when things change.

Q: What does this app do?
A: It generates a few paced day-itinerary options from a child's age and
trip details, then helps parents adjust the plan mid-day, find nearby
essentials (restaurants, changing tables, nursing rooms), and get directions
to each stop.

Q: Who is this website for?
A: Parents of young children (ages 0-5), especially those visiting
Vancouver and unfamiliar with the city, though it's just as useful for a
local parent planning an easier day out.

Q: How many itinerary options does it generate?
A: Three themed options per request: Outdoorsy, Rainy-day, and Culture.

Q: Can I book flights or accommodation through this website?
A: No. Travel with Tots only plans day-to-day itineraries within Vancouver;
it doesn't book flights, hotels, or any other accommodation.

<!-- Add more Q&A pairs here for things not already covered above (e.g.
     support, billing, or policy questions). -->
