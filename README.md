# canal.yt – Analisador/Downloader de Vídeos

## Como rodar localmente

```bash
# Suba backend
docker build -t canalyt .
docker run -e APP_DOMAIN="https://canal.yt" -e TURNSTILE_SECRET="YOUR_SECRET" -p 8000:8000 canalyt

# Frontend: basta abrir frontend/index.html
```

## Deploy no Coolify

1. Configure domínio e SSL (Let’s Encrypt).
2. No painel Coolify, crie app com Dockerfile (porta 8000).
3. Set APP_DOMAIN e TURNSTILE_SECRET como variáveis de ambiente.
4. Suba o repositório com estes arquivos.

## Segurança

- Captcha obrigatório
- Rate limiting IP
- CORS fechado
- Referer check
- CSRF token (implementar na versão completa)
- Bloqueio de bots/curl
- robots.txt bloqueando indexação
- Termos de uso e log de aceite

## Legal

O uso é responsabilidade do usuário. Respeite os Termos do YouTube.
