"""Inicia o robô DEMO dentro do processo que atende o painel/API.

Gunicorn carrega este arquivo por padrão quando ele existe na raiz do serviço.
O hook roda depois que o worker HTTP carregou a aplicação, inclusive quando
o módulo foi pré-carregado no processo master.
"""


def post_worker_init(worker):
    from robo import iniciar_worker_em_processo_web

    iniciar_worker_em_processo_web()
