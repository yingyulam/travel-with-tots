// Form Extractor component test page: send a free text description, render the
// form it produced. Deliberately small and self-contained, same pattern as
// search-web.js. The point of the rendering is to show which fields came from
// the description and which fell back to a default, so a quietly wrong
// extraction is visible rather than hidden behind a filled-in form.
document.addEventListener("DOMContentLoaded", () => {
  const descriptionInput = document.getElementById("extract-description");
  const runBtn = document.getElementById("extract-run");
  const heading = document.getElementById("extract-heading");
  const resultList = document.getElementById("extract-result-list");

  // Fields the parent never types into a description, so showing them as
  // "default" would be noise rather than information.
  const INTERNAL_FIELDS = ["child_ids", "plan_child_id", "revise_feedback"];

  function formatValue(value) {
    if (Array.isArray(value)) {
      if (!value.length) return "(none)";
      // Naps are objects; everything else is a list of plain strings.
      return value
        .map((item) => (typeof item === "object" && item !== null
          ? `${item.start} for ${item.duration_min} min`
          : item))
        .join(", ");
    }
    if (typeof value === "boolean") return value ? "yes" : "no";
    return value === "" || value === null ? "(not set)" : String(value);
  }

  function renderForm(form, found) {
    resultList.innerHTML = "";
    const fields = Object.keys(form)
      .filter((field) => !INTERNAL_FIELDS.includes(field))
      .sort((a, b) => {
        // What the description supplied first: that is what needs checking.
        const byFound = found.includes(b) - found.includes(a);
        return byFound || a.localeCompare(b);
      });

    fields.forEach((field) => {
      const fromDescription = found.includes(field);
      const row = document.createElement("p");
      row.className = "meta";
      const label = document.createElement("strong");
      label.textContent = `${field.replace(/_/g, " ")}: `;
      const badge = document.createElement("span");
      badge.className = fromDescription ? "badge" : "badge badge-pending";
      badge.textContent = fromDescription ? "from your words" : "default";
      row.append(label, document.createTextNode(formatValue(form[field]) + " "), badge);
      resultList.appendChild(row);
    });
  }

  runBtn.addEventListener("click", async () => {
    const description = descriptionInput.value.trim();
    if (!description) return;
    runBtn.disabled = true;
    heading.textContent = "Reading…";
    resultList.innerHTML = "";
    try {
      const res = await fetch("/extract-form/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Extraction failed.");
      const count = data.found.length;
      heading.textContent =
        `${count} field${count === 1 ? "" : "s"} came from your description `
        + `(${data.model}, ${data.response_time}s)`;
      renderForm(data.form, data.found);
    } catch (e) {
      heading.textContent = e.message || "Couldn't read that right now.";
    } finally {
      runBtn.disabled = false;
    }
  });
});
