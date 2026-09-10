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

Variables de entorno opcionales (backfill de un rango en vez de "ayer"):
    BACKFILL_DESDE=2026-09-01
    BACKFILL_HASTA=2026-09-09

Salida:
    data/recorridos_YYYY-MM-DD.csv  (un archivo por día -- en modo
        normal es el de ayer; en backfill, uno por cada día del rango
        que tuvo recorridos)
    docs/latest.csv, docs/latest.json  (snapshot estable para consumir
        desde afuera, ej. un dashboard -- solo se actualiza en modo
        normal, no durante un backfill)
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

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
# dependemos de lo que haya quedado tildado en la sesión. "1037" es el
# label real en Urbetrack para lo que era "1RECDOM1037F6" -- confirmado
# a mano en la UI, el código se acortó ahí y en ningún otro.
TARGET_ROUTES = {
    "1RECDOM1032F6", "1RECDOM1033F3", "1RECDOM1034F3", "1RECDOM1035F3",
    "1RECDOM1036F6", "1037", "1RECDOM1038F6", "1RECDOM1039F3",
    "1RECDON2031F3", "1RECDON2032F6", "1RECDON2033F3", "1RECDON2034F3",
    "1RECDON2035F3", "1RECDON2037F6", "1RECDON2039F6", "1RECDON2040F6",
}

OUTPUT_DIR = "data"
PAGES_DIR = "docs"


def get_yesterday_range():
    yesterday = datetime.now() - timedelta(days=1)
    date_str = yesterday.strftime("%d/%m/%Y")
    return f"{date_str} 00:00:00", f"{date_str} 23:59:59", yesterday.strftime("%Y-%m-%d")


def get_backfill_range(desde_iso: str, hasta_iso: str):
    """Convierte BACKFILL_DESDE/BACKFILL_HASTA (YYYY-MM-DD) al formato
    DD/MM/YYYY que espera el filtro de Urbetrack."""
    desde = datetime.strptime(desde_iso, "%Y-%m-%d")
    hasta = datetime.strptime(hasta_iso, "%Y-%m-%d")
    return (
        f"{desde.strftime('%d/%m/%Y')} 00:00:00",
        f"{hasta.strftime('%d/%m/%Y')} 23:59:59",
    )


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

    # Capturamos (id, label) de TODOS los checkboxes antes de tocar nada.
    # El click en uno de ellos puede reordenar/re-renderizar la lista (el
    # onclick de la tabla actualiza el resumen y potencialmente el DOM),
    # y como `checkboxes` es un locator que se re-evalúa en vivo, seguir
    # iterando por índice (`nth(i)`) después de empezar a clickear termina
    # apuntando a elementos distintos de los que se leyeron originalmente
    # -- eso causaba resultados inconsistentes entre corridas.
    items = []
    for i in range(count):
        cb = checkboxes.nth(i)
        cb_id = cb.get_attribute("id")
        label_text = page.locator(f'label[for="{cb_id}"]').inner_text().strip()
        items.append((cb_id, label_text))

    found_routes = set()
    for cb_id, label_text in items:
        should_check = label_text in target_routes
        if should_check:
            found_routes.add(label_text)

        # Volvemos a buscar el checkbox por su id (selector estable) en
        # vez de reusar el índice, por la misma razón de arriba.
        cb = page.locator(f'#{cb_id}')
        if should_check != cb.is_checked():
            # El <input> nativo tiene tamaño/posición que Playwright no
            # puede usar para calcular un punto de click (ni con
            # force=True -- eso solo saltea el chequeo de visibilidad,
            # no la necesidad de un bounding box real). Disparamos el
            # click nativo por JS, que sí dispara el onclick igual.
            cb.evaluate("el => el.click()")

    missing_routes = target_routes - found_routes
    if missing_routes:
        print(
            f"ADVERTENCIA: {len(missing_routes)} ruta(s) del filtro no "
            f"aparecen en el listado de Urbetrack (código cambiado o ruta "
            f"dada de baja): {sorted(missing_routes)}",
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


def _read_pager_info(page):
    """Lee el total de resultados y la página más alta referenciada en
    el pager (título de todos los <span>/<a> dentro de tr.C1PagerRow).
    Se llama una sola vez, en la página 1 -- todavía no tocamos nada."""
    return page.evaluate(
        """() => {
            const pager = document.querySelector('tr.C1PagerRow');
            if (!pager) return {total: null, maxPage: 1};
            const totalSpan = pager.querySelector('td > span');
            const match = totalSpan ? totalSpan.textContent.match(/(\\d+)/) : null;
            const total = match ? parseInt(match[1], 10) : null;
            const nums = Array.from(pager.querySelectorAll('a.C1Link'))
                .map(a => parseInt(a.getAttribute('title'), 10))
                .filter(n => !isNaN(n));
            nums.push(1);
            return {total, maxPage: Math.max(...nums)};
        }"""
    )


def _wait_for_page_stable(page, next_page: int, expected_count, max_polls: int = 50, poll_ms: int = 300) -> None:
    """El postback del grid actualiza el pager y las filas en pasos
    separados -- esperar solo a que el pager muestre la página nueva no
    alcanza, a veces se lee una mezcla a medio re-renderizar (filas de
    la página anterior conviviendo con las de la nueva). Sondeamos hasta
    que el pager marque la página correcta, el contenido deje de cambiar
    en tres lecturas seguidas, Y la cantidad de filas coincida con lo
    que el pager dice que debería tener esta página -- verificar contra
    ese número (en vez de solo "dejó de cambiar") evita quedarnos con un
    render a medio terminar que resultó estable por casualidad."""
    prev_fp = None
    stable_reads = 0
    for _ in range(max_polls):
        page.wait_for_timeout(poll_ms)
        state = page.evaluate(
            """({pageNum, gridSel}) => {
                const pager = document.querySelector('tr.C1PagerRow');
                const pagerOk = pager
                    ? Array.from(pager.querySelectorAll('td > span')).some(s => s.textContent.trim() === String(pageNum))
                    : false;
                const grid = document.querySelector(gridSel);
                const dataRows = grid ? Array.from(grid.querySelectorAll('tr.C1Row')) : [];
                const first = dataRows[0] ? dataRows[0].textContent.trim().slice(0, 80) : '';
                const last = dataRows.length ? dataRows[dataRows.length - 1].textContent.trim().slice(0, 80) : '';
                return {pagerOk, count: dataRows.length, fp: dataRows.length + '|' + first + '|' + last};
            }""",
            {"pageNum": next_page, "gridSel": SEL_GRID},
        )
        count_ok = expected_count is None or state["count"] == expected_count
        if state["pagerOk"] and count_ok and state["fp"] == prev_fp and state["fp"] != "0||":
            stable_reads += 1
            if stable_reads >= 3:
                return
        else:
            stable_reads = 0
        prev_fp = state["fp"]


def collect_all_pages(page) -> list[dict]:
    """El grid pagina de a N filas por página (C1PagerRow, con links
    "2", "3", ... que disparan __doPostBack). Si no se recorren todas
    las páginas, un rango con más resultados que una página se trunca
    en silencio -- pasó con el backfill de septiembre (108 resultados
    reales, solo se leían 50)."""
    all_rows = list(parse_grid(page))
    page_size = len(all_rows)

    pager_info = _read_pager_info(page)
    total = pager_info["total"]
    total_pages = pager_info["maxPage"]

    def expected_count(page_num: int):
        if total is None or not page_size:
            return None
        if page_num < total_pages:
            return page_size
        return total - page_size * (total_pages - 1)

    visited = {1}
    while True:
        link_titles = page.eval_on_selector_all(
            "tr.C1PagerRow a.C1Link", "els => els.map(e => e.getAttribute('title'))"
        )
        unvisited = {int(t) for t in link_titles if t and t.isdigit()} - visited
        if not unvisited:
            break

        next_page = min(unvisited)
        link = page.locator(f'tr.C1PagerRow a.C1Link[title="{next_page}"]').first
        with page.expect_response(lambda r: REPORT_PATH in r.url, timeout=30000):
            link.click()

        # La cantidad de filas puede coincidir con lo esperado por
        # casualidad aunque el contenido siga siendo una mezcla a medio
        # renderizar (algunas filas de la página anterior conviviendo
        # con algunas de la nueva) -- verificamos también que ninguna
        # fila de esta página sea un duplicado exacto de una ya
        # recolectada, reintentando unas cuantas veces si lo es.
        existing_keys = {tuple(sorted(r.items())) for r in all_rows}
        page_rows = []
        for _ in range(5):
            _wait_for_page_stable(page, next_page, expected_count(next_page))
            page_rows = parse_grid(page)
            if not any(tuple(sorted(r.items())) in existing_keys for r in page_rows):
                break
            page.wait_for_timeout(500)
        else:
            print(
                f"ADVERTENCIA: la página {next_page} siguió trayendo filas "
                f"duplicadas de páginas anteriores después de reintentar.",
                file=sys.stderr,
            )

        visited.add(next_page)
        all_rows.extend(page_rows)

    if total is not None and len(all_rows) != total:
        print(
            f"ADVERTENCIA: el pager reporta {total} resultados pero se "
            f"leyeron {len(all_rows)} filas (antes de deduplicar).",
            file=sys.stderr,
        )

    # Filas 100% idénticas en todos sus campos son casi con certeza un
    # artefacto de la transición entre páginas (dos servicios reales
    # nunca coinciden en TODOS los horarios y valores a la vez) -- se
    # descartan como red de seguridad adicional a _wait_for_page_stable.
    deduped = list({tuple(sorted(row.items())): row for row in all_rows}.values())
    return deduped


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


def write_csv_by_day(rows: list[dict]) -> list[str]:
    """Para un backfill de varios días: separa las filas por su columna
    'Fecha' (formato "DD/MM/YYYY HH:MM") y escribe un CSV por día, igual
    que produciría la corrida diaria normal para cada una de esas fechas."""
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        fecha_cell = row.get("Fecha", "")
        date_part = fecha_cell.split(" ")[0]  # "DD/MM/YYYY"
        try:
            date_label = datetime.strptime(date_part, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            date_label = "sin_fecha"
        by_day.setdefault(date_label, []).append(row)

    paths = []
    for date_label, day_rows in sorted(by_day.items()):
        paths.append(write_csv(day_rows, date_label))
    return paths


def write_latest_snapshot(rows: list[dict], date_label: str) -> None:
    """Pisa docs/latest.csv y docs/latest.json con la corrida de hoy --
    URL estable para que algo externo (ej. un dashboard) los consuma sin
    tener que calcular la fecha de ayer por su cuenta. Se sirve por
    GitHub Pages apuntando a la carpeta docs/ en Settings > Pages."""
    os.makedirs(PAGES_DIR, exist_ok=True)

    csv_path = os.path.join(PAGES_DIR, "latest.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            f.write("Sin resultados para el rango consultado.\n")

    json_path = os.path.join(PAGES_DIR, "latest.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fecha": date_label,
                "generado": datetime.now(timezone.utc).isoformat(),
                "recorridos": rows,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def main():
    username = os.environ.get("URBETRACK_USER")
    password = os.environ.get("URBETRACK_PASS")

    if not username or not password:
        print("Faltan las variables de entorno URBETRACK_USER / URBETRACK_PASS.", file=sys.stderr)
        sys.exit(1)

    backfill_desde = os.environ.get("BACKFILL_DESDE")
    backfill_hasta = os.environ.get("BACKFILL_HASTA")
    is_backfill = bool(backfill_desde and backfill_hasta)

    if is_backfill:
        desde, hasta = get_backfill_range(backfill_desde, backfill_hasta)
    else:
        desde, hasta, _ = get_yesterday_range()
    print(f"Buscando recorridos de: {desde} a {hasta}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="es-AR")
        page = context.new_page()

        try:
            login(page, username, password)
            apply_date_filter_and_search(page, desde, hasta)
            rows = collect_all_pages(page)
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

    # El grid no siempre devuelve las filas en el mismo orden entre
    # corridas (recorrer las páginas del pager no garantiza un orden
    # estable) -- eso generaba diffs de git en filas que en realidad no
    # cambiaron. Se ordena por fecha y código para un resultado estable.
    rows.sort(key=lambda r: (r.get("Fecha", ""), r.get("Código", "")))

    print(f"Filas parseadas: {len(rows)}")

    if is_backfill:
        paths = write_csv_by_day(rows)
        print(f"CSVs escritos: {', '.join(paths) if paths else '(ninguno, sin resultados en el rango)'}")
    else:
        _, _, date_label = get_yesterday_range()
        path = write_csv(rows, date_label)
        write_latest_snapshot(rows, date_label)
        print(f"CSV escrito en: {path}")
        print(f"Snapshot 'latest' actualizado en: {PAGES_DIR}/")


if __name__ == "__main__":
    main()
