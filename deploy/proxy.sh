#!/usr/bin/env bash
# AutoPostingTG: собственный прокси к Telegram ТОЛЬКО для нашего приложения.
#
# Ставит отдельный экземпляр Xray в /opt/tgautoposting/bin (не трогает системный xray, если он есть),
# конфиг — /opt/tgautoposting/proxy/config.json (только для пользователя tgautoposting),
# служба — tgautoposting-proxy, слушает только 127.0.0.1:10808 (SOCKS5). Приложение находит его само
# (TG_PROXY=auto) и пускает через него только Telegram.
#
# Запуск:  sudo bash proxy.sh /путь/к/proxy.json
#   proxy.json — клиентский конфиг Xray (как в VoidVPN srv-tg-proxy.sh). Из него берутся outbounds
#   и routing, а входы (inbounds) скрипт заменяет на один локальный SOCKS5 127.0.0.1:10808.
# Обновить Xray:  sudo bash proxy.sh /путь/к/proxy.json --upgrade
set -euo pipefail

APP=tgautoposting
APP_USER=tgautoposting
APP_DIR=/opt/tgautoposting
BIN_DIR=$APP_DIR/bin
CONF_DIR=$APP_DIR/proxy
PORT=10808
SRC=${1:-}
UPGRADE=${2:-}

say() { echo -e "\n==> $*"; }
[[ $EUID -eq 0 ]] || { echo "Запустите от root: sudo bash $0 /путь/к/proxy.json"; exit 1; }
[[ -n $SRC && -f $SRC ]] || { echo "Укажите конфиг Xray: sudo bash $0 /путь/к/proxy.json"; exit 1; }
id -u "$APP_USER" >/dev/null 2>&1 || { echo "Сначала установите приложение: deploy/install.sh"; exit 1; }
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$SRC" || { echo "$SRC — не JSON"; exit 1; }

if [[ ! -x $BIN_DIR/xray || $UPGRADE == "--upgrade" ]]; then
  say "Скачиваю Xray (официальный релиз XTLS) в $BIN_DIR"
  command -v unzip >/dev/null || { apt-get update -qq && apt-get install -y -qq unzip; }
  case $(uname -m) in
    x86_64) ARCH=64 ;;
    aarch64|arm64) ARCH=arm64-v8a ;;
    *) echo "Неизвестная архитектура $(uname -m)"; exit 1 ;;
  esac
  TMP=$(mktemp -d)
  curl -fsSL -o "$TMP/xray.zip" "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-$ARCH.zip"
  unzip -q -o "$TMP/xray.zip" -d "$TMP/x"
  install -d -o "$APP_USER" -g "$APP_USER" "$BIN_DIR"
  install -m 755 -o "$APP_USER" -g "$APP_USER" "$TMP/x/xray" "$BIN_DIR/xray"
  install -m 644 -o "$APP_USER" -g "$APP_USER" "$TMP/x/geoip.dat" "$TMP/x/geosite.dat" "$BIN_DIR/"
  rm -rf "$TMP"
fi
"$BIN_DIR/xray" version | head -1

say "Конфиг прокси (только локальный SOCKS5 127.0.0.1:$PORT)"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$CONF_DIR"
python3 - "$SRC" "$CONF_DIR/config.json" "$PORT" <<'PY'
import json, sys
src, dst, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
cfg = json.load(open(src))
# Только один вход: SOCKS5 на localhost. Никаких внешних портов.
cfg["inbounds"] = [{
    "tag": "socks", "listen": "127.0.0.1", "port": port, "protocol": "socks",
    "settings": {"udp": True},
    "sniffing": {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": True},
}]
cfg.setdefault("log", {})["loglevel"] = "warning"
cfg["log"].pop("access", None); cfg["log"].pop("error", None)   # логи — в journald
if not any(o.get("protocol") not in ("freedom", "blackhole", "dns") for o in cfg.get("outbounds", [])):
    sys.exit("В конфиге нет ни одного прокси-выхода (outbounds) — нечего использовать")
json.dump(cfg, open(dst, "w"), ensure_ascii=False, indent=2)
PY
chown "$APP_USER:$APP_USER" "$CONF_DIR/config.json"
chmod 600 "$CONF_DIR/config.json"
XRAY_LOCATION_ASSET=$BIN_DIR "$BIN_DIR/xray" run -test -c "$CONF_DIR/config.json" >/dev/null
echo "Конфиг корректен."

say "Служба $APP-proxy"
cat > "/etc/systemd/system/$APP-proxy.service" <<UNIT
[Unit]
Description=AutoPostingTG — прокси к Telegram только для приложения (Xray, 127.0.0.1:$PORT)
After=network-online.target
Wants=network-online.target
PartOf=$APP.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
Environment=XRAY_LOCATION_ASSET=$BIN_DIR
ExecStart=$BIN_DIR/xray run -c $CONF_DIR/config.json
Restart=always
RestartSec=5
MemoryMax=200M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectControlGroups=true
ProtectKernelModules=true
ProtectKernelTunables=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$APP-proxy" >/dev/null 2>&1
systemctl restart "$APP-proxy"
sleep 3

say "Проверяю Telegram через прокси приложения"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -x "socks5h://127.0.0.1:$PORT" https://api.telegram.org/ || true)
if [[ $code =~ ^(200|301|302|404)$ ]]; then
  echo "OK: api.telegram.org отвечает через 127.0.0.1:$PORT (HTTP $code)"
else
  echo "!! Telegram через прокси не ответил (HTTP ${code:-нет}). Логи: journalctl -u $APP-proxy -n 30"
  exit 1
fi

if systemctl is-enabled "$APP" >/dev/null 2>&1; then
  systemctl restart "$APP"
  echo "Приложение перезапущено и само подхватит прокси (TG_PROXY=auto). Проверьте /server в боте."
fi
cat <<MSG

Готово. Прокси работает только для AutoPostingTG:
  служба:  systemctl status|restart $APP-proxy  ·  логи: journalctl -u $APP-proxy -f
  порт:    127.0.0.1:$PORT (снаружи недоступен)
  конфиг:  $CONF_DIR/config.json (не в git)
Системный xray и другие приложения не затронуты.
MSG
