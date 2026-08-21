// Shared stop-list rendering for the Plan Trips / Replan a Trip component
// test pages -- both render a plan's stops into a ".preview-list" the same
// way, matching _stop_preview.html's stop_line macro's look.
function stopDisplayName(stop) {
  if (stop.kind === "meal") {
    return "🍽 Lunch break" + (stop.venue ? " - " + stop.venue.name : "");
  }
  if (stop.venue) return stop.venue.name;
  if (stop.kind === "leave") return "🚗 Leave your accommodation";
  if (stop.kind === "bonus") return "Free time";
  return stop.kind.charAt(0).toUpperCase() + stop.kind.slice(1);
}

function renderPreviewStops(listEl, stops) {
  listEl.innerHTML = "";
  stops.forEach((stop) => {
    const li = document.createElement("li");
    const time = document.createElement("span");
    time.className = "preview-time";
    time.textContent = stop.time;
    const name = document.createElement("span");
    name.className = "preview-name";
    name.textContent = stopDisplayName(stop);
    if (stop.adjusted) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "✨ adjusted";
      name.append(" ", badge);
    }
    li.append(time, name);
    listEl.appendChild(li);
  });
}
