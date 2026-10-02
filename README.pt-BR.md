# Smart Home Hub (resumo em português)

Hub de casa inteligente multi-residência, feito para demonstrar **arquitetura orientada a
eventos, telemetria em tempo real, séries temporais, segurança de dispositivos e
observabilidade**. Não precisa de hardware: um simulador fala o mesmo protocolo MQTT que um
ESP32 real falaria.

> **Status:** fase 1 de 11 (fundação). A documentação completa está no [README em inglês](README.md).

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
