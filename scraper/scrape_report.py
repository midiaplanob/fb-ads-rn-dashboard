#!/usr/bin/env python3
"""
scrape_report.py — RN Ad Tracker
=================================
Coleta dados de gasto em anúncios políticos do Meta Ad Library para o
Rio Grande do Norte usando três estratégias em cascata:

  1. Interceptação de rede  — captura as chamadas GraphQL/API da própria página
  2. Extração DOM           — lê a tabela já renderizada no HTML
  3. Download CSV           — tenta clicar no botão de download
  4. Busca individual       — pesquisa cada candidato do candidates.json

Uso:
    python scraper/scrape_report.py           # headless (GitHub Actions)
    python scraper/scrape_report.py --debug   # abre navegador visível + screenshots
"""

import io, csv, json, re, sys, time
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Configurações ──────────────────────────────────────────────────────────
REPORT_URL  = "https://www.facebook.com/ads/library/report/?country=BR"
LIBRARY_URL = "https://www.facebook.com/ads/library/"
REGION_NAME = "Rio Grande do Norte"
TZ_BRT      = ZoneInfo("America/Sao_Paulo")
TIMEOUT_MS  = 60_000
DEBUG       = "--debug" in sys.argv
HEADLESS    = not DEBUG

ROOT_DIR        = Path(__file__).resolve().parent.parent
DATA_DIR        = ROOT_DIR / "data"
HISTORY_FILE    = DATA_DIR / "history.json"
DOWNLOAD_DIR    = DATA_DIR / "_downloads"
SCREENSHOT_DIR  = DATA_DIR / "_screenshots"
CANDIDATES_FILE = ROOT_DIR / "candidates.json"

COOKIE_SELECTORS = [
    "button[data-cookiebanner='accept_button']",
    "[data-testid='cookie-policy-manage-dialog-accept-button']",
    "button:has-text('Allow all cookies')",
    "button:has-text('Aceitar todos')",
    "button:has-text('Accept All')",
    "button:has-text('OK')",
    "[aria-label='Close']", "[aria-label='Fechar']",
]

REGION_SELECTORS = [
    "input[placeholder*='Rio' i]",
    "input[placeholder*='egião' i]",
    "input[placeholder*='egiao' i]",
    "input[placeholder*='egion' i]",
    "input[placeholder*='tado' i]",
    "input[placeholder*='tate' i]",
    "input[placeholder*='ocation' i]",
    "input[placeholder*='ocalidade' i]",
    "input[aria-label*='egion' i]",
    "input[aria-label*='tado' i]",
    "input[aria-label*='ocation' i]",
    "[data-testid*='region'] input",
    "[data-testid*='location'] input",
    "[data-testid*='filter'] input",
    "input[type='search']",
    "input[type='text']",
]

DOWNLOAD_SELECTORS = [
    "button[aria-label*='Download' i]", "button[aria-label*='Baixar' i]",
    "a[download]", "a[href*='.csv']",
    "button:has-text('Download')", "button:has-text('Baixar')",
    "button:has-text('Export')", "button:has-text('Exportar')",
    "[data-testid*='download']", "[data-testid*='export']",
    "button[aria-label*='csv' i]", "[role='button']:has-text('Download')",
]

# Chaves JSON conhecidas do Meta Ad Library (API GraphQL)
NAME_KEYS  = ['page_name','advertiser_name','name','entity_name','pageName','page_profile_name']
SPEND_KEYS = ['amount_spent','spend','total_spend','amount_spent_lower_bound',
              'spendLower','spend_lower','estimated_audience_size']
ADS_KEYS   = ['ad_count','num_ads','ads_count','adCount','number_of_ads','total_ads']
ID_KEYS    = ['page_id','id','pageId','entity_id','advertiser_id']
REGION_KEYS= ['region','delivery_by_region','byRegion','regions']


# ── Utilitários ────────────────────────────────────────────────────────────
def parse_spend(text: str) -> tuple[int, int]:
    t = re.sub(r"[^\d\-––a-zA-Z\s]", "", str(text)).strip()
    m = re.search(r"(\d[\d\s]*)\s*[-––]\s*(\d[\d\s]*)", t)
    if m:
        return int(re.sub(r"\s","",m.group(1))), int(re.sub(r"\s","",m.group(2)))
    m = re.search(r"(?:menos\s*de|less\s*than)\s+(\d[\d\s]*)", t, re.I)
    if m:
        hi = int(re.sub(r"\s","",m.group(1)))
        return 0, hi
    m = re.search(r"\d[\d\s]*", t)
    if m:
        n = int(re.sub(r"\s","",m.group()))
        return n, n
    return 0, 0


def screenshot(page, name: str):
    if DEBUG:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{name}.png"
        page.screenshot(path=str(path))
        print(f"  📸 Screenshot: {path}")


def dismiss_popups(page):
    for sel in COOKIE_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1_200):
                el.click()
                page.wait_for_timeout(600)
                break
        except Exception:
            pass


# ── Extração de JSON recursiva ─────────────────────────────────────────────
def extract_records(obj, depth=0) -> list[dict]:
    """Percorre recursivamente qualquer estrutura JSON procurando registros com nome+gasto."""
    records = []
    if depth > 25:
        return records

    if isinstance(obj, dict):
        name = next((obj[k] for k in NAME_KEYS if k in obj and obj[k]), None)
        spend_raw = next((obj[k] for k in SPEND_KEYS if k in obj), None)
        ads_raw   = next((obj[k] for k in ADS_KEYS  if k in obj), None)
        pid       = next((str(obj[k]) for k in ID_KEYS if k in obj), "")

        if name and spend_raw is not None:
            lo, hi = parse_spend(str(spend_raw))
            try:
                ads = int(re.sub(r"\D","", str(ads_raw or 0)) or 0)
            except ValueError:
                ads = 0
            records.append({"name": name, "page_id": pid,
                             "spend_lower": lo, "spend_upper": hi, "ads_count": ads})

        for v in obj.values():
            records.extend(extract_records(v, depth + 1))

    elif isinstance(obj, list):
        for item in obj:
            records.extend(extract_records(item, depth + 1))

    return records


def dedupe(records: list[dict]) -> list[dict]:
    seen = {}
    for r in records:
        key = r.get("page_id") or r["name"]
        if key not in seen or (r["spend_lower"] > seen[key]["spend_lower"]):
            seen[key] = r
    return list(seen.values())


# ── Estratégia 1: Interceptação de rede ───────────────────────────────────
def strategy_network(page) -> list[dict]:
    """Captura chamadas de API enquanto a página carrega e extrai dados."""
    print("\n[Estratégia 1] Interceptação de rede")

    captured = []

    def on_response(response):
        try:
            url = response.url
            if not any(kw in url for kw in
                       ['facebook.com/ads', 'facebook.com/api', 'graphql',
                        'ads_archive', 'ads/library']):
                return
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "image" in ct or "font" in ct or "css" in ct:
                return

            text = response.text()
            if not text or len(text) < 100:
                return

            # Remove o prefixo anti-hijacking do Facebook
            clean = re.sub(r"^for\s*\(;;\s*\);?\s*", "", text).strip()

            try:
                data = json.loads(clean)
                recs = extract_records(data)
                if recs:
                    captured.extend(recs)
                    print(f"  ✓ API capturada: {len(recs)} registros de {url[:80]}")
            except json.JSONDecodeError:
                # Tenta extrair fragmentos JSON
                for m in re.finditer(r'\{[^{}]{20,}\}', text):
                    try:
                        data = json.loads(m.group())
                        recs = extract_records(data)
                        captured.extend(recs)
                    except Exception:
                        pass
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(REPORT_URL, timeout=TIMEOUT_MS, wait_until="networkidle")
    except Exception:
        try:
            page.goto(REPORT_URL, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:
            print(f"  ✗ Erro ao carregar: {e}")
            return []

    dismiss_popups(page)
    page.wait_for_timeout(3_000)
    screenshot(page, "01_pagina_carregada")

    # Tenta selecionar a região para acionar mais chamadas de API
    _try_select_region(page)
    page.wait_for_timeout(4_000)
    screenshot(page, "02_apos_regiao")

    result = dedupe(captured)
    print(f"  → {len(result)} registros únicos capturados via rede")
    return result


def _try_select_region(page):
    """Tenta selecionar Rio Grande do Norte na UI (best-effort)."""
    for sel in REGION_SELECTORS:
        try:
            inp = page.locator(sel).first
            if not inp.is_visible(timeout=2_000):
                continue
            # Verifica se é um campo de texto (não hidden ou submit)
            tag = inp.evaluate("el => el.tagName").lower()
            typ = inp.get_attribute("type") or "text"
            if typ in ("hidden", "submit", "checkbox", "radio"):
                continue
            inp.click()
            inp.fill("")
            inp.type(REGION_NAME[:5], delay=90)
            page.wait_for_timeout(1_500)

            option = page.locator(
                f"li:has-text('{REGION_NAME}'),"
                f"[role='option']:has-text('{REGION_NAME}'),"
                f"div:has-text('{REGION_NAME}')"
            ).first
            if option.is_visible(timeout=3_000):
                option.click()
                print(f"  ✓ Região selecionada")
                return True
            inp.press("Enter")
            print(f"  ✓ Região confirmada via Enter")
            return True
        except Exception:
            continue
    print("  ⚠ Região não selecionada (continua sem filtro)")
    return False


# ── Estratégia 2: Extração DOM ─────────────────────────────────────────────
def strategy_dom(page) -> list[dict]:
    """Lê a tabela diretamente do DOM renderizado."""
    print("\n[Estratégia 2] Extração DOM")
    try:
        records = page.evaluate("""
        () => {
            const results = [];
            const NAME_PAT  = /candidato|anunciante|página|page|nome/i;
            const SPEND_PAT = /gasto|spend|valor|amount|R\\$/i;

            // Tenta [role="row"] primeiro
            const rows = document.querySelectorAll('[role="row"]');
            rows.forEach(row => {
                const cells = [...row.querySelectorAll('[role="cell"], td')];
                if (cells.length < 2) return;
                const name = cells[0]?.textContent?.trim();
                const spend = cells[1]?.textContent?.trim();
                const ads   = cells[2]?.textContent?.trim() || '0';
                if (name && spend && name.length > 2 && /\\d/.test(spend))
                    results.push({ name, spend, ads });
            });

            // Fallback: tr > td
            if (!results.length) {
                document.querySelectorAll('tr').forEach(tr => {
                    const tds = [...tr.querySelectorAll('td')];
                    if (tds.length < 2) return;
                    const name = tds[0]?.textContent?.trim();
                    const spend = tds[1]?.textContent?.trim();
                    const ads   = tds[2]?.textContent?.trim() || '0';
                    if (name && spend && name.length > 2 && /\\d/.test(spend))
                        results.push({ name, spend, ads });
                });
            }

            return results;
        }
        """)

        parsed = []
        for r in (records or []):
            if not r.get("name"):
                continue
            lo, hi = parse_spend(r.get("spend", "0"))
            try:
                ads = int(re.sub(r"\D", "", r.get("ads", "0")) or 0)
            except ValueError:
                ads = 0
            parsed.append({"name": r["name"], "page_id": "",
                            "spend_lower": lo, "spend_upper": hi, "ads_count": ads})

        print(f"  → {len(parsed)} registros do DOM")
        return parsed
    except Exception as e:
        print(f"  ✗ Erro DOM: {e}")
        return []


# ── Estratégia 3: Download CSV ─────────────────────────────────────────────
def strategy_csv(page) -> list[dict]:
    """Tenta clicar no botão de download e parsear o CSV."""
    print("\n[Estratégia 3] Download CSV")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    for sel in DOWNLOAD_SELECTORS:
        try:
            btn = page.locator(sel).first
            if not btn.is_visible(timeout=2_000):
                continue
            print(f"  → Tentando: {sel!r}")
            with page.expect_download(timeout=25_000) as dl_info:
                btn.click()
            dl = dl_info.value
            path = DOWNLOAD_DIR / dl.suggested_filename
            dl.save_as(path)
            content = path.read_text(encoding="utf-8-sig", errors="replace")
            records = _parse_csv(content)
            print(f"  ✓ CSV baixado: {dl.suggested_filename} ({len(records)} registros)")
            return records
        except Exception as e:
            print(f"  ✗ {sel!r}: {e}")

    return []


def _parse_csv(content: str) -> list[dict]:
    lines = content.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if re.search(r"page.?name|nome.+p.+gina|advertiser", ln, re.I):
            start = i
            break
    try:
        reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    except Exception:
        return []

    results = []
    for row in reader:
        norm = {k.lower().strip().strip('"'): v.strip().strip('"')
                for k, v in row.items() if k}
        name = (norm.get("page name") or norm.get("page_name")
                or norm.get("nome da página") or norm.get("advertiser") or "")
        if not name:
            continue
        spend_raw = (norm.get("amount spent (brl)") or norm.get("amount spent")
                     or norm.get("valor gasto (brl)") or "0")
        ads_raw   = (norm.get("number of ads in library") or norm.get("number of ads")
                     or norm.get("número de anúncios") or "0")
        lo, hi = parse_spend(spend_raw)
        try:
            ads = int(re.sub(r"\D", "", ads_raw) or 0)
        except ValueError:
            ads = 0
        results.append({"name": name, "page_id": norm.get("page id",""),
                         "spend_lower": lo, "spend_upper": hi, "ads_count": ads})
    return results


# ── Estratégia 4: Busca individual por candidato ───────────────────────────
def strategy_individual(ctx, candidates: list[dict]) -> list[dict]:
    """Abre a página de cada candidato no Ad Library e extrai dados."""
    if not candidates:
        print("\n[Estratégia 4] candidates.json vazio — pulando busca individual")
        return []

    print(f"\n[Estratégia 4] Busca individual — {len(candidates)} candidatos")
    results = []
    page = ctx.new_page()
    page.on("response", lambda r: None)  # reset listener

    captured = []
    def on_resp(response):
        try:
            if 'facebook.com' not in response.url: return
            text = response.text()
            clean = re.sub(r"^for\s*\(;;\s*\);?\s*", "", text).strip()
            try:
                data = json.loads(clean)
                captured.extend(extract_records(data))
            except Exception:
                pass
        except Exception:
            pass
    page.on("response", on_resp)

    for cand in candidates:
        name    = cand["name"]
        page_id = cand.get("page_id", "")
        captured.clear()
        print(f"  🔍 {name} ...")

        try:
            url = (f"{LIBRARY_URL}?active_status=all&ad_type=political_and_issue_ads"
                   f"&country=BR&id={page_id}" if page_id else
                   f"{LIBRARY_URL}?active_status=all&ad_type=political_and_issue_ads"
                   f"&country=BR&q={name.replace(' ', '+')}&search_type=page")

            page.goto(url, timeout=TIMEOUT_MS, wait_until="networkidle")
            dismiss_popups(page)
            page.wait_for_timeout(3_000)

            # Dados da interceptação
            recs = dedupe(captured)
            lo = recs[0]["spend_lower"] if recs else 0
            hi = recs[0]["spend_upper"] if recs else 0
            ads = recs[0]["ads_count"]  if recs else 0

            # Fallback: lê texto da página
            if not lo:
                text = page.inner_text("body")
                m = re.search(
                    r"R\$\s*([\d\.,]+)\s*[–\-]\s*R\$\s*([\d\.,]+)|"
                    r"Menos de R\$\s*([\d\.,]+)", text, re.I)
                if m:
                    lo, hi = parse_spend(m.group(0))
                m2 = re.search(r"(\d[\d\.]*)\s*(?:anúncio|resultado)", text, re.I)
                if m2:
                    ads = int(re.sub(r"\D","",m2.group(1)) or 0)

            results.append({"name": name, "page_id": page_id,
                             "spend_lower": lo, "spend_upper": hi,
                             "ads_count": ads, "party": cand.get("party","")})
            spend_str = f"R$ {lo:,}–{hi:,}" if lo else "sem dados"
            print(f"    ✓ {ads} anúncios | {spend_str}")

        except Exception as e:
            print(f"    ✗ Erro: {e}")
            results.append({"name": name, "page_id": page_id,
                             "spend_lower": 0, "spend_upper": 0, "ads_count": 0,
                             "party": cand.get("party","")})
    page.close()
    return results


# ── Candidatos monitorados ─────────────────────────────────────────────────
def load_candidates() -> list[dict]:
    if not CANDIDATES_FILE.exists():
        return []
    try:
        data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
        cands = [c for c in data.get("candidates", []) if c.get("name")]
        if cands:
            print(f"  📋 Candidatos monitorados: {', '.join(c['name'] for c in cands)}")
        return cands
    except Exception as e:
        print(f"  ⚠ Erro ao ler candidates.json: {e}")
        return []


def filter_by_candidates(advertisers: list[dict], candidates: list[dict]) -> list[dict]:
    if not candidates:
        return advertisers
    matched, not_found = [], []
    for cand in candidates:
        cn   = cand["name"].lower()
        cid  = str(cand.get("page_id",""))
        hit  = None
        if cid:
            hit = next((a for a in advertisers if str(a.get("page_id","")) == cid), None)
        if not hit:
            hit = next((a for a in advertisers
                        if cn in a["name"].lower() or a["name"].lower() in cn), None)
        if hit:
            enriched = dict(hit)
            if cand.get("party"):
                enriched["party"] = cand["party"]
            matched.append(enriched)
        else:
            not_found.append(cand["name"])
    if not_found:
        print(f"  ⚠ Sem dados: {', '.join(not_found)}")
    return matched if matched else advertisers


# ── Persistência ───────────────────────────────────────────────────────────
def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"last_updated": None, "snapshots": []}


def save_history(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def today_brt() -> str:
    return datetime.now(TZ_BRT).strftime("%Y-%m-%d")


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    today = today_brt()
    print(f"\n{'='*55}")
    print(f"  RN Ad Tracker — {today}  |  debug={DEBUG}")
    print(f"{'='*55}\n")

    candidates = load_candidates()
    advertisers = []
    source = "unknown"

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if DEBUG:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        # Estratégia 1: interceptação de rede
        net_results = strategy_network(page)
        if net_results:
            advertisers = filter_by_candidates(net_results, candidates)
            source = "network_intercept"

        # Estratégia 2: DOM (se rede não deu resultado)
        if not advertisers:
            dom_results = strategy_dom(page)
            if dom_results:
                advertisers = filter_by_candidates(dom_results, candidates)
                source = "dom_scrape"

        # Estratégia 3: CSV download
        if not advertisers:
            csv_results = strategy_csv(page)
            if csv_results:
                advertisers = filter_by_candidates(csv_results, candidates)
                source = "csv_download"

        # Estratégia 4: busca individual (fallback final)
        if not advertisers and candidates:
            advertisers = strategy_individual(ctx, candidates)
            source = "individual_search"

        browser.close()

    if not advertisers:
        print(
            "\n❌ Nenhum dado coletado por nenhuma estratégia.\n\n"
            "Próximos passos:\n"
            "  1. Adicione candidatos ao candidates.json para ativar a busca individual\n"
            "  2. Rode localmente com --debug para inspecionar o navegador:\n"
            "       python scraper/scrape_report.py --debug\n"
            "  3. Verifique os screenshots em data/_screenshots/\n"
        )
        sys.exit(1)

    advertisers.sort(key=lambda x: x["spend_lower"], reverse=True)

    history   = load_history()
    snapshots = [s for s in history.get("snapshots",[]) if s["date"] != today]
    snapshots.append({"date": today, "advertisers": advertisers, "source": source})
    snapshots = sorted(snapshots, key=lambda s: s["date"])[-365:]

    history["snapshots"]    = snapshots
    history["last_updated"] = datetime.now(TZ_BRT).isoformat()
    save_history(history)

    print(f"\n✅ {len(advertisers)} candidatos salvos em data/history.json  (fonte: {source})")


if __name__ == "__main__":
    main()
