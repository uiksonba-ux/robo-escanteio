# Baskaraido Trader PRO — controle PRACTICE

## Implementado no código

O APK usa o monitor isolado como relay de configurações e comandos. O monitor não importa `robo.py`, não autentica na corretora e não inicia processos de negociação. O processo já existente de `robo.py` consulta o relay somente quando configurado e habilitado explicitamente. `CONTA = "PRACTICE"` é obrigatório; `EXECUTAR_ORDENS` continua sendo autorização independente no servidor.

Todos os endpoints `/api/` exigem HTTPS e retornam `Cache-Control: no-store`. Leituras usam `MONITOR_API_TOKEN`; somente o robô recebe `MONITOR_ROBOT_TOKEN`; alterações usam `MONITOR_CONTROL_TOKEN`. Os três valores precisam ser diferentes e não são incluídos em respostas ou logs. O APK recebe tokens do monitor, nunca e-mail ou senha IQ Option. Clientes permitem somente `https://baskaraido-monitor-test.onrender.com`, sem porta alternativa, query, credenciais na URL ou redirecionamentos.

## Persistência e confirmação

Configure `MONITOR_CONTROL_DB` como caminho absoluto de um arquivo SQLite em armazenamento persistente do **monitor isolado**. Diretório e arquivo precisam ser acessíveis ao usuário do processo. Ausência de configuração, falha de armazenamento ou tokens incompletos bloqueiam controles com HTTP 503. Não há fallback de escrita para memória. O arquivo guarda configurações, revisões, IDs de comandos, sequência de telemetria e últimos ACKs. Deve ser preservado em reinícios; um arquivo novo começa pausado e sem confirmação.

`GET /api/v1/practice/settings` retorna as cinco faixas desejadas, `revision`, `config_id` SHA-256, `persistencia: "sqlite"`, `pending` e `aplicada_no_robo`. `effective_config` contém somente a configuração confirmada pela telemetria recente do robô; pode diferir da desejada enquanto uma alteração estiver pendente. Os limites inteiros das cinco faixas devem cobrir 0–100 em ordem, sem lacunas ou sobreposição; Gales 0–9 e multiplicador 1–10. Os padrões são 0–49/4, 50–59/3, 60–69/2, 70–79/1, 80–100/0, todos ×2.

`PUT /api/v1/practice/settings` recebe exatamente `schema_version: 1`, `conta: "PRACTICE"`, `enabled`, `faixas`, `expected_revision`, `expected_config_id`, `command_id` UUID e `expires_at` UTC. O prazo deve estar no futuro e até 120 segundos. `GET /api/v1/practice/faixas` é o alias de compatibilidade; toda escrita nesse alias passa pelas mesmas validações, token de controle e persistência. Sem DB, somente a leitura legada volátil permanece disponível.

`GET /api/v1/practice/control` retorna `revision`, `command_id`, `desired_paused`, `expires_at`, `target_session`, `confirmed`, `pending`, `robot_online`, `authorized`, `paused`, `last_paused`, `applied_revision` e `lease_expires_at`. `paused` só representa o comando confirmado; `last_paused` pode descrever o último estado efetivo de uma revisão anterior. ACKs ficam recentes por no máximo 45 segundos. Cada consulta produz uma licença curta de controle de 30 segundos para o robô, que pausa novas entradas se não a renovar.

`PUT /api/v1/practice/control` recebe exatamente `schema_version: 1`, `conta: "PRACTICE"`, `action: "pause_new_entries"` ou `"resume_new_entries"`, `expected_revision`, `command_id` UUID e `expires_at` UTC. Resume exige autorização prévia e telemetria recente do robô. O comando é vinculado a `target_session`: uma nova sessão do robô invalida o resume persistido, volta o desejo a pausa e exige liberação explícita posterior. O APK não altera `EXECUTAR_ORDENS`.

PUTs aceitos retornam HTTP 202, que significa **recebido**, nunca confirmação efetiva. Revisões conflitantes e IDs repetidos retornam HTTP 409; entradas inválidas/expiradas, HTTP 400. O SQLite verifica revisão e registra o UUID numa única transação. Um retry consulta o estado em vez de reenviar cegamente. Uma falha HTTP ou timeout deixa o estado desconhecido no APK.

## Confirmação pelo processo existente

`PUT /api/v1/practice/telemetry` aceita somente o token do robô, PRACTICE, as dez bancas, contagens globais consistentes, sessão UUID, sequência crescente e timestamp válido. Sequências persistidas impedem replay após reinício do monitor. Campos opcionais permitidos incluem conexão IQ, saldo PRACTICE, ativos, operações, estados das bancas, Gales/multiplicador e gestão da recuperação; dados arbitrários e credenciais são removidos.

O robô publica `control_ack: {revision, command_id, paused, authorized}` somente depois de aplicar o estado sob o mesmo bloqueio que admite novas operações. `control_ack: null`, `control_error` ou ausência de confirmação invalidam imediatamente o ACK anterior. Para configurações, `config_revision` e `config_id` precisam coincidir com o snapshot efetivo. Uma telemetria posterior que não confirma a configuração anterior deixa o estado pendente. Operações abertas preservam o snapshot; a pausa bloqueia novas operações e permite concluir ordens e Gales existentes.

O robô precisa de `PRACTICE_CONTROL_ENABLED=true`, `PRACTICE_FAIXAS_URL=https://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas` e `MONITOR_ROBOT_TOKEN` contendo o token **do robô** (fallback legado: `PRACTICE_FAIXAS_TOKEN`). Sem opt-in, os controles não assumem autorização nem ativam negociação. O ledger financeiro do robô continua sendo estado do processo: um reinício exige reconciliar operações pendentes e prejuízo com a IQ Option antes de autorizar novas entradas. Recuperação usa 100% do prejuízo líquido, payout e saldo disponível; não garante recuperação.

## Configuração ainda necessária no servidor

Este trabalho não faz deploy e não modifica Render principal, Telegram ou `main`. É necessário instalar esta revisão no processo PRACTICE existente e no monitor isolado, configurar os três tokens, o SQLite persistente e o transporte HTTPS, e comprovar um ciclo de comando/ACK com o robô real. Não iniciar um segundo worker.

Se TLS terminar em um proxy, o servidor WSGI deve receber o esquema HTTPS de um proxy explicitamente confiável (por exemplo, a configuração de proxy confiável do Gunicorn na rede da implantação). A aplicação rejeita esquema HTTP com 426 e não confia por conta própria em `X-Forwarded-Proto` enviado pelo cliente. `PREFERRED_URL_SCHEME=https` controla apenas geração de URL, não altera o esquema de uma conexão recebida. Não habilite confiança irrestrita em cabeçalhos de encaminhamento.

Os testes usam IQ Option e transporte simulados. Compilação e assinatura do APK não comprovam conectividade real, saldo real observado nem execução de comandos por uma instância implantada do robô. A integração permanece sem confirmação em ambiente real até essa verificação.
