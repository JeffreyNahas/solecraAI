# RUNBOOK — Solecra agents

How to set up, run, test, deploy and operate the bot. Keep this updated as features land.

---

## 1. One-time: Meta (WhatsApp) setup for development

You need a Facebook account. Business verification is **not** needed for the test number.

1. Go to <https://developers.facebook.com/> → **My Apps** → **Create app**.
   Use case: **Other** → type **Business**. Name it e.g. `Solecra Bot (dev)`.
2. In the app dashboard, add the **WhatsApp** product → **API Setup**.
   Meta gives you a free **test phone number**.
3. On **API Setup**, note:
   - **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID`
   - **Temporary access token** → `WHATSAPP_ACCESS_TOKEN` (expires after ~24 h; see step 7 below)
4. Under **To**, add each owner's phone (and yours) as a **recipient** (max 5). Each person
   confirms with a code sent to their WhatsApp.
5. **App settings → Basic → App secret** → `WHATSAPP_APP_SECRET`.
6. Invent a random string for `WHATSAPP_VERIFY_TOKEN` (e.g. `openssl rand -hex 16`).
7. Longer-lived token (do this once Gate 1 works): Business settings → **System users** → add an
   admin system user → **Generate token** for this app with `whatsapp_business_messaging` and
   `whatsapp_business_management` permissions → replace `WHATSAPP_ACCESS_TOKEN`.

The webhook URL is configured in section 3 once the server is reachable.

**24-hour rule:** the bot can only send free-form messages to someone who messaged the test number
in the last 24 h. Before testing, each owner sends any message (e.g. `help`) to the test number.
If a send is silently not delivered, check the logs for `whatsapp delivery failed ... 131047`.

---

## 2. Run locally

Requirements: Docker, and `uv` (<https://docs.astral.sh/uv/>) for tests and scripts.

```bash
cp .env.example .env        # then fill in the values from section 1
# OWNER_PHONES: country code + number, digits only, e.g. 15145551234:Jeff,15145556789:Owner2
docker compose up --build   # api on http://localhost:8000, postgres on localhost:5432
curl localhost:8000/health  # -> {"status":"ok"}
```

Without Meta at all, set `WHATSAPP_DRY_RUN=true` (outgoing messages only appear in the logs) and
simulate owner messages with signed fake webhooks:

```bash
uv run python -m scripts.fake_webhook text 15145551234 "test draft"
uv run python -m scripts.fake_webhook button 15145551234 approve:1:1
docker compose logs -f api
```

## 3. Connect Meta to your local machine (tunnel)

```bash
cloudflared tunnel --url http://localhost:8000
# prints https://<random>.trycloudflare.com
```

In the Meta app → **WhatsApp → Configuration → Webhook → Edit**:
- Callback URL: `https://<random>.trycloudflare.com/webhook`
- Verify token: your `WHATSAPP_VERIFY_TOKEN`
- Click **Verify and save** (step 2 done when Meta accepts it), then under **Webhook fields**
  subscribe to **messages**.

The tunnel URL changes each time you restart cloudflared; update it in Meta when it does.

## 4. Gate 1 checklist

From an owner's WhatsApp, to the test number:

| # | Do | Expect |
|---|----|--------|
| 3 | send `hello` | logs show `text from <name>: hello` **once** |
| 4 | (same) | bot replies `Echo: hello` |
| 5 | send `test draft` | both owners get a French draft with **Approve / Edit / Reject** |
| 6 | `docker compose restart`, wait for `/health`, then tap **Approve** | both owners get `✅ Approved by <name>`; the other owner's later tap gets `Already handled by <name>` |

Also try: **Edit** → send replacement text → both owners get the final text. **Reject**.
A message from a number not in `OWNER_PHONES` gets no reply.

Handy checks:

```bash
docker compose exec postgres psql -U solecra -c "select id,status,version,decided_by from approvals"
docker compose exec postgres psql -U solecra -c "select created_at,actor,action,entity_id from audit_log order by id desc limit 20"
```

## 5. Tests

```bash
docker compose up -d postgres
docker compose exec postgres createdb -U solecra solecra_test
TEST_DATABASE_URL=postgresql://solecra:<POSTGRES_PASSWORD>@localhost:5432/solecra_test uv run pytest
uv run ruff check . && uv run ruff format --check .
```

---

## 6. Deploy to the VPS (step 7)

Target: OVHcloud VPS, Beauharnois (BHS), Ubuntu 26.04, on Solecra's account.

### 6.1 Server basics (as the `ubuntu` admin user, over SSH)

Server: OVHcloud VPS `vps-79cc5313.vps.ovh.ca` (`15.235.92.201`), Beauharnois, Ubuntu 26.04.
OVH creates the `ubuntu` user with `sudo`; we use it as the admin account (no separate user).
First add your SSH key from your Mac: `ssh-copy-id ubuntu@<SERVER_IP>` (uses the OVH password once).

**1. Update, then reboot if needed**
```bash
sudo apt update && sudo apt full-upgrade -y
[ -f /var/run/reboot-required ] && sudo reboot
```

**2. SSH: keys only, no root.** Cloud images ship `/etc/ssh/sshd_config.d/50-cloud-init.conf`
with `PasswordAuthentication yes`, and sshd uses the *first* value it reads, so editing
`sshd_config` is not enough. A `00-` drop-in is read first and wins:
```bash
sudo tee /etc/ssh/sshd_config.d/00-hardening.conf > /dev/null <<'CONF'
# Solecra: key-only SSH (see RUNBOOK §6.1)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
CONF
sudo sshd -t && sudo systemctl restart ssh
sudo sshd -T | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin)'   # all "no"
```
Keep the current session open; from a **new** terminal check that `ssh ubuntu@<SERVER_IP>` works and
`ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password ubuntu@<SERVER_IP>` is refused.

**3. Firewall** (allow SSH *before* enabling):
```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw enable && sudo ufw status verbose
```

**4. Automatic security updates** (installed by default; reboot at 04:00 UTC only when required):
```bash
cat /etc/apt/apt.conf.d/20auto-upgrades     # both lines "1"; else: sudo dpkg-reconfigure -plow unattended-upgrades
sudo tee /etc/apt/apt.conf.d/52solecra-auto-reboot > /dev/null <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
CONF
```

**5. Docker** (official repository, with log size limits):
```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo tee /etc/docker/daemon.json > /dev/null <<'CONF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
CONF
sudo systemctl restart docker
sudo usermod -aG docker ubuntu     # log out and back in
docker run --rm hello-world && docker compose version && systemctl is-enabled docker
```

Docker publishes ports past ufw, which is why compose binds Postgres and the API to `127.0.0.1` only.
Only Caddy (80/443) is public.

### 6.2 DNS

At your domain registrar, add an **A record**: `bot.<domain>` → the VPS IP. Wait until
`dig +short bot.<domain>` returns the IP.

### 6.3 App

```bash
sudo mkdir -p /opt/solecra && sudo chown ubuntu: /opt/solecra
git clone https://github.com/jeffreynahas/solecraai.git /opt/solecra   # private repo: use a deploy key or token
cd /opt/solecra
cp .env.example .env && nano .env     # strong POSTGRES_PASSWORD, BOT_DOMAIN=bot.<domain>, WHATSAPP_DRY_RUN=false
docker compose --profile prod up -d --build
curl https://bot.<domain>/health      # Caddy gets the HTTPS certificate automatically
```

Then in Meta, change the webhook Callback URL to `https://bot.<domain>/webhook` and verify again.

### 6.4 Backups

```bash
crontab -e
# add:
15 3 * * * cd /opt/solecra && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Copy backups off the server (a server dump alone isn't a backup). Simplest: OVHcloud Object
Storage (S3-compatible) + `rclone`:

```bash
sudo apt install -y rclone && rclone config       # create remote "objstore" (S3, OVHcloud endpoint)
# add to crontab after the backup line:
45 3 * * * rclone copy /opt/solecra/backups objstore:solecra-backups --include '*.sql.gz'
```

Restore (test this once):

```bash
gunzip -c backups/solecra_YYYYMMDD_HHMMSS.sql.gz | docker compose exec -T postgres psql -U solecra solecra
```

### 6.5 Uptime monitor

Free monitor (UptimeRobot or Better Stack) on `https://bot.<domain>/health`, every 5 min,
alerting Jeff by email/SMS.

### 6.6 Done when

Run the Gate 1 checklist (section 4) against the server, using
`docker compose --profile prod restart api postgres` for the restart step.

---

## 7. Day-to-day operations

| Task | Command (in `/opt/solecra`) |
|---|---|
| Logs | `docker compose logs -f --tail 200 api` |
| Restart | `docker compose --profile prod restart api` |
| Deploy update | `git pull && docker compose --profile prod up -d --build` |
| Stuck messages | `select message_id,status,attempts,last_error from inbound_messages where status<>'done'` |
| Change owners | edit `OWNER_PHONES` in `.env`, then `docker compose --profile prod up -d api` |

**Troubleshooting**
- `webhook rejected: bad signature` → `WHATSAPP_APP_SECRET` doesn't match the Meta app.
- Messages not delivered, log shows `131047` → 24 h window closed; the owner must message the bot first.
- `HTTP 401` from Graph API → access token expired (temporary tokens last ~24 h); see section 1 step 7.
- Meta says webhook verification failed → `WHATSAPP_VERIFY_TOKEN` mismatch or URL not reachable.

---

## 8. Job failure alerts and `[agent]` GitHub issues

Every job run is recorded in the `runs` table. On failure, owners get one WhatsApp alert per failure
streak; after `RUN_FAILURE_ISSUE_THRESHOLD` failures in a row, one GitHub issue `[agent] <job> failing`
is opened (and commented on after that). Details: `docs/workflows.md`.

**Turn on GitHub issues** (once, on the server):
1. GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens
   → Generate new token**. Name `solecra-agent-issues`, expiry 1 year, **Only select repositories:
   `solecraAI`**, Repository permissions → **Issues: Read and write** (nothing else).
2. Put it in the server's `.env` as `GITHUB_ISSUES_TOKEN=...`, then `docker compose --profile prod up -d api`.
3. Set a calendar reminder to renew it before it expires.

**Test it:**
```bash
docker compose exec api python -m scripts.run_job selftest_fail   # repeat 3 times
docker compose exec postgres psql -U solecra -c "select id, job, status, error from runs order by id desc limit 5"
```
Owners get one alert; after the 3rd run an `[agent] selftest_fail failing` issue appears. Close it after.
Then `selftest_ok` is the matching success job.
