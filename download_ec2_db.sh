#!/usr/bin/env bash
set -e

# Usage: ./download_ec2_db.sh <EC2_IP_OR_HOST> [PATH_TO_PEM_KEY] [EC2_USER]
# Example: ./download_ec2_db.sh 54.210.12.34 ~/.ssh/my-key.pem ubuntu

EC2_HOST="${1}"
KEY_PATH="${2}"
EC2_USER="${3:-ubuntu}"

if [ -z "$EC2_HOST" ]; then
    echo "Usage: $0 <EC2_IP_OR_HOST> [PATH_TO_PEM_KEY] [EC2_USER]"
    echo "Example: $0 54.210.12.34 ~/.ssh/my-ec2-key.pem ubuntu"
    exit 1
fi

SSH_OPT=""
if [ -n "$KEY_PATH" ]; then
    SSH_OPT="-i $KEY_PATH"
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILENAME="rera_db_${TIMESTAMP}.dump"
REMOTE_TEMP="/tmp/${DUMP_FILENAME}"
LOCAL_DUMP="/tmp/${DUMP_FILENAME}"

echo "=== 1. Exporting PostgreSQL database on AWS EC2 (${EC2_USER}@${EC2_HOST}) ==="
ssh $SSH_OPT "${EC2_USER}@${EC2_HOST}" "sudo -u postgres pg_dump -Fc rera_db > ${REMOTE_TEMP}"

echo "=== 2. Downloading dump file to local machine (${LOCAL_DUMP}) ==="
scp $SSH_OPT "${EC2_USER}@${EC2_HOST}:${REMOTE_TEMP}" "${LOCAL_DUMP}"

echo "=== 3. Cleaning up temporary dump on EC2 ==="
ssh $SSH_OPT "${EC2_USER}@${EC2_HOST}" "rm -f ${REMOTE_TEMP}"

echo "=== 4. Ensuring local database 'rera_db' exists ==="
sudo -u postgres psql -c "CREATE DATABASE rera_db;" 2>/dev/null || echo "Local database rera_db already exists."
sudo -u postgres psql -c "CREATE USER rera_user WITH PASSWORD 'rera_password';" 2>/dev/null || echo "User rera_user already exists."
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE rera_db TO rera_user;" 2>/dev/null || true
sudo -u postgres psql -d rera_db -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" 2>/dev/null || true

echo "=== 5. Restoring dump into local PostgreSQL database ==="
sudo -u postgres pg_restore -d rera_db "${LOCAL_DUMP}"

echo "=== Success! PostgreSQL database 'rera_db' synced to local machine. ==="
echo "Dump file saved locally at: ${LOCAL_DUMP}"
