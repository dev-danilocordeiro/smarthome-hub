# Smart Home Hub (resumo em português)

Hub de casa inteligente multi-residência, feito para demonstrar **arquitetura orientada a
eventos, telemetria em tempo real, séries temporais, segurança de dispositivos e
observabilidade**. Não precisa de hardware: um simulador fala o mesmo protocolo MQTT que um
ESP32 real falaria.

> **Status:** fase 6 de 11 (telemetria). A documentação completa está no [README em inglês](README.md).

## Arquitetura

Monolito modular com três entrypoints (`api`, `ingestor`, `worker`) sobre o mesmo código.
Cada módulo tem seu próprio schema no Postgres, e as fronteiras entre módulos são
verificadas no CI pelo import-linter. Detalhes no
[ADR 0001](docs/adr/0001-modular-monolith-with-multiple-entrypoints.md).

## Como rodar

```bash
make up                              # sobe tudo e espera os healthchecks
curl localhost:8000/health/ready
make check && make test              # gates estáticos e testes
```

| Serviço    | Endereço                 |
|------------|--------------------------|
| API        | http://localhost:8000    |
| PostgreSQL | `localhost:15432`        |
| Redis      | `localhost:16379`        |
| Web (dev)  | http://localhost:5173    |
| Keycloak   | http://localhost:8080    |
| MQTT (TLS) | `localhost:8883`         |
| Grafana    | http://localhost:3000    |
| Prometheus | http://localhost:9090    |
| Tempo      | http://localhost:3200    |
| Loki       | http://localhost:3100    |

## Observabilidade

Todos os serviços enviam traces, métricas e logs via OTLP para o OpenTelemetry Collector,
que distribui para Tempo, Prometheus e Loki. No Grafana, o dashboard **Service Overview**
liga métrica → trace (exemplars) → logs (por `trace_id`). Rode `make demo-traffic` e
siga o tour no [README em inglês](README.md#tour-follow-one-request-through-every-signal).
Detalhes no [ADR 0002](docs/adr/0002-observability-pipeline.md).

## Login

`make up && make web-dev`, depois abra http://localhost:5173. Usuários de desenvolvimento
(senha `smarthome-dev-1`): `alice`, `bob`, `carol`, `dave`. A API funciona como BFF: o
navegador só recebe um cookie de sessão `HttpOnly`/`SameSite=Strict`, os tokens ficam no
servidor e as requisições de escrita exigem token CSRF. Detalhes nos ADRs
[0003](docs/adr/0003-bff-sessions-and-csrf.md) e [0004](docs/adr/0004-tenancy-roles-and-audit-log.md).

## Dispositivos simulados

`make simulate` sobe 3 casas com 20 dispositivos cada, falando o [protocolo v1](docs/device-protocol.md)
via MQTT 5 com TLS. Cada dispositivo tem credenciais próprias e ACL restrita aos próprios
tópicos ([ADR 0005](docs/adr/0005-mqtt-broker-and-qos.md)). O simulador modela o dia da casa
(temperatura, luz do sol, presença, consumo) e injeta falhas: quedas de conexão (detectadas
pelo Last Will), leituras ruidosas, payloads inválidos e bateria fraca.

## Pareamento

Na primeira execução, `make simulate` cria três casas da `alice` e um código de pareamento por
dispositivo. Cada dispositivo simulado se pareia sozinho via HTTP (`POST /provisioning/claim`),
como um ESP32 real faria, e recebe credenciais próprias no broker. O ingestor mantém presença
(inclusive via Last Will) e o twin (desired/reported) de cada dispositivo.
Detalhes no [ADR 0006](docs/adr/0006-device-provisioning-credentials-and-twin.md).

## Telemetria

A telemetria entra em lote numa hypertable do TimescaleDB, com agregados contínuos de 1 minuto,
1 hora e 1 dia, compressão após 7 dias e retenção configurável. O ingestor deduplica mensagens,
aplica backpressure e coloca em quarentena dispositivos que inundam o broker. O estado atual
fica no Redis, o histórico no TimescaleDB e a configuração no Postgres
([ADR 0007](docs/adr/0007-telemetry-in-timescaledb.md), [ADR 0008](docs/adr/0008-current-state-history-configuration.md)).
