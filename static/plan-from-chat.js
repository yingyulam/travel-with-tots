// Fill-the-form-from-chat workflow test page: shows the form the agent extracted from
// each message sent through the real chat bubble. Driven by the
// "twt:chat-reply" event chatbot.js fires once per reply, so there is nothing
// to poll and a message cannot be processed twice.
document.addEventListener("DOMContentLoaded", () => {
  const runBtn = document.getElementById("plan-from-chat-run");
  const listenBtn = document.getElementById("plan-from-chat-listen");
  const status = document.getElementById("plan-from-chat-status");
  const statusText = document.getElementById("plan-from-chat-status-text");
  const resultList = document.getElementById("plan-from-chat-result-list");

  // Fields a parent never describes, so showing them as "default" is noise.
  const INTERNAL_FIELDS = ["child_ids", "plan_child_id", "revise_feedback"];

  // "once" stops after the next message; "many" keeps going. Kept as one
  // variable so Run and Listen cannot both be armed at the same time.
  let mode = "off";

  // The state drives the banner's colour in CSS, so the wording and the look
  // cannot disagree about whether the page is armed.
  function setMode(next) {
    mode = next;
    listenBtn.textContent = mode === "many" ? "⏹ Stop listening" : "👂 Listen";
    status.dataset.state = mode;
    statusText.textContent = {
      off: "Not watching",
      once: "Waiting for your next message",
      many: "Listening: every message you send will be processed",
    }[mode];
  }

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

  function renderTurn({ message, reply, tool_calls }) {
    const card = document.createElement("div");
    card.className = "need-card";

    const said = document.createElement("p");
    said.className = "meta";
    said.textContent = `You said: ${message}`;
    card.appendChild(said);

    renderReply(card, reply);

    // The extractor's real result rides on the tool call, not the reply text.
    const extraction = (tool_calls || []).find(
      (call) => call.name === "extract_form_tool" && call.data && call.data.form);

    if (!extraction) {
      // Two very different reasons to have no form, and conflating them would
      // hide a real extraction failure behind "it did something else".
      const attempted = (tool_calls || []).find(
        (call) => call.name === "extract_form_tool");
      const used = (tool_calls || []).map((c) => c.name).join(", ");
      const note = document.createElement("p");
      note.className = "empty-body";
      note.textContent = attempted
        ? `The extractor ran but returned no form. ${attempted.output}`
        : used
          ? `No form extracted, the agent used ${used} instead.`
          : "No form extracted, the agent answered directly.";
      card.appendChild(note);
    } else {
      renderForm(card, extraction.data.form, extraction.data.found || []);
    }

    resultList.prepend(card);
  }

  document.addEventListener("twt:chat-reply", (event) => {
    if (mode === "off") return;
    renderTurn(event.detail);
    setMode(mode === "once" ? "off" : "many");
  });

  runBtn.addEventListener("click", () => setMode("once"));
  listenBtn.addEventListener("click", () => setMode(mode === "many" ? "off" : "many"));

  setMode("off");
});
