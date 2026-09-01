# Travel with Tots: Knowledge Base

This file is the source material for the chatbot's retrieval-augmented
answers (see `src/rag.py` and `src/prompts/website_chatbot.txt`). It gets
split into chunks, embedded, and stored in `data/rag_index.json`; the
chatbot only ever sees the top few chunks relevant to a given question,
not this whole file at once. Edit the sections below to add or update
what the chatbot can learn. Saving this file from the Settings page
automatically re-chunks and re-indexes it.

## About

Travel with Tots plans realistic, low-stress single-day outings in
Vancouver for parents travelling with kids aged 0-5. It builds a paced
itinerary sequenced around nap and feeding windows, then lets parents
adjust on the fly when the day doesn't go to plan. The whole
idea is to remove the mental load of trip planning with a small child:
parents answer a short form once, and the app does the sequencing,
pacing, and backup planning.

Who it's for: parents of young children, especially those visiting
Vancouver and unfamiliar with the city. It's equally useful for a local
parent who just wants an easier Saturday than trying to plan one from
scratch.

## Features

1. Itinerary generation: parents enter their child's age and trip
   details, and the site builds a day plan paced around naps and
   feedings, from real venues with real opening hours.
2. Re-planning mid-day: if something changes (a nap runs long, the
   family falls behind schedule), the app proposes a new plan for the
   rest of the day. The original plan isn't lost; parents can switch
   back to it anytime.
3. Tap-first nearby search: while out and about, parents can tap a
   button for an immediate need (kid-friendly restaurant, family room,
   changing table) and get quick suggestions nearby.
4. Google Maps hand-off: once a destination is chosen, one tap opens
   directions in Google Maps.
5. A chat assistant, on every page. It answers questions about the site,
   and it can start any of these tasks in conversation instead of by
   form: planning a day, finding somewhere nearby, re-planning, or
   logging a place.
6. Logging a place the app doesn't have: a parent can tell the chat, or
   use the Log a Place page, about somewhere good that isn't in the
   venue list.
7. Reporting what's actually at a stop: on the in-trip page a parent can
   tick which kid-friendly features a place really has, or say it was
   shut when they arrived.

## How itinerary pacing works

The parent says how many places they'd like to visit, and the planner
takes that as the starting point rather than deciding for them. It is
then clamped to what's realistic: never fewer than 2, never more than 4,
and one lower than the ceiling for a child under 24 months, who tires
faster and needs more downtime between activities. So a parent asking
for five places with a one-year-old gets three, and the form says up
front that the number will be adjusted a little for the child's age.

The day always starts with a two-hour morning buffer after wake-up time,
covering breakfast and getting out the door, before the first stop is
scheduled. Parents also choose a dining style: "dine out" builds a
dedicated midday food stop of about 1.5 hours into the plan, at a
preferred lunch time if one was given, while "on the go" skips a
separate food stop and assumes the family eats during transit or at an
activity instead.

When a parent enters a nap time on the trip form, that window isn't
skipped or left empty. The planner schedules a real stop right at that
time, at a calm, stroller-friendly spot (like a park or garden) where
the child can nap on the go. So the family still has somewhere to be
during the nap; it's just a venue chosen so the child can sleep through
it undisturbed, keeping the day moving instead of standing still. The
form also asks whether the child naps well in a stroller, car or bus,
which changes how much the planner leans on a nap-friendly venue.

A parent can pin where they're staying on a map, and the day is then
anchored to it: the first stop is chosen near the accommodation rather
than wherever happens to score well, so the morning doesn't start with a
long trip across the city. They also say how they're getting between
stops (car or ride-share, public transit, on foot) and how long they're
willing to spend getting to any one stop: 20, 30 or 40 minutes, with 20
as the default.

A trip can cover more than one day. The form asks for an arriving date
and, optionally, a leaving date; leaving it blank plans a single day out,
which is how the app worked before. With both dates given, the planner
builds one itinerary per day, up to seven days at a time, and refuses a
longer range rather than quietly planning only part of it. Each day is
planned for its own date, so opening hours resolve for that weekday or
holiday, and no venue is used on two days of the same visit. The family
is assumed to stay at the same accommodation throughout for now.

On the in-trip page, a multi-day trip shows a day picker above the plan
version tabs, and everything else on that page works within whichever day
is selected. Saving a multi-day trip saves each day separately, so the
dashboard lists them as individual days that reopen the whole trip.

The trip form also asks which kinds of place the family would like, as a
list of every kind the app has venues for, all ticked to begin with.
Unticking a kind is a preference rather than a rule: the ticked kinds
come forward in the ordering, and an unticked one can still appear if it
fits the day better, which the plan says in its description. Ticking
every kind and ticking none produce the same day, so the fully ticked
list is just the question asked plainly. Clearing every box is refused:
the form asks for at least one kind before it will build a day.

That travel time is a hard limit, not a preference. It applies to each
leg on its own rather than to the day's total, and to every leg
including the journey back to the accommodation at the end, so the last
stop of the day is somewhere the family can actually get home from. A
day of four short walks is fine; a single walk over the limit is not.
The travel time shown for each leg is an estimate from the straight-line
distance rather than a real route.

If there isn't enough within the limit, the plan comes back shorter
rather than reaching further. It names how many stops it left out and
why, and offers a button to include places further away. Nothing widens
the limit on the parent's behalf. Pinning an accommodation outside Metro
Vancouver is allowed, and the plan says plainly that very little will be
within reach of it.

One plan comes back per request, built from real venues with real
opening hours. A rule-based draft is written first, then an AI pass
adjusts it, and the result is checked against each venue's hours for the
date of the trip. If a parent wants something different, "Need changes?"
takes a note in their own words and re-plans on it, rather than making
them start the form again.

## Account & Data

Parents sign up or log in to save their info. From the dashboard, they
can add, edit, or remove a child's profile (name, date of birth; age is
always computed from date of birth, never stored directly), and reopen
any previously saved trip itinerary. The trip-planning form itself also
lets a parent enter a child's age on the spot if they haven't saved a
profile yet, so an account isn't required just to try generating a plan.
It's only required to save one for later or to keep a child's info
around between visits.

A saved trip belongs to the parent, not to a child. A child can be
attached to one, and saved children show up as chips to pick from on the
planning form, but none is required: a parent can save a day without
having logged a child at all. Where children are picked, the pacing of
stops and timing is still built around one chosen child at a time, since
nap schedules and stamina differ per child.

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
"It's raining", or "Do something else". For the two situations
involving a duration, the parent either taps a preset or types the exact
number of minutes, and the app shifts the remaining schedule
accordingly. A free-text box sits alongside the buttons and is sent with
every re-plan, so a parent can add "somewhere indoors nearby" to any of
them. The box also has its own Replan button, for when the words are the
whole request and no button fits: that leaves the remaining stops' times
alone and just acts on what was typed. If a situation frees up time in the day, the app
will try to fill it with a real venue that matches the trip's chosen
features and isn't already elsewhere in the plan, rather than just
leaving a gap. The original plan a parent started with is never mutated
by this process; it stays saved and selectable, so switching back to
"the plan as it was" is always one tap away.

## Tap-first nearby search, in detail

While a trip is in progress, the "need something now?" buttons cover
five options: kid-friendly restaurant, family room, changing table,
nursing room, and a free-text "other" for anything not covered by the
first four. Each of the first four maps to a specific attribute on the
venue data; for example "family room" looks for venues flagged as having
a family washroom, and "nursing room" looks for venues flagged as having
a nursing room, so the suggestions are always venues actually known to
have that amenity, not just nearby venues in general. The app returns one
or two matching suggestions at a time, deliberately kept short so a
stressed parent can make a quick decision rather than scroll through a
long list. If the browser has shared a location, results are ranked by
real distance from where the family is standing; if nothing curated
matches, it falls back to a web search and says which source the answer
came from.

The same request can be made in the chat instead of by button, and it
offers the same five options as tappable choices when it isn't clear
what's needed.

## Product Facts

- Coverage area: Vancouver only, single-day trips only (no multi-day, no other cities yet)
- Child age range supported: 0-5 years old
- Trip pacing: itineraries are paced around one child per trip, though multiple children can be included on the same trip
- Account required: yes, to save a trip and to keep child profiles; not required to try generating a plan
- A saved trip belongs to the parent; attaching a child is optional
- Cost to use: free during MVP (no billing feature yet)
- Number of plans generated: one per request, which can then be re-planned with a note
- Stops per day: the parent chooses, clamped to 2-4, and one lower for a child under 24 months
- Nearby search options: kid-friendly restaurant, family room, changing table, nursing room, other
- Navigation: hands off to Google Maps, no in-app turn-by-turn
- Dining styles: "dine out" (a midday food stop of about 1.5 hours, at a preferred time if given) or "on the go" (no dedicated food stop)
- Getting around: car or ride-share, public transit, or on foot
- Trip length: one arriving date and an optional leaving date, up to 7 days, one itinerary per day
- Kinds of place: every kind starts ticked, and unticking one moves it down the list rather than removing it; at least one must stay ticked
- Travel time between stops: 20, 30 or 40 minutes, default 20, applied to each leg including the journey home
- Accommodation: optional, and pinnable on a map; the day is anchored to it when given
- Ways to reach a task: the planning form, the in-trip buttons, or the chat
- Parent reports on venue features: reviewed by an admin before anyone else sees them

## The chat assistant, in detail

The chat bubble sits on every page and does two things. It answers
questions about the site from this knowledge base, quoting the passages
it used so a parent can see where an answer came from. And it starts
tasks: describing a day out fills the planning form, asking for
something nearby runs the nearby search, saying something has changed
mid-trip collects a re-plan, and telling it about a missing place starts
logging one. A parent doesn't have to know which is which, or give the
details up front: saying "I want to add a place" is enough, and the
assistant asks for what it needs, offering tappable options wherever
there is a fixed set to choose from.

Once a task is underway the conversation stays with it until it's
finished or the parent backs out, so answers like "yes" or "Gastown" are
read as answers to the question just asked. Saying "cancel", "never
mind" or similar leaves at any point.

What it will not do is write an itinerary in the chat. A day belongs in
the planner, where it can be compared, re-planned and used with the
in-trip tools, so the assistant fills the form and hands it over rather
than printing a plan into the conversation.

## Reporting what you found at a stop

On the in-trip page, each stop can be asked two things. "Find
kid-friendly features here?" offers the amenities the app has a field
for, with anything already known ticked; a parent ticks what's there and
unticks what has gone. The second asks whether the place was as
described, which is where to say it was shut on arrival.

Reports are reviewed before they change anything. A parent's answers are
recorded against them and held, and an admin approves or rejects them;
only then do they change what other parents see. The parent's own view
shows their answer marked as awaiting review in the meantime, so it is
clear the report was received rather than lost. Reporting that a place
was closed is handled the same way: it files a note for a reviewer to
check against the hours on record, and doesn't change the stored opening
hours by itself.

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
plan" step after the plan is generated; generating one doesn't save it
until a parent asks). If
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
A: It builds a paced day itinerary from a child's age and trip details, then
helps parents adjust the plan mid-day, find nearby essentials (restaurants,
changing tables, nursing rooms), and get directions to each stop. Everything
can be done through the form or by talking to the chat assistant.

Q: Who is this website for?
A: Parents of young children (ages 0-5), especially those visiting
Vancouver and unfamiliar with the city, though it's just as useful for a
local parent planning an easier day out.

Q: How many itinerary options does it generate?
A: One plan per request. If it isn't right, "Need changes?" takes a note in
your own words and re-plans on it, and the previous version stays available
to switch back to.

Q: Can I book flights or accommodation through this website?
A: No. Travel with Tots only plans day-to-day itineraries within Vancouver;
it doesn't book flights, hotels, or any other accommodation.

Q: How many places will it plan for one day?
A: As many as you ask for, within reason: between 2 and 4, and one fewer for
a child under two, who tires faster and needs more downtime.

Q: Can I plan a day without adding a child to my account?
A: Yes. A saved trip belongs to you, not to a child. You can enter an age on
the form without saving a profile, and you can save the plan either way.

Q: I told you a place has a nursing room. Why can't I see it yet?
A: Reports are checked by a reviewer before they change what other parents
see. Yours is recorded and shown on your own trip marked as awaiting review,
and it applies for everyone once it's approved.

Q: Can the chat plan the day for me?
A: It fills the planning form from what you tell it, then hands you the form
to check and plan from. The itinerary itself is built on the planning page,
so that you can compare versions, re-plan, and use the in-trip tools on it.

<!-- Add more Q&A pairs here for things not already covered above (e.g.
     support, billing, or policy questions). -->
