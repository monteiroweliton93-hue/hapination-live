# Hapination Live — protótipo Discord

Protótipo educativo para gerir uploads e sessões **simuladas** no Discord.
Esta versão NÃO transmite para TikTok, não contorna restrições de plataforma e não processa dinheiro real.

## Requisitos
- Python 3.10+
- Uma aplicação/bot criado no Discord Developer Portal
- Permissões do bot: `applications.commands` e `bot`; permissão para enviar mensagens e usar comandos de aplicação

## Instalação
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS/Termux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
Edite `.env` e coloque o token do bot. Nunca compartilhe esse token nem publique o `.env`.

## Executar
```bash
python bot.py
```
Convide o bot para seu servidor com os escopos `bot` e `applications.commands`.

## Comandos
- `/painel`
- `/saldo`
- `/adicionar_creditos_teste` (administrador; créditos fictícios)
- `/enviar_video`
- `/meus_videos`
- `/iniciar video_id` — inicia uma sessão válida por 24 horas
- `/renovar` — renova uma sessão anterior por mais 24 horas, usando outros R$ 0,50 fictícios
- `/encerrar`

## Limites desta versão
- Cada cliente pode ter uma sessão simulada ativa por vez.
- Cada sessão dura 24 horas; depois expira automaticamente. Renovar custa mais R$ 0,50 em créditos fictícios, ou o cliente pode iniciar uma nova sessão.
- Vários usuários podem manter sessões simuladas ao mesmo tempo.
- Vídeos ficam localmente em `data/uploads/`; proteja e apague os arquivos quando não forem mais necessários.
- O custo de R$ 0,50 é apenas uma simulação de créditos, sem cobrança.
- Para qualquer transmissão real, use apenas fluxos de ingestão e APIs oficialmente autorizados pela plataforma, respeite os termos, direitos autorais, privacidade e requisitos de idade.
