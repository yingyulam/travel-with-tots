const POLL_INTERVAL_MS = 5000;

function truncate(text, length = 50) {
  if (text.length <= length) return text;
  const cut = text.slice(0, length);
  const lastSpace = cut.lastIndexOf(" ");
  return (lastSpace > 0 ? cut.slice(0, lastSpace) : cut) + "...";
}

function renderRow(r) {
  const details = document.createElement("details");
  details.className = "twt-chunk-row";

  const summary = document.createElement("summary");
  summary.appendChild(document.createTextNode(r.rating === "up" ? "👍 " : "👎 "));
  const badge = document.createElement("span");
  badge.className = "twt-badge";
  badge.textContent = r.model;
  summary.appendChild(badge);
  summary.appendChild(
    document.createTextNode(` ${truncate(r.question, 50)} → ${truncate(r.response, 50)}`)
  );
  details.appendChild(summary);

  const questionP = document.createElement("p");
  questionP.className = "twt-chunk-text";
  const questionLabel = document.createElement("strong");
  questionLabel.textContent = "Question:";
  questionP.append(questionLabel, document.createTextNode(" " + r.question));
  details.appendChild(questionP);

  const responseP = document.createElement("p");
  responseP.className = "twt-chunk-text";
  const responseLabel = document.createElement("strong");
  responseLabel.textContent = "Response:";
  responseP.append(responseLabel, document.createTextNode(" " + r.response));
  details.appendChild(responseP);

  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent =
    `Model: ${r.model} · Rating: ${r.rating} · Timestamp: ${r.timestamp} · ` +
    `Response time: ${r.response_time}s · Input tokens: ${r.input_tokens} · ` +
    `Output tokens: ${r.output_tokens}`;
  details.appendChild(meta);

  return details;
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("results-root");
  const statsEl = document.getElementById("results-stats");
  const headingEl = document.getElementById("results-count-heading");
  const listEl = document.getElementById("results-list");
  let knownTotal = Number(root.dataset.total);

  setInterval(async () => {
    try {
      const res = await fetch("/results/data");
      if (!res.ok) throw new Error(`/results/data returned ${res.status}`);
      const data = await res.json();
      if (data.stats.total === knownTotal) return;
      knownTotal = data.stats.total;

      statsEl.textContent =
        `👍 ${data.stats.up} · 👎 ${data.stats.down} · ` +
        `${data.stats.percent_positive}% positive (${data.stats.total} rated)`;
      headingEl.textContent = `Rated responses (${data.results.length})`;

      listEl.innerHTML = "";
      if (data.results.length === 0) {
        const empty = document.createElement("p");
        empty.className = "empty-plan";
        empty.textContent = "No ratings yet.";
        listEl.appendChild(empty);
      } else {
        data.results.forEach((r) => listEl.appendChild(renderRow(r)));
      }
    } catch (err) {
      console.error("Results auto-refresh check failed:", err);
    }
  }, POLL_INTERVAL_MS);
});
