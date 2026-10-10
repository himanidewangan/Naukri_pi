#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS. Run from the project folder:  bash setup_pi.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "==> Installing Chromium + chromedriver"
sudo apt update
# Package names differ by OS version; try both pairs.
sudo apt install -y chromium-browser chromium-chromedriver python3-venv xvfb \
  || sudo apt install -y chromium chromium-driver python3-venv xvfb

echo "==> Python venv + selenium"
python3 -m venv venv
./venv/bin/pip install --upgrade pip selenium

echo "==> Timezone to IST (so cron hours below are IST)"
sudo timedatectl set-timezone Asia/Kolkata || true

echo "==> Run wrapper (flock prevents overlapping runs)"
cat > run.sh <<EOF
#!/usr/bin/env bash
cd "$DIR"
exec /usr/bin/flock -n /tmp/naukri.lock ./venv/bin/python naukri_upload.py >> "$DIR/naukri.log" 2>&1
EOF
chmod +x run.sh

echo "==> Installing cron: 09:00, 13:00, 17:00, 21:00"
( crontab -l 2>/dev/null | grep -v "$DIR/run.sh" ; echo "0 9,13,17,21 * * * $DIR/run.sh" ) | crontab -

echo "Done. Edit credentials in naukri_upload.py, then test once with:  ./run.sh && tail -n 30 naukri.log"
