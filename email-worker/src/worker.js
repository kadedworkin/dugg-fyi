export default {
  async email(message, env, ctx) {
    const to = message.to;
    const [localPart, domain] = to.split("@");
    if (!domain || !domain.endsWith(".dugg.fyi")) return;

    // Subdomain pattern: {key}@{host-with-dots-as-double-dashes}.dugg.fyi
    // e.g. dugg_abc@chino-bandido--kadedworkin--com.dugg.fyi
    //   -> key=dugg_abc, host=chino-bandido.kadedworkin.com
    const hostSlug = domain.slice(0, -".dugg.fyi".length);
    const host = hostSlug.replace(/--/g, ".");
    const userKey = localPart;
    if (!host || !userKey) return;

    const subject = message.headers.get("subject") || "Forwarded email";
    const rawDate = message.headers.get("date") || "";

    // Read raw MIME and extract text body — zero dependencies
    const raw = await new Response(message.raw).text();
    const body = extractText(raw);

    let publishedAt = "";
    if (rawDate) {
      const d = new Date(rawDate);
      if (!isNaN(d.getTime())) publishedAt = d.toISOString();
    }

    await fetch(`https://${host}/tools/dugg_paste`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dugg-Key": userKey },
      body: JSON.stringify({
        title: subject,
        body: body,
        source_type: "email",
        source_label: "email",
        published_at: publishedAt,
      }),
    });
  },
};

function extractText(raw) {
  const headerEnd = raw.indexOf("\r\n\r\n");
  if (headerEnd === -1) return raw;

  const headers = raw.slice(0, headerEnd);
  const ctMatch = headers.match(/^content-type:\s*(.+)/im);
  const ct = ctMatch ? ctMatch[1] : "";

  // Non-multipart: return decoded body directly
  if (!ct.includes("multipart")) {
    return decodeBody(raw.slice(headerEnd + 4), headers);
  }

  // Multipart: find text/plain, fall back to text/html
  const bMatch = ct.match(/boundary="?([^";\s]+)"?/);
  if (!bMatch) return raw.slice(headerEnd + 4);

  const parts = raw.split("--" + bMatch[1]);

  for (const pref of ["text/plain", "text/html"]) {
    for (const part of parts) {
      const re = new RegExp("content-type:\\s*" + pref.replace("/", "\\/"), "i");
      if (!re.test(part)) continue;
      const pEnd = part.indexOf("\r\n\r\n");
      if (pEnd === -1) continue;
      let text = decodeBody(part.slice(pEnd + 4), part.slice(0, pEnd));
      if (pref === "text/html") {
        text = text.replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();
      }
      if (text.trim()) return text.trim();
    }
  }

  return raw.slice(headerEnd + 4);
}

function decodeBody(body, headers) {
  if (/content-transfer-encoding:\s*base64/i.test(headers)) {
    try { return atob(body.replace(/\s/g, "")); } catch (e) { return body; }
  }
  if (/content-transfer-encoding:\s*quoted-printable/i.test(headers)) {
    return body
      .replace(/=\r?\n/g, "")
      .replace(/=([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
  }
  return body;
}
