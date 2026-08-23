// Log-a-place-from-chat workflow test page: shows the submission the agent
// has collected after each message sent through the real chat bubble. The
// Run/Listen machine is shared, in workflow-watch.js; only the rendering here
// is this page's own.
document.addEventListener("DOMContentLoaded", () => {
  const resultList = document.getElementById("log-place-from-chat-result-list");

  const WORKFLOW_NAME = "Log a place we don't have";

  // What the submission holds, in the order the page asks for it, so a reader
  // can see how far the conversation has got. The keys are the Log a Place
  // form's own field names, which is what the handoff posts.
  const FIELDS = [
    ["name", "name"],
    ["neighbourhood", "area"],
    ["kid_friendly", "kid-friendly"],
    ["has_family_room", "family room"],
    ["has_nursing_room", "nursing room"],
    ["stroller_accessible", "stroller / step-free"],
    ["notes", "notes"],
  ];

  function line(card, text, className) {
    const p = document.createElement("p");
    p.className = className;
    p.textContent = text;
    card.appendChild(p);
  }

  // Which path answered. A message the classifier sent elsewhere still gets a
  // card saying so, rather than being mistaken for this workflow's work.
  function renderRouting(card, workflow) {
    const p = document.createElement("p");
    p.className = "meta";
    const badge = document.createElement("span");
    if (workflow === WORKFLOW_NAME) {
      badge.className = "badge";
      badge.textContent = `⚙️ ${workflow}`;
    } else {
      badge.className = "badge badge-pending";
      badge.textContent = workflow
        ? `⚙️ ${workflow}, not this workflow`
        : "💬 no workflow, the agent answered";
    }
    p.appendChild(badge);
    card.appendChild(p);
  }

  // Mid-conversation the values live in the workflow's state; on the last turn
  // they are handed over at the top level, which is the signal to submit.
  function collected(result) {
    if (!result) return null;
    if (result.place_form) return { values: result.place_form, ready: true };
    const state = result.state;
    if (state && state.values) return { values: state.values, ready: false };
    return null;
  }

  function renderValues(card, values, ready) {
    if (ready) line(card, "Ready to submit to the Log a Place page.", "reply-line");
    FIELDS.forEach(([key, label]) => {
      const value = values[key];
      const p = document.createElement("p");
      p.className = "meta";
      const name = document.createElement("strong");
      name.textContent = `${label}: `;
      const badge = document.createElement("span");
      const have = value !== undefined && value !== null && value !== "";
      badge.className = have ? "badge" : "badge badge-pending";
      badge.textContent = have ? "collected" : "not yet";
      const shown = value === true ? "yes" : (have ? String(value) : "(not set)");
      p.append(name, document.createTextNode(`${shown} `), badge);
      card.appendChild(p);
    });
  }

  function renderTurn({ message, reply, workflow, workflow_result }) {
    const card = document.createElement("div");
    card.className = "need-card";

    line(card, `You said: ${message}`, "meta");
    renderRouting(card, workflow);
    line(card, reply || "(no reply)", "reply-line");

    const found = collected(workflow_result);
    if (found) {
      renderValues(card, found.values, found.ready);
    } else {
      // Three different reasons to have nothing, and conflating them would
      // hide a conversation that has started behind one that never did.
      line(card, workflow === WORKFLOW_NAME
        ? "Nothing collected yet: the workflow is still asking."
        : "Nothing collected: this message was not about logging a place.",
        "empty-body");
    }

    resultList.prepend(card);
  }

  watchChatReplies({
    runId: "log-place-from-chat-run",
    listenId: "log-place-from-chat-listen",
    statusId: "log-place-from-chat-status",
    statusTextId: "log-place-from-chat-status-text",
    onTurn: renderTurn,
  });
});
