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

## Identificar DERIV_DEMO_ACCOUNT_ID pelo GitHub Actions

A descoberta é isolada e somente leitura: executa um único
`GET https://api.derivws.com/trading/v1/options/accounts`. Não executa o
`preflight.py`, não cria OTP, não inicia WebSocket e não envia ordens.

1. Em [Settings > Secrets and variables > Actions](https://github.com/uiksonba-ux/robo-escanteio/settings/secrets/actions),
   use **New repository secret** para cadastrar `DERIV_APP_ID` (App ID da aplicação
   Deriv) e `DERIV_TOKEN` (token autorizado a consultar suas contas Options).
   Não cole credenciais no código, em logs ou no chat. Secrets do Render não são
   disponibilizados automaticamente no GitHub Actions.
2. O workflow **Identificar conta Deriv DEMO (sem ordens)** roda por push somente
   na branch `feature/baskaraido-deriv-demo`, quando este diretório ou o workflow
   muda. Os testes offline precisam passar antes da validação dos Secrets e da
   consulta. Não é necessário Shell no Render.
3. Se uma execução falhar por Secrets ausentes, cadastre-os e abra **Actions >
   execução mais recente dessa branch > Re-run all jobs**. O gatilho manual
   `workflow_dispatch` depende de o workflow também existir na branch padrão;
   não altere `main` para habilitá-lo. Reexecutar o run da branch é suficiente.
4. Após sucesso, o resumo e o log da etapa de consulta mostram
   `DERIV_DEMO_ACCOUNT_ID=<account_id>` e confirmam `account_type=demo` e
   `status=active`. O identificador da conta será visível nos logs deste
   repositório público; tokens e App ID nunca são impressos pelo script.
5. Copie apenas o identificador confirmado para a configuração DEMO apropriada
   no Render quando for validá-la. Esta descoberta não altera variáveis, não
   reinicia serviços nem publica a integração.

O script interrompe sem selecionar conta se houver mais de uma DEMO (inclusive
inativa), nenhuma DEMO ativa, tipo ausente/desconhecido, dados malformados,
paginação incompleta, erro HTTP ou redirecionamento. Um
`DERIV_DEMO_ACCOUNT_ID` previamente configurado apenas confirma o resultado;
não permite escolher entre múltiplas DEMOs. A descoberta não precisa dessa
variável e o workflow não a fornece.

Testes locais sem credenciais e com HTTP simulado:

```sh
python -m pip install 'requests==2.32.5'
python -m unittest discover -s deriv-demo -p 'test_*.py' -v
```
