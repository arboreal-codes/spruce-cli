# spruce

[English](README.md) | **Español**

Reduce el almacenamiento de SharePoint Online que usa un tenant de Microsoft 365, paso a paso y
sin dejar a nadie bloqueado.

spruce (proyecto `spruce-cli`, comando `spruce`) es una herramienta de consola multiplataforma (macOS, Linux, Windows) que mide cada
sitio, calcula quotas por sitio para un presupuesto de almacenamiento, simula políticas de
historial de versiones sobre los reportes de versiones del propio SharePoint, aplica políticas
y recortes de versiones, y encuentra el espacio retenido en las papeleras de reciclaje y en la
Preservation Hold Library. Los reportes de cada corrida se guardan en una carpeta de sesión,
así las rondas nunca se mezclan.

Habla directamente con SharePoint (login con MSAL + REST/CSOM). No necesita PowerShell, PnP,
Node ni la CLI `m365`: solo [uv](https://docs.astral.sh/uv/).

## Cómo funciona, en resumen

El almacenamiento se baja hasta un presupuesto B en rondas. En cada ronda:

1. Se **mide** cada sitio.
2. Se **planifican las quotas** para B con un tope de _water-filling_: los sitios chicos
   conservan margen para crecer y los grandes se **congelan** apenas por encima de su uso
   actual (nunca por debajo, porque quedarían en solo lectura), con una meta de cuánto tienen
   que liberar.
3. Se **aplican** las quotas.
4. Se le piden a SharePoint los **reportes de versiones** de los sitios congelados (una fila
   por versión de cada archivo).
5. Se **simulan** políticas de versiones (conservar N versiones, borrar las de más de D días)
   para ver cuánto liberaría cada una por sitio y por archivo.
6. Se define la **política de versiones** (para que el problema no vuelva), se **recortan** las
   versiones existentes (permanente, con confirmaciones y salvaguardas) y se vacían las
   **papeleras**.
7. Se esperan ~48 h, se vuelve a medir y, cuando la ronda termina, se baja B.

El método completo, sus reglas y sus compromisos están en
**[docs/strategy.es.md](docs/strategy.es.md)**. Conviene leerlo antes de cambiar algo en un
tenant de producción.

## Inicio rápido

Instalar uv:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Correr spruce directamente desde GitHub (uv descarga Python y las dependencias la primera vez):

```bash
uvx --from git+https://github.com/arboreal-codes/spruce-cli spruce --help
```

O instalarlo como un comando más, y actualizarlo después:

```bash
uv tool install git+https://github.com/arboreal-codes/spruce-cli
uv tool upgrade spruce-cli
```

Para fijar una versión, agregar `@v0.2.1` (un tag) o `@<commit>` a la URL.

Después, en la carpeta donde se quieran guardar la configuración y los reportes:

```bash
spruce init                          # pide los datos del tenant y escribe spruce.toml
spruce login                         # navegador o device code
spruce session new "budget 5TB"
spruce sites export                  # paso 1
spruce quota plan --budget-tb 5      # paso 2, sin conexión
spruce quota apply --dry-run         # paso 3, vista previa
```

## Requisitos

1. **Un registro de app propio en Entra ID** (Microsoft no ofrece uno compartido):
   - Plataforma _Mobile and desktop applications_ con redirect URI `http://localhost`.
   - _Allow public client flows_ = Yes (necesario para `--auth device-code`).
   - Permiso de API **SharePoint → Delegated → AllSites.FullControl**, con consentimiento de
     administrador.

   Una app creada con `m365 setup` (CLI for Microsoft 365) o con
   `Register-PnPEntraIDAppForInteractiveLogin` (PnP.PowerShell) ya cumple con esto.

2. **Una cuenta con el rol SharePoint Administrator.** Para los reportes y recortes de
   versiones, además tiene que ser **administradora de colección** de cada sitio. Estos
   trabajos solo funcionan con login delegado (de usuario), no con app-only.
3. **Límites de almacenamiento manuales** en el centro de administración de SharePoint
   (Settings → Site storage limits). Ver la
   [configuración inicial del tenant](docs/strategy.es.md#configuración-inicial-del-tenant).

## Configuración y perfiles

`spruce init` pide la URL del centro de administración, el tenant (sugerido a partir de la
URL), el client ID de la app y el modo de login, y escribe `spruce.toml` en la carpeta actual.
Con `--admin-url`, `--tenant`, `--client-id`, `--auth` y `--sessions-dir` no pregunta nada;
`--force` sobrescribe. [`config.example.toml`](config.example.toml) documenta cada clave.

Si se le da un nombre a `init`, se pueden tener varias configuraciones a la vez (otro tenant,
otra app, pruebas y producción). Cada perfil tiene su propio archivo, su carpeta de sesiones y
su cache de login:

```bash
spruce init contoso                  # -> spruce.contoso.toml, sesiones en sessions-contoso/
spruce init lab --auth device-code
spruce profiles                      # lista los archivos de configuración de esta carpeta
spruce -p contoso login
export SPRUCE_PROFILE=contoso        # Windows: $env:SPRUCE_PROFILE="contoso"
```

Orden de búsqueda de la configuración: `--config PATH`, si no el perfil (`-p` /
`SPRUCE_PROFILE`), si no `SPRUCE_CONFIG`, si no `./spruce.toml`. El token de login se guarda
en `~/.spruce/`, con un cache por tenant + app.

## Sesiones

Cada reporte se guarda en una carpeta de sesión, así las rondas nunca se mezclan y se pueden
correr dos a la vez (por ejemplo, una ronda de quotas en una terminal y una limpieza de
papeleras en otra).

```bash
spruce session new "budget 3TB" --note "ronda de noviembre"   # -> sessions/20261110-231344_budget-3TB/
spruce session list
spruce -s budget-3TB session show                              # archivos de una sesión
```

La sesión se elige con `-s/--session` (nombre de la carpeta, nombre o prefijo de fecha) o con
`SPRUCE_SESSION`. Las dos son opciones globales y van antes del comando
(`spruce -s budget-3TB quota plan ...`). Sin ninguna, se usa la última sesión y se avisa.
Dentro de una sesión, los comandos toman por defecto su último archivo de entrada (el último
`site-storage` para el plan, el último `quota-plan` para todo lo que sigue). Cada sesión guarda
`session.json` y un `history.log` con los comandos que se corrieron en ella.

## Comandos

| Paso                     | Comando                                                                             |
| ------------------------ | ----------------------------------------------------------------------------------- |
| 1. Medir                 | `spruce sites export`                                                              |
| 2. Plan de quotas        | `spruce quota plan --budget-tb 3 [--strategy NOMBRE]`                              |
| 3. Aplicar quotas        | `spruce quota apply --dry-run`, después sin `--dry-run`                            |
| 4. Reportes de versiones | `spruce versions report-start --status OverCap`, después `report-receive` cada día |
| 5. Simular               | `spruce versions whatif --major-version-limit 10,50,100 --expire-after-days 0,365` |
| 6. Política              | `spruce versions policy --site URL --major-version-limit 100 --apply-existing`     |
| 6. Recorte               | `spruce versions trim --site URL --mode count --major-version-limit 100`           |
| 6. Papeleras             | `spruce cleanup analyze --status OverCap` / `cleanup purge-recycle-bin`            |
| Cualquiera               | `spruce sites select ...` muestra qué sitios elige una selección                   |
| Cualquiera               | `spruce ui` abre el [panel web](#panel-web)                                        |

`spruce --help` y `spruce <comando> --help` listan todas las opciones. Qué hace cada paso y
por qué está en [docs/strategy.es.md](docs/strategy.es.md).

**Salvaguardas:** nunca se aplica una quota igual o menor al uso actual; se rechazan políticas
de menos de 100 versiones; los recortes por debajo de 100 requieren
`--allow-below-policy-minimum`; los recortes se vuelven a leer y se cancelan si SharePoint
registró otra cosa; nunca se pide dos veces el reporte de versiones de un sitio mientras uno
está en curso; los comandos destructivos aceptan `--dry-run` y piden escribir una palabra de
confirmación (se saltea con `--yes`).

## Elegir sitios: filtros y exclusiones

Todos los comandos que actúan sobre sitios (`versions report-start`, `policy`, `trim`,
`cleanup analyze`, `cleanup purge-recycle-bin`) aceptan las mismas opciones. Los candidatos son
el último export `site-storage` de la sesión unido a su último `quota-plan` (se pueden cambiar
con `--storage` / `--plan`). Los filtros se combinan con Y:

| Opción                                   | Deja                                                                   |
| ---------------------------------------- | ---------------------------------------------------------------------- |
| `--status OverCap,Fixed`                 | los sitios con esos estados en el plan                                 |
| `--min-used-gb 80` / `--max-used-gb 500` | los sitios según su uso                                                |
| `--match '*Proyectos*'`                  | URL o título que coincide con el patrón (repetible, O)                 |
| `--template GROUP`                       | plantilla del sitio (`GROUP`, `STS`, `SITEPAGEPUBLISHING`; repetible)  |
| `--inactive-days 365`                    | sin cambios de contenido en los últimos N días (candidatos a archivar) |
| `--top 10`                               | solo los N más grandes, después de los otros filtros                   |
| `--all`                                  | todos los sitios, cuando de verdad es eso lo que se quiere             |

Un comando sin filtros se niega a correr, así nada toca todos los sitios por accidente.
`--site URL` (repetible) elige sitios explícitamente y saltea los filtros. Las exclusiones se
aplican después de los filtros, también a `--site`:

| Opción                   | Deja afuera                                                                                                                                                 |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--exclude URL_O_PATRÓN` | los sitios que coinciden (repetible)                                                                                                                        |
| `--exclude-file PATH`    | los sitios de un CSV con columna `Url` o `SiteUrl` (sirve cualquier salida de spruce) o de un archivo de texto con una URL por línea (comentarios con `#`) |
| `--skip-done`            | los sitios que este mismo comando ya procesó bien en **cualquier** sesión                                                                                   |

"Procesado" significa: `cleanup analyze` = fila sin error en un `cleanup-summary`;
`report-start` = reporte `Downloaded`; `policy` = `Applied`; `trim` = `Queued`;
`purge-recycle-bin` = `Deleted`.

```bash
spruce sites select --min-used-gb 80 --skip-done-for cleanup        # vista previa
spruce cleanup analyze --min-used-gb 80 --skip-done --exclude-file no-tocar.txt
spruce versions report-start --status OverCap --top 5 --exclude '*Archivo*'
```

`report-start` además saltea cualquier sitio con un reporte todavía `Requested` o
`InProgress` en cualquier sesión (un pedido nuevo lo reemplazaría y se perderían los días ya
esperados). Si ese trabajo en realidad terminó, hay que correr `report-receive` en su sesión;
`--force` lo pide igual.

## Estrategias de quotas

```bash
spruce quota strategies
spruce quota plan --budget-tb 3 --reserve-percent 15 --strategy budget-fill
```

| Estrategia                    | Quota de los sitios UnderCap                                                               |
| ----------------------------- | ------------------------------------------------------------------------------------------ |
| `growth-margin` (por defecto) | min(c, uso × (1 + margen de crecimiento))                                                  |
| `water-filling`               | el tope c completo                                                                         |
| `budget-fill`                 | min(demanda, c′), con demanda = uso × (1 + crecimiento) y Σ min(demanda, c′) = presupuesto |

Todas congelan los sitios OverCap en uso × (1 + margen de congelamiento) y respetan las quotas
fijas (`--fixed-quotas-csv` con `Url,QuotaGB`). El nombre del archivo del plan incluye la
estrategia, así se pueden comparar varias en una misma sesión. Ventajas y desventajas de cada
una: [docs/strategy.es.md](docs/strategy.es.md#estrategias-de-quotas).

**Agregar una estrategia:** escribir una función en
[`src/spruce_cli/strategies.py`](src/spruce_cli/strategies.py) que reciba el uso en MB de cada sitio,
el presupuesto en MB y `QuotaOptions`, y devuelva `(cap, [Allocation, ...])`. Registrarla en
`STRATEGIES`; la primera línea del docstring es su descripción. La CLI, el CSV y el resumen la
toman solos.

## Papeleras de reciclaje y Preservation Hold Library

Las dos cuentan en el almacenamiento de un sitio, pero no aparecen en los reportes de
versiones.

```bash
spruce cleanup analyze --min-used-gb 20 --detail
spruce cleanup purge-recycle-bin --site URL --stage 2 --older-than-days 30 --dry-run
```

- `analyze` escribe `cleanup-summary_<ts>.csv`: por sitio, el uso en MB, los elementos y MB de
  cada etapa de la papelera (y cuánto se borró hace más de 30 días), los archivos y MB de la
  Preservation Hold Library, y qué parte del uso está retenida ahí. `--detail` agrega una fila
  por elemento / archivo retenido.
- `purge-recycle-bin` borra **permanentemente** elementos de la papelera (etapa `1`, `2` o
  `all`, opcionalmente solo los borrados hace más de N días).
- La Preservation Hold Library solo se informa: se libera cambiando la política de retención en
  Microsoft Purview. Solo se mide la biblioteca del sitio raíz (los subsitios tienen la suya).

## Panel web

Un panel local sobre los archivos de la sesión: sitios con los mismos filtros y exclusiones que
la CLI, todas las estrategias de quota comparadas lado a lado con las opciones que escribas, los
pedidos de reportes de versiones de todas las sesiones, la simulación de versiones y el análisis
de papeleras / Preservation Hold.

Los sitios seleccionados en la tabla se pueden copiar como opciones `--site`, bajar como CSV
para `--exclude-file`, pedirles el reporte de versiones o analizar sus papeleras; todas las
tablas se pueden bajar en CSV.

Las vistas solo leen archivos de la sesión. Las acciones que llaman a SharePoint (login,
exportar sitios, pedir y recibir reportes de versiones, análisis de papeleras) corren el mismo
código que la CLI, de a una, con su salida en una ventana de log, y quedan anotadas en el
`history.log` de la sesión. El panel también crea sesiones y guarda planes de quota ("Save
plan"). Nunca cambia quotas ni políticas de versiones ni borra nada: para eso, la CLI.

La pestaña "Command builder" ayuda con eso: elegís cualquier comando, completás sus opciones
(salen de la propia CLI, con la ayuda de cada una al pasar el mouse) y copiás el comando, con la
sesión y el perfil ya puestos. Los sitios seleccionados en la pestaña Sites pueden completar
`--site`, y los comandos que cambian el tenant arrancan con `--dry-run` marcado. Las comillas son
las de shells de macOS/Linux; en PowerShell de Windows, revisalas antes de correrlo.

```bash
uvx --from "spruce-cli[ui] @ git+https://github.com/arboreal-codes/spruce-cli" spruce ui
```

Escucha solo en `http://127.0.0.1:8765` (`--port` para cambiarlo, `--no-browser` para no
abrirlo). La sesión se elige en el encabezado; `-s` y `-p` funcionan como en todos los comandos.

## Desarrollo

```bash
git clone https://github.com/arboreal-codes/spruce-cli && cd spruce-cli
uv run spruce --help
uv run pytest
uv run ruff check --fix && uv run ruff format
uv run ty check
```

| Módulo (`src/spruce_cli/`) | Para qué                                             |
| ----------------------- | ---------------------------------------------------- |
| `cli.py`                | comandos de Typer                                    |
| `common.py`             | configuración, perfiles, sesiones, utilidades de CSV |
| `sharepoint.py`         | login con MSAL, llamadas REST y CSOM                 |
| `targets.py`            | filtros y exclusiones de sitios                      |
| `strategies.py`         | estrategias de quotas                                |
| `quota.py`              | pasos 1 a 3                                          |
| `versions.py`           | pasos 4 y 6                                          |
| `whatif.py`             | paso 5 (Polars)                                      |
| `cleanup.py`            | papeleras y Preservation Hold Library                |
| `dashboard.py`          | datos del panel web (sin imports de UI)              |
| `ui.py`                 | panel web (NiceGUI, extra opcional `ui`)             |

Las llamadas que modifican SharePoint no están cubiertas por los tests (necesitan un tenant).
El código nuevo conviene probarlo con `--dry-run` y primero en un sitio chico.

## Aviso

Los recortes de versiones y las purgas de papelera borran datos de forma permanente. spruce
se ofrece tal cual, sin garantías (ver [LICENSE](LICENSE)). Correr primero la simulación y
`--dry-run`, y tener la aprobación de los dueños de los sitios.

## Licencia

[MIT](LICENSE)

## Por qué "spruce"

El nombre junta tres ideas:

- **SP**, por SharePoint.
- **Un árbol.** spruce nació en Arboreal, una empresa del sector forestal de Uruguay: un
  aserradero que produce mass timber y hace proyectos con mass timber. Por eso elegimos el
  nombre de un árbol. Pero en Arboreal no se trabaja con *spruce* (abeto), sino sobre todo con
  pino y algo de eucalipto: el abeto se eligió por sus letras y por su verbo, no por la especie.
- ***To spruce up***, que en inglés es ordenar algo hasta dejarlo prolijo, que es lo que la
  herramienta hace con el almacenamiento de un tenant. Podar versiones viejas para que lo demás
  siga creciendo es, además, trabajo de forestales.

El proyecto y el repositorio se llaman `spruce-cli` porque `spruce` ya estaba ocupado en PyPI;
el comando sigue siendo `spruce`.
