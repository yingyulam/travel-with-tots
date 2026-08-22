// Log a place workflow test page: set an area, name a place we don't have,
// say what it offers, submit it for verification. Four steps in order, each
// one only useful once the one above it has an answer.
document.addEventListener("DOMContentLoaded", () => {
  const useLocationBtn = document.getElementById("use-my-location");
  const areaInput = document.getElementById("place-area");
  const locationStatus = document.getElementById("location-status");
  const nameInput = document.getElementById("place-name");
  const resolveBtn = document.getElementById("resolve-place");
  const resolveStatus = document.getElementById("resolve-status");
  const amenityBar = document.getElementById("amenity-bar");
  const typeInput = document.getElementById("place-type");
  const submitBtn = document.getElementById("log-place-submit");
  const submitStatus = document.getElementById("log-place-status");
  const resultList = document.getElementById("log-place-result-list");

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "That didn't work.");
    return data;
  }

  // Step 1. The area is only ever text: it gets concatenated onto the place
  // name for the lookup, so sharing a location just fills this box in.
  useLocationBtn.addEventListener("click", () => {
    useLocationBtn.disabled = true;
    requestCoordinates({
      fallbackAdvice: () => "Type an area by hand instead.",
      onStatus: (text) => {
        locationStatus.textContent = text;
        if (!text.startsWith("Asking")) useLocationBtn.disabled = false;
      },
      onCoords: async (coords) => {
        locationStatus.textContent = "Naming that location…";
        try {
          const { area } = await postJson("/workflows/log-a-place/area", coords);
          areaInput.value = area;
          locationStatus.textContent = `You're near ${area}.`;
        } catch (e) {
          locationStatus.textContent =
            `${e.message} Type an area by hand instead.`;
        } finally {
          useLocationBtn.disabled = false;
        }
      },
    });
  });

  areaInput.addEventListener("input", () => {
    locationStatus.textContent = areaInput.value.trim()
      ? `Looking around “${areaInput.value.trim()}”.` : "No area set yet.";
  });

  // Step 2. Looking the name up is what turns a submission into something an
  // admin can check, so the resolved address is shown for confirmation rather
  // than quietly stored. The result is not kept: the run route resolves again
  // server-side rather than trusting coordinates sent back from here.
  resolveBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) {
      resolveStatus.textContent = "Type the place's name first.";
      return;
    }
    resolveBtn.disabled = true;
    resolveStatus.textContent = "Looking it up…";
    try {
      const data = await postJson("/workflows/log-a-place/resolve", {
        name, area: areaInput.value.trim(),
      });
      resolveStatus.textContent = data.resolved
        ? `Found: ${data.place.formatted_address}`
        : "Couldn't find that address. You can still submit it, "
          + "but without coordinates there is less for an admin to check.";
    } catch (e) {
      resolveStatus.textContent = e.message;
    } finally {
      resolveBtn.disabled = false;
    }
  });

  function checkedAmenities() {
    return [...amenityBar.querySelectorAll("input:checked")].map((box) => box.value);
  }

  // Step 4. Renders what the server said it stored, not what we hoped it
  // would: the point of the page is seeing the record, coordinates included.
  function renderStored(stored) {
    const card = document.createElement("div");
    card.className = "need-card";

    const title = document.createElement("h4");
    title.textContent = stored.name;
    card.appendChild(title);

    const rows = [
      ["Kind", stored.venue_type || "not given"],
      ["Area", stored.neighbourhood || "not given"],
      ["City", stored.city || "not resolved"],
      ["Address", stored.formatted_address || "not resolved"],
      ["Coordinates", stored.lat != null
        ? `${stored.lat.toFixed(5)}, ${stored.lng.toFixed(5)}`
        : "none, so it can't be distance-ranked yet"],
      ["Has", stored.amenities.length ? stored.amenities.join(", ") : "nothing recorded"],
    ];
    rows.forEach(([label, value]) => {
      const p = document.createElement("p");
      p.className = "meta";
      const strong = document.createElement("strong");
      strong.textContent = `${label}: `;
      p.append(strong, document.createTextNode(value));
      card.appendChild(p);
    });

    const gate = document.createElement("p");
    gate.className = "meta";
    const badge = document.createElement("span");
    badge.className = "badge badge-pending";
    badge.textContent = "awaiting verification";
    gate.append(document.createTextNode("Not searchable "), badge);
    card.appendChild(gate);

    resultList.prepend(card);
  }

  submitBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) {
      submitStatus.textContent = "A place needs a name.";
      return;
    }
    submitBtn.disabled = true;
    submitStatus.textContent = "Submitting…";
    const body = { name, area: areaInput.value.trim(),
                   venue_type: typeInput.value.trim() };
    checkedAmenities().forEach((key) => { body[key] = true; });
    try {
      const data = await postJson("/workflows/log-a-place/run", body);
      renderStored(data.stored);
      submitStatus.textContent =
        "Logged. It's on your dashboard and in the verification queue.";
      nameInput.value = "";
      typeInput.value = "";
      amenityBar.querySelectorAll("input:checked").forEach((box) => { box.checked = false; });
      resolveStatus.textContent = "Not looked up yet.";
    } catch (e) {
      submitStatus.textContent = e.message;
    } finally {
      submitBtn.disabled = false;
    }
  });
});
