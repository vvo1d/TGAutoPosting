#!/usr/bin/env bash
# AutoPostingTG: установка/обновление на сервере как служба systemd «tgautoposting».
#
# Ничего чужого не трогает: отдельный системный пользователь, своя папка /opt/tgautoposting,
# своя служба. Портов не открывает (бот работает через long polling) — nginx не нужен.
#
# Запуск:            sudo bash install.sh
# С переносом данных: sudo IMPORT_DIR=/tmp/tgap bash install.sh
#   (в IMPORT_DIR лежат .env и папка data/ с Mac: autoposting.db, reader.session, media/)
set -euo pipefail

APP=tgautoposting
APP_USER=tgautoposting
APP_DIR=/opt/tgautoposting
REPO=${REPO:-git@github.com:vvo1d/TGAutoPosting.git}
IMPORT_DIR=${IMPORT_DIR:-}

say() { echo -e "\n==> $*"; }
[[ $EUID -eq 0 ]] || { echo "Запустите от root: sudo bash $0"; exit 1; }

say "Проверяю системные зависимости"
python3 -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null \
  || { echo "Нужен Python 3.10+ (сейчас: $(python3 --version 2>&1))"; exit 1; }
need=()
command -v git >/dev/null || need+=(git)
command -v curl >/dev/null || need+=(curl)
python3 -c 'import ensurepip, venv' 2>/dev/null || need+=(python3-venv)
if (( ${#need[@]} )); then
  echo "Ставлю: ${need[*]}"
  apt-get update -qq && apt-get install -y -qq "${need[@]}"
fi

say "Системный пользователь $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 \
  || useradd --system --create-home --home-dir "/var/lib/$APP" --shell /usr/sbin/nologin "$APP_USER"
HOME_DIR=$(getent passwd "$APP_USER" | cut -d: -f6)
as_app() { sudo -u "$APP_USER" -H "$@"; }

say "Ключ доступа к GitHub (deploy key, только чтение)"
KEY="$HOME_DIR/.ssh/id_ed25519"
if [[ ! -f $KEY ]]; then
  install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$HOME_DIR/.ssh"
  as_app ssh-keygen -q -t ed25519 -N "" -C "$APP@$(hostname)" -f "$KEY"
fi
SSH_CMD="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
if ! as_app env GIT_SSH_COMMAND="$SSH_CMD" git ls-remote "$REPO" >/dev/null 2>&1; then
  echo
  echo "Добавьте этот ключ в GitHub: репозиторий TGAutoPosting → Settings → Deploy keys → Add deploy key"
  echo "(галочку «Allow write access» НЕ ставить):"
  echo
  cat "$KEY.pub"
  echo
  read -rp "Добавили? Нажмите Enter… "
  as_app env GIT_SSH_COMMAND="$SSH_CMD" git ls-remote "$REPO" >/dev/null \
    || { echo "Нет доступа к $REPO — проверьте ключ"; exit 1; }
fi

say "Код в $APP_DIR"
if [[ ! -d $APP_DIR/.git ]]; then
  install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
  as_app env GIT_SSH_COMMAND="$SSH_CMD" git clone -q "$REPO" "$APP_DIR"
else
  as_app env GIT_SSH_COMMAND="$SSH_CMD" git -C "$APP_DIR" pull --ff-only
fi
as_app git -C "$APP_DIR" config core.sshCommand "$SSH_CMD"   # чтобы /update в боте мог делать git pull

say "Python-окружение и зависимости"
[[ -x $APP_DIR/.venv/bin/python ]] || as_app python3 -m venv "$APP_DIR/.venv"
as_app "$APP_DIR/.venv/bin/pip" install -q --no-cache-dir --upgrade pip
as_app "$APP_DIR/.venv/bin/pip" install -q --no-cache-dir -r "$APP_DIR/requirements.txt"

say "Настройки и данные"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/data"
if [[ -n $IMPORT_DIR ]]; then
  echo "Переношу данные из $IMPORT_DIR"
  [[ -f $IMPORT_DIR/.env ]] && install -m 600 -o "$APP_USER" -g "$APP_USER" "$IMPORT_DIR/.env" "$APP_DIR/.env"
  [[ -d $IMPORT_DIR/data ]] && cp -a "$IMPORT_DIR/data/." "$APP_DIR/data/"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
fi
if [[ ! -f $APP_DIR/.env ]]; then
  install -m 600 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Создал $APP_DIR/.env из шаблона — его нужно заполнить."
fi

say "Доступ к Telegram из этого сервера"
if curl -s -o /dev/null --max-time 10 https://api.telegram.org; then
  echo "api.telegram.org доступен напрямую."
else
  echo "!! api.telegram.org напрямую НЕ доступен."
  if ss -ltn 2>/dev/null | grep -q '127.0.0.1:1080'; then
    echo "   Нашёл локальный SOCKS5 на 127.0.0.1:1080 — пропишите в $APP_DIR/.env:"
    echo "   TG_PROXY=socks5://127.0.0.1:1080"
  else
    echo "   Поднимите локальный прокси и пропишите TG_PROXY в $APP_DIR/.env"
  fi
fi
grep -q '^TG_PROXY=' "$APP_DIR/.env" && echo "Сейчас в .env: $(grep '^TG_PROXY=' "$APP_DIR/.env")"

say "Служба systemd $APP"
cat > "/etc/systemd/system/$APP.service" <<UNIT
[Unit]
Description=AutoPostingTG — автопостинг в Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m app.main
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=PIP_NO_CACHE_DIR=1

# Бережём соседние приложения на сервере
MemoryMax=700M
CPUQuota=80%
Nice=5

# Изоляция: пишет только в свою папку
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR $HOME_DIR
ProtectControlGroups=true
ProtectKernelModules=true
ProtectKernelTunables=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$APP" >/dev/null 2>&1

ready=1
grep -qE '^BOT_TOKEN=.+' "$APP_DIR/.env" || { echo "!! В .env не заполнен BOT_TOKEN"; ready=0; }
[[ -f $APP_DIR/data/reader.session ]] || { echo "!! Нет сессии аккаунта-читателя (data/reader.session)"; ready=0; }

if (( ready )); then
  systemctl restart "$APP"
  sleep 6
  systemctl --no-pager --lines=15 status "$APP" || true
  echo
  echo "Готово. Логи: journalctl -u $APP -f   ·   управление: systemctl restart|stop $APP"
else
  cat <<MSG

Почти готово. Осталось:
  1) Заполнить настройки:   sudo nano $APP_DIR/.env
  2) Войти аккаунтом-читателем (если не переносили data/ с Mac):
       cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m scripts.login --qr
  3) Запустить:             sudo systemctl restart $APP
MSG
fi
