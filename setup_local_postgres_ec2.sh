#!/usr/bin/env bash
set -e

echo "=== Setting up local PostgreSQL on EC2 (EBS Volume Storage) ==="

# 1. Install PostgreSQL & dependencies
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib python3-pip

# 2. Enable & start PostgreSQL service
sudo systemctl enable postgresql
sudo systemctl start postgresql

# 3. Create database and user
sudo -u postgres psql -c "CREATE DATABASE rera_db;" || echo "Database rera_db already exists."
sudo -u postgres psql -c "CREATE USER rera_user WITH PASSWORD 'rera_password';" || echo "User rera_user already exists."
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE rera_db TO rera_user;"
sudo -u postgres psql -d rera_db -c "GRANT ALL ON SCHEMA public TO rera_user;"

# 4. Install crawler dependencies
pip install -r requirements.txt

# 5. Setup passwordless shutdown permission for ubuntu user
echo "ubuntu ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/poweroff" | sudo tee /etc/sudoers.d/crawler-shutdown

# 6. Install systemd service
sudo cp rera-crawler.service /etc/systemd/system/rera-crawler.service
sudo systemctl daemon-reload
sudo systemctl enable rera-crawler.service

echo "=== Setup Complete! ==="
echo "PostgreSQL is running locally and database data will persist on your EBS volume."
echo "You can test the crawler with: DATABASE_URL='postgresql://rera_user:rera_password@localhost:5432/rera_db' python3 rera_crawler.py --all"
