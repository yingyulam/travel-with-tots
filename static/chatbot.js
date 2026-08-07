(function () {
  const MODEL_STORAGE_KEY = "twt_chatbot_model";
  const MAX_HISTORY_TURNS = 10;
  // \s (not a literal space) since models sometimes emit typographic spaces
  // like U+202F (narrow no-break space) instead of a plain ASCII space.
  const CITATION_RE = /(\[Source\s+\d+\])/g;

  document.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector(".twt-chatbot");
    if (!root) return;

    const bubble = root.querySelector(".twt-chatbot-bubble");
    const panel = root.querySelector(".twt-chatbot-panel");
    const messages = root.querySelector(".twt-chatbot-messages");
    const progress = root.querySelector(".twt-progress");
    const progressLabel = progress.querySelector(".twt-progress-label");
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
      if (!panel.hidden && !form.hidden) input.focus();
    });

    function showReady() {
      bubble.classList.remove("indexing");
      progress.hidden = true;
      form.hidden = false;
    }

    function showIndexing() {
      bubble.classList.add("indexing");
      progress.classList.remove("error");
      progressLabel.textContent = "Preparing knowledge base…";
      progress.hidden = false;
      form.hidden = true;
    }

    function showError(message) {
      bubble.classList.remove("indexing");
      progress.classList.add("error");
      progressLabel.textContent = message || "Something went wrong preparing the knowledge base.";
      progress.hidden = false;
      form.hidden = true;

      let retryBtn = progress.querySelector("button");
      if (!retryBtn) {
        retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.textContent = "Retry";
        progress.appendChild(retryBtn);
      }
      retryBtn.onclick = watchStatus;
    }

    function watchStatus() {
      pollRagStatus({
        onState: (data) => {
          if (data.state === "indexing" || data.state === "not_started") showIndexing();
        },
        onReady: showReady,
        onError: (data) => showError(data.message || data.error),
      });
    }

    watchStatus();

    let history = [];

    function addMessage(role, text) {
      const el = document.createElement("div");
      el.className = "twt-chatbot-msg " + role;
      const textSpan = document.createElement("span");
      textSpan.className = "twt-chatbot-msg-text";
      textSpan.textContent = text;
      el.appendChild(textSpan);
      messages.appendChild(el);
      messages.scrollTop = messages.scrollHeight;
      return el;
    }

    function renderAssistantReply(bubbleEl, text, sources) {
      bubbleEl.innerHTML = "";
      const textSpan = document.createElement("span");
      textSpan.className = "twt-chatbot-msg-text";

      text.split(CITATION_RE).forEach((part) => {
        const match = /^\[Source\s+(\d+)\]$/.exec(part);
        if (!match) {
          textSpan.appendChild(document.createTextNode(part));
          return;
        }
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "twt-citation";
        btn.textContent = `[${match[1]}]`;
        btn.dataset.sourceIndex = match[1];
        btn.setAttribute("aria-expanded", "false");
        textSpan.appendChild(btn);
      });
      bubbleEl.appendChild(textSpan);

      const detail = document.createElement("div");
      detail.className = "twt-citation-detail";
      detail.hidden = true;
      bubbleEl.appendChild(detail);

      bubbleEl.querySelectorAll(".twt-citation").forEach((btn) => {
        btn.addEventListener("click", () => {
          const alreadyOpen = !detail.hidden && detail.dataset.openIndex === btn.dataset.sourceIndex;
          detail.hidden = alreadyOpen;
          detail.textContent = "";
          if (alreadyOpen) return;

          detail.dataset.openIndex = btn.dataset.sourceIndex;
          const source = (sources || []).find((s) => String(s.index) === btn.dataset.sourceIndex);
          if (!source) {
            detail.textContent = "Source details unavailable.";
            return;
          }
          const meta = document.createElement("div");
          meta.className = "twt-citation-detail-meta";
          meta.textContent = `${source.section} · similarity ${source.score.toFixed(2)}`;
          const body = document.createElement("p");
          body.textContent = source.text;
          detail.append(meta, body);
        });
      });
      messages.scrollTop = messages.scrollHeight;
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

        renderAssistantReply(placeholder, data.reply, data.sources);
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
