#!/usr/bin/env bash
set -e

echo "=== Setting up local PostgreSQL, Tesseract OCR, & Nginx on EC2 / Linux VPS ==="

# 1. Install PostgreSQL, Tesseract OCR with Kannada support, Nginx & Python dependencies
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib python3-pip python3-venv \
    tesseract-ocr tesseract-ocr-kan poppler-utils nginx certbot python3-certbot-nginx

# 2. Enable & start PostgreSQL service
sudo systemctl enable postgresql
sudo systemctl start postgresql

# 3. Create database and user
sudo -u postgres psql -c "CREATE DATABASE rera_db;" || echo "Database rera_db already exists."
sudo -u postgres psql -c "CREATE USER rera_user WITH PASSWORD 'rera_password';" || echo "User rera_user already exists."
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE rera_db TO rera_user;"
sudo -u postgres psql -d rera_db -c "GRANT ALL ON SCHEMA public TO rera_user;"

# 4. Create virtual environment & install dependencies
python3 -m venv /home/ubuntu/rera_crawler/venv
/home/ubuntu/rera_crawler/venv/bin/pip install --upgrade pip
/home/ubuntu/rera_crawler/venv/bin/pip install -r requirements.txt

# 5. Setup passwordless shutdown permission for ubuntu user (for auto-shutdown crawler)
echo "ubuntu ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/poweroff" | sudo tee /etc/sudoers.d/crawler-shutdown

# 6. Install systemd services
sudo cp rera-crawler.service /etc/systemd/system/rera-crawler.service
sudo cp rera-api.service /etc/systemd/system/rera-api.service
sudo systemctl daemon-reload
sudo systemctl enable rera-crawler.service
sudo systemctl enable rera-api.service
sudo systemctl start rera-api.service

# 7. Configure Nginx Reverse Proxy
if [ -f nginx.conf ]; then
    sudo cp nginx.conf /etc/nginx/sites-available/rera
    sudo ln -sf /etc/nginx/sites-available/rera /etc/nginx/sites-enabled/rera
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t && sudo systemctl reload nginx
    echo "Nginx reverse proxy configured and reloaded."
fi

echo "=== Setup Complete! ==="
echo "PostgreSQL is running locally and database data will persist on your EBS volume."
echo "API Server is running on port 8000 & proxied via Nginx on port 80 (Docs: http://<EC2_IP>/docs)"
