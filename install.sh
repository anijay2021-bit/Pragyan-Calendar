#!/usr/bin/env bash
# Pragyan Calendar - one-command installer for a fresh Ubuntu EC2 instance.
#   curl -fsSL <raw-url>/install.sh | bash      (or: bash install.sh)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(whoami)"
say(){ printf "\n\033[1;34m==> %s\033[0m\n" "$1"; }

say "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip

say "Creating virtualenv"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

say "Preparing config"
mkdir -p "$DIR/logs" "$DIR/cache" "$DIR/state"
if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  chmod 600 "$DIR/.env"
  echo "Created .env - fill in your AngelOne credentials before starting."
else
  echo ".env already exists, leaving it untouched."
fi

say "Installing systemd services"
for svc in pragyan-calendar pragyan-dashboard; do
  sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" \
      "$DIR/systemd/$svc.service" | sudo tee "/etc/systemd/system/$svc.service" >/dev/null
done

# The dashboard restarts the agent, so allow just those two commands via sudo.
echo "$USER_NAME ALL=(ALL) NOPASSWD: /bin/systemctl start pragyan-calendar, \
/bin/systemctl stop pragyan-calendar, /bin/systemctl restart pragyan-calendar" \
  | sudo tee /etc/sudoers.d/pragyan-calendar >/dev/null
sudo chmod 440 /etc/sudoers.d/pragyan-calendar
sudo systemctl daemon-reload

cat <<DONE

--------------------------------------------------------------------
Installed to: $DIR

Next:
  1. nano $DIR/.env          # AngelOne credentials + DASH_PASS
  2. $DIR/venv/bin/python main.py --check    # verify, places NO orders
  3. sudo systemctl enable --now pragyan-calendar pragyan-dashboard

Dashboard: http://<server-ip>:8080  (open port 8080 in your EC2 security
group, and prefer restricting it to your own IP)

Mode is PAPER until you change TRADING_MODE in .env or flip it in the UI.
--------------------------------------------------------------------
DONE
