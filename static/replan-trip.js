// Replan a Trip component test page: build a sample day (reusing the Plan
// Trips component's own /plan-trip/run endpoint), then re-plan it for a
// situation. Shows the original and the after-replan result side by side --
// "Original" stays frozen at the sample day; "After replan" chains from
// whichever result is currently shown, same as the real /trip page replans
// from whatever's currently active, not always the very first plan. Stop
// rendering is shared with plan-trip.js via stop-render.js.
document.addEventListener("DOMContentLoaded", () => {
  const buildBtn = document.getElementById("replan-trip-build");
  const currentTimeInput = document.getElementById("replan-trip-current-time");
  const situationSelect = document.getElementById("replan-trip-situation");
  const minutesInput = document.getElementById("replan-trip-minutes");
  const themeSelect = document.getElementById("replan-trip-theme");
  const runBtn = document.getElementById("replan-trip-run");
  const heading = document.getElementById("replan-trip-heading");
  const originalList = document.getElementById("replan-trip-original-list");
  const changedList = document.getElementById("replan-trip-changed-list");

  let latestPlan = null;

  buildBtn.addEventListener("click", async () => {
    buildBtn.disabled = true;
    heading.textContent = "Building a sample day…";
    originalList.innerHTML = "";
    changedList.innerHTML = "";
    try {
      const res = await fetch("/plan-trip/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          destination: "Vancouver", age_months: 24,
          wake_up: "07:00", bedtime: "20:00", stop_count: 3, dining: "dine_out",
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't build a sample day.");
      latestPlan = data;
      heading.textContent = "Sample day ready. Pick a situation and click Run.";
      renderPreviewStops(originalList, data.stops);
      runBtn.disabled = false;
    } catch (e) {
      heading.textContent = e.message || "Couldn't build a sample day right now.";
    } finally {
      buildBtn.disabled = false;
    }
  });

  runBtn.addEventListener("click", async () => {
    if (!latestPlan) return;
    runBtn.disabled = true;
    heading.textContent = "Re-planning…";
    try {
      const res = await fetch("/replan-trip/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan: latestPlan,
          situation: situationSelect.value,
          current_time: currentTimeInput.value,
          minutes: minutesInput.value ? Number(minutesInput.value) : null,
          theme: themeSelect.value || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't re-plan.");
      latestPlan = data;
      heading.textContent = data.adjusted
        ? "Replanned, AI-smoothed."
        : "Replanned (rule-based draft only, AI smoothing didn't run).";
      renderPreviewStops(changedList, data.stops);
    } catch (e) {
      heading.textContent = e.message || "Couldn't re-plan right now.";
    } finally {
      runBtn.disabled = false;
    }
  });
});
