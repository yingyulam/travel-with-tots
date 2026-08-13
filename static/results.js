const POLL_INTERVAL_MS = 5000;

function truncate(text, length = 50) {
  if (text.length <= length) return text;
  const cut = text.slice(0, length);
  const lastSpace = cut.lastIndexOf(" ");
  return (lastSpace > 0 ? cut.slice(0, lastSpace) : cut) + "...";
}

function renderRow(r) {
  const details = document.createElement("details");
  details.className = "twt-result-card";

  const summary = document.createElement("summary");
  summary.className = "twt-result-summary";

  const rating = document.createElement("span");
  rating.className = `twt-result-rating ${r.rating === "up" ? "up" : "down"}`;
  rating.textContent = r.rating === "up" ? "👍" : "👎";
  summary.appendChild(rating);

  const main = document.createElement("div");
  main.className = "twt-result-main";
  const questionLine = document.createElement("p");
  questionLine.className = "twt-result-question";
  questionLine.textContent = truncate(r.question_display, 50);
  const responseLine = document.createElement("p");
  responseLine.className = "twt-result-response";
  responseLine.textContent = truncate(r.response_display, 50);
  main.append(questionLine, responseLine);
  summary.appendChild(main);

  const metaCol = document.createElement("div");
  metaCol.className = "twt-result-meta";
  const badge = document.createElement("span");
  badge.className = "twt-badge";
  badge.textContent = r.model;
  const timestamp = document.createElement("span");
  timestamp.className = "twt-result-timestamp";
  timestamp.textContent = r.timestamp;
  metaCol.append(badge, timestamp);
  summary.appendChild(metaCol);

  const chevron = document.createElement("span");
  chevron.className = "twt-result-chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.textContent = "⌄";
  summary.appendChild(chevron);

  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "twt-result-details";

  const questionP = document.createElement("p");
  questionP.className = "twt-chunk-text";
  const questionLabel = document.createElement("strong");
  questionLabel.textContent = "Question:";
  questionP.append(questionLabel, document.createTextNode(" " + r.question_display));
  body.appendChild(questionP);

  const responseP = document.createElement("p");
  responseP.className = "twt-chunk-text";
  const responseLabel = document.createElement("strong");
  responseLabel.textContent = "Response:";
  responseP.append(responseLabel, document.createTextNode(" " + r.response_display));
  body.appendChild(responseP);

  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent =
    `Model: ${r.model} · Rating: ${r.rating} · Timestamp: ${r.timestamp} · ` +
    `Response time: ${r.response_time}s · Input tokens: ${r.input_tokens} · ` +
    `Output tokens: ${r.output_tokens}`;
  body.appendChild(meta);

  details.appendChild(body);

  return details;
}

function renderSession(session) {
  const statsEl = document.getElementById(`results-stats-${session.kind}`);
  const headingEl = document.getElementById(`results-count-heading-${session.kind}`);
  const listEl = document.getElementById(`results-list-${session.kind}`);

  statsEl.textContent =
    `👍 ${session.stats.up} · 👎 ${session.stats.down} · ` +
    `${session.stats.percent_positive}% positive (${session.stats.total} rated)`;
  headingEl.textContent = `${session.title} (${session.results.length})`;

  listEl.innerHTML = "";
  if (session.results.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-plan";
    empty.textContent = "No ratings yet.";
    listEl.appendChild(empty);
  } else {
    session.results.forEach((r) => listEl.appendChild(renderRow(r)));
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("results-root");
  let knownTotal = Number(root.dataset.total);

  setInterval(async () => {
    try {
      const res = await fetch("/results/data");
      if (!res.ok) throw new Error(`/results/data returned ${res.status}`);
      const data = await res.json();
      const total = data.sessions.reduce((sum, s) => sum + s.stats.total, 0);
      if (total === knownTotal) return;
      knownTotal = total;
      data.sessions.forEach(renderSession);
    } catch (err) {
      console.error("Results auto-refresh check failed:", err);
    }
  }, POLL_INTERVAL_MS);
});
