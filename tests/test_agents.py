import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
from src.agents import ask

if __name__ == "__main__":
    reply = ask("confirm it's working and say hello")
    print(reply)
