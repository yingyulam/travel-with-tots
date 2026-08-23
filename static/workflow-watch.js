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
function watchChatReplies({ runId, listenId, statusId, statusTextId, onTurn }) {
  const runBtn = document.getElementById(runId);
  const listenBtn = document.getElementById(listenId);
  const status = document.getElementById(statusId);
  const statusText = document.getElementById(statusTextId);

  // "once" stops after the next message; "many" keeps going. Kept as one
  // variable so Run and Listen cannot both be armed at the same time.
  let mode = "off";

  // The state drives the banner's colour in CSS, so the wording and the look
  // cannot disagree about whether the page is armed.
  function setMode(next) {
    mode = next;
    listenBtn.textContent = mode === "many" ? "⏹ Stop listening" : "👂 Listen";
    status.dataset.state = mode;
    statusText.textContent = {
      off: "Not watching",
      once: "Waiting for your next message",
      many: "Listening: every message you send will be processed",
    }[mode];
  }

  document.addEventListener("twt:chat-reply", (event) => {
    if (mode === "off") return;
    onTurn(event.detail);
    setMode(mode === "once" ? "off" : "many");
  });

  runBtn.addEventListener("click", () => setMode("once"));
  listenBtn.addEventListener("click", () => setMode(mode === "many" ? "off" : "many"));

  setMode("off");
}
