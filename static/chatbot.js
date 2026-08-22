// Shared thumbs-up/down rating widget, used by both the chatbot bubbles here
// and the AI-generated plan cards in templates/plan.html -- kept as true
// globals (outside the IIFE below) so plan.html's separate inline script can
// call buildFeedbackRow too, and as a document-level delegated click handler
// (rather than per-button listeners) so it keeps working after plan.html
// restores a card from its sessionStorage snapshot, which replaces raw HTML
// and would otherwise leave old per-button listeners behind.
function buildFeedbackRow(context) {
  const row = document.createElement("div");
  row.className = "twt-feedback";
  row.dataset.feedback = JSON.stringify(context);
  const upBtn = document.createElement("button");
  upBtn.type = "button";
  upBtn.className = "twt-feedback-btn";
  upBtn.dataset.rating = "up";
  upBtn.textContent = "👍";
  const downBtn = document.createElement("button");
  downBtn.type = "button";
  downBtn.className = "twt-feedback-btn";
  downBtn.dataset.rating = "down";
  downBtn.textContent = "👎";
  row.append(upBtn, downBtn);
  return row;
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".twt-feedback-btn");
  if (!btn || btn.disabled) return;
  const row = btn.closest(".twt-feedback");
  const context = JSON.parse(row.dataset.feedback);
  row.querySelectorAll(".twt-feedback-btn").forEach((b) => { b.disabled = true; });
  btn.classList.add("selected");
  fetch("/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...context, rating: btn.dataset.rating }),
  }).catch(() => {});
});

(function () {
  const MODEL_STORAGE_KEY = "twt_chatbot_model";
  const MAX_HISTORY_TURNS = 10;
  // Matches any bracket that mentions "source"/"sources" (case-insensitive,
  // any spacing) so it also catches model variations like "[source1]" or
  // multiple refs combined in one bracket like "[source1, source2]" --
  // every digit found inside becomes its own clickable citation.
  const CITATION_RE = /\[([^[\]]*sources?[^[\]]*)\]/gi;

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
    const endBtn = root.querySelector(".twt-chatbot-end");

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

    endBtn.addEventListener("click", () => {
      messages.innerHTML = "";
      history = [];
      panel.hidden = true;
    });

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

    function renderAssistantReply(bubbleEl, text, sources, feedbackContext) {
      bubbleEl.innerHTML = "";
      const textSpan = document.createElement("span");
      textSpan.className = "twt-chatbot-msg-text";

      let lastIndex = 0;
      for (const match of text.matchAll(CITATION_RE)) {
        if (match.index > lastIndex) {
          textSpan.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
        }
        const numbers = match[1].match(/\d+/g) || [];
        if (numbers.length === 0) {
          // A bracket that mentions "source" but has no digit in it isn't
          // actually a citation (e.g. "[outsourced]") -- leave it as text.
          textSpan.appendChild(document.createTextNode(match[0]));
        } else {
          numbers.forEach((num) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "twt-citation";
            btn.textContent = `[${num}]`;
            btn.dataset.sourceIndex = num;
            btn.setAttribute("aria-expanded", "false");
            textSpan.appendChild(btn);
          });
        }
        lastIndex = match.index + match[0].length;
      }
      if (lastIndex < text.length) {
        textSpan.appendChild(document.createTextNode(text.slice(lastIndex)));
      }
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

      if (feedbackContext) {
        bubbleEl.appendChild(buildFeedbackRow({ ...feedbackContext, response: text, kind: "chatbot" }));
      }
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

        renderAssistantReply(placeholder, data.reply, data.sources, {
          question: message,
          model: data.model,
          response_time: data.response_time,
          input_tokens: data.input_tokens,
          output_tokens: data.output_tokens,
        });
        history.push({ role: "user", content: message });
        history.push({ role: "assistant", content: data.reply });

        // One event per reply, so a page can watch what the agent actually did
        // with a message without a second chat of its own. The agent test page
        // and the Plan-from-chat workflow page both listen for this; ordinary
        // pages have no listener and are unaffected.
        document.dispatchEvent(new CustomEvent("twt:chat-reply", {
          detail: { message, ...data },
        }));
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
