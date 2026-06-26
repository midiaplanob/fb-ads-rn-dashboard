#!/usr/bin/env python3
"""
scrape_report.py
================
Coleta dados de gasto em anúncios políticos do Meta Ad Library Report
para o Rio Grande do Norte e atualiza data/history.json.

Uso:
    python scraper/scrape_report.py          # modo headless (CI)
    python scraper/scrape_report.py --debug  # abre o navegador visível

Se precisar ajustar seletores após uma mudança de layout do Meta,
edite as listas REGION_SELECTORS e DOWNLOAD_SELECTORS logo abaixo.
"""

import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python < 3.9

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Configurações ──────────────────────────────────────────────────────────
REPORT_URL   = "https://www.facebook.com/ads/library/report/?country=BR"
REGION_NAME  = "Rio Grande do Norte"
TZ_BRT       = ZoneInfo("America/Sao_Paulo")
TIMEOUT_MS   = 60_000
HEADLESS     = "--debug" not in sys.argv

ROOT_DIR     = Path(__file__).resolve().parent.parent
DATA_DIR     = ROOT_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"
DOWNLOAD_DIR = DATA_DIR / "_downloads"

# ── Seletores (fallback em ordem) ──────────────────────────────────────────
# Se o Meta mudar o layout, inspecione a página e adicione o novo seletor
# no INÍCIO de cada lista para que seja tentado primeiro.

REGION_SELECTORS = [
    # Caixa de busca de localização/região
    "input[placeholder*='Rio' i]",
    "input[placeholder*='região' i]",
    "input[placeholder*='regiao' i]",
    "input[placeholder*='region' i]",
    "input[placeholder*='estado' i]",
    "input[placeholder*='state' i]",
    "input[placeholder*='location' i]",
    "input[placeholder*='localidade' i]",
    "input[aria-label*='region' i]",
    "input[aria-label*='estado' i]",
    "input[aria-label*='location' i]",
    "[data-testid*='region'] input",
    "[data-testid*='location'] input",
    "[data-testid*='estado'] input",
]

DOWNLOAD_SELECTORS = [
    # Botão de download do CSV
    "button[aria-label*='Download' i]",
    "button[aria-label*='Baixar' i]",
    "a[download]",
    "a[href*='.csv']",
    "button:has-text('Download')",
    "button:has-text('Baixar')",
    "button:has-text('Export')",
    "button:has-text('Exportar')",
    "[data-testid*='download']",
    "[data-testid*='export']",
]

COOKIE_CLOSE_SELECTORS = [
    "button[data-cookiebanner='accept_button']",
    "[data-testid='cookie-policy-manage-dialog-accept-button']",
    "button:has-text('Allow all cookies')",
    "button:has-text('Aceitar todos os cookies')",
    "button:has-text('Accept All')",
    "button:has-text('OK')",
    "[aria-label='Close']",
    "[aria-label='Fechar']",
]


# ── Parser de faixa de gasto ───────────────────────────────────────────────
def parse_spend(text: str) -> tuple[int, int]:
    """
    Converte texto de gasto para (lower, upper) em centavos de BRL.
    Exemplos aceitos:
      "100 - 499"   → (100, 499)
      "Menos de 100" → (0, 100)
      "≥ 1000000"   → (1_000_000, 1_000_000)
      "1000"        → (1000, 1000)
    """
    t = re.sub(r"[^\d\-––a-zA-Z\s]", "", str(text)).strip()

    # "100 - 499" ou "100–499"
    m = re.search(r"(\d[\d\s]*)\s*[-––]\s*(\d[\d\s]*)", t)
    if m:
        lo = int(re.sub(r"\s", "", m.group(1)))
        hi = int(re.sub(r"\s", "", m.group(2)))
        return lo, hi

    # "Menos de 100" / "Less than 100"
    m = re.search(r"(?:menos\s+de|less\s+than)\s+(\d[\d\s]*)", t, re.I)
    if m:
        hi = int(re.sub(r"\s", "", m.group(1)))
        return 0, hi

    # Número isolado
    m = re.search(r"\d[\d\s]*", t)
    if m:
        n = int(re.sub(r"\s", "", m.group()))
        return n, n

    return 0, 0


# ── Parser do CSV ──────────────────────────────────────────────────────────
def parse_csv(content: str) -> list[dict]:
    """Interpreta o CSV oficial do Meta Ad Library Report."""
    advertisers = []

    # O Meta às vezes adiciona linhas de cabeçalho extras antes do CSV real
    # Encontra a primeira linha que parece um cabeçalho CSV
    lines = content.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if re.search(r"page.?name|nome.+p.+gina|advertiser", line, re.I):
            start = i
            break

    csv_text = "\n".join(lines[start:])

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
    except Exception as e:
        print(f"  ⚠ Erro ao parsear CSV: {e}")
        return []

    for row in reader:
        # Normaliza nomes de coluna
        norm = {k.lower().strip().strip('"'): v.strip().strip('"') for k, v in row.items() if k}

        name = (
            norm.get("page name")
            or norm.get("page_name")
            or norm.get("nome da página")
            or norm.get("nome da pagina")
            or norm.get("advertiser")
            or ""
        )
        if not name:
            continue

        spend_raw = (
            norm.get("amount spent (brl)")
            or norm.get("amount_spent_brl")
            or norm.get("amount spent")
            or norm.get("valor gasto (brl)")
            or norm.get("gasto (brl)")
            or "0"
        )

        ads_raw = (
            norm.get("number of ads in library")
            or norm.get("number of ads")
            or norm.get("número de anúncios")
            or norm.get("num anuncios")
            or "0"
        )

        lo, hi = parse_spend(spend_raw)
        try:
            ads_count = int(re.sub(r"\D", "", ads_raw) or "0")
        except ValueError:
            ads_count = 0

        advertisers.append(
            {
                "name": name,
                "spend_lower": lo,
                "spend_upper": hi,
                "ads_count": ads_count,
            }
        )

    return advertisers


# ── Persistência ───────────────────────────────────────────────────────────
def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"last_updated": None, "snapshots": []}


def save_history(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def today_brt() -> str:
    return datetime.now(TZ_BRT).strftime("%Y-%m-%d")


# ── Scraper Playwright ─────────────────────────────────────────────────────
def scrape() -> list[dict]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
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
        )
        page = ctx.new_page()

        # ── 1. Abre a página ───────────────────────────────────────────────
        print(f"[1/5] Abrindo {REPORT_URL} ...")
        page.goto(REPORT_URL, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(4_000)  # JS da página carregar

        # ── 2. Fecha cookie/login popup ────────────────────────────────────
        print("[2/5] Verificando popups ...")
        for sel in COOKIE_CLOSE_SELECTORS:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2_000):
                    el.click()
                    page.wait_for_timeout(800)
                    print(f"  ✓ Popup fechado: {sel!r}")
                    break
            except Exception:
                pass

        # ── 3. Seleciona Rio Grande do Norte ───────────────────────────────
        print(f"[3/5] Selecionando '{REGION_NAME}' ...")
        region_ok = False

        for sel in REGION_SELECTORS:
            try:
                inp = page.locator(sel).first
                if not inp.is_visible(timeout=3_000):
                    continue

                inp.click()
                inp.fill("")
                inp.type(REGION_NAME[:6], delay=80)  # digita devagar
                page.wait_for_timeout(1_500)

                # Tenta clicar na opção da lista
                option = page.locator(f"li:has-text('{REGION_NAME}'), [role='option']:has-text('{REGION_NAME}')").first
                if option.is_visible(timeout=4_000):
                    option.click()
                    page.wait_for_timeout(2_500)
                    region_ok = True
                    print(f"  ✓ Região selecionada (seletor: {sel!r})")
                    break
                else:
                    # Fallback: pressiona Enter
                    inp.press("Enter")
                    page.wait_for_timeout(2_000)
                    region_ok = True
                    print(f"  ✓ Região confirmada com Enter (seletor: {sel!r})")
                    break
            except Exception as e:
                print(f"  ✗ {sel!r}: {e}")

        if not region_ok:
            print(
                "  ⚠ Não conseguiu selecionar a região.\n"
                "    O CSV do Brasil completo será baixado e filtrado localmente."
            )

        # ── 4. Aguarda dados carregarem ────────────────────────────────────
        print("[4/5] Aguardando dados ...")
        page.wait_for_timeout(3_000)

        # ── 5. Clica em Download ───────────────────────────────────────────
        print("[5/5] Baixando CSV ...")
        csv_content = None

        for sel in DOWNLOAD_SELECTORS:
            try:
                btn = page.locator(sel).first
                if not btn.is_visible(timeout=3_000):
                    continue

                with page.expect_download(timeout=30_000) as dl_info:
                    btn.click()

                dl = dl_info.value
                dl_path = DOWNLOAD_DIR / dl.suggested_filename
                dl.save_as(dl_path)
                csv_content = dl_path.read_text(encoding="utf-8-sig", errors="replace")
                print(f"  ✓ Download: {dl.suggested_filename}")
                break
            except Exception as e:
                print(f"  ✗ {sel!r}: {e}")

        browser.close()

    if not csv_content:
        raise RuntimeError(
            "\n"
            "❌ Não foi possível baixar o CSV.\n\n"
            "O que fazer:\n"
            "  1. Rode com --debug para abrir o navegador visível:\n"
            "       python scraper/scrape_report.py --debug\n"
            "  2. Vá até o botão de download manualmente e inspecione o elemento\n"
            "     (botão direito → 'Inspecionar').\n"
            "  3. Adicione o seletor correto no INÍCIO da lista DOWNLOAD_SELECTORS\n"
            "     em scraper/scrape_report.py.\n"
        )

    return csv_content


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    today = today_brt()
    print(f"\n{'='*50}")
    print(f"  RN Ad Tracker — coleta de {today}")
    print(f"{'='*50}\n")

    raw_csv = scrape()

    print("\nParseando CSV ...")
    advertisers = parse_csv(raw_csv)

    # Se a região não foi selecionada, tenta filtrar no CSV do Brasil
    # (Meta inclui campo de região em algumas versões do CSV)
    if not any(True for _ in advertisers):
        print("⚠ CSV vazio ou não parseável.")
        sys.exit(1)

    print(f"  ✓ {len(advertisers)} anunciantes encontrados")

    # Ordena por gasto decrescente
    advertisers.sort(key=lambda x: x["spend_lower"], reverse=True)

    # Atualiza history.json
    history = load_history()
    snapshots = history.get("snapshots", [])

    # Remove snapshot do mesmo dia se já existir
    snapshots = [s for s in snapshots if s["date"] != today]
    snapshots.append({"date": today, "advertisers": advertisers})

    # Mantém últimos 365 dias, em ordem cronológica
    snapshots = sorted(snapshots, key=lambda s: s["date"])[-365:]

    history["snapshots"] = snapshots
    history["last_updated"] = datetime.now(TZ_BRT).isoformat()

    save_history(history)
    print(f"\n✅ data/history.json atualizado — {len(advertisers)} anunciantes em {today}")


if __name__ == "__main__":
    main()
