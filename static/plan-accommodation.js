// Accommodation picker on the planning form: a pin the planner measures from.
//
// Leaflet with OpenStreetMap tiles rather than an embedded Google map, for the
// reason set out in log-a-place.js: every Google embedding option needs the API
// key in the browser, and this app keeps all three of its keys server-side.
// Searching by name still goes through Google Places, via the server, so the
// results are Google's even though the tiles are not.
//
// Deliberately a smaller thing than the Log a Place picker. That one is
// building a venue, so it reverse-geocodes the pin to name the spot. This one
// only needs coordinates: the text field beside it is whatever the parent chose
// to call the place, and a plan is not stored against a pin the way a venue is.
document.addEventListener("DOMContentLoaded", () => {
  const mapHost = document.getElementById("accommodation-map");
  if (!mapHost) return;

  const nameInput = document.getElementById("accommodation");
  const latInput = document.getElementById("accommodation-lat");
  const lngInput = document.getElementById("accommodation-lng");
  const status = document.getElementById("accommodation-status");
  const searchHint = document.getElementById("accommodation-search-hint");
  const results = document.getElementById("accommodation-results");

  // Results appear as they type, so every keystroke is a potential Google
  // Places call, billed per request. These two are what keep that honest:
  // wait for a pause rather than firing per character, and do not search a
  // fragment too short to mean anything ("Sy" matches most of the city).
  const TYPING_PAUSE_MS = 350;
  const MIN_QUERY = 3;

  // Where the map opens before anyone has said anything: the city the curated
  // venues are in, so the first pin is somewhere plausible.
  const START = { lat: 49.2827, lng: -123.1207, zoom: 12 };
  const PIN_ZOOM = 15;
  const PIN_STYLE = {
    radius: 9, color: "#1e88e5", fillColor: "#1e88e5", fillOpacity: 0.7,
  };

  const map = L.map(mapHost).setView([START.lat, START.lng], START.zoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    // Required by the OpenStreetMap tile usage policy, and Leaflet only shows
    // it if we pass it. Do not remove.
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  // A circleMarker rather than L.marker: the default marker needs icon images
  // from Leaflet's dist/images, which are not vendored, so it would render as
  // a broken image. A circle needs no assets.
  let pin = null;

  function setPin(lat, lng, { recentre = false, label = "" } = {}) {
    if (pin) {
      pin.setLatLng([lat, lng]);
    } else {
      pin = L.circleMarker([lat, lng], PIN_STYLE).addTo(map);
    }
    if (recentre) map.setView([lat, lng], PIN_ZOOM);
    latInput.value = lat;
    lngInput.value = lng;
    status.textContent = label
      ? `Staying at ${label}. We'll plan the day around it.`
      : `Pin set at ${lat.toFixed(4)}, ${lng.toFixed(4)}.`;
  }

  function clearPin() {
    if (pin) {
      pin.remove();
      pin = null;
    }
    // Both, always: read_form drops half a coordinate, so leaving one behind
    // would silently discard the other too.
    latInput.value = "";
    lngInput.value = "";
    status.textContent = "No pin, so the day is planned without a start point.";
  }

  // A pin that survived a failed submit, so the choice is not lost.
  const saved = { lat: parseFloat(latInput.value), lng: parseFloat(lngInput.value) };
  if (!Number.isNaN(saved.lat) && !Number.isNaN(saved.lng)) {
    setPin(saved.lat, saved.lng, { recentre: true, label: nameInput.value.trim() });
  }

  map.on("click", (event) => setPin(event.latlng.lat, event.latlng.lng));

  document.getElementById("accommodation-clear")
    ?.addEventListener("click", clearPin);

  // The query whose results are on screen, so re-searching it is skipped.
  // Set when a result is picked, since that writes the field.
  let settled = null;
  let pending = null;      // the typing-pause timer
  let inFlight = null;     // the request to abandon when a newer one starts

  function closeResults() {
    results.replaceChildren();
    nameInput.setAttribute("aria-expanded", "false");
  }

  function choosePlace(place) {
    // The name they picked replaces whatever they typed, so the text and the
    // pin describe the same place. The AI prompt reads this field. Writing to
    // the field does not fire `input`, but a later edit would, so `settled`
    // stops the choice immediately re-searching for itself.
    nameInput.value = place.name;
    settled = place.name;
    closeResults();
    searchHint.textContent = "";
    if (place.lat != null && place.lng != null) {
      setPin(place.lat, place.lng,
             { recentre: true, label: place.address || place.name });
    }
  }

  function renderResults(places) {
    results.replaceChildren();
    nameInput.setAttribute("aria-expanded", places.length ? "true" : "false");
    places.forEach((place) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "place-result";
      card.setAttribute("role", "option");
      const name = document.createElement("strong");
      name.textContent = place.name;
      const detail = document.createElement("span");
      detail.className = "meta";
      detail.textContent = place.address || "";
      card.append(name, detail);
      card.addEventListener("click", () => choosePlace(place));
      results.appendChild(card);
    });
  }

  async function runSearch(query) {
    // Abandon whatever was already running. Without this the answer to "Syl"
    // can land after the answer to "Sylvia" and overwrite it, which is the
    // classic way a live search shows results for a query nobody can see.
    if (inFlight) inFlight.abort();
    const request = new AbortController();
    inFlight = request;
    searchHint.textContent = "Searching…";
    try {
      const centre = map.getCenter();
      const res = await fetch("/plan/accommodation-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, lat: centre.lat, lng: centre.lng }),
        signal: request.signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't search for that.");
      if (!data.places.length) {
        closeResults();
        searchHint.textContent =
          "Nothing matched. Click the map to drop the pin yourself.";
        return;
      }
      searchHint.textContent = "Pick the right one:";
      renderResults(data.places);
    } catch (e) {
      // A cancelled request is this code's own doing, not a failure to report.
      if (e.name === "AbortError") return;
      // The typed text is still a complete answer on its own, and a pin they
      // already dropped still stands, so this never costs them what they had.
      closeResults();
      searchHint.textContent = `${e.message} Click the map to pin it instead.`;
    } finally {
      if (inFlight === request) inFlight = null;
    }
  }

  function searchAfterPause() {
    clearTimeout(pending);
    const query = nameInput.value.trim();
    if (query === settled) return;
    settled = null;
    if (query.length < MIN_QUERY) {
      // Below the threshold there is nothing to show, and an old result list
      // for a query they have deleted is worse than none.
      if (inFlight) inFlight.abort();
      closeResults();
      searchHint.textContent = "";
      return;
    }
    pending = setTimeout(() => runSearch(query), TYPING_PAUSE_MS);
  }

  nameInput.addEventListener("input", searchAfterPause);
  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      clearTimeout(pending);
      if (inFlight) inFlight.abort();
      closeResults();
      searchHint.textContent = "";
    }
    if (e.key === "Enter") {
      // The field lives inside the plan form, so Enter would submit it. With
      // no button left, this is also how someone impatient skips the pause.
      e.preventDefault();
      clearTimeout(pending);
      const query = nameInput.value.trim();
      if (query.length >= MIN_QUERY) runSearch(query);
    }
  });
});
