document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("rerun-form");
  const btn = document.getElementById("rerun-btn");
  const progress = document.getElementById("rerun-progress");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const chunkSize = Number(new FormData(form).get("chunk_size"));

    btn.disabled = true;
    progress.classList.remove("error");
    progress.hidden = false;
    progress.innerHTML =
      '<span class="twt-progress-dots"><span></span><span></span><span></span></span>' +
      '<span class="twt-progress-label">Re-chunking…</span>';

    try {
      await fetch("/chunks/rerun", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chunk_size: chunkSize }),
      });
    } catch (err) {
      progress.classList.add("error");
      progress.querySelector(".twt-progress-label").textContent = "Could not reach the server.";
      btn.disabled = false;
      return;
    }

    pollRagStatus({
      onReady: () => location.reload(),
      onError: (data) => {
        progress.classList.add("error");
        progress.querySelector(".twt-progress-label").textContent =
          data.message || data.error || "Re-chunking failed.";
        btn.disabled = false;
      },
    });
  });
});
