"""Find-a-nearby-place workflow: ask for somewhere nearby, in the chat.

Declaration only for now -- when this is implemented, the run function belongs
in this file.

The shape matters here, because a flat chip row cannot show it: Web Search is
not a step the agent sequences. The agent calls Find Nearby, and Find Nearby
decides for itself whether the answer comes from the curated venue table or from
the web (see `_search_places` and the fallback in src/components/find_nearby.py).
The /trip page's need buttons are a second way into the same component, with no
agent involved at all.

Two call sites bypass that component today, so the chat path this card describes
cannot actually reach Web Search yet. Both are the next task, and both are the
same mistake: there are two functions named `find_nearby`, and these reach for
the deterministic one in `interactions` rather than the component.

- `src/llms.py`'s `find_nearby_tool` calls `interactions.find_nearby(need,
  VENUES)`, so the agent gets no web fallback and no location awareness.
- `app.py`'s `/find_nearby` does the same on its no-location branch, and reports
  `source: "curated"` without having consulted the web. Its with-location branch
  calls the component and is already right.
"""

WORKFLOW = {
    "name": "Find a nearby place",
    "emoji": "📍",
    "trigger": "message",
    "description": (
        "A parent asks the chatbot for a place they need and the AI Agent hands "
        "the request to Find nearby stops, which chooses its own source: the "
        "curated venue table, or Web Search when nothing there matches, cited so "
        "the parent can tell the two apart. The trip page's need buttons reach "
        "the same component directly, without going through the agent."
    ),
    "steps": [
        {"component": "AI Agent (OpenRouter)", "built": True},
        {"component": "Find nearby stops", "built": True},
        {"component": "Web Search", "built": True},
    ],
}
