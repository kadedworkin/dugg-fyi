document.addEventListener("DOMContentLoaded", async () => {
  const config = await chrome.storage.sync.get(["agentUrl", "apiKey"]);

  if (!config.agentUrl || !config.apiKey) {
    document.getElementById("setup").style.display = "block";
    document.getElementById("openSettings").addEventListener("click", (e) => {
      e.preventDefault();
      chrome.runtime.openOptionsPage();
    });
    return;
  }

  document.getElementById("main").style.display = "block";
  document.getElementById("settingsLink").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const pageTitle = document.getElementById("pageTitle");
  const noteInput = document.getElementById("note");
  const digBtn = document.getElementById("digBtn");
  const toast = document.getElementById("toast");
  const selectionNote = document.getElementById("selectionNote");

  pageTitle.textContent = tab.title || tab.url;

  let selectedText = "";
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.getSelection().toString(),
    });
    selectedText = (result && result.result) || "";
  } catch (_) {}

  if (selectedText) {
    selectionNote.style.display = "block";
  }

  digBtn.addEventListener("click", async () => {
    digBtn.disabled = true;
    digBtn.textContent = "Sending...";
    toast.className = "toast";
    toast.style.display = "none";

    const note = [noteInput.value.trim(), selectedText].filter(Boolean).join("\n\n---\n\n");

    try {
      const url = config.agentUrl.replace(/\/+$/, "");
      const res = await fetch(`${url}/tools/dugg_add`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Dugg-Key": config.apiKey,
        },
        body: JSON.stringify({
          url: tab.url,
          note: note || undefined,
        }),
      });

      if (res.ok) {
        toast.textContent = "\u2713 Sent to agent";
        toast.className = "toast success";
        digBtn.textContent = "Dugg!";
      } else {
        const data = await res.json().catch(() => ({}));
        toast.textContent = "\u2717 " + (data.error || `Error ${res.status}`);
        toast.className = "toast error";
        digBtn.textContent = "Dugg it";
        digBtn.disabled = false;
      }
    } catch (err) {
      toast.textContent = "\u2717 Failed \u2014 check connection";
      toast.className = "toast error";
      digBtn.textContent = "Dugg it";
      digBtn.disabled = false;
    }
  });
});
