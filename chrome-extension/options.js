document.addEventListener("DOMContentLoaded", async () => {
  const config = await chrome.storage.sync.get(["agentUrl", "apiKey"]);
  document.getElementById("agentUrl").value = config.agentUrl || "";
  document.getElementById("apiKey").value = config.apiKey || "";

  document.getElementById("saveBtn").addEventListener("click", async () => {
    const agentUrl = document.getElementById("agentUrl").value.trim().replace(/\/+$/, "");
    const apiKey = document.getElementById("apiKey").value.trim();

    await chrome.storage.sync.set({ agentUrl, apiKey });

    const toast = document.getElementById("toast");
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 2000);
  });
});
