# 90-second demo script

Roughly 205 spoken words. Read at a normal pace and it lands near 85 seconds,
leaving room to pause while a page loads.

## Before you start

1. `python app.py`, then open **http://localhost:8016**.
2. Log in as `demo@travelwithtots.app` / `demo1234`.
3. Open the chat bubble once and set the model to **Claude Sonnet 5**. That
   dropdown now governs planning and replanning too, not just chat. On GPT-4o
   mini the AI adjuster fails and the page says so, which is honest but not
   what you want mid-demo.
4. **Pre-generate a plan** in a second tab and leave it on screen, and click
   **Start this day** in a third so `/trip` is ready. Generating live is a real
   AI call and you do not want to narrate silence.
5. Grant the browser location permission once, so Find Nearby ranks by real
   distance instead of prompting on camera.

## The script

| Time | On screen | Say |
| --- | --- | --- |
| 0:00 | The landing page | "Travel with Tots plans a day out for parents of under-fives. The hard part isn't finding places, it's fitting them around a nap." |
| 0:10 | `/plan`, then switch to the pre-generated tab | "I give it our day: up at seven, bed at half seven, one nap at one. It builds a timed itinerary from real Vancouver venues, then an AI pass smooths the pacing. Every stop carries a reason and a Maps link." |
| 0:28 | `/trip`, tap **Nap happened here**, type 90 | "Out and about, the nap runs long. I tap 'Nap happened here' and say ninety minutes. It keeps everything already done, re-times the rest, and swaps anything that would now be closed. The original is still there to compare." |
| 0:46 | Same page, **Need something now?** → Nursing room | "Mid-trip you need something right now. Nursing room. It ranks our curated venues by real distance, and if nothing matches it falls back to a live web search and tells you which one answered." |
| 1:00 | `/log-place`, drop a pin, tick two boxes | "Found somewhere good we don't have? Drop a pin, name it, tick what it offers. It's stored as user-submitted and stays out of search until an admin verifies it." |
| 1:12 | Chat bubble: "find the nearest nursing room" | "All of that is reachable by talking too. The chat bubble is an AI agent: it works out what you're asking for and routes it to the right workflow. Here it runs Find Nearby and hands back real Maps links. Same components, second front door." |

## If something stalls

- **A plan is slow to generate.** Keep talking through the next line; the page
  shows "Building your day…" so it does not look stuck. Better: use the
  pre-generated tab and never generate on camera.
- **The banner says the AI fine-tuning step didn't finish.** The plan on screen
  is the rule-based one and is still a real plan. Say "the AI pass is optional,
  the rule-based planner always produces a day" and move on.
- **The chat answers without a workflow badge.** The classifier declined; retry
  with plainer wording, like "find the nearest nursing room".

## If you get 30 seconds more

Show `/workflows` as the admin (`admin@travelwithtots.app` / `admin1234`) and
open one card's **Try it** page. Press **▶ Run once**, send a message in the
bubble, and the captured turn appears with the chain it went through. That is
the piece that shows the components and workflows are real and observable,
rather than a diagram.
