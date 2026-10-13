#!/bin/sh
# Встановлює vm-guardian як systemd-сервіс. Запускати від root з теки з вихідниками.
#   FIBERAX_TOKEN_FILE=/шлях/до/токена ./install.sh     (або скрипт спитає токен сам)
set -e
cd "$(dirname "$0")"
install -d -m 755 /opt/vm-guardian
install -d -m 700 /etc/vm-guardian /var/lib/vm-guardian
install -m 755 vm_guardian.py /opt/vm-guardian/vm_guardian.py
[ -f /etc/vm-guardian/config.json ] || echo '{}' > /etc/vm-guardian/config.json
chmod 600 /etc/vm-guardian/config.json
if [ ! -f /etc/vm-guardian/fiberax.token ]; then
    if [ -n "$FIBERAX_TOKEN_FILE" ]; then
        install -m 600 "$FIBERAX_TOKEN_FILE" /etc/vm-guardian/fiberax.token
    elif [ -t 0 ]; then
        printf 'Fiberax API токен (fbx_..., з правом запису): '
        stty -echo; read -r TOKEN; stty echo; echo
        (umask 077; printf '%s\n' "$TOKEN" > /etc/vm-guardian/fiberax.token)
    else
        echo "Немає токена: задай FIBERAX_TOKEN_FILE або поклади його в /etc/vm-guardian/fiberax.token" >&2
        exit 1
    fi
fi
printf '#!/bin/sh\nexec /usr/bin/python3 /opt/vm-guardian/vm_guardian.py "$@"\n' > /usr/local/bin/vm-guardian
chmod 755 /usr/local/bin/vm-guardian
install -m 644 vm-guardian.service /etc/systemd/system/vm-guardian.service
systemctl daemon-reload
systemctl enable vm-guardian.service
systemctl restart vm-guardian.service
# Ліміт витрат залежить від ціни цієї VM - питаємо власника. Без нього демон ресурсів не додає.
if ! grep -q '"stop_eur": *[0-9]' /etc/vm-guardian/config.json; then
    if [ -t 0 ]; then vm-guardian setup; else echo "УВАГА: ліміт витрат не задано. Виконай: vm-guardian setup"; fi
fi
