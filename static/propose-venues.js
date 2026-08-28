// Propose venues page: run one proposal batch and report what it found.
document.addEventListener("DOMContentLoaded", () => {
  const runBtn = document.getElementById("propose-run");
  const batchInput = document.getElementById("propose-batch");
  const heading = document.getElementById("propose-heading");
  const resultList = document.getElementById("propose-result-list");

  const line = (text) => {
    const p = document.createElement("p");
    p.className = "meta";
    p.textContent = text;
    return p;
  };

  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    heading.textContent = "Searching, extracting, locating…";
    resultList.replaceChildren();
    try {
      const res = await fetch("/propose-venues/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_size: Number(batchInput.value) }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Run failed.");
      heading.textContent = `PROPOSED: ${data.proposed} new, ${data.skipped} skipped`;
      resultList.appendChild(line(`Queries: ${data.queries.join(" | ") || "none"}`));
      resultList.appendChild(line(`Model: ${data.model}, ${data.response_time}s`));
      resultList.appendChild(line(
        `On file: ${data.counts.pending} pending, ${data.counts.approved} approved, ` +
        `${data.counts.rejected} rejected.`));
      const link = document.createElement("a");
      link.className = "cta-secondary";
      link.href = "/venues/review";
      link.textContent = "Review them";
      resultList.appendChild(link);
    } catch (e) {
      heading.textContent = e.message || "Couldn't run right now.";
    } finally {
      runBtn.disabled = false;
    }
  });
});
