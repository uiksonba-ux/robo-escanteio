# Baskaraido Trader PRO — Deriv DEMO (protótipo isolado)

Este diretório é uma migração experimental para a API oficial da Deriv, mantida na branch `feature/baskaraido-deriv-demo`. Não altera o `robo.py` nem o serviço Render existente.

## Estado
- [x] Documentação e configuração inicial, exclusivamente DEMO
- [ ] Validar símbolos e contratos disponíveis na conta/região
- [ ] Implementar autenticação OTP e WebSocket DEMO
- [ ] Adaptar indicadores, M1/M5/M15 e gestão de 10 bancas
- [ ] Testar reconexão, idempotência, confirmação de ordens e liquidação
- [ ] Habilitar ordens somente após validação e autorização expressa

## Fluxo oficial
1. Criar/usar conta de demonstração Deriv.
2. Gerar token pessoal (PAT) e App ID, mantendo-os em variáveis de ambiente, nunca no GitHub.
3. Obter URL WebSocket DEMO via `POST /trading/v1/options/accounts/{accountId}/otp`.
4. Consultar `active_symbols`, `contracts_for`, `ticks` e `ticks_history`.
5. Somente depois de validar contratos, adicionar `proposal`, `buy` e `proposal_open_contract`.

## Regras de segurança
- Endpoint `/ws/real` proibido no código.
- Sem fallback para conta real; falhas de autenticação ou disponibilidade bloqueiam operações.
- Sem ordens nesta fase. A gestão com martingale e recuperação não garante lucro e exige limites de risco.
- Preservar o robô IQ Option, Telegram, Render e branch `main` sem alterações.

Documentação: https://developers.deriv.com/docs/workflows/
