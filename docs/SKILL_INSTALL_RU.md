# Установка навыка AutoCAD на другом компьютере

Навык `autocad-drawings` находится в `skills/autocad-drawings`.
Он предназначен для чтения, анализа, создания и редактирования DWG через MCP.
Blender нужен только для отдельно запрошенной реконструкции.

## Установка навыка

Склонируйте репозиторий и перейдите в него:

```powershell
git clone https://github.com/AndreyAdamenko/autocad-mcp.git
cd autocad-mcp
```

Из корня клона выполните:

```powershell
$skillSource = Join-Path $PWD 'skills\autocad-drawings'
$skillRoot = if ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME 'skills'
} else {
    Join-Path $env:USERPROFILE '.codex\skills'
}
$skillTarget = Join-Path $skillRoot 'autocad-drawings'
if (Test-Path -LiteralPath $skillTarget) {
    throw "Навык уже установлен: $skillTarget. Сравните версии перед заменой."
}
New-Item -ItemType Directory -Path $skillRoot -Force | Out-Null
Copy-Item -LiteralPath $skillSource -Destination $skillTarget -Recurse
```

Копируйте всю папку: `SKILL.md` использует материалы из `references/`,
а `agents/openai.yaml` содержит название и зависимость от MCP.
Откройте новый чат; если навык не появился в списке, перезапустите Codex.

Примеры вызова:

```text
$autocad-drawings Прочитай DWG и составь список слоёв, блоков и размеров.
```

```text
$autocad-drawings Перенеси указанные объекты на 500 мм вправо,
сохрани результат в отдельный DWG и проверь координаты.
```

## Подключение MCP

Навык задаёт рабочий порядок, но сам по себе не устанавливает AutoCAD,
Python, MCP или COM-компоненты. На целевом Windows-компьютере подготовьте
AutoCAD 2014 и Python 3.10+ по [инструкции интеграции](INTEGRATION_RU.md).
Из клона можно установить зависимости в отдельное окружение:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

Добавьте MCP с именем `autocad_2014` в настройки Codex:

- `command`: абсолютный путь к `.venv\Scripts\python.exe` этого клона.
- `args`: `-X`, `utf8`, абсолютный путь к `server.py`.
- `cwd`: абсолютный путь к корню клона.
- `ACAD_MCP_PROGID=AutoCAD.Application.19.1`.
- `ACAD_MCP_EXPECTED_VERSION=19.1`.
- `ACAD_MCP_READ_ONLY=1` для первоначальной проверки.
- `ACAD_MCP_OUTPUT_ROOT`: выбранный каталог результатов.

Если используете `config/codex-mcp.toml` как образец, замените пути исходного
компьютера на свои. Существующий `config.toml` целиком не перезаписывайте.
После настройки перезапустите MCP и выполните `diagnose_compatibility`.
AutoCAD должен быть запущен в той же пользовательской сессии и с теми же правами.

Для разрешённых задач редактирования требуется режим `ACAD_MCP_READ_ONLY=0`
и перезапуск MCP; установка навыка этот режим автоматически не включает.
Некоторые виды редактирования пока отсутствуют в сервере — навык сверяется
с фактически доступными инструментами и не обещает произвольную правку DWG.

## Обновление

Обновляйте клон через `git pull --ff-only`. Установленная пользовательская
папка — отдельная копия: после сравнения и сохранения собственных изменений
замените её содержимым новой версии `skills/autocad-drawings`.
Не смешивайте старые и новые дополнительные файлы.

Пути к исходникам и конфигурации определяются на целевом компьютере.
Токены, модели и проектные чертежи для установки навыка не нужны.
