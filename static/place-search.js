// Place Search component test page: a query in, candidates out. Deliberately
// small and self-contained, same pattern as search-web.js.
document.addEventListener("DOMContentLoaded", () => {
  const queryInput = document.getElementById("query");
  const latInput = document.getElementById("lat");
  const lngInput = document.getElementById("lng");
  const runBtn = document.getElementById("run");
  const heading = document.getElementById("heading");
  const resultList = document.getElementById("result-list");

  // Every field the component returns, so this page shows the whole result
  // rather than the subset Log a Place happens to use.
  const FIELDS = [
    ["name", "Name"],
    ["type", "Kind"],
    ["address", "Address"],
    ["city", "City"],
    ["neighbourhood", "Area"],
  ];

  function renderPlaces(places) {
    resultList.replaceChildren();
    places.forEach((place) => {
      const card = document.createElement("div");
      card.className = "need-card";
      FIELDS.forEach(([key, label]) => {
        const p = document.createElement("p");
        p.className = "meta";
        const strong = document.createElement("strong");
        strong.textContent = `${label}: `;
        p.append(strong, document.createTextNode(place[key] || "(empty)"));
        card.appendChild(p);
      });
      const coords = document.createElement("p");
      coords.className = "meta";
      const label = document.createElement("strong");
      label.textContent = "Coordinates: ";
      coords.append(label, document.createTextNode(
        place.lat != null ? `${place.lat}, ${place.lng}` : "(none)"));
      card.appendChild(coords);
      resultList.appendChild(card);
    });
  }

  async function run() {
    const query = queryInput.value.trim();
    if (!query) {
      heading.textContent = "Type a query first.";
      return;
    }
    runBtn.disabled = true;
    heading.textContent = "Searching…";
    resultList.replaceChildren();
    try {
      const body = { query };
      // Sent only when both are given: a half-filled bias is not a location.
      if (latInput.value.trim() && lngInput.value.trim()) {
        body.lat = Number(latInput.value);
        body.lng = Number(lngInput.value);
      }
      const res = await fetch("/place-search/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't search.");
      heading.textContent = data.places.length
        ? `${data.places.length} result(s) for “${data.query}”.`
        : `Nothing matched “${data.query}”. That is an answer, not a failure.`;
      renderPlaces(data.places);
    } catch (e) {
      heading.textContent = e.message;
    } finally {
      runBtn.disabled = false;
    }
  }

  runBtn.addEventListener("click", run);
  queryInput.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
});
