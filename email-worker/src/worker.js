export default {
  async email(message, env, ctx) {
    const to = message.to;
    const localPart = to.split("@")[0];
    const plusIndex = localPart.indexOf("+");
    if (plusIndex === -1) return;

    const host = localPart.substring(0, plusIndex);
    const userKey = localPart.substring(plusIndex + 1);
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
