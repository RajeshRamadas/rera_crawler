# Production Deployment Guide for RERA Crawler & FastAPI

This guide details how to deploy and host the RERA Crawler & Web Portal using either **Docker Containers** (recommended for Render/Railway/DigitalOcean App Platform) or a **Direct Linux Virtual Server / AWS EC2**.

---

## Prerequisites

* Python 3.10+
* PostgreSQL (Local or Managed)
* Tesseract OCR with Kannada language pack (`tesseract-ocr-kan`) for processing document verification.

---

## Method 1: Docker Deployment (Recommended)

Docker packages all system OCR dependencies, Python packages, and database configurations into consistent containers.

### 1. Build and Run via Docker Compose

Run the API server and PostgreSQL database in the background:

```bash
docker compose up -d --build
```

This starts:
* **`rera_db`**: PostgreSQL container running on port `5432`.
* **`rera_api`**: FastAPI Web & API server running on port `8000`.

Check running services:
```bash
docker compose ps
```

View logs:
```bash
docker compose logs -f api
```

### 2. Run the Crawler Worker (On-Demand)

To trigger the bulk continuous crawler inside a container:

```bash
docker compose run --rm crawler
```

---

## Method 2: AWS EC2 / Ubuntu VPS Deployment

If you are hosting directly on an Ubuntu 22.04 / 24.04 EC2 instance or Linux VPS:

### 1. Run Automated Provisioning Script

Clone the repository to `/home/ubuntu/rera_crawler` and execute:

```bash
chmod +x setup_local_postgres_ec2.sh
./setup_local_postgres_ec2.sh
```

This script automatically:
1. Installs PostgreSQL, `tesseract-ocr`, `tesseract-ocr-kan`, `poppler-utils`, Nginx, and Certbot.
2. Creates the `rera_db` PostgreSQL database and user `rera_user`.
3. Creates a Python virtual environment and installs `requirements.txt`.
4. Installs and starts `rera-api.service` via `systemd`.
5. Configures Nginx reverse proxy on port 80.

### 2. Configure Domain & SSL Certificate (HTTPS)

Once Nginx is active and your domain points to your EC2 Public IP address, enable SSL with Let's Encrypt:

```bash
sudo certbot --nginx -d rera.yourdomain.com
```

### 3. Managing Services with `systemctl`

* **API Server Status**:
  ```bash
  sudo systemctl status rera-api.service
  ```
* **Restart API Server**:
  ```bash
  sudo systemctl restart rera-api.service
  ```
* **Run Crawler Service**:
  ```bash
  sudo systemctl start rera-crawler.service
  ```
* **View Real-Time Logs**:
  ```bash
  journalctl -u rera-api.service -f
  ```

---

## Method 3: Containerized PaaS (Render / Railway / Fly.io)

To host on managed cloud platforms like Render or Railway:

1. **New Web Service**: Choose **Docker** as the environment.
2. **Repository**: Connect your GitHub repository (`rera_crawler`).
3. **Environment Variables**:
   * Set `DATABASE_URL`: `postgresql://<user>:<password>@<host>:5432/<dbname>`
   * Set `JWT_SECRET_KEY` (required so logins survive restarts): `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Docker: put it in `.env`. systemd: add `JWT_SECRET_KEY=...` to `/home/ubuntu/rera_crawler/.env`.
4. The platform will automatically build using the included [`Dockerfile`](file:///home/rajesh/Documents/GitHub/rera_crawler/Dockerfile) and start the FastAPI server on port 8000.
