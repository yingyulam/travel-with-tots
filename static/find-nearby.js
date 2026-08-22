// Find Nearby component test page: resolve a location (browser geolocation
// or typed by hand), then find kid-friendly places matching a need.
// Deliberately small and self-contained, same pattern as search-web.js.
document.addEventListener("DOMContentLoaded", () => {
  const keyInput = document.getElementById("maps-key-input");
  const showBtn = document.getElementById("maps-key-show");
  const saveBtn = document.getElementById("maps-key-save");
  const keyStatus = document.getElementById("maps-key-status");
  const useLocationBtn = document.getElementById("use-my-location");
  const manualInput = document.getElementById("manual-location");
  const setLocationBtn = document.getElementById("set-location");
  const locationStatus = document.getElementById("location-status");
  const locationPanel = document.getElementById("location-panel");
  const manualHint = document.getElementById("manual-location-hint");
  const needBar = document.getElementById("need-bar");
  const heading = document.getElementById("find-nearby-heading");
  const resultList = document.getElementById("find-nearby-result-list");

  // Whichever location the parent has chosen: either {lat, lng} from the
  // browser or {address} typed by hand. Sent with every need request so the
  // server can resolve it the same way in both cases.
  let location = null;

  // Sharing a location needs no key; only a typed address does, since
  // something has to turn text into coordinates. Say so before the parent
  // types rather than failing the request afterwards.
  let keyIsSet = locationPanel.dataset.keySet === "yes";

  function syncAddressAvailability() {
    manualInput.disabled = !keyIsSet;
    setLocationBtn.disabled = !keyIsSet;
    manualHint.textContent = keyIsSet ? "" :
      "Address search needs a Google Maps API key (see below). " +
      "Sharing your location works without one.";
  }
  syncAddressAvailability();

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
      const res = await fetch("/find-nearby/key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't save the key.");
      keyStatus.textContent = "Key set ✓";
      keyInput.value = "";
      keyInput.type = "password";
      showBtn.textContent = "Show";
      keyIsSet = true;
      syncAddressAvailability();
    } catch (e) {
      keyStatus.textContent = e.message || "Couldn't save the key.";
    } finally {
      saveBtn.disabled = false;
    }
  });

  // Never tell the parent to "set one by hand" when that input is disabled
  // for want of a key: point at whichever escape hatch actually exists.
  function fallbackAdvice() {
    return keyIsSet
      ? "Set a location by hand instead."
      : "Allow location access in your browser, or add a Google Maps API key "
        + "below to search by address instead.";
  }

  // The asking, and its guards, live in geolocate.js so the log-a-place page
  // shares them rather than re-learning why the button looked dead.
  useLocationBtn.addEventListener("click", () => {
    useLocationBtn.disabled = true;
    requestCoordinates({
      fallbackAdvice,
      onStatus: (text) => {
        locationStatus.textContent = text;
        // Re-enable on anything that isn't the in-flight message, so a refusal
        // doesn't leave the button stuck.
        if (!text.startsWith("Asking")) useLocationBtn.disabled = false;
      },
      onCoords: (coords) => {
        location = coords;
        locationStatus.textContent =
          `Using your location (${coords.lat.toFixed(4)}, ${coords.lng.toFixed(4)}). Pick a need below.`;
        useLocationBtn.disabled = false;
      },
    });
  });

  setLocationBtn.addEventListener("click", () => {
    const address = manualInput.value.trim();
    if (!address) return;
    location = { address };
    locationStatus.textContent = `Using “${address}”. Pick a need below.`;
  });

  function renderPlaces(places, source) {
    resultList.innerHTML = "";
    if (!places.length) {
      resultList.innerHTML =
        '<p class="empty-body">Nothing found for that need, in the curated venues or on the web.</p>';
      return;
    }
    places.forEach((place) => {
      const card = document.createElement("div");
      card.className = "need-card";
      const title = document.createElement("h4");
      title.textContent = place.name;
      const meta = document.createElement("p");
      meta.className = "meta";
      const distance = place.distance_km != null ? `${place.distance_km} km away` : "";
      meta.textContent = [place.type, place.neighbourhood, distance]
        .filter(Boolean).join(" · ");
      card.append(title, meta);
      if (source === "search" && place.reason) {
        const snippet = document.createElement("p");
        snippet.className = "reason";
        snippet.textContent = place.reason;
        card.appendChild(snippet);
      }
      if (place.maps_url) {
        const link = document.createElement("a");
        link.className = "maps-link";
        link.href = place.maps_url;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = source === "search" ? "🔗 Open result" : "📍 Open in Google Maps";
        card.appendChild(link);
      }
      resultList.appendChild(card);
    });
  }

  needBar.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-need]");
    if (!btn) return;
    if (!location) {
      heading.textContent = "Set a location first.";
      return;
    }
    btn.disabled = true;
    heading.textContent = "Finding…";
    resultList.innerHTML = "";
    try {
      const res = await fetch("/find-nearby/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ need: btn.dataset.need, ...location }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't find anything.");
      const where = data.location && data.location.formatted_address;
      const via = data.source === "search" ? "web search" : "curated venues";
      heading.textContent =
        `${data.places.length} result${data.places.length === 1 ? "" : "s"} from ${via}` +
        (where ? ` near ${where}` : "");
      renderPlaces(data.places, data.source);
    } catch (e) {
      heading.textContent = e.message || "Couldn't find anything right now.";
    } finally {
      btn.disabled = false;
    }
  });
});
