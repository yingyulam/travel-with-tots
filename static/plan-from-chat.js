// Fill-the-form-from-chat workflow test page: what the workflow collected from
// each message, beside the form fields that collection would actually post.
// Driven by the "twt:chat-reply" event chatbot.js fires once per reply, so there
// is nothing to poll and a message cannot be processed twice.
//
// Two panels rather than one, because the two are not the same thing and only
// the pair is verifiable. The left is the workflow's own state; the right is the
// result of twtPlanFormFields, the same mapping the real hand-off performs, so
// this page cannot agree with itself while disagreeing with what /plan receives.
// The interesting bugs live in that mapping: naps become parallel
// nap_start/nap_duration lists, a checkbox becomes the literal "on", and an
// empty value is dropped so the server falls back to its own default.
//
// Admin-only and for desktop, so the panels sit side by side rather than
// stacking. They collapse to one column only when there is genuinely no room.
document.addEventListener("DOMContentLoaded", () => {
  const resultList = document.getElementById("plan-from-chat-result-list");

  // Fields a parent never describes, so showing them as "default" is noise.
  const INTERNAL_FIELDS = ["child_ids", "plan_child_id", "revise_feedback",
    // Carried and posted, but a parent reads the accommodation's name,
    // not the two decimals behind it.
    "accommodation_lat", "accommodation_lng"];


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

  // Three sources now, not two: the parent said it, memory recalled it, or it
  // is riding on the form's own default. Collapsing the middle one into either
  // neighbour is the thing worth catching here, since a recalled value looks
  // exactly like a supplied one in the finished form.
  function provenance(field, found, remembered) {
    if (found.includes(field)) return ["badge", "from your words"];
    if (remembered.includes(field)) return ["badge badge-deterministic", "remembered"];
    return ["badge badge-pending", "default"];
  }

  function renderForm(panel, form, found, remembered) {
    Object.keys(form)
      .filter((field) => !INTERNAL_FIELDS.includes(field))
      .sort((a, b) => (found.includes(b) - found.includes(a))
        || (remembered.includes(b) - remembered.includes(a))
        || a.localeCompare(b))
      .forEach((field) => {
        const [className, label] = provenance(field, found, remembered);
        const p = document.createElement("p");
        p.className = "meta";
        const name = document.createElement("strong");
        name.textContent = `${field.replace(/_/g, " ")}: `;
        const badge = document.createElement("span");
        badge.className = className;
        badge.textContent = label;
        p.append(name, document.createTextNode(formatValue(form[field]) + " "), badge);
        panel.appendChild(p);
      });
  }

  // What a hand-off would post, read off the shared mapping rather than
  // recomputed. Internal fields are kept here, unlike the panel on the left:
  // plan_child_id is not something a parent describes, but it is posted, and it
  // is the field that decides whose age /plan plans around.
  function renderPosted(panel, form) {
    const fields = twtPlanFormFields(form);
    if (!fields.length) {
      const note = document.createElement("p");
      note.className = "empty-body";
      note.textContent = "Nothing to post yet.";
      panel.appendChild(note);
      return;
    }
    fields.forEach(([name, value]) => {
      const p = document.createElement("p");
      p.className = "meta posted-field";
      const key = document.createElement("code");
      key.textContent = name;
      p.append(key, document.createTextNode(` = ${value === "" ? "(empty)" : value}`));
      panel.appendChild(p);
    });
  }

  // One titled column of the comparison.
  function panelFor(row, title, hint) {
    const panel = document.createElement("div");
    panel.className = "wf-panel";
    const heading = document.createElement("p");
    heading.className = "panel-title";
    heading.textContent = title;
    panel.appendChild(heading);
    if (hint) {
      const note = document.createElement("p");
      note.className = "field-hint";
      note.textContent = hint;
      panel.appendChild(note);
    }
    row.appendChild(panel);
    return panel;
  }

  // Where the form lives depends on which path answered. Mid-conversation the
  // workflow holds it in its state; on the last turn it is handed over at the
  // top level. Neither is a tool call any more, so the tool call is only the
  // fallback for a message the agent answered instead.
  function collectedForm(result) {
    if (!result) return null;
    if (result.form) {
      return { form: result.form, found: result.found || [],
               remembered: result.remembered || [] };
    }
    const state = result.state;
    // An empty form is the opening turn, before anything has been asked.
    if (state && state.form && Object.keys(state.form).length) {
      return { form: state.form, found: state.found || [],
               remembered: state.remembered || [] };
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
      const row = document.createElement("div");
      row.className = "wf-compare";
      renderForm(panelFor(row, "What the workflow has collected",
                          "The workflow's own state, and where each value came from."),
                 collected.form, collected.found, collected.remembered);
      renderPosted(panelFor(row, "What it would post to /plan",
                            "The real form field names, from the same mapping the hand-off uses."),
                   collected.form);
      card.appendChild(row);
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
