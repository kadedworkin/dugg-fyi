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
  const surpriseBtn = document.getElementById("surpriseBtn");

  pageTitle.textContent = tab.title || tab.url;

  // --- Check if this page is already Dugg ---
  try {
    const matches = await chrome.runtime.sendMessage({
      type: "getMatches",
      url: tab.url,
    });
    if (matches && matches.length > 0) {
      const matchInfo = document.getElementById("matchInfo");
      const matchDetails = document.getElementById("matchDetails");
      matchInfo.style.display = "block";

      // Group by server
      const byServer = {};
      for (const m of matches) {
        const srv = m.server || "local";
        if (!byServer[srv]) byServer[srv] = [];
        byServer[srv].push(m);
      }

      let html = "";
      for (const [server, entries] of Object.entries(byServer)) {
        const submitters = [...new Set(entries.map(e => e.by).filter(Boolean))];
        html += `<div class="match-server">${server}</div>`;
        if (submitters.length > 0) {
          html += `<div class="match-by">by ${submitters.join(", ")}</div>`;
        }
      }
      matchDetails.innerHTML = html;
    }
  } catch (_) {}

  // --- Show last sync time ---
  try {
    const { duggLastSync } = await chrome.storage.local.get(["duggLastSync"]);
    if (duggLastSync) {
      const ago = Math.round((Date.now() - duggLastSync) / 60000);
      const label = ago < 1 ? "just now" : ago === 1 ? "1 min ago" : `${ago} min ago`;
      document.getElementById("syncInfo").textContent = `Cache synced ${label}`;
    }
  } catch (_) {}

  // --- Surprise Me ---
  surpriseBtn.addEventListener("click", async () => {
    surpriseBtn.disabled = true;
    surpriseBtn.textContent = "...";
    try {
      const pick = await chrome.runtime.sendMessage({
        type: "surpriseMe",
        excludeUrl: tab.url,
      });
      if (pick && pick.url) {
        chrome.tabs.update(tab.id, { url: pick.url });
        window.close();
      } else {
        surpriseBtn.textContent = "Nothing yet";
        setTimeout(() => {
          surpriseBtn.textContent = "Surprise me";
          surpriseBtn.disabled = false;
        }, 1500);
      }
    } catch (_) {
      surpriseBtn.textContent = "Surprise me";
      surpriseBtn.disabled = false;
    }
  });

  // --- Scrape page content ---
  let selectedText = "";
  let pageDescription = "";
  let pageTranscript = "";
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const out = { selection: window.getSelection().toString() };

        // Extract description from meta tags
        const ogDesc = document.querySelector('meta[property="og:description"]');
        const metaDesc = document.querySelector('meta[name="description"]');
        out.description = (ogDesc && ogDesc.content) || (metaDesc && metaDesc.content) || "";

        // YouTube-specific: extract description from the page DOM
        if (location.hostname.includes("youtube.com") && location.pathname === "/watch") {
          const descEl = document.querySelector(
            "ytd-watch-metadata #description-inner, " +
            "ytd-text-inline-expander .content, " +
            "#structured-description .content"
          );
          if (descEl && descEl.innerText.length > out.description.length) {
            out.description = descEl.innerText.trim();
          }

          // YouTube transcript: check if transcript panel is open
          const transcriptSegments = document.querySelectorAll(
            "ytd-transcript-segment-renderer .segment-text, " +
            "yt-formatted-string.segment-text"
          );
          if (transcriptSegments.length > 0) {
            out.transcript = Array.from(transcriptSegments)
              .map(el => el.innerText.trim())
              .filter(Boolean)
              .join(" ");
          }
        }

        return out;
      },
    });
    const data = (result && result.result) || {};
    selectedText = data.selection || "";
    pageDescription = data.description || "";
    pageTranscript = data.transcript || "";
  } catch (_) {}

  if (selectedText) {
    selectionNote.style.display = "block";
  }

  // --- Fetch distribution targets ---
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

  // --- Dugg it button ---
  digBtn.addEventListener("click", async () => {
    digBtn.disabled = true;
    digBtn.textContent = "Sending...";
    toast.className = "toast";
    toast.style.display = "none";

    const note = [noteInput.value.trim(), selectedText].filter(Boolean).join("\n\n---\n\n");

    try {
      // Step 1: Save locally (include scraped page content)
      const payload = { url: tab.url };
      if (note) payload.note = note;
      if (pageDescription) payload.description = pageDescription;
      if (pageTranscript) payload.transcript = pageTranscript;

      const res = await fetch(`${url}/tools/dugg_add`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Dugg-Key": config.apiKey,
        },
        body: JSON.stringify(payload),
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
        const addData = await res.clone().json().catch(() => null);
        let resourceId = null;
        if (addData) {
          const text = typeof addData === "string" ? addData : (addData.text || addData.result || JSON.stringify(addData));
          const idMatch = String(text).match(/id[=: ]+([a-f0-9]{12})/i);
          if (idMatch) resourceId = idMatch[1];
        }

        if (resourceId) {
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

      // Trigger a cache sync so this new URL shows up in badges
      chrome.runtime.sendMessage({ type: "syncNow" }).catch(() => {});

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
