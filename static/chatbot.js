(function () {
  const MODEL_STORAGE_KEY = "twt_chatbot_model";
  const MAX_HISTORY_TURNS = 10;

  document.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector(".twt-chatbot");
    if (!root) return;

    const bubble = root.querySelector(".twt-chatbot-bubble");
    const panel = root.querySelector(".twt-chatbot-panel");
    const messages = root.querySelector(".twt-chatbot-messages");
    const form = root.querySelector(".twt-chatbot-form");
    const input = form.querySelector("input");
    const sendBtn = form.querySelector("button");
    const modelSelect = root.querySelector(".twt-chatbot-model-row select");

    const savedModel = localStorage.getItem(MODEL_STORAGE_KEY);
    if (savedModel && [...modelSelect.options].some((o) => o.value === savedModel)) {
      modelSelect.value = savedModel;
    }
    modelSelect.addEventListener("change", () => {
      localStorage.setItem(MODEL_STORAGE_KEY, modelSelect.value);
    });

    bubble.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) input.focus();
    });

    let history = [];

    function addMessage(role, text) {
      const el = document.createElement("div");
      el.className = "twt-chatbot-msg " + role;
      el.textContent = text;
      messages.appendChild(el);
      messages.scrollTop = messages.scrollHeight;
      return el;
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message) return;

      input.value = "";
      input.disabled = true;
      sendBtn.disabled = true;

      addMessage("user", message);
      const placeholder = addMessage("assistant", "Thinking…");

      try {
        const res = await fetch("/chatbot", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            model: modelSelect.value,
            history: history.slice(-MAX_HISTORY_TURNS),
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Something went wrong.");

        placeholder.textContent = data.reply;
        history.push({ role: "user", content: message });
        history.push({ role: "assistant", content: data.reply });
      } catch (err) {
        placeholder.className = "twt-chatbot-msg error";
        placeholder.textContent = err.message || "The chatbot is unavailable right now.";
      } finally {
        input.disabled = false;
        sendBtn.disabled = false;
        input.focus();
      }
    });
  });
})();
