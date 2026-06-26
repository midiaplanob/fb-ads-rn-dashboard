#!/usr/bin/env python3
"""
scrape_report.py
================
Coleta dados de gasto em anúncios políticos do Meta Ad Library para o
Rio Grande do Norte e atualiza data/history.json.

Estratégia dupla:
  1. Baixa o CSV do relatório do RN (mais completo, tem dados regionais)
  2. Fallback: busca cada candidato individualmente na Ad Library

Configuração de candidatos:
  Edite candidates.json na raiz do projeto para listar os perfis a monitorar.
  Se a lista estiver vazia, todos os anunciantes do RN são capturados.

Uso:
    python scraper/scrape_report.py          # modo headless (CI/GitHub Actions)
    python scraper/scrape_report.py --debug  # abre o navegador visível
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
REPORT_URL  = "https://www.facebook.com/ads/library/report/?country=BR"
LIBRARY_URL = "https://www.facebook.com/ads/library/"
REGION_NAME = "Rio Grande do Norte"
TZ_BRT      = ZoneInfo("America/Sao_Paulo")
TIMEOUT_MS  = 60_000
HEADLESS    = "--debug" not in sys.argv

ROOT_DIR     = Path(__file__).resolve().parent.parent
DATA_DIR     = ROOT_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"
DOWNLOAD_DIR = DATA_DIR / "_downloads"
CANDIDATES_FILE = ROOT_DIR / "candidates.json"

# ── Seletores (fallback em ordem) ──────────────────────────────────────────
REGION_SELECTORS = [
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
]

DOWNLOAD_SELECTORS = [
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


# ── Candidatos monitorados ─────────────────────────────────────────────────
def load_candidates() -> list[dict]:
    """Carrega a lista de candidatos do candidates.json."""
    if not CANDIDATES_FILE.exists():
        return []
    try:
        data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
        candidates = data.get("candidates", [])
        if candidates:
            names = [c["name"] for c in candidates]
            print(f"  📋 {len(candidates)} candidatos monitorados: {', '.join(names)}")
        return candidates
    except Exception as e:
        print(f"  ⚠ Erro ao ler candidates.json: {e}")
        return []


def filter_by_candidates(advertisers: list[dict], candidates: list[dict]) -> list[dict]:
    """
    Se candidates não é vazio, retorna apenas os anunciantes que correspondem.
    Usa correspondência parcial no nome (case-insensitive).
    Se nenhum candidato for encontrado nos dados, retorna todos (não oculta nada).
    """
    if not candidates:
        return advertisers  # Sem filtro: retorna todos

    tracked = []
    not_found = []

    for cand in candidates:
        cand_name = cand["name"].lower()
        cand_id   = str(cand.get("page_id", ""))
        match = None

        # Tenta por page_id primeiro (mais preciso)
        if cand_id:
            match = next((a for a in advertisers if str(a.get("page_id", "")) == cand_id), None)

        # Fallback: correspondência parcial no nome
        if not match:
            match = next(
                (a for a in advertisers if cand_name in a["name"].lower() or a["name"].lower() in cand_name),
                None
            )

        if match:
            # Enriquece com dados do candidates.json
            enriched = dict(match)
            if cand.get("party"):
                enriched["party"] = cand["party"]
            tracked.append(enriched)
        else:
            not_found.append(cand["name"])

    if not_found:
        print(f"  ⚠ Sem dados de gasto para: {', '.join(not_found)}")
        print("    (Podem não ter anunciado no RN no período, ou o nome difere do cadastrado)")

    # Se nenhum candidato foi encontrado, retorna todos para não perder dados
    return tracked if tracked else advertisers


# ── Parser de gasto ────────────────────────────────────────────────────────
def parse_spend(text: str) -> tuple[int, int]:
    t = re.sub(r"[^\d\-––a-zA-Z\s]", "", str(text)).strip()

    m = re.search(r"(\d[\d\s]*)\s*[-––]\s*(\d[\d\s]*)", t)
    if m:
        lo = int(re.sub(r"\s", "", m.group(1)))
        hi = int(re.sub(r"\s", "", m.group(2)))
        return lo, hi

    m = re.search(r"(?:menos\s+de|less\s+than)\s+(\d[\d\s]*)", t, re.I)
    if m:
        hi = int(re.sub(r"\s", "", m.group(1)))
        return 0, hi

    m = re.search(r"\d[\d\s]*", t)
    if m:
        n = int(re.sub(r"\s", "", m.group()))
        return n, n

    return 0, 0


# ── Parser do CSV ──────────────────────────────────────────────────────────
def parse_csv(content: str) -> list[dict]:
    advertisers = []
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
        norm = {k.lower().strip().strip('"'): v.strip().strip('"') for k, v in row.items() if k}

        name = (
            norm.get("page name") or norm.get("page_name")
            or norm.get("nome da página") or norm.get("nome da pagina")
            or norm.get("advertiser") or ""
        )
        if not name:
            continue

        page_id = norm.get("page id") or norm.get("page_id") or norm.get("id") or ""

        spend_raw = (
            norm.get("amount spent (brl)") or norm.get("amount_spent_brl")
            or norm.get("amount spent") or norm.get("valor gasto (brl)")
            or norm.get("gasto (brl)") or "0"
        )

        ads_raw = (
            norm.get("number of ads in library") or norm.get("number of ads")
            or norm.get("número de anúncios") or norm.get("num anuncios") or "0"
        )

        lo, hi = parse_spend(spend_raw)
        try:
            ads_count = int(re.sub(r"\D", "", ads_raw) or "0")
        except ValueError:
            ads_count = 0

        advertisers.append({
            "name":         name,
            "page_id":      page_id,
            "spend_lower":  lo,
            "spend_upper":  hi,
            "ads_count":    ads_count,
        })

    return advertisers


# ── Estratégia 1: Download do relatório regional ───────────────────────────
def strategy_report(page) -> str | None:
    """Tenta baixar o CSV do relatório do Meta Ad Library para o RN."""
    print(f"\n[Estratégia 1] Relatório regional — {REPORT_URL}")
    try:
        page.goto(REPORT_URL, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(4_000)
    except Exception as e:
        print(f"  ✗ Erro ao abrir página: {e}")
        return None

    # Fecha popups de cookies/login
    for sel in COOKIE_CLOSE_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1_500):
                el.click()
                page.wait_for_timeout(600)
                break
        except Exception:
            pass

    # Seleciona Rio Grande do Norte
    region_ok = False
    for sel in REGION_SELECTORS:
        try:
            inp = page.locator(sel).first
            if not inp.is_visible(timeout=2_500):
                continue
            inp.click()
            inp.fill("")
            inp.type(REGION_NAME[:6], delay=80)
            page.wait_for_timeout(1_500)

            option = page.locator(
                f"li:has-text('{REGION_NAME}'), [role='option']:has-text('{REGION_NAME}')"
            ).first
            if option.is_visible(timeout=3_500):
                option.click()
                page.wait_for_timeout(2_500)
                region_ok = True
                print(f"  ✓ Região selecionada")
                break
            else:
                inp.press("Enter")
                page.wait_for_timeout(2_000)
                region_ok = True
                print(f"  ✓ Região confirmada (Enter)")
                break
        except Exception:
            pass

    if not region_ok:
        print("  ⚠ Não conseguiu selecionar a região — tentando download sem filtro")

    page.wait_for_timeout(3_000)

    # Tenta download
    for sel in DOWNLOAD_SELECTORS:
        try:
            btn = page.locator(sel).first
            if not btn.is_visible(timeout=2_500):
                continue
            with page.expect_download(timeout=30_000) as dl_info:
                btn.click()
            dl = dl_info.value
            dl_path = DOWNLOAD_DIR / dl.suggested_filename
            dl.save_as(dl_path)
            content = dl_path.read_text(encoding="utf-8-sig", errors="replace")
            print(f"  ✓ CSV baixado: {dl.suggested_filename}")
            return content
        except Exception as e:
            print(f"  ✗ {sel!r}: {e}")

    return None


# ── Estratégia 2: Busca individual por candidato ───────────────────────────
def strategy_individual(page, candidates: list[dict]) -> list[dict]:
    """
    Para cada candidato em candidates.json, abre a página do Ad Library
    e extrai dados de gasto. Usada como fallback quando o download CSV falha.
    """
    if not candidates:
        print("\n[Estratégia 2] Sem candidatos listados — pulando busca individual")
        return []

    print(f"\n[Estratégia 2] Busca individual para {len(candidates)} candidatos")
    results = []

    for cand in candidates:
        name    = cand["name"]
        page_id = cand.get("page_id", "")
        print(f"\n  🔍 {name} ...")

        try:
            if page_id:
                url = (
                    f"{LIBRARY_URL}?active_status=all"
                    f"&ad_type=political_and_issue_ads"
                    f"&country=BR&id={page_id}"
                )
            else:
                url = (
                    f"{LIBRARY_URL}?active_status=all"
                    f"&ad_type=political_and_issue_ads"
                    f"&country=BR&q={name.replace(' ', '+')}"
                    f"&search_type=page"
                )

            page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(3_500)

            # Fecha popups
            for sel in COOKIE_CLOSE_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=1_000):
                        el.click()
                        break
                except Exception:
                    pass

            page.wait_for_timeout(2_000)
            text = page.inner_text("body")

            # Extrai número de anúncios
            ads_count = 0
            m = re.search(r"(\d[\d\.]*)\s*(?:anúncio|ad|resultado)", text, re.I)
            if m:
                ads_count = int(re.sub(r"\D", "", m.group(1)))

            # Extrai gasto (Meta mostra range de BRL para anúncios políticos no Brasil)
            lo, hi = 0, 0
            m = re.search(
                r"R\$\s*([\d\.,]+)\s*[-–]\s*R\$\s*([\d\.,]+)|"
                r"([\d\.,]+)\s*[-–]\s*([\d\.,]+)\s*BRL|"
                r"Menos de R\$\s*([\d\.,]+)|"
                r"Less than R\$\s*([\d\.,]+)",
                text, re.I
            )
            if m:
                lo, hi = parse_spend(m.group(0))

            results.append({
                "name":        name,
                "page_id":     page_id,
                "spend_lower": lo,
                "spend_upper": hi,
                "ads_count":   ads_count,
                "party":       cand.get("party", ""),
                "source":      "individual_search",
            })
            spend_str = f"R$ {lo:,} – {hi:,}" if lo or hi else "sem dados de gasto"
            print(f"    ✓ {ads_count} anúncios | {spend_str}")

        except Exception as e:
            print(f"    ✗ Erro: {e}")
            results.append({
                "name": name, "page_id": page_id,
                "spend_lower": 0, "spend_upper": 0, "ads_count": 0,
                "party": cand.get("party", ""), "source": "error",
            })

    return results


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


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    today = today_brt()
    print(f"\n{'='*55}")
    print(f"  RN Ad Tracker — coleta automática de {today}")
    print(f"{'='*55}\n")

    candidates = load_candidates()
    advertisers = []

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

        # ── Estratégia 1: download do CSV regional ─────────────────────────
        csv_content = strategy_report(page)

        if csv_content:
            print("\nParseando CSV ...")
            all_advertisers = parse_csv(csv_content)
            print(f"  ✓ {len(all_advertisers)} anunciantes no relatório")

            if all_advertisers:
                advertisers = filter_by_candidates(all_advertisers, candidates)
                print(f"  ✓ {len(advertisers)} após filtro de candidatos")

        # ── Estratégia 2: busca individual (fallback) ──────────────────────
        if not advertisers and candidates:
            advertisers = strategy_individual(page, candidates)

        browser.close()

    if not advertisers:
        print(
            "\n❌ Nenhum dado coletado.\n\n"
            "Possíveis causas:\n"
            "  - Meta exige login para acessar o relatório\n"
            "  - O layout da página mudou (seletores desatualizados)\n"
            "  - Candidatos do candidates.json não anunciaram no período\n\n"
            "Rode com --debug para inspecionar o navegador:\n"
            "  python scraper/scrape_report.py --debug\n"
        )
        sys.exit(1)

    # Ordena por gasto
    advertisers.sort(key=lambda x: x["spend_lower"], reverse=True)

    # Salva no history.json
    history   = load_history()
    snapshots = history.get("snapshots", [])
    snapshots = [s for s in snapshots if s["date"] != today]
    snapshots.append({
        "date":        today,
        "advertisers": advertisers,
        "source":      "report_csv" if csv_content else "individual_search",
    })
    snapshots = sorted(snapshots, key=lambda s: s["date"])[-365:]

    history["snapshots"]    = snapshots
    history["last_updated"] = datetime.now(TZ_BRT).isoformat()

    save_history(history)
    print(f"\n✅ Concluído — {len(advertisers)} candidatos salvos em data/history.json")


if __name__ == "__main__":
    main()
