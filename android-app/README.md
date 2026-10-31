# Baskaraido Trader Android (PRACTICE)

Protótipo Android nativo somente leitura. O motor Python permanece no Render; nenhum arquivo de produção foi modificado.

## Compilar
Abra a pasta android-app no Android Studio com JDK 17 e Android SDK 35 e gere um APK de debug. Ainda não há pipeline de build/artefato publicado.

## Conexão
Informe a URL HTTPS do serviço Render. A tela faz GET / e apresenta o JSON retornado. O backend atual pode expor dados sensíveis em /; **não disponibilize o serviço publicamente sem autenticação e revisão de campos**. O protótipo não contém controles de início/parada, ordens ou credenciais IQ Option. Próximas etapas: endpoint /api/mobile/status com autenticação, resposta reduzida, testes instrumentados e CI Android.

## Segurança
Conta PRACTICE continua fixada no Python. Não inserir senhas ou tokens da corretora no app. Não publicar em produção antes de validação.
