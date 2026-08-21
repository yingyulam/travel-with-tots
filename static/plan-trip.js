// Plan Trips component test page: run the rule-based-draft + AI-smoothing
// component directly and render the resulting stops. Deliberately small and
// self-contained, same pattern as agent-chat.js / search-web.js. Stop
// rendering is shared with replan-trip.js via stop-render.js.
document.addEventListener("DOMContentLoaded", () => {
  const destinationInput = document.getElementById("plan-trip-destination");
  const ageInput = document.getElementById("plan-trip-age-months");
  const wakeUpInput = document.getElementById("plan-trip-wake-up");
  const bedtimeInput = document.getElementById("plan-trip-bedtime");
  const stopCountInput = document.getElementById("plan-trip-stop-count");
  const diningSelect = document.getElementById("plan-trip-dining");
  const runBtn = document.getElementById("plan-trip-run");
  const heading = document.getElementById("plan-trip-heading");
  const stopList = document.getElementById("plan-trip-stop-list");

  runBtn.addEventListener("click", async () => {
    const destination = destinationInput.value.trim();
    if (!destination) return;
    runBtn.disabled = true;
    heading.textContent = "Planning…";
    stopList.innerHTML = "";
    try {
      const res = await fetch("/plan-trip/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          destination,
          age_months: Number(ageInput.value),
          wake_up: wakeUpInput.value,
          bedtime: bedtimeInput.value,
          stop_count: Number(stopCountInput.value),
          dining: diningSelect.value,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't build a plan.");
      heading.textContent = data.adjusted
        ? "Plan ready, AI-smoothed."
        : "Plan ready (rule-based draft only, AI smoothing didn't run).";
      renderPreviewStops(stopList, data.stops);
    } catch (e) {
      heading.textContent = e.message || "Couldn't build a plan right now.";
    } finally {
      runBtn.disabled = false;
    }
  });
});
