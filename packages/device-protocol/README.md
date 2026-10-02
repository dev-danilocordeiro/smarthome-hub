# device-protocol

Single source of truth for the MQTT contract between devices and the hub:
topic layout, JSON Schemas, schema versions and QoS per message type.

Consumed by `apps/api` (and therefore `ingestor` / `worker`), `apps/simulator`
and `firmware-example/`. Populated in phase 4.
