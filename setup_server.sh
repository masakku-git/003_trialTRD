#!/bin/bash
# Hetzner (Ubuntu 24.04) 初期セットアップスクリプト
# 実行: bash setup_server.sh
set -e

BOT_USER="trading"
BOT_DIR="/home/${BOT_USER}/bot"

echo "=== [1/7] システム更新 ==="
apt-get update && apt-get upgrade -y
apt-get install -y python3 python3-venv python3-pip git ufw curl wget

echo "=== [2/7] ユーザー作成 ==="
if ! id "${BOT_USER}" &>/dev/null; then
    useradd -m -s /bin/bash "${BOT_USER}"
    echo "ユーザー ${BOT_USER} 作成完了"
fi

echo "=== [3/7] ファイアウォール設定 ==="
ufw allow OpenSSH
ufw allow 11111/tcp  # Futu OpenD
ufw --force enable

echo "=== [4/7] JSTタイムゾーン設定 ==="
timedatectl set-timezone Asia/Tokyo

echo "=== [5/7] Python環境構築 ==="
sudo -u "${BOT_USER}" bash -c "
    mkdir -p ${BOT_DIR}
    cd ${BOT_DIR}
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install yfinance pandas ta python-dotenv
    # futu-apiはLinux対応を確認後インストール
    # pip install futu-api
"

echo "=== [6/7] ディレクトリ作成 ==="
sudo -u "${BOT_USER}" mkdir -p "${BOT_DIR}"/{agents,tools,db,data,logs}

echo "=== [7/7] cron設定 ==="
CRON_CMD="45 8 * * 1-5 ${BOT_DIR}/venv/bin/python ${BOT_DIR}/main.py >> ${BOT_DIR}/logs/trading_\$(date +\\%Y\\%m\\%d).log 2>&1"
(crontab -u "${BOT_USER}" -l 2>/dev/null; echo "${CRON_CMD}") | crontab -u "${BOT_USER}" -

echo ""
echo "=== セットアップ完了 ==="
echo "次のステップ:"
echo "  1. ${BOT_DIR}/.env を作成（.env.example を参考に）"
echo "  2. GitHubからコードをデプロイ:"
echo "     sudo -u ${BOT_USER} git clone <repo> ${BOT_DIR}"
echo "  3. DB初期化:"
echo "     sudo -u ${BOT_USER} ${BOT_DIR}/venv/bin/python ${BOT_DIR}/main.py --init-db"
echo "  4. テスト実行:"
echo "     sudo -u ${BOT_USER} ${BOT_DIR}/venv/bin/python ${BOT_DIR}/main.py --dry-run"
