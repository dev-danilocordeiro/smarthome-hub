# Smart Home Hub (resumo em português)

Hub de casa inteligente multi-residência, feito para demonstrar **arquitetura orientada a
eventos, telemetria em tempo real, séries temporais, segurança de dispositivos e
observabilidade**. Não precisa de hardware: um simulador fala o mesmo protocolo MQTT que um
ESP32 real falaria.

> **Status:** fase 4 de 11 (protocolo de dispositivos e broker). A documentação completa está no [README em inglês](README.md).

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
