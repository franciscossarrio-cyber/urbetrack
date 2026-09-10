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

SEL_DISTRITO = "#ctl00_ctl00_ContentPlaceHolder1_TitleFilterPanel_FilterPanel_ContentFiltros_cbDistrito"
TARGET_DISTRITO = "MUNICIPALIDAD DE MORENO"

SEL_DESDE = "#ctl00_ctl00_ContentPlaceHolder1_TitleFilterPanel_FilterPanel_ContentFiltros_dtDesde_dtDesde_textBox"
SEL_HASTA = "#ctl00_ctl00_ContentPlaceHolder1_TitleFilterPanel_FilterPanel_ContentFiltros_dtHasta_dtHasta_textBox"
SEL_SEARCH_BTN = "#ctl00_ctl00_ContentPlaceHolder1_TitleFilterPanel_FilterPanel_buttonSearch"
SEL_GRID = "#ctl00_ctl00_ContentPlaceHolder1_grid"

# Dropdown de checkboxes de "Ruta" -- selectores por atributo parcial
# porque el ID completo es larguísimo y repetitivo (ASP.NET WebForms).
SEL_ROUTE_DROPDOWN_LABEL = 'span[id*="ddlCheckRutaHu"][id$="_label"]'
SEL_ROUTE_CHECKBOXES = 'input[type="checkbox"][id*="ddlCheckRutaHu_list"]'

# Las 16 rutas finales a usar en el filtro, siempre exactas -- no
# dependemos de lo que haya quedado tildado en la sesión.
TARGET_ROUTES = {
    "1RECDOM1032F6", "1RECDOM1033F3", "1RECDOM1034F3", "1RECDOM1035F3",
    "1RECDOM1036F6", "1RECDOM1037F6", "1RECDOM1038F6", "1RECDOM1039F3",
    "1RECDON2031F3", "1RECDON2032F6", "1RECDON2033F3", "1RECDON2034F3",
    "1RECDON2035F3", "1RECDON2037F6", "1RECDON2039F6", "1RECDON2040F6",
}

OUTPUT_DIR = "data"


def get_yesterday_range():
    yesterday = datetime.now() - timedelta(days=1)
    date_str = yesterday.strftime("%d/%m/%Y")
    return f"{date_str} 00:00:00", f"{date_str} 23:59:59", yesterday.strftime("%Y-%m-%d")


def login(page, username: str, password: str) -> None:
    page.goto(BASE_URL + LOGIN_PATH, wait_until="networkidle")
    page.fill(SEL_USER, username)
    page.fill(SEL_PASS, password)

    # El click dispara ValidateRecaptcha() -> grecaptcha.execute() (async,
    # puede tardar varios segundos) -> recién ahí __doPostBack('btLogin','')
    # hace un form.submit() real (navegación de página completa, no AJAX).
    # Por eso hay que esperar la NAVEGACIÓN explícitamente, no solo que la
    # red esté "quieta" en la página actual.
    with page.expect_navigation(timeout=60000):
        page.click(SEL_LOGIN_BTN)

    page.wait_for_load_state("networkidle", timeout=45000)

    # No chequeamos que aparezca el label de usuario -- vive dentro de un
    # dropdown de Bootstrap y Playwright puede considerarlo "no visible"
    # por CSS aunque el contenido ya esté en el DOM (esto causó falsos
    # negativos reiterados). En cambio, chequeamos que DESAPAREZCA el
    # formulario de login -- señal mucho más confiable de que navegamos
    # a otra página.
    if page.query_selector(SEL_USER) is not None:
        raise RuntimeError(
            "Seguimos viendo el formulario de login tras el submit -- "
            "el login probablemente falló (credenciales, recaptcha con "
            "score bajo, etc.). Revisar screenshot/HTML para diagnosticar."
        )


def set_distrito_filter(page, distrito_label: str) -> None:
    """El dropdown de 'Ruta' está en cascada respecto a 'Distrito': si el
    distrito seleccionado no es el correcto, el panel de rutas queda
    vacío (0 opciones) sin ningún error visible. Cambiar el <select>
    dispara un postback de ASP.NET que repuebla la lista de rutas."""
    with page.expect_response(lambda r: REPORT_PATH in r.url, timeout=20000):
        page.select_option(SEL_DISTRITO, label=distrito_label)
    page.wait_for_load_state("networkidle", timeout=20000)


def set_route_filter(page, target_routes: set) -> None:
    """Tilda exactamente las rutas de target_routes en el dropdown de
    'Ruta', destildando cualquier otra que haya quedado de una sesión
    anterior. No depende de qué esté guardado server-side."""
    page.click(SEL_ROUTE_DROPDOWN_LABEL)
    # Los checkboxes de esta lista están estilizados con un indicador visual
    # aparte (el <input> nativo no cuenta como "visible" para Playwright),
    # así que alcanza con que estén en el DOM -- no con que pasen el
    # chequeo de visibilidad por defecto.
    page.wait_for_selector(SEL_ROUTE_CHECKBOXES, timeout=10000, state="attached")

    checkboxes = page.locator(SEL_ROUTE_CHECKBOXES)
    count = checkboxes.count()

    matched = 0
    for i in range(count):
        cb = checkboxes.nth(i)
        cb_id = cb.get_attribute("id")
        label_text = page.locator(f'label[for="{cb_id}"]').inner_text().strip()

        should_check = label_text in target_routes
        if should_check:
            matched += 1
        if should_check != cb.is_checked():
            # El <input> nativo tiene tamaño/posición que Playwright no
            # puede usar para calcular un punto de click (ni con
            # force=True -- eso solo saltea el chequeo de visibilidad,
            # no la necesidad de un bounding box real). Disparamos el
            # click nativo por JS, que sí dispara el onclick igual.
            cb.evaluate("el => el.click()")

    if matched != len(target_routes):
        print(
            f"ADVERTENCIA: se esperaban {len(target_routes)} rutas y solo "
            f"se encontraron {matched} en el listado. Revisar si algún "
            f"código de ruta cambió o no existe más.",
            file=sys.stderr,
        )

    # Cerrar el dropdown para que no tape el botón "Buscar"
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


def apply_date_filter_and_search(page, desde: str, hasta: str) -> None:
    page.goto(BASE_URL + REPORT_PATH, wait_until="networkidle")
    page.wait_for_selector(SEL_DESDE, timeout=20000)

    set_distrito_filter(page, TARGET_DISTRITO)
    set_route_filter(page, TARGET_ROUTES)

    # fill() limpia el campo y tipea el valor; dispara los eventos que
    # ASP.NET necesita para tomar el valor en el próximo postback. No
    # tocamos el filtro de tipo de servicio -> Urbetrack conserva lo
    # que ya esté tildado ahí en la sesión del usuario.
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
