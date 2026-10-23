#!/usr/bin/env bash
# One-time setup for the C182 Trip Planner on Ubuntu / WSL.
# Run from inside the project folder:   bash setup_wsl.sh
set -euo pipefail

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

say "1/5  System packages (python, venv, git, curl)"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl wget

say "2/5  GitHub CLI (gh)"
if command -v gh >/dev/null 2>&1; then
  gh --version | head -1
else
  sudo mkdir -p -m 755 /etc/apt/keyrings
  out=$(mktemp)
  wget -nv -O"$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg
  sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg < "$out" > /dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
  sudo apt-get update -y
  sudo apt-get install -y gh
fi

say "3/5  Claude Code"
if command -v claude >/dev/null 2>&1; then
  echo "Claude Code already installed: $(claude --version)"
  claude update || true
else
  # Official native installer (Linux / WSL). It auto-updates afterwards.
  curl -fsSL https://claude.ai/install.sh | bash
  echo "If 'claude' isn't found, open a new terminal (or add ~/.local/bin to PATH)."
fi

say "4/5  Python virtual environment + dependencies"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

say "5/5  Tests (offline)"
python -m pytest -q

cat <<'EOF'

All set. Next:
  source .venv/bin/activate
  streamlit run app.py          # opens http://localhost:8501 in your Windows browser

GitHub (one time):
  gh auth login                 # choose GitHub.com -> HTTPS -> login with a web browser
  git init && git add . && git commit -m "C182 trip planner"
  gh repo create c182-trip-planner --private --source=. --push

Keep building with Claude Code:
  claude                        # run inside this folder; it reads CLAUDE.md first
EOF
