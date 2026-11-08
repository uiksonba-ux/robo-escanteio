# Baskaraido Trader PRO — painel de operações

O painel mobile é somente leitura e acompanha o processo Deriv DEMO existente. Não envia ordens, não altera gerenciamento, não acessa nem armazena o token da Deriv ou credenciais do Telegram.

## Abrir e instalar

Com esta alteração implantada no serviço existente, abra `https://robo-escanteio.onrender.com/app` no Chrome Android e escolha **Adicionar à tela inicial**. O painel também pode ser aberto no navegador de computador.

## Acesso

O serviço precisa ter `MONITOR_API_TOKEN` configurado. No painel, informe esse token de leitura; ele fica somente na sessão atual do navegador e é enviado exclusivamente ao endpoint HTTPS `/api/v1/monitor`. O app não guarda o token em armazenamento permanente.

O endpoint responde apenas à autorização de leitura já implementada no robô. Não use `DERIV_TOKEN`, `TELEGRAM_TOKEN` ou qualquer token de controle no aplicativo.

## Dados exibidos

- Saldo DEMO informado pelo processo do robô, resultado líquido, assertividade global e WIN/LOSS.
- Estado, assertividade, recuperação, gales e P&L de cada banca.
- Até 100 operações encerradas mais recentes, incluindo banca, ativo, direção, duração, resultado líquido e nível final.
- Atualização automática a cada 10 segundos, somente enquanto o app estiver aberto.

O histórico, o P&L e as estatísticas continuam voláteis no processo atual e são reiniciados quando o robô reinicia. O painel indica a hora da última atualização; ele não é um ledger financeiro persistente e não substitui o extrato da Deriv.

## Instalação no serviço

A branch `feature/deriv-operations-monitor-app` não está conectada ao auto-deploy do Render, que continua apontando para `main`. Não faça merge nem deploy antes da validação funcional e da revisão.
