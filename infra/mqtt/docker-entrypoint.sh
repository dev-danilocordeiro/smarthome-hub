#!/bin/sh
# Create the dynamic-security config with the admin account on first boot, then run the broker.
set -eu
CONFIG=/mosquitto/data/dynamic-security.json
if [ ! -f "$CONFIG" ]; then
  : "${MQTT_ADMIN_USER:?}" "${MQTT_ADMIN_PASSWORD:?}"
  mosquitto_ctrl dynsec init "$CONFIG" "$MQTT_ADMIN_USER" "$MQTT_ADMIN_PASSWORD" >/dev/null
  chown mosquitto:mosquitto "$CONFIG"
  echo "dynamic-security initialised with admin user $MQTT_ADMIN_USER"
fi
exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
