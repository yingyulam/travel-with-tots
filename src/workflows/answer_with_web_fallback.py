"""Answer-with-web-fallback workflow: don't refuse when the knowledge base misses.

Declaration only for now -- when this is implemented, the run function belongs
in this file. Both components already exist; the Web Search component's own
card on /components says it is "not yet wired in as a fallback", and this is
the wiring it is waiting for.
"""

WORKFLOW = {
    "name": "Answer with a web fallback",
    "emoji": "🔎",
    "trigger": "message",
    "description": (
        "A question the knowledge base cannot answer falls through to a live "
        "web search instead of a polite refusal. Either way the reply cites "
        "where the answer came from, so a parent can tell curated guidance "
        "from something found on the web just now."
    ),
    "steps": [
        {"component": "Chatbot + RAG", "built": True},
        {"component": "Web Search", "built": True},
    ],
}
