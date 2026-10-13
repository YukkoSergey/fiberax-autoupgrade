#!/bin/sh
# Встановлює vm-guardian як systemd-сервіс. Запускати від root з теки з вихідниками.
#   FIBERAX_TOKEN_FILE=/шлях/до/токена ./install.sh     (або скрипт спитає токен сам)
set -e
cd "$(dirname "$0")"
# На чистих образах Fiberax немає logrotate - без нього демон не зможе приборкувати логи
if ! command -v logrotate >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq logrotate
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q logrotate
    elif command -v yum >/dev/null 2>&1; then yum install -y -q logrotate
    else echo "УВАГА: не вдалося встановити logrotate - постав його вручну" >&2; fi
fi
install -d -m 755 /opt/vm-guardian
install -d -m 700 /etc/vm-guardian /var/lib/vm-guardian
install -m 755 vm_guardian.py /opt/vm-guardian/vm_guardian.py
install -m 644 apps.json /opt/vm-guardian/apps.json
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
if [ ! -x /usr/sbin/sendmail ] && ! grep -q '"smtp_host": *"[^"]' /etc/vm-guardian/config.json; then
    echo "УВАГА: на машині немає sendmail, листи не надсилатимуться."
    echo "  Додай у /etc/vm-guardian/config.json: smtp_host, smtp_port, smtp_user, mail_from"
    echo "  і поклади пароль у /etc/vm-guardian/smtp.password (chmod 600). Перевірка: vm-guardian test-mail"
fi
