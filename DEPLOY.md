# Deploy Dugg to a Server

Take Dugg from local-only to a live URL that anyone can reach. This guide assumes a fresh Ubuntu 22.04+ VPS (DigitalOcean, Linode, Hetzner, etc.) with root or sudo access.

## What you'll end up with

- Dugg running as a system service (auto-restarts on crash or reboot)
- HTTPS with auto-renewing SSL certificate
- Your own URL like `https://dugg.yoursite.com`
- Invite links that work in a browser

Total time: ~10 minutes. Total cost: $4-6/month for a basic VPS.

---

## 1. Server basics

SSH into your server:

```bash
ssh youruser@your-server-ip
```

Make sure the system is up to date:

```bash
sudo apt update && sudo apt upgrade -y
```

Install the essentials:

```bash
sudo apt install -y python3 python3-venv git curl nginx certbot python3-certbot-nginx
```

### Firewall

If you're using `ufw` (Ubuntu's default firewall), make sure HTTP and HTTPS are open:

```bash
sudo ufw allow 'Nginx Full'
sudo ufw status
```

You should see ports 22 (SSH), 80 (HTTP), and 443 (HTTPS) open. You do **not** need to open Dugg's internal port (8411) — Nginx handles all public traffic.

---

## 2. Install Dugg

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env

# Clone and install
sudo mkdir -p /var/www/dugg-fyi
sudo chown $USER:$USER /var/www/dugg-fyi
git clone https://github.com/kadedworkin/dugg-fyi.git /var/www/dugg-fyi
cd /var/www/dugg-fyi
uv sync

# Make dugg available system-wide
sudo ln -sf /var/www/dugg-fyi/.venv/bin/dugg /usr/local/bin/dugg
```

Now `dugg` works from anywhere on the server — no need to activate a venv or type full paths.

---

## 3. Initialize with your URL

Pick your domain (e.g., `dugg.yoursite.com`) and initialize:

```bash
.venv/bin/dugg init --server https://dugg.yoursite.com
```

This creates the database and stores your server URL so invite links work automatically.

If you already ran `dugg init`, set the URL after the fact:

```bash
.venv/bin/dugg set-url https://dugg.yoursite.com
```

---

## 4. Create your account

```bash
.venv/bin/dugg add-user "YourName"
```

**Save the API key it prints.** This is your admin key for the instance.

---

## 5. Set up the system service

Create `/etc/systemd/system/dugg.service`:

```bash
sudo tee /etc/systemd/system/dugg.service <<'EOF'
[Unit]
Description=Dugg MCP Server
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/var/www/dugg-fyi
ExecStart=/var/www/dugg-fyi/.venv/bin/dugg serve --transport http --host 127.0.0.1 --port 8411
Restart=always
RestartSec=5
Environment=DUGG_DB_PATH=/home/YOUR_USERNAME/.dugg/dugg.db

[Install]
WantedBy=multi-user.target
EOF
```

Replace `YOUR_USERNAME` with your actual system username.

Start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable dugg
sudo systemctl start dugg
```

Verify it's running:

```bash
sudo systemctl status dugg
curl http://127.0.0.1:8411/health
```

You should see `{"status":"ok","db":"connected","transport":"http+sse"}`.

---

## 6. DNS

Go to your domain registrar's DNS panel and add a record pointing your subdomain to your server:

**Option A: A Record** (simplest)
- Type: `A`
- Host: `dugg` (or whatever subdomain you chose)
- Value: `your-server-ip`
- TTL: Automatic

**Option B: CNAME** (better if you have multiple subdomains on the same IP)
- Type: `CNAME`
- Host: `dugg`
- Value: `yoursite.com.`
- TTL: Automatic

Verify it's propagated:

```bash
dig +short dugg.yoursite.com
```

Should return your server IP. This usually takes 1-5 minutes.

---

## 7. Nginx reverse proxy

Create the Nginx config:

```bash
sudo tee /etc/nginx/sites-available/dugg <<'NGINX'
server {
    listen 80;
    server_name dugg.yoursite.com;

    location / {
        proxy_pass http://127.0.0.1:8411;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support — required for agent connections
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }
}
NGINX
```

Enable it:

```bash
sudo ln -sf /etc/nginx/sites-available/dugg /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Test that HTTP works:

```bash
curl http://dugg.yoursite.com/health
```

---

## 8. SSL with Let's Encrypt

This is the part that gives you HTTPS and the padlock icon:

```bash
sudo certbot --nginx -d dugg.yoursite.com
```

Certbot will:
1. Verify you own the domain (via the HTTP server you just set up)
2. Get a free SSL certificate from Let's Encrypt
3. Automatically update your Nginx config to use HTTPS
4. Set up auto-renewal (certificates renew every 90 days, automatically)

Verify HTTPS works:

```bash
curl https://dugg.yoursite.com/health
```

That's it. Your Dugg instance is live.

---

## 9. Invite people

```bash
cd /var/www/dugg-fyi
.venv/bin/dugg invite-user "TheirName" --key YOUR_API_KEY
```

This prints a message you can send them. It includes:
- A browser link they can click to join
- A CLI command if they prefer terminal

The recipient clicks the link, enters their name, and gets their API key.

---

## 10. Email forwarding (optional)

Let users forward emails into your Dugg instance using self-describing addresses like `your-server.com+dugg_apikey@dugg.fyi`.

This requires a Cloudflare account with `dugg.fyi` (or your own domain) configured for Email Routing. The worker lives in `email-worker/` in this repo.

```bash
cd email-worker
npm install
wrangler login
wrangler deploy
```

Then in the Cloudflare dashboard:
1. **Email Routing** → enable for your domain
2. Set a **catch-all** rule → route to `dugg-email-worker`

No configuration on the Dugg server side — the worker parses the address and POSTs directly to `/tools/dugg_paste`.

---

## Updating Dugg

When there's a new version:

```bash
cd /var/www/dugg-fyi
git pull origin main
uv sync
sudo systemctl restart dugg
```

---

## Troubleshooting

**Dugg won't start:**
```bash
sudo journalctl -u dugg -n 50
```

**Nginx test fails:**
```bash
sudo nginx -t
```

**SSL certificate won't issue:**
- Make sure DNS is pointing to your server: `dig +short dugg.yoursite.com`
- Make sure port 80 is open: `sudo ufw status`
- Make sure Nginx is running: `sudo systemctl status nginx`

**"Bad Gateway" in browser:**
- Dugg isn't running: `sudo systemctl status dugg`
- Wrong port in Nginx config: should be `proxy_pass http://127.0.0.1:8411`

**Health check works but agents can't connect:**
- SSE needs the proxy headers in the Nginx config (no buffering, HTTP 1.1)
- Check that `proxy_read_timeout` is set high (86400s) — SSE connections are long-lived
