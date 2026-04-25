/**
 * Dugg This — Background Service Worker
 *
 * Handles:
 * 1. Hourly URL cache sync from all configured servers
 * 2. Badge matching — lights up when current tab URL is in any Dugg feed
 * 3. "Surprise me" random URL discovery
 */

const SYNC_INTERVAL_MS = 60 * 60 * 1000; // 1 hour
const SYNC_ALARM = "dugg-url-sync";

// --- Alarms ---

chrome.alarms.create(SYNC_ALARM, {
  delayInMinutes: 1,       // first sync 1 min after install/startup
  periodInMinutes: 60,     // then every hour
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === SYNC_ALARM) {
    await syncUrlCache();
  }
});

// Also sync on install/update
chrome.runtime.onInstalled.addListener(() => {
  syncUrlCache();
});

// --- URL Cache Sync ---

async function syncUrlCache() {
  const config = await chrome.storage.sync.get(["agentUrl", "apiKey"]);
  if (!config.agentUrl || !config.apiKey) return;

  const url = config.agentUrl.replace(/\/+$/, "");
  try {
    // First get the list of servers (instances) the user is connected to
    const instances = await fetchInstances(url, config.apiKey);

    // Build the URL map: { url -> [{ title, id, by, server }] }
    const urlMap = {};
    const allEntries = [];
    const unreadEntries = [];

    // Fetch from the primary server
    const primary = await fetchFeedUrls(url, config.apiKey);
    if (primary) {
      for (const entry of primary) {
        const key = normalizeUrl(entry.url);
        if (!urlMap[key]) urlMap[key] = [];
        urlMap[key].push({ ...entry, server: extractServerName(url) });
        allEntries.push({ ...entry, server: extractServerName(url) });
      }
    }
    const primaryUnread = await fetchFeedUrls(url, config.apiKey, { unreadOnly: true });
    if (primaryUnread) {
      for (const entry of primaryUnread) {
        unreadEntries.push({ ...entry, server: extractServerName(url) });
      }
    }

    // Fetch from connected instances
    for (const inst of instances) {
      if (!inst.url || !inst.key) continue;
      const instUrl = inst.url.replace(/\/+$/, "");
      const remote = await fetchFeedUrls(instUrl, inst.key);
      if (remote) {
        for (const entry of remote) {
          const key = normalizeUrl(entry.url);
          if (!urlMap[key]) urlMap[key] = [];
          urlMap[key].push({ ...entry, server: inst.name || extractServerName(instUrl) });
          allEntries.push({ ...entry, server: inst.name || extractServerName(instUrl) });
        }
      }
      const remoteUnread = await fetchFeedUrls(instUrl, inst.key, { unreadOnly: true });
      if (remoteUnread) {
        for (const entry of remoteUnread) {
          unreadEntries.push({ ...entry, server: inst.name || extractServerName(instUrl) });
        }
      }
    }

    await chrome.storage.local.set({
      duggUrlMap: urlMap,
      duggAllEntries: allEntries,
      duggUnreadEntries: unreadEntries,
      duggLastSync: Date.now(),
    });

    // Update badge for the active tab
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab) updateBadge(tab.id, tab.url);
  } catch (e) {
    console.error("[dugg] URL cache sync failed:", e);
  }
}

async function fetchFeedUrls(baseUrl, apiKey, { unreadOnly = false } = {}) {
  try {
    const endpoint = unreadOnly
      ? `${baseUrl}/api/feed/urls?unread=true`
      : `${baseUrl}/feed/urls/${apiKey}`;
    const options = unreadOnly
      ? {
          headers: { "X-Dugg-Key": apiKey },
          signal: AbortSignal.timeout(10000),
        }
      : {
          signal: AbortSignal.timeout(10000),
        };
    const res = await fetch(endpoint, options);
    if (!res.ok) return null;
    const data = await res.json();
    return data.urls || [];
  } catch {
    return null;
  }
}

async function postReadState(baseUrl, apiKey, resourceId, source) {
  if (!resourceId) return false;

  try {
    const res = await fetch(`${baseUrl}/api/read/${resourceId}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Dugg-Key": apiKey,
      },
      body: JSON.stringify({ source }),
      signal: AbortSignal.timeout(10000),
    });
    if (!res.ok) return null;
    return true;
  } catch {
    return false;
  }
}

async function fetchInstances(baseUrl, apiKey) {
  try {
    const res = await fetch(`${baseUrl}/instances`, {
      headers: { "X-Dugg-Key": apiKey },
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return [];
    const data = await res.json();
    return data.instances || [];
  } catch {
    return [];
  }
}

function normalizeUrl(url) {
  try {
    const u = new URL(url);
    // Strip trailing slash, lowercase host
    let path = u.pathname.replace(/\/+$/, "") || "/";
    return `${u.protocol}//${u.host.toLowerCase()}${path}${u.search}`;
  } catch {
    return url;
  }
}

function extractServerName(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

// --- Badge ---

async function updateBadge(tabId, tabUrl) {
  if (!tabUrl || tabUrl.startsWith("chrome://") || tabUrl.startsWith("chrome-extension://")) {
    chrome.action.setBadgeText({ text: "", tabId });
    return;
  }

  const { duggUrlMap } = await chrome.storage.local.get(["duggUrlMap"]);
  if (!duggUrlMap) {
    chrome.action.setBadgeText({ text: "", tabId });
    return;
  }

  const key = normalizeUrl(tabUrl);
  const matches = duggUrlMap[key];
  if (matches && matches.length > 0) {
    chrome.action.setBadgeText({ text: String(matches.length), tabId });
    chrome.action.setBadgeBackgroundColor({ color: "#6366f1", tabId });
  } else {
    chrome.action.setBadgeText({ text: "", tabId });
  }
}

// Update badge on tab change / navigation
chrome.tabs.onActivated.addListener(async (activeInfo) => {
  const tab = await chrome.tabs.get(activeInfo.tabId);
  if (tab.url) updateBadge(tab.id, tab.url);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url || changeInfo.status === "complete") {
    if (tab.url) updateBadge(tabId, tab.url);
  }
});

// --- Message API (for popup/content scripts) ---

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "getMatches") {
    getMatchesForUrl(msg.url).then(sendResponse);
    return true; // async
  }
  if (msg.type === "surpriseMe") {
    const targetTabId = msg.tabId || (sender.tab && sender.tab.id);
    openRandomUnreadUrl(msg.excludeUrl, targetTabId).then(sendResponse);
    return true;
  }
  if (msg.type === "syncNow") {
    syncUrlCache().then(() => sendResponse({ ok: true }));
    return true;
  }
});

async function getMatchesForUrl(url) {
  const { duggUrlMap } = await chrome.storage.local.get(["duggUrlMap"]);
  if (!duggUrlMap) return [];
  return duggUrlMap[normalizeUrl(url)] || [];
}

async function getRandomUrl(excludeUrl) {
  const { duggUnreadEntries } = await chrome.storage.local.get(["duggUnreadEntries"]);
  if (!duggUnreadEntries || duggUnreadEntries.length === 0) return null;

  // Filter out the current page and dugg:// internal URLs.
  const excludeKey = excludeUrl ? normalizeUrl(excludeUrl) : null;
  const candidates = duggUnreadEntries.filter(e =>
    !e.url.startsWith("dugg://") &&
    normalizeUrl(e.url) !== excludeKey
  );

  if (candidates.length === 0) return null;
  const pick = candidates[Math.floor(Math.random() * candidates.length)];
  return pick;
}

async function openRandomUnreadUrl(excludeUrl, tabId) {
  const pick = await getRandomUrl(excludeUrl);
  if (!pick || !pick.url) {
    return { ok: false, reason: "caught_up" };
  }

  if (typeof tabId === "number") {
    await chrome.tabs.update(tabId, { url: pick.url });
  } else {
    await chrome.tabs.create({ url: pick.url });
  }

  const config = await chrome.storage.sync.get(["agentUrl", "apiKey"]);
  if (config.agentUrl && config.apiKey) {
    const baseUrl = config.agentUrl.replace(/\/+$/, "");
    postReadState(baseUrl, config.apiKey, pick.id, "chrome_stumble").catch(() => {});
  }

  return { ok: true, entry: pick };
}
