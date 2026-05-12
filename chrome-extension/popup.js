document.addEventListener("DOMContentLoaded", async () => {
  const els = {
    main: document.getElementById("main"),
    setup: document.getElementById("setup"),
    openSettings: document.getElementById("openSettings"),
    settingsLink: document.getElementById("settingsLink"),
    surpriseBtn: document.getElementById("surpriseBtn"),
    pageTitle: document.getElementById("pageTitle"),
    pageUrl: document.getElementById("pageUrl"),
    toast: document.getElementById("toast"),
    syncInfo: document.getElementById("syncInfo"),
    addState: document.getElementById("addState"),
    existingState: document.getElementById("existingState"),
    addUrl: document.getElementById("addUrl"),
    addNote: document.getElementById("addNote"),
    digBtn: document.getElementById("digBtn"),
    selectionNote: document.getElementById("selectionNote"),
    distributeSection: document.getElementById("distributeSection"),
    distributeList: document.getElementById("distributeList"),
    resourceTitle: document.getElementById("resourceTitle"),
    resourceUrl: document.getElementById("resourceUrl"),
    starBtn: document.getElementById("starBtn"),
    thumbBtn: document.getElementById("thumbBtn"),
    starCount: document.getElementById("starCount"),
    thumbCount: document.getElementById("thumbCount"),
    notesPreview: document.getElementById("notesPreview"),
    notesSummary: document.getElementById("notesSummary"),
    notesSnippet: document.getElementById("notesSnippet"),
    existingSelectionNote: document.getElementById("existingSelectionNote"),
    existingNote: document.getElementById("existingNote"),
    addExistingNoteBtn: document.getElementById("addExistingNoteBtn"),
    openFeedLink: document.getElementById("openFeedLink"),
  };

  const config = await chrome.storage.sync.get(["agentUrl", "apiKey"]);
  if (!config.agentUrl || !config.apiKey) {
    els.setup.style.display = "block";
    els.openSettings.addEventListener("click", (event) => {
      event.preventDefault();
      chrome.runtime.openOptionsPage();
    });
    return;
  }

  const baseUrl = config.agentUrl.replace(/\/+$/, "");
  const apiKey = config.apiKey;
  const state = {
    baseUrl,
    apiKey,
    selectedText: "",
    pageDescription: "",
    pageTranscript: "",
    currentTab: null,
    resource: null,
    distributionTargets: [],
  };

  els.main.style.display = "block";
  els.settingsLink.addEventListener("click", (event) => {
    event.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  state.currentTab = tab;

  els.pageTitle.textContent = tab.title || tab.url || "Current page";
  els.pageUrl.textContent = tab.url || "";
  els.addUrl.value = tab.url || "";

  wireSurpriseButton(els.surpriseBtn, tab);
  showLastSync(els.syncInfo);

  const pageContext = await scrapePageContext(tab.id);
  state.selectedText = pageContext.selection || "";
  state.pageDescription = pageContext.description || "";
  state.pageTranscript = pageContext.transcript || "";

  const selectedTextExists = Boolean(state.selectedText);
  els.selectionNote.style.display = selectedTextExists ? "block" : "none";
  els.existingSelectionNote.style.display = selectedTextExists ? "block" : "none";

  const lookup = isHttpUrl(tab.url)
    ? await lookupExistingResource(baseUrl, apiKey, tab.url)
    : null;

  if (lookup) {
    state.resource = lookup;
    renderExistingState(els, state);
  } else {
    state.distributionTargets = await fetchDistributionTargets(baseUrl, apiKey);
    renderAddState(els, state);
  }
});

function isHttpUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

async function lookupExistingResource(baseUrl, apiKey, currentUrl) {
  try {
    const res = await fetch(`${baseUrl}/api/feed/urls?url=${encodeURIComponent(currentUrl)}`, {
      headers: { "X-Dugg-Key": apiKey },
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) {
      return null;
    }
    const data = await res.json();
    return (data.urls || [])[0] || null;
  } catch (_) {
    return null;
  }
}

async function fetchDistributionTargets(baseUrl, apiKey) {
  try {
    const res = await fetch(`${baseUrl}/instances`, {
      headers: { "X-Dugg-Key": apiKey },
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) {
      return [];
    }
    const data = await res.json();
    return data.instances || [];
  } catch (_) {
    return [];
  }
}

async function scrapePageContext(tabId) {
  if (typeof tabId !== "number") {
    return {};
  }

  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const out = { selection: window.getSelection().toString() };
        const ogDesc = document.querySelector('meta[property="og:description"]');
        const metaDesc = document.querySelector('meta[name="description"]');
        out.description = (ogDesc && ogDesc.content) || (metaDesc && metaDesc.content) || "";

        if (location.hostname.includes("youtube.com") && location.pathname === "/watch") {
          const descEl = document.querySelector(
            "ytd-watch-metadata #description-inner, " +
            "ytd-text-inline-expander .content, " +
            "#structured-description .content"
          );
          if (descEl && descEl.innerText.length > out.description.length) {
            out.description = descEl.innerText.trim();
          }

          const transcriptSegments = document.querySelectorAll(
            "ytd-transcript-segment-renderer .segment-text, " +
            "yt-formatted-string.segment-text"
          );
          if (transcriptSegments.length > 0) {
            out.transcript = Array.from(transcriptSegments)
              .map((el) => el.innerText.trim())
              .filter(Boolean)
              .join(" ");
          }
        }

        return out;
      },
    });
    return (result && result.result) || {};
  } catch (_) {
    return {};
  }
}

function renderAddState(els, state) {
  els.existingState.style.display = "none";
  els.addState.style.display = "block";
  renderDistributionTargets(els.distributeSection, els.distributeList, state.distributionTargets);

  if (!isHttpUrl(state.currentTab.url)) {
    els.digBtn.disabled = true;
    els.digBtn.textContent = "Unsupported page";
    showToast(els.toast, "This page can't be added from the popup.", "error");
    return;
  }

  els.digBtn.addEventListener("click", async () => {
    els.digBtn.disabled = true;
    els.digBtn.textContent = "Sending...";
    resetToast(els.toast);

    const note = composeNote(els.addNote.value, state.selectedText);
    const payload = { url: els.addUrl.value.trim() || state.currentTab.url };
    if (note) payload.note = note;
    if (state.pageDescription) payload.description = state.pageDescription;
    if (state.pageTranscript) payload.transcript = state.pageTranscript;

    try {
      const res = await fetch(`${state.baseUrl}/tools/dugg_add`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Dugg-Key": state.apiKey,
        },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showToast(els.toast, "\u2717 " + (data.error || `Error ${res.status}`), "error");
        els.digBtn.textContent = "Dugg it";
        els.digBtn.disabled = false;
        return;
      }

      const checkedTargets = Array.from(
        document.querySelectorAll('#distributeList input[type="checkbox"]:checked')
      ).map((checkbox) => checkbox.value);

      if (checkedTargets.length > 0) {
        const addData = await res.clone().json().catch(() => null);
        const resourceId = extractResourceId(addData);
        if (resourceId) {
          fetch(`${state.baseUrl}/tools/dugg_publish`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Dugg-Key": state.apiKey,
            },
            body: JSON.stringify({
              resource_id: resourceId,
              targets: checkedTargets,
            }),
          }).catch(() => {});
        }
      }

      chrome.runtime.sendMessage({ type: "syncNow" }).catch(() => {});

      const targetCount = checkedTargets.length;
      const message = targetCount > 0
        ? `\u2713 Dugg + distributing to ${targetCount} server${targetCount > 1 ? "s" : ""}`
        : "\u2713 Dugg!";
      showToast(els.toast, message, "success");
      els.digBtn.textContent = "Dugg!";
      setTimeout(() => window.close(), 1200);
    } catch (_) {
      showToast(els.toast, "\u2717 Failed — check connection", "error");
      els.digBtn.textContent = "Dugg it";
      els.digBtn.disabled = false;
    }
  });
}

function renderDistributionTargets(sectionEl, listEl, targets) {
  listEl.innerHTML = "";
  if (!targets.length) {
    sectionEl.style.display = "none";
    return;
  }

  sectionEl.style.display = "block";
  for (const target of targets) {
    const item = document.createElement("label");
    item.className = "distribute-item";
    item.innerHTML = `<input type="checkbox" value="${escapeHtmlAttr(target.name || "")}" checked>
      <span>${escapeHtml(target.name || "Unnamed server")}</span>`;
    listEl.appendChild(item);
  }
}

function renderExistingState(els, state) {
  const resource = state.resource;
  els.addState.style.display = "none";
  els.existingState.style.display = "block";

  els.resourceTitle.textContent = resource.title || state.currentTab.title || resource.url || state.currentTab.url;
  els.resourceUrl.textContent = resource.url || state.currentTab.url || "";
  els.openFeedLink.href = `${state.baseUrl}/feed/${state.apiKey}#r-${resource.id}`;

  updateNotesPreview(els, resource.notes_count || 0, resource.primary_note_preview || "");
  applyReactionState(els.starBtn, els.starCount, Boolean(resource.viewer_reactions && resource.viewer_reactions.star), resource.reaction_counts && resource.reaction_counts.star);
  applyReactionState(els.thumbBtn, els.thumbCount, Boolean(resource.viewer_reactions && resource.viewer_reactions.thumbsup), resource.reaction_counts && resource.reaction_counts.thumbsup);

  els.starBtn.addEventListener("click", () => toggleReaction(els, state, els.starBtn, els.starCount));
  els.thumbBtn.addEventListener("click", () => toggleReaction(els, state, els.thumbBtn, els.thumbCount));
  els.addExistingNoteBtn.addEventListener("click", () => addSiblingNote(els, state));
}

function updateNotesPreview(els, count, preview) {
  if (!count) {
    els.notesPreview.open = false;
    els.notesPreview.classList.add("empty");
    els.notesSummary.textContent = "No notes yet";
    els.notesSnippet.textContent = "Add the first note from the popup.";
    return;
  }

  els.notesPreview.classList.remove("empty");
  els.notesSummary.textContent = `${count} note${count === 1 ? "" : "s"}`;
  els.notesSnippet.textContent = preview || "Notes exist for this page.";
}

function applyReactionState(button, countEl, isActive, rawCount) {
  const count = Math.max(0, Number(rawCount) || 0);
  button.dataset.active = String(isActive);
  button.dataset.count = String(count);
  button.classList.toggle("is-active", isActive);
  button.setAttribute("aria-pressed", String(isActive));
  countEl.textContent = String(count);
}

async function toggleReaction(els, state, button, countEl) {
  if (button.disabled || !state.resource) {
    return;
  }

  const reactionType = button.dataset.reactionType;
  const wasActive = button.dataset.active === "true";
  const previousCount = Number(button.dataset.count || "0");
  const nextActive = !wasActive;
  const nextCount = nextActive ? previousCount + 1 : Math.max(0, previousCount - 1);

  button.disabled = true;
  applyReactionState(button, countEl, nextActive, nextCount);

  try {
    const endpoint = `${state.baseUrl}/api/react/${encodeURIComponent(state.resource.id)}`;
    const options = nextActive
      ? {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Dugg-Key": state.apiKey,
            "X-Dugg-Surface": "web",
          },
          body: JSON.stringify({ reaction: reactionType }),
        }
      : {
          method: "DELETE",
          headers: {
            "X-Dugg-Key": state.apiKey,
            "X-Dugg-Surface": "web",
          },
        };
    const requestUrl = nextActive ? endpoint : `${endpoint}?type=${encodeURIComponent(reactionType)}`;
    const res = await fetch(requestUrl, options);
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
  } catch (_) {
    applyReactionState(button, countEl, wasActive, previousCount);
    showToast(els.toast, "Reaction update failed.", "error");
  } finally {
    button.disabled = false;
  }
}

async function addSiblingNote(els, state) {
  const rawText = els.existingNote.value.trim();
  const note = composeNote(rawText, state.selectedText);
  if (!note || !state.resource) {
    showToast(els.toast, "Add some note text first.", "error");
    return;
  }

  els.addExistingNoteBtn.disabled = true;
  els.addExistingNoteBtn.textContent = "Adding...";
  resetToast(els.toast);

  try {
    const res = await fetch(`${state.baseUrl}/api/note`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Dugg-Key": state.apiKey,
      },
      body: JSON.stringify({
        resource_id: state.resource.id,
        note,
      }),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    els.existingNote.value = "";
    const nextCount = Number(state.resource.notes_count || 0) + 1;
    state.resource.notes_count = nextCount;
    if (!state.resource.primary_note_preview) {
      state.resource.primary_note_preview = note.slice(0, 100);
    }
    updateNotesPreview(els, nextCount, state.resource.primary_note_preview);
    showToast(els.toast, "Note added", "success");
  } catch (_) {
    showToast(els.toast, "Couldn't add note.", "error");
  } finally {
    els.addExistingNoteBtn.disabled = false;
    els.addExistingNoteBtn.textContent = "Add Note";
  }
}

function composeNote(noteText, selectedText) {
  return [noteText.trim(), selectedText.trim()].filter(Boolean).join("\n\n---\n\n");
}

function extractResourceId(addData) {
  if (!addData) {
    return null;
  }
  const text = typeof addData === "string"
    ? addData
    : (addData.text || addData.result || JSON.stringify(addData));
  const idMatch = String(text).match(/^ID:\s+([a-f0-9]{12})/m);
  return idMatch ? idMatch[1] : null;
}

function showToast(toastEl, message, kind) {
  toastEl.textContent = message;
  toastEl.className = `toast ${kind}`;
}

function resetToast(toastEl) {
  toastEl.textContent = "";
  toastEl.className = "toast";
}

async function showLastSync(syncInfoEl) {
  try {
    const { duggLastSync } = await chrome.storage.local.get(["duggLastSync"]);
    if (!duggLastSync) {
      return;
    }
    const ago = Math.round((Date.now() - duggLastSync) / 60000);
    const label = ago < 1 ? "just now" : ago === 1 ? "1 min ago" : `${ago} min ago`;
    syncInfoEl.textContent = `Cache synced ${label}`;
  } catch (_) {}
}

function wireSurpriseButton(button, tab) {
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "...";
    try {
      const result = await chrome.runtime.sendMessage({
        type: "surpriseMe",
        excludeUrl: tab.url,
        tabId: tab.id,
      });
      if (result && result.ok) {
        window.close();
        return;
      }
      if (result && result.reason === "caught_up") {
        button.textContent = "All caught up";
      } else {
        button.textContent = "Nothing yet";
      }
      setTimeout(() => {
        button.textContent = "Surprise me";
        button.disabled = false;
      }, 1800);
    } catch (_) {
      button.textContent = "Surprise me";
      button.disabled = false;
    }
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;");
}

function escapeHtmlAttr(value) {
  return escapeHtml(value).replace(/'/g, "&#39;");
}
