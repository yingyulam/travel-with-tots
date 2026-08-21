"""Web Search component: live results from the Tavily Search API.

Self-contained, one file per component (see /components). No other module
imports from this one -- it's only reached via app.py's /search-web routes.

Tavily, not Brave: Brave killed its free tier in Feb 2026 (the "identity
verification" card is now an active billing instrument, charged past $5 of
usage/month with no cap). Tavily's free tier -- 1,000 credits/month, no
card required, requests just stop once exhausted -- has no such trap.
"""

import os

import requests

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
RESULT_LIMIT = 5


class WebSearchError(Exception):
    """Raised when the Tavily Search API call fails."""


def search_web(query: str) -> list[dict]:
    """Query Tavily Search, return up to RESULT_LIMIT {"title", "url",
    "snippet"} dicts. Raises KeyError if TAVILY_API_KEY isn't set, or
    WebSearchError if Tavily itself returns an error."""
    api_key = os.environ["TAVILY_API_KEY"]
    response = requests.post(
        TAVILY_SEARCH_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query, "max_results": RESULT_LIMIT},
        timeout=10,
    )
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise WebSearchError(f"Tavily Search returned {response.status_code}") from e

    results = response.json().get("results", [])[:RESULT_LIMIT]
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "snippet": r.get("content", "")}
        for r in results
    ]
