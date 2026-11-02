# Baskaraido Trader PRO — plano de controle PRACTICE

## Estado atual
O APK consulta telemetria e altera faixas no monitor isolado; ainda **não** pode iniciar, pausar ou reconfigurar o robô. O robô já usa `iqoptionapi` e `CONTA = "PRACTICE"`, mas `EXECUTAR_ORDENS` é uma configuração de inicialização, não um comando remoto.

## Contrato proposto (a implementar e testar)
- `GET /api/v1/practice/control`: estado efetivo do robô, conectividade, revisão da configuração, origem e atualização da telemetria.
- `PUT /api/v1/practice/control`: comando `pause_new_entries` ou `resume_new_entries`, com revisão esperada, idempotência e autenticação separada do token de leitura.
- `GET /api/v1/practice/settings`: parâmetros atuais validados e revisados.
- `PUT /api/v1/practice/settings`: alterações para **novas** operações, com validação de valores, revisão esperada e confirmação explícita pelo robô.

## Invariantes obrigatórios
1. Nunca aceitar `REAL`, nunca receber senha IQ Option no Android e nunca executar ordens no monitor.
2. Controle de pausa consultado pelo **processo de robô já existente** antes de aceitar nova operação, sem interromper ordens abertas ou Gales em andamento.
3. Sem conectividade, token inválido, comando expirado ou estado desconhecido: não afirmar no app que a operação foi pausada/iniciada; mostrar status não confirmado.
4. Um botão 'iniciar' só libera novas entradas se o processo existente já estiver autorizado por `EXECUTAR_ORDENS`; não inicia outro worker, não ignora proteção e não ativa produção.
5. Tokens separados para aplicativo e robô, HTTPS, allowlist de host, sem redirects, logs sem segredos, rejeição de comandos repetidos/antigos.
6. Mudanças de estratégia e gestão aplicadas apenas em novos contextos; guardar snapshot por operação.
7. Testes unitários e integração simulada antes de compilar APK; nunca fazer deploy na `main`, Render principal ou Telegram.

## Próxima implementação
Criar modelo de estado e validação de configurações compartilhado, endpoints autenticados no monitor isolado, consumo pelo robô existente, controles Android e testes de ponta a ponta. A memória volátil do monitor exige estratégia de revisão/reconciliação antes de habilitar comandos reais.
