# Kormic Backend - Production Deployment Guide (AWS EC2)

This runbook provides a complete, step-by-step guide to deploying the Kormic Django Backend from scratch on a fresh AWS EC2 instance. It assumes you are deploying the backend using Docker Compose.

## Canonical Runtime Port

The backend application port is **8000** in every environment. Docker runs Gunicorn on `0.0.0.0:8000`, publishes host port `8000`, and checks `/api/health/` on port `8000`. In production, Nginx proxies to `http://127.0.0.1:8000`. Local browser frontends should use `http://127.0.0.1:8000` as their backend base URL. Port `8030` is not part of the supported runtime configuration.

---

## 1. Provisioning the EC2 Instance

1. **Launch an EC2 Instance:**
   - **OS:** Ubuntu 22.04 or 24.04 LTS
   - **Instance Type:** `t3.medium` or larger (recommended due to Celery workers, Redis, and Postgres running concurrently).
   - **Storage:** At least 20-30 GB of EBS storage (gp3).
2. **Configure Security Group:**
   - **SSH (Port 22):** Restrict to your IP address.
   - **HTTP (Port 80):** Anywhere (0.0.0.0/0) - Required for SSL certificate generation and redirecting to HTTPS.
   - **HTTPS (Port 443):** Anywhere (0.0.0.0/0) - Required for secure API access.

---

## 2. Server Setup & Installing Docker

SSH into your new EC2 instance:
```bash
ssh -i /path/to/your-key.pem ubuntu@<your-ec2-ip>
```

Update the package manager and install necessary utilities:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git ufw nginx
```

Install Docker and Docker Compose:
```bash
# Download the official Docker install script
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add the ubuntu user to the docker group so you can run docker without sudo
sudo usermod -aG docker ubuntu

# Apply group changes immediately
newgrp docker
```
Verify the installation:
```bash
docker compose version
```

---

## 3. Cloning the Repository

Generate an SSH key on your server if you need to access a private GitHub repository:
```bash
ssh-keygen -t ed25519 -C "server@kormic.ai"
cat ~/.ssh/id_ed25519.pub
```
*(Add this key to your GitHub account as a Deploy Key)*

Clone your backend repository:
```bash
git clone git@github.com:YourOrg/kormic-Django-Backend-Prajval-1.git backend
cd backend
```

---

## 4. Environment Variables Configuration

The backend requires strict environment variables to run securely in production. Create the `.env` file in the root of the cloned repository:

```bash
nano .env
```

Paste and modify the following template:

```ini
# ==========================================
# 1. Django Security Settings
# ==========================================
# MUST be false in production to prevent leaking stack traces.
DJANGO_DEBUG=false

# Generate a strong, random 50+ character string. Do not use the local key.
# You can generate one via: python3 -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
DJANGO_SECRET_KEY=your_secure_random_long_string_here

# The domain(s) pointing to your backend API. E.g., api.kormic.ai
DJANGO_ALLOWED_HOSTS=api.kormic.ai,localhost,127.0.0.1

# The frontend URLs that are allowed to make cross-origin requests.
DJANGO_CORS_ALLOWED_ORIGINS=https://student.kormic.ai,https://admin.kormic.ai,https://university.kormic.ai

# ==========================================
# 2. Database & Redis (Docker internal)
# ==========================================
POSTGRES_DB=kormic_prod
POSTGRES_USER=kormic_admin
# REQUIRED. Use a strong unique production password; Compose intentionally
# refuses to render/start when POSTGRES_PASSWORD is missing or empty.
POSTGRES_PASSWORD=your_secure_db_password
# Docker Compose connects Django/Celery/migrations to the `postgres` service.
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
DB_CONN_MAX_AGE=60

# Redis connection URL for Celery and Caching
REDIS_URL=redis://redis:6379/0

# ==========================================
# 3. Third-Party API Keys
# ==========================================
# Required for LangGraph and University Agents
ANTHROPIC_API_KEY=sk-ant-api03-...

# ==========================================
# 4. Email Configuration (SMTP)
# ==========================================
# Required for escalation routing and notifications
EMAIL_HOST=smtp.your-email-provider.com
EMAIL_PORT=587
EMAIL_HOST_USER=no-reply@kormic.ai
EMAIL_HOST_PASSWORD=your_smtp_app_password
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

The same `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` values are injected into the PostgreSQL container, the one-shot migration service, the web process, and both Celery services. Do not place alternate database credentials directly in `docker-compose.yml`.

---

## 5. Build and Start the Application

With the `.env` file in place, you can bring up the stack. This will build the Python image and download the Postgres/Redis images.

```bash
# Validate interpolation first. This will fail immediately if the required
# POSTGRES_PASSWORD is missing.
docker compose config >/dev/null

# Build the Docker images
docker compose build

# Start the application in detached mode
docker compose up -d
```

Verify that all containers (web, celery_worker, celery_beat, postgres, redis) are running:
```bash
docker compose ps
```

Verify the host-published backend directly before configuring Nginx:
```bash
curl http://127.0.0.1:8000/api/health/
```

---

## 6. Run Migrations & Collect Static Files

The database schema must be initialized, and static files (CSS/JS for the Django Admin) must be collected so Whitenoise can serve them.

```bash
# Run database migrations
docker compose exec web python manage.py migrate

# Collect static files
docker compose exec web python manage.py collectstatic --noinput

# Create the first Kormic application operator
docker compose exec web python manage.py create_superuser_account --email admin@example.com
```

### First Kormic operator

Replace `admin@example.com` with the operator's email address and enter the password at the interactive prompts. The project command creates an `Account` with `Account.Role.SUPERUSER`, which is required by `/api/superuser/` and the Kormic Superuser portal.

If running directly in the configured backend Python environment instead of Docker, the equivalent command is:

```sh
python manage.py create_superuser_account --email admin@example.com
```

After HTTPS and the Kormic Superuser frontend are configured, log in through Kormic with that email and password (`/api/auth/login/`), then enroll and confirm TOTP using an authenticator app. Save the returned backup codes securely. Superuser API access remains blocked until TOTP enrollment is confirmed.

The command creates a new account; it does not upgrade an existing user. If the email already exists (including from an earlier `createsuperuser` attempt), use a distinct operator email for this bootstrap command.

### Optional: separate Django Admin access

Django Admin access is managed separately through Django's `is_staff` / `is_superuser` flags. The Kormic bootstrap command does not grant these flags. If Django Admin access is also needed, create a separate Django administrator:

```sh
docker compose exec web python manage.py createsuperuser
```

Use a different username/email from the Kormic operator. Django's `createsuperuser` command does **not** create an `Account.Role.SUPERUSER` account and does not by itself grant access to the Kormic Superuser API or portal.

At this point, the backend is available on the server host at `http://127.0.0.1:8000`.

---

## 7. Nginx Reverse Proxy and SSL (HTTPS)

You must never expose Gunicorn directly to the public internet. Nginx routes public HTTP/HTTPS traffic to the backend published on host port 8000.

Create an Nginx configuration file for the API:
```bash
sudo nano /etc/nginx/sites-available/kormic_api
```

Add the following config:
```nginx
server {
    listen 80;
    server_name api.kormic.ai; # Change this to your actual domain

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable the configuration and restart Nginx:
```bash
sudo ln -s /etc/nginx/sites-available/kormic_api /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### Enable HTTPS with Certbot
Install Certbot and request a free Let's Encrypt SSL certificate:
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.kormic.ai
```
Follow the prompts and choose to **Redirect** all HTTP traffic to HTTPS.

---

## 8. Verification & Health Check

Test that your API is accessible and healthy from the outside world:
```bash
curl -i https://api.kormic.ai/api/health/
```
You should see:
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok"}
```

---

### Verify Kormic operator access

1. Sign in to the Kormic Superuser portal with the operator account created in section 6.
2. Complete TOTP enrollment and confirmation if this is the first login, and save the backup codes.
3. Open the university list and verify that `GET /api/superuser/universities/` succeeds (HTTP 200, even if the list is empty).
4. Sign out and sign back in, completing the TOTP challenge, to verify subsequent logins.

A successful Django Admin login does not verify Kormic operator access. If the Superuser API denies access, check that this login has `Account.Role.SUPERUSER` and a confirmed TOTP device.

---

## 9. Day-to-Day Maintenance

### Viewing Logs
To see real-time logs for the web server or background workers:
```bash
# Web server logs
docker compose logs -f web

# Celery worker logs
docker compose logs -f celery_worker
```

### Updating to the Latest Code
When new code is merged to the main branch, deploy it using:
```bash
cd ~/backend
git pull origin main
docker compose build
docker compose up -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
```

### Accessing the Database Shell
If you need to query the Postgres database directly, use the credentials already injected into that container:
```bash
docker compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

### Accessing the Django Shell
```bash
docker compose exec web python manage.py shell
```
