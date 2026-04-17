document.addEventListener("DOMContentLoaded", async () => {
  const config = await chrome.storage.sync.get(["agentUrl", "apiKey"]);
  document.getElementById("agentUrl").value = config.agentUrl || "";
  document.getElementById("apiKey").value = config.apiKey || "";

  document.getElementById("saveBtn").addEventListener("click", async () => {
    const agentUrl = document.getElementById("agentUrl").value.trim().replace(/\/+$/, "");
    const apiKey = document.getElementById("apiKey").value.trim();
    const status = document.getElementById("status");

    status.className = "status";
    status.textContent = "";
    status.style.display = "none";

    if (!agentUrl) {
      showStatus("error", "Agent URL is required.");
      return;
    }

    // Step 1: Check /health (is server reachable?)
    showStatus("testing", "Testing connection...");

    let healthData;
    try {
      const healthRes = await fetch(`${agentUrl}/health`, { signal: AbortSignal.timeout(5000) });
      healthData = await healthRes.json();
      if (healthData.status !== "ok" && healthData.status !== "degraded") {
        showStatus("error", "Server responded but status is unhealthy.");
        return;
      }
    } catch (e) {
      showStatus("error", `Cannot reach server at ${agentUrl}. Check the URL and make sure the server is running.`);
      return;
    }

    // Step 2: If no API key and server is local mode, offer /setup
    if (!apiKey && healthData.mode === "local") {
      showStatus("info", "No API key set. This is a local server — visit " + agentUrl + "/setup to create one.");
      await chrome.storage.sync.set({ agentUrl });
      return;
    }

    if (!apiKey) {
      showStatus("error", "API Key is required. Get one from your server admin or invite link.");
      return;
    }

    // Step 3: Verify API key with /whoami
    try {
      const whoRes = await fetch(`${agentUrl}/whoami`, {
        headers: { "X-Dugg-Key": apiKey },
        signal: AbortSignal.timeout(5000),
      });
      if (whoRes.status === 401) {
        showStatus("error", "Invalid API key. Check your key and try again.");
        return;
      }
      const whoData = await whoRes.json();
      if (!whoData.user) {
        showStatus("error", "Unexpected response from server.");
        return;
      }

      // All good — save
      await chrome.storage.sync.set({ agentUrl, apiKey });
      showStatus("success", `Connected as ${whoData.user.name}. Settings saved.`);
    } catch (e) {
      showStatus("error", `Key verification failed: ${e.message}`);
    }
  });
});

function showStatus(type, message) {
  const status = document.getElementById("status");
  status.style.display = "block";
  status.className = "status " + type;
  status.textContent = message;
}
