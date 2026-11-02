# Baskaraido Trader PRO — Android PRACTICE

Aplicativo nativo para acompanhar o robô existente, pausar/liberar novas entradas e
editar as cinco faixas. O app não inicia processos, não envia ordens e não recebe
e-mail ou senha da IQ Option, nem credenciais do Telegram. `CONTA = "PRACTICE"`
permanece obrigatória no robô. O design, as dez bancas e
`app/src/main/assets/wallpaper.png` foram preservados.

## APK e validação

Na branch `feature/baskaraido-trader-android-practice`, o workflow
[Android APK (PRACTICE)](https://github.com/uiksonba-ux/robo-escanteio/actions/workflows/android-apk.yml)
executa os testes Python de segurança, comandos, recuperação, integração e
regressão antes de `testDebugUnitTest lintDebug assembleDebug`, com JDK 17,
SDK 35 e Gradle 8.9. Os testes substituem IQ Option, Telegram e rede.

O artefato `Baskaraido-Trader-PRO-PRACTICE-<commit>` contém
`Baskaraido-Trader-PRO-PRACTICE.apk`, `COMMIT.txt`, `SHA256SUMS.txt`,
`SIGNATURE.txt` e `BUILD.txt`. O APK tem assinatura Android de desenvolvimento
(debug), verificada por `apksigner`; não é uma assinatura de distribuição de loja.
Cada runner pode usar outra chave de debug, exigindo desinstalar a versão anterior.
A retenção dos artefatos e relatórios é de 30 dias. Baixe em
**Actions → execução concluída → Artifacts** e confira o commit antes de instalar.
Falhas de validação ou build impedem a publicação do APK.

Gerar ou instalar o APK não configura servidores e não comprova conexão real do
robô. Este trabalho não publica no Render, não altera o serviço principal
`robo-escanteio`, o `Procfile` ou Telegram e não ativa operações.

## Configuração do ambiente isolado

O endereço permitido pelo app e pelo cliente do robô é
`https://baskaraido-monitor-test.onrender.com`. TLS é obrigatório; redirects,
hosts alternativos, portas e credenciais na URL são recusados. O monitor é um
relay de estado: nunca importa `robo.py` nem cria um segundo robô.

Defina três tokens distintos no servidor, fora do Git, APK e logs:

| Componente | Variável | Permissão |
| --- | --- | --- |
| Monitor e app | `MONITOR_API_TOKEN` | Leitura de métricas, configurações e estado |
| Monitor e app | `MONITOR_CONTROL_TOKEN` | Gravação de configurações e comandos |
| Monitor e robô PRACTICE existente | `MONITOR_ROBOT_TOKEN` | Leitura de configurações/controle e envio de telemetria/ACK |
| Somente monitor | `MONITOR_CONTROL_DB` | Caminho absoluto do SQLite em armazenamento persistente, por exemplo `/data/practice-control.sqlite3` |
| Somente robô existente | `PRACTICE_FAIXAS_URL` | `https://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas` |
| Somente robô existente | `PRACTICE_CONTROL_ENABLED=true` | Adesão explícita à pausa/liberação remota; padrão `false` |
| Somente robô, compatibilidade | `PRACTICE_FAIXAS_TOKEN` | Alternativa legada ao `MONITOR_ROBOT_TOKEN`; não deve ser usada no APK |

Sem os três tokens distintos, os endpoints de controle/configuração recusam o
acesso. O app recebe apenas os dois tokens do app, mantidos em memória durante a
sessão. Nunca informe o token do robô, IQ Option ou Telegram no aplicativo.

O arquivo SQLite deve ter diretório existente, permissões de escrita e volume
persistente. Ausência de caminho, `:memory:` ou falha de armazenamento retorna
HTTP 503; o monitor não substitui comandos duráveis por memória temporária.
Use um único worker do monitor, por exemplo
`gunicorn --workers 1 --threads 4 monitor_test_app:app`, pois a telemetria completa
continua em memória. O SQLite preserva configurações, revisões, IDs dos comandos,
controle desejado e confirmações, inclusive após reiniciar o monitor.

O backend precisa reconhecer a requisição como HTTPS. Se o TLS terminar num proxy,
configure a aplicação para confiar somente no proxy autorizado e para receber o
esquema HTTPS correto. Não exponha um backend que confie em cabeçalhos encaminhados
de clientes arbitrários. O código não configura infraestrutura ou esse limite de
confiança automaticamente.

A flag de controle só deve ser habilitada no robô PRACTICE já existente após essa
configuração e verificação. Ela começa com novas entradas pausadas e exige nova
liberação para a sessão atual após reiniciar o processo. A autorização de
inicialização `EXECUTAR_ORDENS` permanece independente: o APK nunca a altera.
Sem a flag, permanece o comportamento legado do servidor, incluindo sua
autorização existente; o APK não pode confirmar pausa/liberação nesse modo.

## Uso e confirmação

1. Abra o app, informe a URL isolada e os tokens de leitura e controle e toque em
   **TESTAR CONEXÃO**. O app consulta métricas, configurações e estado do controle.
2. Edite as faixas e use **SALVAR RASCUNHO LOCAL** para guardar somente a edição
   no aparelho. **SALVAR FAIXAS NO ROBÔ PRACTICE** envia o rascunho com a revisão
   e o hash carregados do servidor.
3. Aguarde a confirmação efetiva do robô. HTTP 202 significa recebido pelo monitor;
   não significa aplicado. O app mostra configuração pendente até receber ACK da
   mesma revisão e hash.
4. **PAUSAR NOVAS ENTRADAS** solicita pausa das próximas operações. Ordens abertas
   e Gales em andamento continuam com o snapshot aceito na entrada.
5. **LIBERAR NOVAS ENTRADAS** só fica disponível com confirmação recente de um robô
   já autorizado por `EXECUTAR_ORDENS`. A liberação é vinculada à sessão atual;
   não cria processo, não muda conta e não sobrepõe a autorização do servidor.
6. Timeout, HTTP 401/409/503, resposta inválida ou desconexão deixam comando e
   estado sem confirmação. Consulte novamente para reconciliar antes de reenviar;
   o app não transforma uma falha de comunicação em sucesso.

A consulta periódica ocorre em primeiro plano. O supervisor existente publica
telemetria e ACK a cada 15 segundos e consulta o controle antes de novas entradas.
O lease de controle dura no máximo 30 segundos; falha de consulta ou lease vencido
impede novas entradas quando a extensão está habilitada. Confirmações sem
atualização por mais de 45 segundos deixam de estabelecer estado efetivo.
Uma conexão HTTP com o monitor não comprova conexão com a IQ Option: essa conexão,
o saldo demonstrativo e a origem/idade dos dados aparecem separadamente.

## Faixas, recuperação e dados exibidos

| Assertividade global | Gales padrão | Multiplicador padrão |
| --- | --- | --- |
| 0–49% | 4 | ×2 |
| 50–59% | 3 | ×2 |
| 60–69% | 2 | ×2 |
| 70–79% | 1 | ×2 |
| 80–100% | 0 | ×2 |

Os limites mínimo/máximo dos cinco intervalos, Gales (0 a 9) e multiplicador
(1 a 10) são personalizáveis. Os limites devem ser inteiros, em ordem crescente,
com mínimo menor ou igual ao máximo em cada faixa. A primeira começa em 0, a
última termina em 100 e cada mínimo seguinte é o máximo anterior mais 1; assim,
as cinco faixas cobrem 0–100% sem lacunas ou sobreposições. Os valores da tabela
continuam sendo o padrão. O cálculo usa WIN/LOSS global das dez bancas no ciclo existente,
independentemente do valor monetário da entrada. Cada operação fixa Gales,
multiplicador e revisão até sua conclusão. Desabilitar faixas remotas usa os
padrões do robô. Em falhas temporárias de leitura de configurações, o robô conserva
a última configuração validada; isso não substitui o lease de controle.

`RECUPERACAO_PERCENTUAL = 1.0` define a tentativa de recuperar 100% do prejuízo
líquido global. Uma única operação elegível reserva a recuperação; resultados
líquidos e liquidações identificadas evitam reserva simultânea e contabilização
duplicada. Payout, saldo PRACTICE e ordens pendentes limitam o valor aceito.
Não há promessa de recuperação garantida.

O painel mostra conexão IQ Option, saldo PRACTICE, ativos, operações, WIN/LOSS,
assertividade, dez bancas, Gales, multiplicador, prejuízo acumulado, recuperação
planejada e estado. Saldo individual de banca sem fonte verificável aparece como
“—”; não é inventado.

**Limite de persistência financeira:** prejuízo, estatísticas, posições e ordens
do processo do robô continuam em memória. O SQLite do monitor não é um livro de
operações e não descobre ordens pendentes na IQ Option. Após reiniciar o robô,
mantenha novas entradas pausadas e reconcilie manualmente saldo, ordens e prejuízo
com a corretora antes de liberar a sessão. Não existe restauração financeira ou
reconciliação automática pós-reinício.

## Contrato HTTP

Todas as chamadas autenticadas usam `Authorization: Bearer <token>`.
O estado financeiro é publicado pelo processo existente, nunca executado no monitor.

| Método e rota | Token |
| --- | --- |
| `GET /health` | Sem dados privados |
| `GET /api/v1/monitor` | Leitura do app |
| `GET /api/v1/practice/control` | Leitura do app ou robô |
| `PUT /api/v1/practice/control` | Controle do app |
| `GET /api/v1/practice/settings` | Leitura do app ou robô |
| `PUT /api/v1/practice/settings` | Controle do app |
| `GET /api/v1/practice/faixas` | Leitura legada do app ou robô |
| `PUT /api/v1/practice/faixas` | Mesmo contrato durável de settings, token de controle |
| `PUT /api/v1/practice/telemetry` | Somente robô |

O corpo de controle contém exatamente `schema_version: 1`,
`conta: "PRACTICE"`, `action` (`pause_new_entries` ou `resume_new_entries`),
`expected_revision`, `command_id` e `expires_at`. Use revisão inteira positiva,
UUID canônico novo e prazo ISO 8601 com fuso, maior que agora e no máximo 120
segundos à frente.

O corpo de settings contém exatamente `schema_version`, `conta`, `enabled`,
`faixas`, `expected_revision`, `expected_config_id`, `command_id` e
`expires_at`. Cada faixa tem `min`, `max`, `gales` e `mult`; limites personalizados
devem obedecer à cobertura completa e contínua validada acima. O app, o monitor
e o leitor do robô recusam limites inválidos antes de aplicar a configuração.
`expected_config_id` é o hash retornado pelo GET, sem recalcular a partir de um
rascunho. Revisão ou hash divergentes e UUID repetido retornam HTTP 409.
Comandos expirados ou inválidos são recusados. Os IDs aceitos permanecem no SQLite.

O GET de controle separa `desired_paused` do estado efetivo `paused` e informa
`confirmed`, `pending`, `robot_online`, `authorized`, `revision`,
`command_id`, `lease_expires_at` e `error`. Sem ACK correspondente e recente,
`paused` é desconhecido. O robô confirma via telemetria autenticada,
`control_ack: {revision, command_id, paused, authorized}`.
A configuração é confirmada por `config_revision` e `config_id`; o GET de
settings retorna `aplicada_no_robo`, `pending` e `effective_config`.
Sequência por sessão e data da telemetria impedem reutilizar confirmação antiga.
Uma nova sessão invalida uma liberação anterior.

O upstream legado é opcional e exige `MONITOR_UPSTREAM_URL`,
`MONITOR_UPSTREAM_TOKEN` e `MONITOR_UPSTREAM_ALLOWED_HOSTS`; não é necessário
para o fluxo de publicação pelo robô.

## Testes locais

```sh
python -m pip install Flask==3.1.0
python -m compileall -q robo.py practice_band_config.py practice_control_store.py monitor_test_app.py
python -m unittest discover -v -s . -p 'test_*.py'
python -m unittest discover -v -s tests -p 'test_*.py'
cd android-app
gradle --no-daemon testDebugUnitTest lintDebug assembleDebug
```

Validação simulada não comprova integração real. A confirmação real depende de
configurar o monitor isolado e observar telemetria e ACK do robô PRACTICE existente,
sem criar outro worker e sem alterar o serviço principal.


