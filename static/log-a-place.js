// Log a Place page: a pin on a map, whose coordinates get named by the server.
//
// Leaflet with OpenStreetMap tiles rather than Google, because every Google
// embedding option needs the API key in the browser and this app keeps all
// three of its keys server-side. The pin's coordinates are public information
// the browser already has; naming them still goes through the server, so the
// geocoding key never moves.
document.addEventListener("DOMContentLoaded", () => {
  const mapHost = document.getElementById("place-map");
  const useLocationBtn = document.getElementById("use-my-location");
  const areaInput = document.getElementById("place-area");
  const locationStatus = document.getElementById("location-status");
  // The pin's own coordinates travel with the form: a spot with no address is
  // only findable by them, so they must not be re-derived from text.
  const latInput = document.getElementById("place-lat");
  const lngInput = document.getElementById("place-lng");
  const cityInput = document.getElementById("place-city");
  const addressInput = document.getElementById("place-address");

  // Where the map opens before anyone has said anything: the city the curated
  // venues are in, so the first pin is somewhere plausible rather than in the
  // Atlantic. Zoomed out enough to recognise, close enough to drag.
  const START = { lat: 49.2827, lng: -123.1207, zoom: 12 };
  const PIN_ZOOM = 16;

  const map = L.map(mapHost).setView([START.lat, START.lng], START.zoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    // Required by the OpenStreetMap tile usage policy, and Leaflet only shows
    // it if we pass it. Do not remove.
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  // A circleMarker rather than L.marker on purpose: the default marker needs
  // icon images from Leaflet's dist/images, which are not vendored, so a
  // marker would render as a broken image. A circle needs no assets.
  const PIN_STYLE = {
    radius: 9, color: "#1e88e5", fillColor: "#1e88e5", fillOpacity: 0.7,
  };
  let pin = null;

  function setPin({ lat, lng }, { recentre = false } = {}) {
    if (pin) {
      pin.setLatLng([lat, lng]);
    } else {
      pin = L.circleMarker([lat, lng], PIN_STYLE).addTo(map);
    }
    if (recentre) map.setView([lat, lng], PIN_ZOOM);
    latInput.value = lat;
    lngInput.value = lng;
    nameTheSpot(lat, lng);
  }

  // Coordinates to words. Server-side, so the geocoding key stays there.
  async function nameTheSpot(lat, lng) {
    locationStatus.textContent = "Working out where that is…";
    try {
      const res = await fetch("/log-place/area", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat, lng }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't name that spot.");
      areaInput.value = data.neighbourhood || data.area;
      cityInput.value = data.city || "";
      addressInput.value = data.area || "";
      locationStatus.textContent = `Pin is at ${data.area}.`;
    } catch (e) {
      // The pin still stands, and its coordinates are already on the form, so
      // the submission keeps the one thing only the pin knows.
      locationStatus.textContent =
        `${e.message} The pin still counts; type the area below if you like.`;
    }
  }

  map.on("click", (event) => setPin(event.latlng));

  // Search by name. Google Places rather than geocoding, because geocoding is
  // address-shaped and answers "Nourish Kitchen" with a street. Runs
  // server-side so the key stays there, and is biased to the current map
  // centre so a common name resolves nearby.
  const searchInput = document.getElementById("place-search");
  const searchBtn = document.getElementById("place-search-go");
  const searchHint = document.getElementById("place-search-hint");
  const searchResults = document.getElementById("place-search-results");
  const nameInput = document.getElementById("place-name");
  const typeInput = document.getElementById("place-type");

  // Deliberately no up-front check for whether a key is configured. It used to
  // read a flag the server rendered from os.environ, which is fixed when the
  // process starts: adding a key to .env without restarting left this page
  // insisting there was no key, and disabling a search box that would have
  // worked. The route answers the question accurately at the moment it is
  // asked, so that is where the answer comes from.

  // Picking a result is what fills the form: name, kind, area and the pin all
  // come from the one choice, so there is nothing to retype.
  function choosePlace(place) {
    nameInput.value = place.name;
    if (place.type) typeInput.value = place.type;
    areaInput.value = place.neighbourhood || place.city || place.address || "";
    cityInput.value = place.city || "";
    addressInput.value = place.address || "";
    searchResults.replaceChildren();
    searchHint.textContent = `Using “${place.name}”.`;
    if (place.lat != null && place.lng != null) {
      // Straight to the pin, skipping nameTheSpot: Places already told us the
      // address, so re-asking the geocoder would spend a call to learn less.
      if (pin) {
        pin.setLatLng([place.lat, place.lng]);
      } else {
        pin = L.circleMarker([place.lat, place.lng], PIN_STYLE).addTo(map);
      }
      latInput.value = place.lat;
      lngInput.value = place.lng;
      map.setView([place.lat, place.lng], PIN_ZOOM);
      locationStatus.textContent = `Pin is at ${place.address || place.name}.`;
    }
  }

  function renderResults(places) {
    searchResults.replaceChildren();
    places.forEach((place) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "place-result";
      const name = document.createElement("strong");
      name.textContent = place.name;
      const detail = document.createElement("span");
      detail.className = "meta";
      detail.textContent = [place.type, place.address].filter(Boolean).join(" · ");
      card.append(name, detail);
      card.addEventListener("click", () => choosePlace(place));
      searchResults.appendChild(card);
    });
  }

  async function runSearch() {
    const query = searchInput.value.trim();
    if (!query) {
      searchHint.textContent = "Type what you're looking for first.";
      return;
    }
    searchBtn.disabled = true;
    searchHint.textContent = "Searching…";
    searchResults.replaceChildren();
    try {
      const centre = map.getCenter();
      const res = await fetch("/log-place/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, lat: centre.lat, lng: centre.lng }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't search for that.");
      if (!data.places.length) {
        searchHint.textContent =
          "Nothing matched. Try a fuller name, or drop the pin yourself.";
        return;
      }
      searchHint.textContent = "Pick the right one:";
      renderResults(data.places);
    } catch (e) {
      searchHint.textContent = e.message;
    } finally {
      searchBtn.disabled = false;
    }
  }

  searchBtn.addEventListener("click", runSearch);
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      // The search box lives inside the form, so Enter would submit it.
      e.preventDefault();
      runSearch();
    }
  });

  useLocationBtn.addEventListener("click", () => {
    useLocationBtn.disabled = true;
    requestCoordinates({
      fallbackAdvice: () => "Click the map instead.",
      onStatus: (text) => {
        locationStatus.textContent = text;
        if (!text.startsWith("Asking")) useLocationBtn.disabled = false;
      },
      onCoords: (coords) => {
        setPin(coords, { recentre: true });
        useLocationBtn.disabled = false;
      },
    });
  });

  // Typing an area by hand is a complete answer on its own: the server
  // geocodes the name plus this text, so no pin is required.
  areaInput.addEventListener("input", () => {
    if (!pin) {
      locationStatus.textContent = areaInput.value.trim()
        ? `Looking around “${areaInput.value.trim()}”.`
        : "Drop a pin, or type an area below.";
    }
  });
});
