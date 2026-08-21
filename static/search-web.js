// Web Search component test page: save/show a Tavily API key, run a query,
// render results. Deliberately small and self-contained.
document.addEventListener("DOMContentLoaded", () => {
  const keyInput = document.getElementById("tavily-key-input");
  const showBtn = document.getElementById("tavily-key-show");
  const saveBtn = document.getElementById("tavily-key-save");
  const statusEl = document.getElementById("tavily-key-status");
  const queryInput = document.getElementById("search-web-query");
  const runBtn = document.getElementById("search-web-run");
  const heading = document.getElementById("search-web-heading");
  const resultList = document.getElementById("search-web-result-list");

  // Only toggles visibility of whatever's currently typed -- the server
  // never sends a previously-saved key back to the browser.
  showBtn.addEventListener("click", () => {
    const showing = keyInput.type === "text";
    keyInput.type = showing ? "password" : "text";
    showBtn.textContent = showing ? "Show" : "Hide";
  });

  saveBtn.addEventListener("click", async () => {
    const key = keyInput.value.trim();
    if (!key) return;
    saveBtn.disabled = true;
    try {
      const res = await fetch("/search-web/key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't save the key.");
      statusEl.textContent = "Key set ✓";
      keyInput.value = "";
      keyInput.type = "password";
      showBtn.textContent = "Show";
    } catch (e) {
      statusEl.textContent = e.message || "Couldn't save the key.";
    } finally {
      saveBtn.disabled = false;
    }
  });

  function renderResults(results) {
    resultList.innerHTML = "";
    if (!results.length) {
      resultList.innerHTML = '<p class="empty-plan">No results found.</p>';
      return;
    }
    results.forEach((r) => {
      const card = document.createElement("div");
      card.className = "search-web-result-card";
      const h3 = document.createElement("h3");
      const link = document.createElement("a");
      link.href = r.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = r.title || r.url;
      h3.appendChild(link);
      const urlLine = document.createElement("p");
      urlLine.className = "search-web-result-url";
      urlLine.textContent = r.url;
      const snippet = document.createElement("p");
      snippet.className = "search-web-result-snippet";
      snippet.textContent = r.snippet;
      card.append(h3, urlLine, snippet);
      resultList.appendChild(card);
    });
  }

  runBtn.addEventListener("click", async () => {
    const query = queryInput.value.trim();
    if (!query) return;
    runBtn.disabled = true;
    heading.textContent = "Searching…";
    resultList.innerHTML = "";
    try {
      const res = await fetch("/search-web/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Search failed.");
      heading.textContent = `RESULTS: ${data.results.length} hit${data.results.length === 1 ? "" : "s"}`;
      renderResults(data.results);
    } catch (e) {
      heading.textContent = e.message || "Couldn't search right now.";
    } finally {
      runBtn.disabled = false;
    }
  });
});
