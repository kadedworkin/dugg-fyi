export default {
  async email(message, env, ctx) {
    const to = message.to;
    const [localPart, domain] = to.split("@");
    if (!domain || !domain.endsWith(".dugg.fyi")) return;

    const hostSlug = domain.slice(0, -".dugg.fyi".length);
    const host = hostSlug.replace(/--/g, ".");
    const userKey = localPart;
    if (!host || !userKey) return;

    const rawBody = await new Response(message.raw).text();
    const subject = message.headers.get("subject") || "Forwarded email";

    await fetch(`https://${host}/tools/dugg_paste`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Dugg-Key": userKey,
      },
      body: JSON.stringify({
        title: subject,
        body: rawBody,
        source_type: "email",
        source_label: `from ${message.from}`,
      }),
    });
  },
};
