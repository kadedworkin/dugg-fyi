"""Helpers for Dugg's email forwarding bridge."""

import json
import urllib.error
import urllib.request

from dugg.db import dugg_email_address


def _excerpt(text: str, limit: int = 200) -> str:
    return (text or "").replace("\r", " ").replace("\n", " ")[:limit]


def probe_forwarding(api_key: str, server_url: str, timeout: int = 10) -> dict:
    address = dugg_email_address(api_key, server_url)
    if not server_url:
        return {
            "verdict": "UNREACHABLE",
            "status": None,
            "address": address,
            "server_url": "",
            "excerpt": "No server URL configured.",
        }

    target_url = f"{server_url.rstrip('/')}/tools/dugg_paste"
    payload = {
        "title": "Email forwarding test",
        "body": "If you see this in your feed, email forwarding will work for this address.",
        "source_type": "email",
        "source_label": "email_test",
    }
    req = urllib.request.Request(
        target_url,
        data=json.dumps(payload).encode(),
        headers={"X-Dugg-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "verdict": "PASS",
                "status": getattr(resp, "status", resp.getcode()),
                "address": address,
                "server_url": server_url,
                "excerpt": _excerpt(body),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        verdict = "AUTH-FAIL" if e.code in (401, 403) else "SERVER-ERROR" if 500 <= e.code < 600 else "HTTP-ERROR"
        return {
            "verdict": verdict,
            "status": e.code,
            "address": address,
            "server_url": server_url,
            "excerpt": _excerpt(body),
        }
    except urllib.error.URLError as e:
        return {
            "verdict": "UNREACHABLE",
            "status": None,
            "address": address,
            "server_url": server_url,
            "excerpt": _excerpt(str(e.reason)),
        }


def format_probe_result(result: dict) -> str:
    lines = [
        f"Email bridge address: {result.get('address', '')}",
        f"Target: {result.get('server_url', '')}",
    ]
    status = result.get("status")
    lines.append(f"HTTP status: {status if status is not None else 'n/a'}")
    excerpt = result.get("excerpt", "")
    lines.append(f"Response: {excerpt if excerpt else '(empty)'}")
    verdict = result.get("verdict", "UNKNOWN")
    if verdict == "PASS":
        lines.append("Verdict: PASS — forwarding will work.")
        lines.append("This creates a real test resource in your feed.")
    elif verdict == "AUTH-FAIL":
        lines.append("Verdict: AUTH-FAIL — key is wrong or the user was deleted.")
        lines.append("Verify this forwarding address matches the one shown on your Dugg home page.")
    elif verdict == "SERVER-ERROR":
        lines.append("Verdict: SERVER-ERROR — the server rejected the probe.")
    elif verdict == "UNREACHABLE":
        lines.append("Verdict: UNREACHABLE — network or DNS failure.")
    else:
        lines.append(f"Verdict: {verdict} — unexpected non-2xx response.")
    return "\n".join(lines)
