# Baskaraido Trader PRO — Android PRACTICE

Aplicativo nativo de acompanhamento e configuração das cinco faixas de assertividade.
O app não inicia robôs, não envia ordens e não recebe credenciais da IQ Option ou do Telegram.
O design e o arquivo `assets/wallpaper.png` foram preservados.

## APK e validação

Na branch `feature/baskaraido-trader-android-practice`, o workflow
[Android APK (PRACTICE)](https://github.com/uiksonba-ux/robo-escanteio/actions/workflows/android-apk.yml)
executa os testes Python de segurança, integração e regressão antes de executar
`testDebugUnitTest lintDebug assembleDebug` com JDK 17, SDK 35 e Gradle 8.9.

O artefato `Baskaraido-Trader-PRO-PRACTICE-<commit>` contém um APK instalável
assinado com chave de debug, o commit de origem e SHA-256. A retenção é de 30 dias.
Não é uma assinatura de distribuição de loja. O workflow não realiza deploy.
Baixe o ZIP em **Actions → execução aprovada → Artifacts**.

## Integração isolada

O único endereço aceito pelo app e pelo cliente de sincronização do robô é
`https://baskaraido-monitor-test.onrender.com`. TLS é obrigatório e redirects são
recusados. O token do app permanece apenas em memória; informe-o novamente após
encerrar o processo. Não use tokens da corretora ou do Telegram.

As variáveis abaixo devem existir somente no ambiente de testes autorizado.
Este desenvolvimento não modifica nenhum serviço Render, suas variáveis, ou Telegram.

| Componente | Variável | Uso |
| --- | --- | --- |
| Monitor de testes | `MONITOR_API_TOKEN` | Token exclusivo do app: ler métricas e ler/gravar faixas |
| Monitor e robô PRACTICE já existente | `MONITOR_ROBOT_TOKEN` | Outro token: robô lê faixas e publica telemetria |
| Robô PRACTICE já existente | `PRACTICE_FAIXAS_URL` | URL completa `https://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas` |
| Robô, compatibilidade | `PRACTICE_FAIXAS_TOKEN` | Alternativa legada quando `MONITOR_ROBOT_TOKEN` não existe |

Use valores distintos para os dois tokens. Nenhum valor real deve entrar no Git,
APK ou logs. A ausência de autenticação impede o acesso à API. Não inicie outro
processo do robô para usar esta integração.

O serviço isolado é `monitor_test_app:app`. Configurações e telemetria ficam em
memória: utilize **um único worker**. Reiniciar esse serviço restaura as faixas
padrão e remove a telemetria recebida. O hash do conteúdo e a revisão impedem
confirmações incorretas após reinício. Não trate este armazenamento como durável
nem como compatível com vários workers. O `Procfile` existente não foi alterado.

## Fluxo

1. No app, informe o endereço isolado e o token de monitoramento e toque em
   **TESTAR CONEXÃO**. O app consulta métricas e a revisão atual das faixas.
2. Edite e salve as faixas localmente. Toque em **ENVIAR FAIXAS AO AMBIENTE DE
   TESTES** para sincronizar. Uma alteração concorrente retorna HTTP 409 e exige
   recarregar a revisão; o app preserva edições locais enquanto consulta o servidor.
3. O supervisor já existente do robô consulta as faixas e publica métricas a cada
   15 segundos; também consulta antes de uma operação inicial. A confirmação do
   robô identifica a revisão disponível para novas operações.
4. O contexto de cada operação fixa Gales, multiplicador e versão ao aceitar a
   ordem inicial. Alterar faixas não altera os Gales de uma operação em andamento.
5. O app atualiza WIN, LOSS, assertividade e as dez bancas a cada cinco segundos,
   enquanto está em primeiro plano. Falhas e telemetria antiga são sinalizadas.

O robô conserva a última configuração validada em falhas temporárias. Sem
configuração remota válida, continua usando as regras já existentes na branch.
Desativar faixas remotas também usa esse padrão do robô. Não implica interromper
operações ou eliminar Gales. Saldo individual de banca indisponível é mostrado
como “—”; nenhum saldo é inventado.

## Endpoints

| Método e rota | Autorização |
| --- | --- |
| `GET /health` | Diagnóstico sem dados privados |
| `GET /api/v1/monitor` | Token do app |
| `GET /api/v1/practice/faixas` | Token do app ou do robô |
| `PUT /api/v1/practice/faixas` | Somente token do app; conta PRACTICE, cinco faixas e controle de revisão |
| `PUT /api/v1/practice/telemetry` | Somente token do robô; conta PRACTICE, dez bancas e sequência por sessão |

O modo legado de upstream é opcional e exige `MONITOR_UPSTREAM_URL`,
`MONITOR_UPSTREAM_TOKEN` e o host explicitamente listado em
`MONITOR_UPSTREAM_ALLOWED_HOSTS` (separação por vírgulas). Não é necessário no
fluxo de publicação de telemetria. Redirects, endereços privados e respostas fora
do contrato são recusados.

## Testes locais

```sh
python -m pip install Flask==3.1.0
python -m unittest discover -v -s . -p 'test_*.py'
python -m unittest discover -v -s tests -p 'test_*.py'
cd android-app
gradle --no-daemon testDebugUnitTest lintDebug assembleDebug
```

Os testes isolam IQ Option, Telegram, rede e a inicialização do worker.
Eles validam o contrato e as mudanças de estado sem executar operações reais.
A validação com a conta PRACTICE conectada depende da configuração autorizada do
ambiente de testes; gerar o APK não ativa essa integração nem realiza deploy.

