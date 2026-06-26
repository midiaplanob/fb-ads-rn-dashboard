# 📊 Painel de Gastos em Anúncios Políticos — Rio Grande do Norte

Dashboard automático que monitora, diariamente, quanto cada anunciante político está
gastando no Meta (Facebook/Instagram) com alcance no **Rio Grande do Norte**.

Dados coletados pela [Meta Ad Library Report](https://www.facebook.com/ads/library/report/?country=BR)
via scraping automatizado com Playwright + GitHub Actions.

---

## Estrutura do projeto

```
fb-ads-rn-dashboard/
├── scraper/
│   ├── requirements.txt       # dependências Python
│   └── scrape_report.py       # coleta os dados e atualiza history.json
├── .github/workflows/
│   └── scrape.yml             # roda o scraper todo dia (09h BRT)
├── data/
│   ├── history.json           # histórico real (começa vazio)
│   └── history.example.json   # dados fictícios para preview do layout
└── site/
    └── index.html             # dashboard (HTML+JS puro, sem build)
```

---

## Deploy — passo a passo

### 1. Permissões do GitHub Actions

No repositório: **Settings → Actions → General → Workflow permissions**
→ selecione **"Read and write permissions"** → salvar.

*(Necessário para o robô poder commitar o `history.json` atualizado.)*

### 2. Ativar GitHub Pages

**Settings → Pages → Build and deployment**
- Source: **Deploy from a branch**
- Branch: `main` / pasta: `/site`
- Salvar → aguardar ~1 min → o URL aparece no topo da página.

### 3. Rodar o scraper pela primeira vez

**Actions → "Coletar gastos RN" → Run workflow → Run workflow**

- ✅ Bolinha verde = funcionou. Abra `data/history.json` para confirmar.
- ❌ Bolinha vermelha = abra o log e procure a mensagem de erro.
  Provavelmente os seletores da página mudaram (ver seção abaixo).

Após o primeiro sucesso, o scraper roda automaticamente todo dia às 09h BRT.

---

## Se o scraper falhar (seletores desatualizados)

A Meta muda o layout da página ocasionalmente. Quando isso acontece:

1. Execute localmente com o navegador visível:
   ```bash
   pip install -r scraper/requirements.txt
   playwright install chromium
   python scraper/scrape_report.py --debug
   ```
2. Quando o navegador abrir, inspecione o elemento que precisa ser clicado
   (botão direito → "Inspecionar").
3. Adicione o seletor correto no **início** das listas
   `REGION_SELECTORS` ou `DOWNLOAD_SELECTORS` em `scrape_report.py`.
4. Faça commit e push — o Actions usará o seletor novo.

---

## Desenvolvimento local

```bash
# Clonar
git clone https://github.com/midiaplanob/fb-ads-rn-dashboard.git
cd fb-ads-rn-dashboard

# Instalar dependências
pip install -r scraper/requirements.txt
playwright install chromium

# Rodar o scraper (headless)
python scraper/scrape_report.py

# Ver o dashboard localmente
# Abra site/index.html no navegador
# (usa dados de exemplo por padrão; clique em "Ver dados reais" para
#  buscar o history.json diretamente do GitHub raw)
```

---

## Alternativa via API oficial

Para uma solução mais robusta a longo prazo, a Meta oferece a
[Ad Library API](https://www.facebook.com/ads/library/api/) com o campo
`delivery_by_region`, que retorna dados por estado sem precisar de automação
de navegador. Porém, exige:
- Conta de desenvolvedor Meta aprovada
- Verificação de identidade
- Token de acesso com permissão `ads_read`

---

## Licença

MIT — use livremente.
