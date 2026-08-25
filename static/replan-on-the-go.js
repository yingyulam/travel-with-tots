// Replan-on-the-go workflow test page: shows the replan the agent collected
// from each message sent through the real chat bubble. The Run/Listen machine
// is shared, in workflow-watch.js; only the rendering here is this page's own.
document.addEventListener("DOMContentLoaded", () => {
  const resultList = document.getElementById("replan-on-the-go-result-list");

  const WORKFLOW_NAME = "Replan on the go";

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

  // Mid-conversation the situation lives in the workflow's state; once the
  // parent confirms it is handed over at the top level, which is the signal
  // the trip page acts on.
  function collected(result) {
    if (!result) return null;
    if (result.replan_request) return { values: result.replan_request, ready: true };
    const state = result.state;
    if (state && state.values) return { values: state.values, ready: false };
    return null;
  }

  function renderRequest(card, values, ready) {
    const status = document.createElement("p");
    status.className = "meta";
    const badge = document.createElement("span");
    badge.className = ready ? "badge" : "badge badge-pending";
    badge.textContent = ready ? "ready to replan" : "still asking";
    status.appendChild(badge);
    card.appendChild(status);

    [["situation", "situation"], ["minutes", "how long"],
     ["note", "in their words"]].forEach(([key, label]) => {
      if (values[key] === undefined || values[key] === "") return;
      line(card, `${label}: ${values[key]}`, "meta");
    });
  }

  // A turn the classifier sent elsewhere is not this page's to show. One line
  // saying where it went, so a misroute stays visible and a page that has just
  // been armed does not look broken, and `false` so Run stays armed.
  function notMine(workflow) {
    const card = document.createElement("div");
    card.className = "need-card";
    line(card, workflow
      ? `That message went to ${workflow}. Still waiting for one this workflow handles.`
      : "That message was answered by the agent, not a workflow. Still waiting.",
      "empty-body");
    resultList.prepend(card);
    return false;
  }

  function renderTurn({ message, reply, workflow, workflow_result }) {
    if (workflow !== WORKFLOW_NAME) return notMine(workflow);

    const card = document.createElement("div");
    card.className = "need-card";

    line(card, `You said: ${message}`, "meta");
    renderRouting(card, workflow);
    line(card, reply || "(no reply)", "reply-line");

    const found = collected(workflow_result);
    if (found) {
      renderRequest(card, found.values, found.ready);
    } else {
      // Three different reasons to have nothing, and conflating them would
      // hide a conversation that started behind one that never did.
      line(card, workflow === WORKFLOW_NAME
        ? "Nothing collected yet: the workflow is still asking, or no trip is open."
        : "Nothing collected: this message was not about replanning.",
        "empty-body");
    }

    resultList.prepend(card);
    return true;
  }

  watchChatReplies({
    runId: "replan-on-the-go-run",
    listenId: "replan-on-the-go-listen",
    statusId: "replan-on-the-go-status",
    statusTextId: "replan-on-the-go-status-text",
    workflow: WORKFLOW_NAME,
    onTurn: renderTurn,
  });
});
