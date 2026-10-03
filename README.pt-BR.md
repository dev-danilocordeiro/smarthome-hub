# Smart Home Hub (resumo em português)

Hub de casa inteligente multi-residência, feito para demonstrar **arquitetura orientada a
eventos, telemetria em tempo real, séries temporais, segurança de dispositivos e
observabilidade**. Não precisa de hardware: um simulador fala o mesmo protocolo MQTT que um
ESP32 real falaria.

> **Status:** as 11 fases estão concluídas; o próximo passo é um ESP32 de verdade. A
> documentação completa está no [README em inglês](README.md).

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
| Alertmanager | http://localhost:9093  |
| Mailpit    | http://localhost:8025    |

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

## Comandos

`POST /homes/{casa}/devices/{dispositivo}/commands` responde `202` na hora: o comando e a
mensagem MQTT são gravados na mesma transação (**transactional outbox**) e o `worker` publica
depois do commit, acordado por `NOTIFY`. O dispositivo confirma via MQTT, o ingestor registra
o resultado e comandos sem resposta até o prazo viram `timed_out` (uma resposta atrasada não
muda isso). Fechaduras e câmeras exigem `operate_locks` e login feito nos últimos 5 minutos.
Cada comando tem um `trace_id`: no Tempo, um único trace cobre API, worker, dispositivo e
ingestor ([ADR 0009](docs/adr/0009-commands-outbox-and-trace-propagation.md)).

## Automações

Automações são documentos JSON ([DSL v1](docs/automations.md)) com gatilhos de telemetria,
propriedades do twin, presença ou horário (no fuso da casa, com horário de verão), com espera
opcional (`for_s`: "sem movimento por 5 minutos"), condições, e comandos ou cenas como ações.
Os gatilhos disparam na borda (quando passam a valer) e cada disparo vira um *run* com
`trace_id`.

O ingestor publica o que mudou num **Redis stream**; o `worker` consome com um consumer group,
então réplicas dividem o trabalho e um evento reentregue nunca roda uma automação duas vezes.
Automações que se disparariam em loop são apontadas ao salvar e **suspensas** em execução
(profundidade da cadeia causal e limite de taxa). Edições são versionadas (`If-Match` e
histórico de revisões) e o **dry run** reexecuta até uma semana de telemetria gravada com as
mesmas regras do motor ([ADR 0010](docs/adr/0010-automations-event-stream-and-loop-protection.md)).

## App web

`make up`, `make simulate` e `make web-dev`, depois http://localhost:5173 (usuária `alice`,
senha `smarthome-dev-1`). Cada casa aparece como uma **planta ao vivo**: cômodos com seus
dispositivos, atualizados por WebSocket conforme os dispositivos reportam. Ao selecionar um
dispositivo dá para controlá-lo e ver estado, leituras, gráfico de histórico e comandos
recentes. Há editor de automações (modelos, validação no servidor, dry run das últimas 24 h e
execuções recentes) e cenas. O cliente TypeScript é tipado a partir do OpenAPI da API (o CI
falha se ficar desatualizado) e o app é instalável como PWA
([ADR 0011](docs/adr/0011-web-app-live-updates-and-typed-client.md)).

## Energia

Tomadas e o medidor geral reportam um contador acumulado (`energy_wh_total`). O `worker`
transforma as diferenças em consumo por hora e por dispositivo a cada minuto: um contador
que diminuiu é tratado como reinício do aparelho (nunca energia negativa) e as horas
recentes são recalculadas, então leituras atrasadas ou reenviadas caem no lugar certo. A
tarifa é exata (strings decimais na API, `numeric` no Postgres, `Decimal` no Python,
arredondada uma vez só) e aceita postos horários, como a tarifa branca. Com medidor geral,
o total da casa é o medidor e as tomadas viram detalhamento
([ADR 0012](docs/adr/0012-energy-accounting-from-counters.md)).

## Alertas e notificações

Fechadura offline por mais de 5 minutos, bateria abaixo de 15 %, mês passando de 80 % ou
100 % do orçamento de energia: cada condição vira **um** alerta (`pending → open →
resolved`), por mais que o sensor oscile e por mais workers que vejam o evento; quem
garante é um índice único parcial no banco. Os membros recebem a notificação no app (o
sininho atualiza pelo WebSocket), e-mail conforme as preferências (gravidade mínima,
horário de silêncio, que alertas críticos ignoram) e o webhook da casa recebe um POST
assinado com HMAC-SHA256. Em desenvolvimento todo e-mail cai no Mailpit
([ADR 0013](docs/adr/0013-alerts-and-notifications.md), [guia do webhook](docs/notifications.md)).

## Dashboards, alertas operacionais e teste de carga

O Grafana tem quatro dashboards (serviços, pipeline de dispositivos, comandos e
automações, energia e notificações). O Prometheus avalia 14 regras de alerta, testadas com
`promtool test rules` no `make check-infra`, e o Alertmanager manda tudo para o Mailpit.
`make loadtest-ingest` e `make loadtest-api` (k6) medem ingestão e leitura; método e
resultados em [docs/load-test.md](docs/load-test.md).

## Arquitetura, segurança e testes de ponta a ponta

Diagramas de contexto, contêineres, módulos e fluxos em [docs/architecture.md](docs/architecture.md).
Modelo de ameaças (STRIDE por fronteira de confiança, com riscos em aberto) em
[docs/security/threat-model.md](docs/security/threat-model.md) e autoavaliação OWASP ASVS 5.0
nível 2 em [docs/security/asvs.md](docs/security/asvs.md). As portas da stack escutam só em
`127.0.0.1` (exceto o MQTT, que os dispositivos da rede local usam) e o Redis exige senha.
`make e2e` roda testes Playwright num navegador de verdade contra a stack completa (login no
Keycloak, controle ao vivo, energia, alertas, isolamento entre casas), também no CI a cada PR
([ADR 0015](docs/adr/0015-testing-strategy.md)).
