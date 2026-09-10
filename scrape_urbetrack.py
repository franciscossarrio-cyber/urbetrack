"""
Scraper de Urbetrack - Reporte de Cumplimiento de Turnos (recorridos)
----------------------------------------------------------------------
Corre con un browser real (Playwright) para que el login pase por el
reCAPTCHA v3 sin problemas (headless Chromium con un browser real es
indistinguible de un usuario normal a los ojos de reCAPTCHA v3, a
diferencia de un POST directo sin JS).

Uso:
    URBETRACK_USER=uibañez URBETRACK_PASS='...' python scrape_urbetrack.py

Variables de entorno requeridas:
    URBETRACK_USER
    URBETRACK_PASS

Salida:
    data/recorridos_YYYY-MM-DD.csv  (fecha = ayer, la que se consultó)
"""

import csv
import os
import sys
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://red.urbetrack.com"
LOGIN_PATH = "/default.aspx"
REPORT_PATH = "/HigieneUrbana/Servicios/ReporteCumplimientoTurnos.aspx"

# IDs confirmados desde el HTML real de Urbetrack
SEL_USER = "#txtUsuario"
SEL_PASS = "#txtPassword"
SEL_LOGIN_BTN = "#btLogin"

SEL_DESDE = "#ctl00_ctl00_ContentPlaceHolder1_TitleFilterPanel_FilterPanel_ContentFiltros_dtDesde_dtDesde_textBox"
SEL_HASTA = "#ctl00_ctl00_ContentPlaceHolder1_TitleFilterPanel_FilterPanel_ContentFiltros_dtHasta_dtHasta_textBox"
SEL_SEARCH_BTN = "#ctl00_ctl00_ContentPlaceHolder1_TitleFilterPanel_FilterPanel_buttonSearch"
SEL_GRID = "#ctl00_ctl00_ContentPlaceHolder1_grid"
SEL_USER_LABEL = "#ctl00_ctl00_lblUserName"  # aparece solo si el login fue exitoso

OUTPUT_DIR = "data"


def get_yesterday_range():
    yesterday = datetime.now() - timedelta(days=1)
    date_str = yesterday.strftime("%d/%m/%Y")
    return f"{date_str} 00:00:00", f"{date_str} 23:59:59", yesterday.strftime("%Y-%m-%d")


def login(page, username: str, password: str) -> None:
    page.goto(BASE_URL + LOGIN_PATH, wait_until="networkidle")
    page.fill(SEL_USER, username)
    page.fill(SEL_PASS, password)

    # El click dispara ValidateRecaptcha() -> grecaptcha.execute() (async)
    # -> __doPostBack('btLogin','') recién cuando llega el token. Con un
    # browser real esto se resuelve solo; solo hay que esperar bien.
    page.click(SEL_LOGIN_BTN)

    # Esperar a que aparezca el layout post-login (label de usuario en
    # la barra superior) en vez de asumir una navegación con URL fija.
    try:
        page.wait_for_selector(SEL_USER_LABEL, timeout=30000)
    except Exception:
        # Si no apareció, probablemente el login falló (credenciales,
        # captcha con score bajo, etc.) - dejamos que el caller falle
        # con un mensaje claro.
        raise RuntimeError(
            "No se detectó el login exitoso (no apareció el label de "
            "usuario). Revisar screenshot/HTML de la página en ese "
            "momento para diagnosticar."
        )


def apply_date_filter_and_search(page, desde: str, hasta: str) -> None:
    page.goto(BASE_URL + REPORT_PATH, wait_until="networkidle")
    page.wait_for_selector(SEL_DESDE, timeout=20000)

    # fill() limpia el campo y tipea el valor; dispara los eventos que
    # ASP.NET necesita para tomar el valor en el próximo postback. No
    # tocamos el filtro de tipo de servicio ni el de rutas -> Urbetrack
    # conserva lo que ya esté tildado en la sesión del usuario.
    page.fill(SEL_DESDE, desde)
    page.fill(SEL_HASTA, hasta)

    with page.expect_response(lambda r: REPORT_PATH in r.url, timeout=30000):
        page.click(SEL_SEARCH_BTN)

    # Pequeño margen para que el UpdatePanel termine de re-renderizar
    # el grid tras la respuesta async.
    page.wait_for_timeout(1500)
    page.wait_for_selector(SEL_GRID, timeout=20000)


def parse_grid(page) -> list[dict]:
    grid_html = page.inner_html(SEL_GRID)
    soup = BeautifulSoup(grid_html, "html.parser")

    header_row = soup.select_one("tr.C1Heading.Grid_Header")
    headers = []
    if header_row:
        for th in header_row.select("th"):
            link = th.select_one("a.C1Link")
            text = link.get_text(strip=True) if link else ""
            headers.append(text or f"col{len(headers) + 1}")

    rows = []
    for tr in soup.select("tr.C1Row"):
        classes = tr.get("class", [])
        if "C1GroupHeaderRow" in classes:
            continue  # fila de agrupación (ej. "Turno: Mañana"), no es data

        cells = [td.get_text(strip=True) for td in tr.select("td")]
        if not cells:
            continue

        row = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"col{i + 1}"
            row[key] = cell
        rows.append(row)

    return rows


def write_csv(rows: list[dict], date_label: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"recorridos_{date_label}.csv")

    if not rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("Sin resultados para el rango consultado.\n")
        return path

    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return path


def main():
    username = os.environ.get("URBETRACK_USER")
    password = os.environ.get("URBETRACK_PASS")

    if not username or not password:
        print("Faltan las variables de entorno URBETRACK_USER / URBETRACK_PASS.", file=sys.stderr)
        sys.exit(1)

    desde, hasta, date_label = get_yesterday_range()
    print(f"Buscando recorridos de: {desde} a {hasta}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="es-AR")
        page = context.new_page()

        try:
            login(page, username, password)
            apply_date_filter_and_search(page, desde, hasta)
            rows = parse_grid(page)
        except Exception as e:
            # Guardamos evidencia para poder diagnosticar en el log del
            # workflow si algo falla (ej. cambió el HTML, el captcha
            # bloqueó, etc.)
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            page.screenshot(path=os.path.join(OUTPUT_DIR, "error_screenshot.png"))
            with open(os.path.join(OUTPUT_DIR, "error_page.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"ERROR: {e}", file=sys.stderr)
            browser.close()
            sys.exit(1)

        browser.close()

    path = write_csv(rows, date_label)
    print(f"Filas parseadas: {len(rows)}")
    print(f"CSV escrito en: {path}")


if __name__ == "__main__":
    main()
