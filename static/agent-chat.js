// AI Agent test page: shows what the agent did with each message sent through
// the real chat bubble. No chat of its own, deliberately -- the bubble is the
// agent's interface, so observing it is what tests the path a parent uses.
// Driven by the "twt:chat-reply" event chatbot.js fires once per reply.
document.addEventListener("DOMContentLoaded", () => {
  const heading = document.getElementById("agent-heading");
  const resultList = document.getElementById("agent-result-list");

  function row(label, value) {
    const p = document.createElement("p");
    p.className = "meta";
    const strong = document.createElement("strong");
    strong.textContent = `${label}: `;
    p.append(strong, document.createTextNode(value));
    return p;
  }

  function renderTurn({ message, reply, model, tool_calls, sources }) {
    const card = document.createElement("div");
    card.className = "need-card";

    card.appendChild(row("You said", message));
    card.appendChild(row("Agent replied", reply || "(nothing)"));
    card.appendChild(row("Model", model || "unknown"));

    const names = (tool_calls || []).map((c) => c.name);
    card.appendChild(row(
      "Tools used",
      names.length ? names.join(", ") : "none, it answered directly"));

    // The structured result is the proof a tool really ran, rather than the
    // reply merely claiming it did.
    (tool_calls || []).forEach((call) => {
      const summary = document.createElement("p");
      summary.className = "reason";
      summary.textContent = `${call.name}: ${call.output}`;
      card.appendChild(summary);
      if (call.data && Object.keys(call.data).length) {
        card.appendChild(row(`${call.name} returned`, Object.keys(call.data).join(", ")));
      }
    });

    if (sources && sources.length) {
      card.appendChild(row("Knowledge-base sources", String(sources.length)));
    }

    // Newest first, so the latest turn is visible without scrolling.
    resultList.prepend(card);
  }

  document.addEventListener("twt:chat-reply", (event) => {
    heading.textContent = "Latest turn first.";
    renderTurn(event.detail);
  });
});
