// The Run/Listen machine every workflow test page shares.
//
// Each page watches the same thing: replies from the real chat bubble, one
// "twt:chat-reply" event per reply, so there is nothing to poll and a message
// cannot be processed twice. ▶ Run handles the next one and stops. 👂 Listen
// keeps handling them until stopped. Only what a page does with a captured
// turn differs, which is the `onTurn` callback.
//
// It lives here because three pages wanted it. Two workflow pages that armed
// themselves differently would be two things to learn, and a test already
// asserted the two copies were identical, which is the sign to extract.
function watchChatReplies({ runId, listenId, statusId, statusTextId, workflow,
                           onTurn }) {
  const runBtn = document.getElementById(runId);
  const listenBtn = document.getElementById(listenId);
  const status = document.getElementById(statusId);
  const statusText = document.getElementById(statusTextId);

  // "once" stops after the next message; "many" keeps going. Kept as one
  // variable so Run and Listen cannot both be armed at the same time.
  let mode = "off";

  // The state drives the banner's colour in CSS, so the wording and the look
  // cannot disagree about whether the page is armed.
  //
  // Arming also tells the widget to send every message to *this* workflow. The
  // chat is both the input to a workflow and the general-purpose front door, so
  // without this a test page cannot reach its own workflow at all when the
  // classifier prefers another. Cleared on disarm, so normal routing resumes.
  function setMode(next, finished) {
    mode = next;
    window.twtForceWorkflow = mode === "off" ? null : workflow;
    listenBtn.textContent = mode === "many" ? "⏹ Stop listening" : "👂 Listen";
    status.dataset.state = mode;
    statusText.textContent = {
      off: finished ? "That run finished" : "Not watching",
      once: "Running: this workflow handles your messages until it finishes",
      many: "Listening: every message runs this workflow, run after run",
    }[mode];
  }

  // "Once" is one execution, not one turn. A conversational workflow asks
  // follow-up questions, so Run stays armed through them and lets go when the
  // workflow finishes. `conversation` going null is that signal, and it covers
  // a cancelled run too, since abandoning one still ends it. Listen never lets
  // go on its own: it carries straight into the next run.
  document.addEventListener("twt:chat-reply", (event) => {
    if (mode === "off") return;
    if (onTurn(event.detail) === false) return;
    const finished = !event.detail.conversation;
    if (mode === "once" && finished) setMode("off", true);
  });

  runBtn.addEventListener("click", () => setMode("once"));
  listenBtn.addEventListener("click", () => setMode(mode === "many" ? "off" : "many"));

  setMode("off");
}
