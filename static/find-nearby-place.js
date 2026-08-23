// Find-a-nearby-place workflow test page: shows the places the agent found for
// each message sent through the real chat bubble. Driven by the
// "twt:chat-reply" event chatbot.js fires once per reply, so there is nothing
// to poll and a message cannot be processed twice.
//
// The same Run/Listen machine as plan-from-chat.js, deliberately: two workflow
// pages that arm themselves differently would be two things to learn.
document.addEventListener("DOMContentLoaded", () => {
  const runBtn = document.getElementById("find-nearby-place-run");
  const listenBtn = document.getElementById("find-nearby-place-listen");
  const status = document.getElementById("find-nearby-place-status");
  const statusText = document.getElementById("find-nearby-place-status-text");
  const resultList = document.getElementById("find-nearby-place-result-list");

  const WORKFLOW_NAME = "Find a nearby place";

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

  function line(card, text, className) {
    const p = document.createElement("p");
    p.className = className;
    p.textContent = text;
    card.appendChild(p);
    return p;
  }

  // Which path answered. A message the classifier sent elsewhere still gets a
  // card, saying so, rather than being silently dropped or mistaken for this
  // workflow's work.
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

  // One found place. Built from the record rather than from anything the model
  // wrote, and the link target is checked by twtSafeUrl (a global in
  // chatbot.js): a web result carries a URL nobody in this project chose, and
  // anything that is not plain http(s) loses its link rather than rendering.
  function renderPlace(card, place, source) {
    const row = document.createElement("div");
    row.className = "need-card";

    const name = document.createElement("p");
    name.className = "panel-title";
    name.textContent = place.name;
    row.appendChild(name);

    const facts = [place.type, place.neighbourhood].filter(Boolean);
    if (typeof place.distance_km === "number") {
      facts.push(`${place.distance_km}km away`);
    }
    if (facts.length) line(row, facts.join(" · "), "meta");
    if (place.reason) line(row, place.reason, "empty-body");

    const href = twtSafeUrl(place.maps_url);
    if (href) {
      const link = document.createElement("a");
      link.className = "maps-link";
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = source === "search"
        ? "🔗 Open result"
        : "📍 Open in Google Maps";
      row.appendChild(link);
    }
    card.appendChild(row);
  }

  function renderTurn({ message, reply, workflow, places, source }) {
    const card = document.createElement("div");
    card.className = "need-card";

    line(card, `You said: ${message}`, "meta");
    renderRouting(card, workflow);
    line(card, reply || "(no reply)", "reply-line");

    if (places && places.length) {
      // Curated or a live search: the whole point of having two sources is
      // being able to tell them apart.
      const badge = document.createElement("span");
      badge.className = source === "search" ? "badge badge-pending" : "badge";
      badge.textContent = source === "search" ? "web search" : "curated";
      const where = document.createElement("p");
      where.className = "meta";
      where.append(`${places.length} place(s) from `, badge);
      card.appendChild(where);

      places.forEach((place) => renderPlace(card, place, source));
    } else {
      // Two very different reasons to have no places, and conflating them
      // would hide a workflow that ran and found nothing behind one that
      // never ran at all.
      line(card, workflow === WORKFLOW_NAME
        ? "The workflow ran and found nothing for that need."
        : "No places: this message was not a nearby request.", "empty-body");
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
