import PostalMime from "postal-mime";

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

    const rawBody = await new Response(message.raw).arrayBuffer();
    const parser = new PostalMime();
    const parsed = await parser.parse(rawBody);

    const subject = parsed.subject || message.headers.get("subject") || "Forwarded email";
    const body = parsed.text || (parsed.html || "").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim() || "";

    let publishedAt = "";
    const rawDate = parsed.date || message.headers.get("date") || "";
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
