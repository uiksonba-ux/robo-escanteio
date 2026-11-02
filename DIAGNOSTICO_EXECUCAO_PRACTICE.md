# Execução de sinais digitais em PRACTICE

Base investigada: `b5c76fc90b65427a2f6d6f36af7f7ef8cc1e3498`.
Branch: `fix/practice-normal-signal-execution`.
Escopo: somente este repositório e o serviço Render `robo-escanteio`
(`srv-daip5th594qs739lg8eg`). Nenhuma alteração de APK ou de outros serviços.

## Evidência anterior à correção

Consulta aos logs Render em 2026-11-02, desde 15:07 UTC, sem paginação restante:

- Deploy `dep-dbkae7rbc2fs73ebtrag` Live, commit base, desde 15:08:12 UTC.
- Conexão anunciada PRACTICE às 15:08:09 UTC, saldo de referência R$600.
- 51 respostas de `buy_digital_spot_v2`, de 15:08:34 a 15:52:15 UTC.
  Todas: `(False, {'code': 'error_place_digital_order', 'message': 'rejected'})`.
- Nenhum registro `ORDEM DIGITAL ACEITA` ou `RESULTADO` nessa janela.
- Últimas varreduras às 15:52:32/34/37 UTC; depois continuam respostas HTTP.

Esses dados comprovam recusa, não comprovam sua causa específica. A API retornou
apenas `rejected`; não há base para afirmar que foi senha, saldo, ativo, payout
ou expiração. Aprovação pelo indicador e `DIAGNOSTICO DIGITAL` antecedem a
confirmação da corretora e nunca devem ser apresentados como compra aceita.

## Problemas encontrados e comportamento corrigido

1. Chamadas do SDK podem esperar indefinidamente. Candles seguravam `api_lock`,
   compartilhado por saldo e envio; `is_alive()` no supervisor não detectava
   falta de progresso. A paralisação observada é compatível com esse mecanismo,
   mas os logs antigos não identificam qual chamada ficou presa.
2. A disponibilidade usava `get_all_open_time()`, que também espera outros
   mercados. Agora consulta somente o catálogo digital, com cache por sessão,
   prazo e verificação de horário de funcionamento. Desconhecido é bloqueio
   explícito, sem presumir ativo aberto.
3. O envio v2 e a leitura de resultados usam o protocolo/buffers do SDK fixado,
   evitando seus loops sem prazo. Só identificador válido confirma aceitação;
   só recusa explícita libera uma tentativa enviada. Timeout preserva a mesma
   requisição e suas reservas; resposta tardia é conciliada sem recomprar.
4. Confirmação da sessão PRACTICE, saldo, instrumento, payout, expiração e
   relógio são verificadas antes do envio. Reconexão e alteração de modo são
   serializadas com o envio para proteger o identificador de saldo global do SDK.
5. Uma recuperação integral maior que o saldo disponível não monopoliza todos
   os sinais. A dívida fica registrada e a entrada normal usa a base quando
   houver saldo para ela. Recuperação em andamento reserva a dívida uma vez;
   outras bancas continuam com entradas normais.
6. A recuperação considera o menor payout entre o confirmado e o configurado.
   Payout desconhecido não autoriza alocar recuperação. Somente o resultado
   financeiro confirmado reduz ou aumenta o prejuízo global.

## Rastreabilidade

Os logs distinguem `SINAL APROVADO`, `SINAL RESERVADO`, `SALDO RESERVADO`,
`ORDEM NAO ENVIADA`, `ORDEM ENVIADA`, `ORDEM ACEITA`, `ORDEM RECUSADA`,
`ORDEM INDETERMINADA`, confirmação tardia, `RESULTADO WIN/LOSS/DRAW` e
`OPERACAO CONCILIADA`. Banca, ativo, nível e identificador acompanham as etapas
aplicáveis. Payloads arbitrários e mensagens de exceção com possíveis segredos
não são registrados. A telemetria periódica e `/health` expõem contadores e
progresso sem fazer compras nem chamadas à corretora.

## Regras preservadas

| Assertividade global | Gales |
| --- | ---: |
| 0% a menos de 50% | 4 |
| 50% a menos de 60% | 3 |
| 60% a menos de 70% | 2 |
| 70% a menos de 80% | 1 |
| 80% a 100% | 0 |

A quantidade de gales fica fixa durante cada operação. Multiplicador ×2,
10 bancas, entrada-base de 1% (mínimo R$2), indicadores, Telegram, OTC e
análises M1/M5/M15 foram preservados. M15 mantém expiração digital M5,
conforme configuração anterior. Não há seleção de REAL por ambiente.

## Validação automatizada

Resultado pré-publicação em 2026-11-02: **104 testes aprovados**
(102 de comportamento simulado + 2 verificações estáticas). `git diff --check`
também passou. Revisão independente repetiu os testes de broker e scanner.

Os testes importam o módulo com dependências e worker isolados. Compras,
WebSocket, conexão, saldo, resultados e Telegram são simulados; os testes não
conhecem credenciais e não enviam ordens externas.

Comandos reproduzíveis, sem instalar/conectar o SDK:

```sh
python -m unittest discover -s tests -v
python -m unittest -v test_monitor_prod_static
git diff --check
```

Cobertura: aceitação/recusa, erros e respostas ambíguas, respostas tardias sem
reenvio, sessão PRACTICE, saldo inválido/insuficiente, ativo fechado/desconhecido,
payout, expiração, callbacks de candles por requisição, prazos e progresso dos
scanners; ledger idempotente, concorrência, recuperação pendente e sem saldo,
fronteiras das faixas e ciclos completos WIN, LOSS com quatro gales e DRAW.
O workflow executa as suítes em branches de correção, PRs para main e na main.

## Limites operacionais

- Uma resposta da corretora ausente/ambígua não pode virar recusa nem DRAW.
  A reserva permanece e a telemetria explica a pendência. Se a corretora não
  responder, é necessária conciliação com ela; reiniciar não comprova resultado.
- Resultados dependem de eventos financeiros disponíveis no SDK. Não há aqui
  consulta histórica completa comprovada para toda ordem anterior a uma queda.
- O projeto já mantém contabilidade em memória volátil, sem banco/disco
  persistente. Esta correção não zera dívidas na lógica de recuperação nem
  declara recuperações fictícias; um reinício do processo continua perdendo
  esse estado. Persistência entre deploys exige armazenamento durável próprio.
  Nos logs do deploy base investigado não houve ordem aceita ou resultado.
- Deploy Live confirma que o processo subiu. Execução efetiva exige, além
  disso, log `ORDEM ACEITA` com ID; ganho exige resultado confirmado posterior.
  O acompanhamento deve relatar o bloqueio exato quando não houver aceitação.

## Dependência verificada

O SDK foi fixado em
[`8a903cc094a74af1ed935a56a2d6b5a9ed3319d7`](https://github.com/iqoptionapi/iqoptionapi/tree/8a903cc094a74af1ed935a56a2d6b5a9ed3319d7).
Foram verificados `stable_api.py`, canais de ordens/candles e recebimento de
respostas. O adaptador de candles considera o callback de `websocket-client`
0.56.0, já fixado pelo projeto. Não troca ordens digitais por outro produto.
