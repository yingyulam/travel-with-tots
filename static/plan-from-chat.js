// Fill-the-form-from-chat workflow test page: shows the form the agent extracted from
// each message sent through the real chat bubble. Driven by the
// "twt:chat-reply" event chatbot.js fires once per reply, so there is nothing
// to poll and a message cannot be processed twice.
document.addEventListener("DOMContentLoaded", () => {
  const resultList = document.getElementById("plan-from-chat-result-list");

  // Fields a parent never describes, so showing them as "default" is noise.
  const INTERNAL_FIELDS = ["child_ids", "plan_child_id", "revise_feedback"];


  function formatValue(value) {
    if (Array.isArray(value)) {
      if (!value.length) return "(none)";
      return value
        .map((item) => (typeof item === "object" && item !== null
          ? `${item.start} for ${item.duration_min} min`
          : item))
        .join(", ");
    }
    if (typeof value === "boolean") return value ? "yes" : "no";
    return value === "" || value === null ? "(not set)" : String(value);
  }

  // The agent answers in markdown, which nothing on this page renders, so the
  // whole itinerary arrives as one blob of "## ... **Morning** - 9:00 AM: ...".
  // Split it into a block per line and lift each stop's label out of its
  // asterisks, so a reader can scan the stops instead of parsing a paragraph.
  function renderReply(card, reply) {
    const block = document.createElement("div");
    block.className = "reply-blocks";
    const lines = (reply || "").split("\n").map((line) => line.trim()).filter(Boolean);
    (lines.length ? lines : ["(no reply)"]).forEach((line) => {
      const titled = line.match(/^#{1,6}\s+(.*)$/);
      const text = titled ? titled[1] : line;
      const p = document.createElement("p");
      p.className = titled ? "reply-title" : "reply-line";

      const labelled = text.match(/^\*\*(.+?)\*\*\s*(.*)$/);
      if (labelled) {
        const strong = document.createElement("strong");
        strong.textContent = labelled[1];
        p.append(strong, document.createTextNode(labelled[2] ? ` ${labelled[2]}` : ""));
      } else {
        p.textContent = text.replace(/\*\*/g, "");
      }
      block.appendChild(p);
    });
    card.appendChild(block);
  }

  function renderForm(card, form, found) {
    Object.keys(form)
      .filter((field) => !INTERNAL_FIELDS.includes(field))
      .sort((a, b) => (found.includes(b) - found.includes(a)) || a.localeCompare(b))
      .forEach((field) => {
        const fromWords = found.includes(field);
        const p = document.createElement("p");
        p.className = "meta";
        const label = document.createElement("strong");
        label.textContent = `${field.replace(/_/g, " ")}: `;
        const badge = document.createElement("span");
        badge.className = fromWords ? "badge" : "badge badge-pending";
        badge.textContent = fromWords ? "from your words" : "default";
        p.append(label, document.createTextNode(formatValue(form[field]) + " "), badge);
        card.appendChild(p);
      });
  }

  // Where the form lives depends on which path answered. Mid-conversation the
  // workflow holds it in its state; on the last turn it is handed over at the
  // top level. Neither is a tool call any more, so the tool call is only the
  // fallback for a message the agent answered instead.
  function collectedForm(result) {
    if (!result) return null;
    if (result.form) return { form: result.form, found: result.found || [] };
    const state = result.state;
    // An empty form is the opening turn, before anything has been asked.
    if (state && state.form && Object.keys(state.form).length) {
      return { form: state.form, found: state.found || [] };
    }
    return null;
  }

  const WORKFLOW_NAME = "Fill the form from a chat message";

  // A turn the classifier sent elsewhere is not this page's to show. One line
  // saying where it went, so a misroute stays visible and a page that has just
  // been armed does not look broken, and `false` so Run stays armed.
  function notMine(workflow) {
    const card = document.createElement("div");
    card.className = "need-card";
    const note = document.createElement("p");
    note.className = "empty-body";
    note.textContent = workflow
      ? `That message went to ${workflow}. Still waiting for one this workflow handles.`
      : "That message was answered by the agent, not a workflow. Still waiting.";
    card.appendChild(note);
    resultList.prepend(card);
    return false;
  }

  function renderTurn({ message, reply, workflow, workflow_result }) {
    if (workflow !== WORKFLOW_NAME) return notMine(workflow);

    const card = document.createElement("div");
    card.className = "need-card";

    const said = document.createElement("p");
    said.className = "meta";
    said.textContent = `You said: ${message}`;
    card.appendChild(said);

    const routed = document.createElement("p");
    routed.className = "meta";
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = `⚙️ ${workflow}`;
    routed.appendChild(badge);
    card.appendChild(routed);

    renderReply(card, reply);

    // Only this workflow's turns reach here, so the form is either in its
    // state or handed over at the end. The old fallback to an
    // extract_form_tool call was for agent-answered turns, which the guard
    // above now keeps off this page entirely.
    const collected = collectedForm(workflow_result);
    if (collected) {
      renderForm(card, collected.form, collected.found);
    } else {
      const note = document.createElement("p");
      note.className = "empty-body";
      note.textContent = "Nothing collected yet: the workflow is still asking.";
      card.appendChild(note);
    }

    resultList.prepend(card);
    return true;
  }

  watchChatReplies({
    runId: "plan-from-chat-run",
    listenId: "plan-from-chat-listen",
    statusId: "plan-from-chat-status",
    statusTextId: "plan-from-chat-status-text",
    workflow: WORKFLOW_NAME,
    onTurn: renderTurn,
  });
});
