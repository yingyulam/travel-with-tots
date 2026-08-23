// Shared thumbs-up/down rating widget, used by both the chatbot bubbles here
// and the AI-generated plan cards in templates/plan.html -- kept as true
// globals (outside the IIFE below) so plan.html's separate inline script can
// call buildFeedbackRow too, and as a document-level delegated click handler
// (rather than per-button listeners) so it keeps working after plan.html
// restores a card from its sessionStorage snapshot, which replaces raw HTML
// and would otherwise leave old per-button listeners behind.
// The model the parent picked in the chat widget's dropdown. A true global,
// like buildFeedbackRow below, because the planning and in-trip pages send it
// with their own AI calls: one visible choice governs every model this app
// uses, rather than each page keeping a default nobody can see.
const TWT_MODEL_STORAGE_KEY = "twt_chatbot_model";

function twtSelectedModel() {
  return localStorage.getItem(TWT_MODEL_STORAGE_KEY) || "";
}

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
  // The conversation, so it survives navigating to another page. sessionStorage
  // rather than localStorage: the workflow state in here belongs to one
  // transcript, and sharing it between tabs would let two half-filled forms
  // answer each other's questions. The cost is that closing the tab ends the
  // chat, the same way closing the browser ends any other page's state.
  const SESSION_STORAGE_KEY = "twt_chatbot_session";
  // Enough for a long conversation, bounded so a transcript with citations
  // cannot grow into the storage quota.
  const MAX_STORED_TURNS = 40;
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

    const savedModel = localStorage.getItem(TWT_MODEL_STORAGE_KEY);
    if (savedModel && [...modelSelect.options].some((o) => o.value === savedModel)) {
      modelSelect.value = savedModel;
    }
    modelSelect.addEventListener("change", () => {
      localStorage.setItem(TWT_MODEL_STORAGE_KEY, modelSelect.value);
    });

    const GREETING = "Hello, I'm your Travel with Tots assistant. "
      + "What can I help you with today?";
    // Openers, so the panel says what it is for instead of showing a blank
    // box. "Plan a trip" is phrased to match the fill-the-form workflow, and
    // goes through the classifier like any typed message would.
    const PLAN_SUGGESTION = "Plan a trip";
    const SUGGESTIONS = ["What's Travel with Tots?", PLAN_SUGGESTION];
    let greeted = false;
    // Planning is the thing most parents come here for, so the offer stays
    // under each answer rather than only in the greeting, where it scrolls away
    // after a question or two. It stops once the form-filling flow has run, so
    // it does not keep offering something already under way or just done.
    let planOffered = false;

    // The bubble is a plain toggle with no notion of a first open, so the
    // greeting needs its own flag. End chat resets it, or reopening after
    // ending would give a silently greeting-less panel.
    function greetOnce() {
      if (greeted) return;
      greeted = true;
      const record = { kind: "greeting" };
      turns.push(record);
      const bubbleEl = addMessage("assistant", GREETING);
      bubbleEl.appendChild(choiceRow(SUGGESTIONS, () => {
        record.used = true;
        save();
      }));
      history.push({ role: "assistant", content: GREETING });
      messages.scrollTop = messages.scrollHeight;
      save();
    }

    bubble.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      // Open or closed travels with the transcript: a parent who left the panel
      // open mid-answer should find it open on the next page, not collapsed.
      save();
      if (panel.hidden) return;
      greetOnce();
      if (!form.hidden) input.focus();
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
    // Where a multi-turn workflow has got to, held here and echoed back with
    // every message. Same grain as history: no cookie size ceiling, no clash
    // between tabs, and it works for a visitor who is not logged in.
    let conversation = null;
    // Everything on screen, in the order it was rendered, as data rather than
    // markup. Saved HTML would come back without its citation and choice
    // listeners, and restoring it would mean handing stored text to innerHTML,
    // which is the one thing the rest of this file is careful never to do.
    let turns = [];

    function save() {
      try {
        sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({
          open: !panel.hidden,
          greeted,
          planOffered,
          conversation,
          history,
          turns: turns.slice(-MAX_STORED_TURNS),
        }));
      } catch (err) {
        // A full or blocked store is not worth failing a message over. The
        // chat keeps working for this page, it just will not survive the next.
      }
    }

    endBtn.addEventListener("click", () => {
      messages.innerHTML = "";
      history = [];
      turns = [];
      // These must go with the transcript: a stale conversation would resume a
      // half-filled form the parent can no longer see, and a stale greeted flag
      // would leave the next open unwelcomed.
      conversation = null;
      greeted = false;
      planOffered = false;
      panel.hidden = true;
      // The only thing that clears the stored chat. Navigating away, closing
      // the panel and reloading all deliberately keep it.
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    });

    // The assistant's face. An emoji in a circle, following the login pill in
    // nav.css: the repo has no image assets and every glyph in this UI is an
    // emoji, so a binary asset here would be the odd one out.
    function avatar() {
      const el = document.createElement("span");
      el.className = "twt-chatbot-avatar";
      el.textContent = "🧸";
      el.setAttribute("aria-hidden", "true");
      return el;
    }

    // Returns the bubble, not the row, because renderAssistantReply and the
    // error path both rebuild the element they are handed. An avatar placed
    // inside the bubble would be destroyed by that; a row wrapper survives.
    function addMessage(role, text) {
      const row = document.createElement("div");
      row.className = "twt-chatbot-row " + role;
      if (role !== "user") row.appendChild(avatar());

      const el = document.createElement("div");
      el.className = "twt-chatbot-msg " + role;
      const textSpan = document.createElement("span");
      textSpan.className = "twt-chatbot-msg-text";
      textSpan.textContent = text;
      el.appendChild(textSpan);

      row.appendChild(el);
      messages.appendChild(row);
      messages.scrollTop = messages.scrollHeight;
      return el;
    }

    function renderAssistantReply(bubbleEl, text, sources, feedbackContext, workflow) {
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

      // Which workflow the intent router picked, if any. Shown rather than
      // logged only, so you can tell at a glance whether a reply came from a
      // workflow or from the agent answering directly.
      const routed = document.createElement("div");
      routed.className = "twt-routed";
      const badge = document.createElement("span");
      badge.className = "twt-badge";
      badge.textContent = workflow ? `⚙️ ${workflow}` : "💬 no workflow";
      routed.appendChild(badge);
      bubbleEl.appendChild(routed);

      if (feedbackContext) {
        bubbleEl.appendChild(buildFeedbackRow({ ...feedbackContext, response: text, kind: "chatbot" }));
      }
      messages.scrollTop = messages.scrollHeight;
    }

    // The collected form, posted to /plan as the real form's own fields. Same
    // trick the plan page uses to hand a plan to /trip. `prefill` is what tells
    // /plan to fill the boxes in and stop; without it, the existing POST branch
    // generates, which is exactly the page's own Generate my day.
    function handoffForm(collected) {
      const el = document.createElement("form");
      el.className = "twt-handoff";
      el.method = "post";
      el.action = "/plan";
      // Submitted in this tab, not a new one. Generating is a real AI call of
      // ten seconds and up, and in a background tab that is a blank page with
      // nothing to say it is working, which reads as a button that did nothing.
      // Here the browser's own loading indicator does that job. Safe to leave
      // the page now that the transcript survives navigation.

      const add = (name, value) => {
        const field = document.createElement("input");
        field.type = "hidden";
        field.name = name;
        field.value = value;
        el.appendChild(field);
      };

      Object.entries(collected).forEach(([name, value]) => {
        if (name === "naps") {
          // read_form takes naps as parallel lists, not the array it returns.
          value.forEach((nap) => {
            add("nap_start", nap.start);
            add("nap_duration", nap.duration_min);
          });
        } else if (Array.isArray(value)) {
          value.forEach((item) => add(name, item));
        } else if (typeof value === "boolean") {
          if (value) add(name, "on");   // a checkbox, absent when unticked
        } else if (value !== "") {
          add(name, value);
        }
      });

      // The same model the chat itself is using, so generating from here and
      // generating from the planning page cannot disagree.
      add("model", modelSelect.value);

      const check = document.createElement("button");
      check.type = "submit";
      check.className = "twt-chip";
      check.textContent = "📝 Open the form";
      check.name = "prefill";
      check.value = "1";

      const generate = document.createElement("button");
      generate.type = "submit";
      generate.className = "twt-chip primary";
      generate.textContent = "✨ Generate my day";

      // This page stays on screen while /plan works, so without a visible
      // change the button looks unclicked for the whole ten seconds and gets
      // pressed again. Marked with a class rather than `disabled`, because
      // disabling the submitter mid-submit can drop its name from the post,
      // and "Open the form" is nothing but its name.
      el.addEventListener("submit", (event) => {
        el.classList.add("working");
        const clicked = event.submitter === check ? check : generate;
        clicked.textContent = clicked === check
          ? "📝 Opening the form…"
          : "✨ Building your day…";
      });

      el.append(check, generate);
      return el;
    }

    // A row of one-tap answers. Clicking sends exactly the text on the button,
    // so a tapped choice and a typed one take the same path through the server.
    // `onUse` records that the row is spent, so restoring the transcript after
    // a navigation does not put an answered question back on screen.
    function choiceRow(choices, onUse) {
      const row = document.createElement("div");
      row.className = "twt-chips";
      choices.forEach((choice) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "twt-chip";
        btn.textContent = choice;
        btn.addEventListener("click", () => {
          row.remove();          // one answer per question
          if (onUse) onUse();
          send(choice);
        });
        row.appendChild(btn);
      });
      return row;
    }

    // Buttons for whatever the assistant just offered, so a choice can be
    // clicked as well as typed. They send the same text either way, which
    // keeps one path through the server.
    function renderFollowUps(bubbleEl, data, record) {
      // Any of these three means the form-filling flow is running or has just
      // finished, and no other workflow produces them. Cheaper than matching
      // the workflow by name, which would mean threading it into the template.
      if (data.conversation || data.form || data.open_form) planOffered = true;

      if (data.form) {
        bubbleEl.appendChild(handoffForm(data.form));
        return;
      }
      if (data.open_form) {
        const link = document.createElement("a");
        link.className = "twt-chip";
        link.href = "/plan";
        link.textContent = "📝 Open the planning form";
        const row = document.createElement("div");
        row.className = "twt-chips";
        row.appendChild(link);
        bubbleEl.appendChild(row);
        return;
      }
      // A row already clicked is not offered again. Everything else on screen
      // comes back, including older rows never answered, because those are
      // still live on the page itself.
      if (record && record.used) return;
      const used = () => { if (record) { record.used = true; save(); } };

      if (data.choices && data.choices.length) {
        bubbleEl.appendChild(choiceRow(data.choices, used));
        return;
      }
      // Nothing else to offer, so re-offer planning. Only here, or an answer
      // would end with two competing rows of buttons.
      if (!planOffered) bubbleEl.appendChild(choiceRow([PLAN_SUGGESTION], used));
    }

    // One send path, whether the parent typed the message or clicked a choice
    // button. A clicked choice is just a message they did not have to type.
    async function send(message) {
      if (!message) return;

      input.value = "";
      input.disabled = true;
      sendBtn.disabled = true;

      addMessage("user", message);
      turns.push({ kind: "user", text: message });
      const placeholder = addMessage("assistant", "Thinking…");

      try {
        const res = await fetch("/chatbot", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            model: modelSelect.value,
            history: history.slice(-MAX_HISTORY_TURNS),
            conversation,
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Something went wrong.");

        const feedback = {
          question: message,
          model: data.model,
          response_time: data.response_time,
          input_tokens: data.input_tokens,
          output_tokens: data.output_tokens,
        };
        renderAssistantReply(placeholder, data.reply, data.sources, feedback,
          data.workflow);
        conversation = data.conversation || null;
        // Recorded before the buttons are drawn, so clicking one can mark this
        // very turn as answered.
        const record = { kind: "reply", data, feedback };
        turns.push(record);
        renderFollowUps(placeholder, data, record);
        history.push({ role: "user", content: message });
        history.push({ role: "assistant", content: data.reply });
        save();

        // One event per reply, so a page can watch what the agent actually did
        // with a message without a second chat of its own. The agent test page
        // and the Plan-from-chat workflow page both listen for this; ordinary
        // pages have no listener and are unaffected.
        document.dispatchEvent(new CustomEvent("twt:chat-reply", {
          detail: { message, ...data },
        }));
      } catch (err) {
        const text = err.message || "The chatbot is unavailable right now.";
        placeholder.className = "twt-chatbot-msg error";
        placeholder.textContent = text;
        // Kept in the transcript, so a parent who navigates away does not come
        // back to a message of theirs that appears to have gone unanswered.
        turns.push({ kind: "error", text });
        save();
      } finally {
        input.disabled = false;
        sendBtn.disabled = false;
        input.focus();
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      send(input.value.trim());
    });

    // Rebuilds the transcript through the same render functions that drew it
    // the first time, so a restored reply keeps its citations, its badge and
    // its buttons rather than being a screenshot of one.
    function restore() {
      let saved = null;
      try {
        saved = JSON.parse(sessionStorage.getItem(SESSION_STORAGE_KEY));
      } catch (err) {
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
      }
      if (!saved || !Array.isArray(saved.turns)) return;

      greeted = Boolean(saved.greeted);
      // Recomputed by the replay below rather than restored, so each turn is
      // drawn with the flag as it stood at the time and gets back the buttons
      // it actually had.
      planOffered = false;
      conversation = saved.conversation || null;
      history = Array.isArray(saved.history) ? saved.history : [];
      turns = saved.turns;

      turns.forEach((turn) => {
        if (turn.kind === "user") {
          addMessage("user", turn.text);
        } else if (turn.kind === "greeting") {
          const bubbleEl = addMessage("assistant", GREETING);
          if (!turn.used) {
            bubbleEl.appendChild(choiceRow(SUGGESTIONS, () => {
              turn.used = true;
              save();
            }));
          }
        } else if (turn.kind === "error") {
          const bubbleEl = addMessage("assistant", turn.text);
          bubbleEl.className = "twt-chatbot-msg error";
        } else if (turn.kind === "reply" && turn.data) {
          const bubbleEl = addMessage("assistant", "");
          renderAssistantReply(bubbleEl, turn.data.reply, turn.data.sources,
            turn.feedback, turn.data.workflow);
          renderFollowUps(bubbleEl, turn.data, turn);
        }
      });

      // The replay only saw the turns still stored, so an older offer trimmed
      // by MAX_STORED_TURNS is remembered from the saved flag instead.
      planOffered = planOffered || Boolean(saved.planOffered);
      panel.hidden = !saved.open;
      messages.scrollTop = messages.scrollHeight;
    }

    restore();
  });
})();
