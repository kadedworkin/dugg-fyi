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

  // Fetch distribution targets
  const url = config.agentUrl.replace(/\/+$/, "");
  let instances = [];
  try {
    const instRes = await fetch(`${url}/instances`, {
      headers: { "X-Dugg-Key": config.apiKey },
      signal: AbortSignal.timeout(3000),
    });
    if (instRes.ok) {
      const data = await instRes.json();
      instances = data.instances || [];
    }
  } catch (_) {}

  if (instances.length > 0) {
    const section = document.getElementById("distributeSection");
    const list = document.getElementById("distributeList");
    section.style.display = "block";
    for (const inst of instances) {
      const item = document.createElement("label");
      item.className = "distribute-item";
      item.innerHTML = `<input type="checkbox" value="${inst.name}" checked>
        <span class="inst-name">${inst.name}</span>`;
      list.appendChild(item);
    }
  }

  digBtn.addEventListener("click", async () => {
    digBtn.disabled = true;
    digBtn.textContent = "Sending...";
    toast.className = "toast";
    toast.style.display = "none";

    const note = [noteInput.value.trim(), selectedText].filter(Boolean).join("\n\n---\n\n");

    try {
      // Step 1: Save locally
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

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        toast.textContent = "\u2717 " + (data.error || `Error ${res.status}`);
        toast.className = "toast error";
        digBtn.textContent = "Dugg it";
        digBtn.disabled = false;
        return;
      }

      // Step 2: Publish to checked targets
      const checked = [...document.querySelectorAll('#distributeList input[type="checkbox"]:checked')]
        .map(cb => cb.value);

      if (checked.length > 0) {
        // Get resource ID from the add response
        const addData = await res.clone().json().catch(() => null);
        let resourceId = null;
        if (addData) {
          // The tool response text contains the resource info — parse the ID
          const text = typeof addData === "string" ? addData : (addData.text || addData.result || JSON.stringify(addData));
          const idMatch = String(text).match(/id[=: ]+([a-f0-9]{12})/i);
          if (idMatch) resourceId = idMatch[1];
        }

        if (resourceId) {
          // Fire publish — don't block the success toast on it
          fetch(`${url}/tools/dugg_publish`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Dugg-Key": config.apiKey,
            },
            body: JSON.stringify({
              resource_id: resourceId,
              targets: checked,
            }),
          }).catch(() => {});
        }
      }

      const targetCount = checked.length;
      const msg = targetCount > 0
        ? `\u2713 Dugg + distributing to ${targetCount} server${targetCount > 1 ? "s" : ""}`
        : "\u2713 Dugg!";
      toast.textContent = msg;
      toast.className = "toast success";
      digBtn.textContent = "Dugg!";
      setTimeout(() => window.close(), 1200);
    } catch (err) {
      toast.textContent = "\u2717 Failed \u2014 check connection";
      toast.className = "toast error";
      digBtn.textContent = "Dugg it";
      digBtn.disabled = false;
    }
  });
});
