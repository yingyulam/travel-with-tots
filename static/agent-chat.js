// Standalone AI Agent test page. Deliberately smaller than chatbot.js: no
// citation parsing, no RAG-indexing polling -- this agent doesn't depend on
// the knowledge base, it decides between its own tools.
document.addEventListener("DOMContentLoaded", () => {
  const messages = document.getElementById("agent-messages");
  const form = document.getElementById("agent-form");
  const input = document.getElementById("agent-input");
  const sendBtn = form.querySelector("button");
  const runTestBtn = document.getElementById("agent-run-test");
  const runResult = document.getElementById("agent-run-result");

  let history = [];

  function addMessage(role, text) {
    const el = document.createElement("div");
    el.className = "twt-chatbot-msg " + role;
    el.textContent = text;
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
    return el;
  }

  // The concrete, debuggable proof a tool actually ran -- not just a reply
  // that claims it did. Shown after every turn, since it's useful beyond
  // just the "Run test" button.
  function describeToolCalls(toolCalls) {
    if (!toolCalls || !toolCalls.length) return "No tool was called -- the agent answered directly.";
    return toolCalls.map((c) => `✅ Called ${c.name} → ${c.output}`).join("\n");
  }

  async function sendMessage(message) {
    addMessage("user", message);
    const placeholder = addMessage("assistant", "Thinking…");
    try {
      const res = await fetch("/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Something went wrong.");

      placeholder.textContent = data.reply;
      history.push({ role: "user", content: message });
      history.push({ role: "assistant", content: data.reply });
      return data;
    } catch (e) {
      placeholder.className = "twt-chatbot-msg error";
      placeholder.textContent = e.message || "Couldn't reach the AI Agent right now.";
      throw e;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    input.disabled = true;
    sendBtn.disabled = true;

    try {
      await sendMessage(message);
    } catch (e) {
      // already rendered as an error bubble
    } finally {
      input.disabled = false;
      sendBtn.disabled = false;
      input.focus();
    }
  });

  runTestBtn.addEventListener("click", async () => {
    runTestBtn.disabled = true;
    runResult.hidden = false;
    runResult.textContent = "Running…";
    try {
      const data = await sendMessage("Find a nearby quiet spot");
      runResult.textContent = describeToolCalls(data.tool_calls);
    } catch (e) {
      runResult.textContent = "Test call failed: " + (e.message || "unknown error");
    } finally {
      runTestBtn.disabled = false;
    }
  });
});
