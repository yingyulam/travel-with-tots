/** Poll /rag/status until the knowledge base index is ready or errors out. */
function pollRagStatus({ onState, onReady, onError, intervalMs = 1000 }) {
  let stopped = false;

  async function tick() {
    if (stopped) return;
    try {
      const res = await fetch("/rag/status");
      const data = await res.json();
      if (onState) onState(data);
      if (data.state === "ready") return onReady(data);
      if (data.state === "error") return onError(data);
    } catch (err) {
      onError({ message: "Could not reach the server." });
      return;
    }
    setTimeout(tick, intervalMs);
  }

  tick();
  return () => {
    stopped = true;
  };
}
