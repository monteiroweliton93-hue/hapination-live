#!/usr/bin/env bash
# Script de referência: como criar a branch e fazer push se quiser fazer por conta própria.
# NÃO execute isto se o agente já tiver feito o push e aberto o PR.
# Uso: bash scripts/comandos_git_push.sh
set -euo pipefail

REPO="https://github.com/monteiroweliton93-hue/hapination-live.git"
BRANCH="feature/hapination-audit-completa"

echo "1. Garanta que está na pasta do projeto e com o remote configurado:"
echo "   git remote -v"
echo "   # se necessário: git remote add origin $REPO"

echo
echo "2. Criar a branch a partir da main atual:"
echo "   git checkout main && git pull origin main"
echo "   git checkout -b $BRANCH"

echo
echo "3. Adicionar e commitar as alterações:"
echo "   git add -A"
echo "   git commit -m \"Auditoria e refatoracao completa: modularizacao em cogs, creditos proporcionais, sessoes renovaveis, painel com botoes funcionais, videos 24h, admin, tickets, backups, manutencao\""

echo
echo "4. Fazer push da branch (NÃO na main):"
echo "   git push -u origin $BRANCH"
echo "   # na primeira vez, o GitHub pede autenticação. Use um Personal Access Token"
echo "   # com escopo 'repo' como senha. Revogue o token após o push."

echo
echo "5. Abrir Pull Request (link gerado no terminal após o push) ou via API:"
echo "   curl -X POST -H \"Authorization: token SEU_TOKEN\" \\"
echo "     https://api.github.com/repos/monteiroweliton93-hue/hapination-live/pulls \\"
echo "     -d '{\"title\":\"Auditoria e implementacao completa Hapination Live\",\"head\":\"$BRANCH\",\"base\":\"main\",\"body\":\"Ver RELATORIO_AUDITORIA.md e CHANGELOG.md\"}'"

echo
echo "6. Após revisar, faça merge do PR na main (squash recomendado) e atualize a FadeHost (ver GUIA_FADEHOST.md)."
