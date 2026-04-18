/**
 * Dugg This — Content Script
 *
 * Injects a top-of-page banner when the current URL matches entries
 * in the user's Dugg feed cache. Shows who submitted it and which server(s).
 */

(async () => {
  // Don't inject on chrome:// or extension pages
  if (location.protocol === "chrome:" || location.protocol === "chrome-extension:") return;

  try {
    const matches = await chrome.runtime.sendMessage({
      type: "getMatches",
      url: location.href,
    });

    if (!matches || matches.length === 0) return;

    // Build the banner
    const banner = document.createElement("div");
    banner.id = "dugg-banner";

    const logo = document.createElement("span");
    logo.className = "dugg-logo";
    logo.textContent = "dugg";
    banner.appendChild(logo);

    const info = document.createElement("div");
    info.className = "dugg-info";

    // Group matches by server
    const byServer = {};
    for (const m of matches) {
      const srv = m.server || "local";
      if (!byServer[srv]) byServer[srv] = [];
      byServer[srv].push(m);
    }

    for (const [server, entries] of Object.entries(byServer)) {
      const chip = document.createElement("span");
      chip.className = "dugg-match";

      const srvSpan = document.createElement("span");
      srvSpan.className = "dugg-server";
      srvSpan.textContent = server;
      chip.appendChild(srvSpan);

      // Show who submitted (deduplicate)
      const submitters = [...new Set(entries.map(e => e.by).filter(Boolean))];
      if (submitters.length > 0) {
        const bySpan = document.createElement("span");
        bySpan.className = "dugg-by";
        bySpan.textContent = `by ${submitters.join(", ")}`;
        chip.appendChild(bySpan);
      }

      info.appendChild(chip);
    }

    banner.appendChild(info);

    const close = document.createElement("button");
    close.className = "dugg-close";
    close.textContent = "\u00d7";
    close.addEventListener("click", () => {
      banner.remove();
      document.body.style.marginTop = "";
    });
    banner.appendChild(close);

    document.body.insertBefore(banner, document.body.firstChild);
    // Push page content down so banner doesn't overlay
    document.body.style.marginTop = (banner.offsetHeight) + "px";
  } catch {
    // Extension context may be invalid — silently ignore
  }
})();
