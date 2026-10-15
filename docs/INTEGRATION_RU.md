# AutoCAD 2014 → MCP → Blender

Вход — оригинальный DWG. `Documents.Open(path, True)` открывает его для чтения.
Сервер подключается к уже работающему AutoCAD, проверяя реальный Version=19.1,
включая суффикс сервисного пакета. Ни DXF, ни PDF, ни FBX/OBJ, ни геометрический
JSON-файл не используются. Обмен с Blender идёт по анонимному stdin pipe.
Исходные данные сохраняются внутри итогового BLEND для проверки.

## Запуск

Нужны Python >=3.10, полный AutoCAD 2014, Blender. Здесь уже доступны
`C:\Python312\python.exe` (pywin32, mcp 1.26.0, Pillow) и Blender 5.2.2.
На новой машине: `py -3 -m venv .venv`, затем `.venv\Scripts\python.exe -m pip install .`.

Готовые настройки этого компьютера: `config/codex-mcp.toml`. Поля stdio и timeout
проверены по [официальной документации OpenAI](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
После изменения конфигурации требуется перезапуск MCP-хоста. AutoCAD и сервер
должны работать в одной пользовательской сессии Windows с одинаковыми правами.
Изолированный процесс может не видеть ROT и возвращать MK_E_UNAVAILABLE.

```powershell
C:\Python312\python.exe -X utf8 -m autocad_mcp.diagnostics --source 'D:\Temp\Ердаут\5800-1.3-АР-2_3_REV.0.dwg' --output evidence\com_source.json
C:\Python312\python.exe -X utf8 tools\acax_probe.py iface IAcadLWPolyline
C:\Python312\python.exe -X utf8 tools\check_flagged_methods.py
C:\Python312\python.exe -X utf8 tools\check_extraction_api.py
C:\Python312\python.exe -X utf8 -m autocad_mcp.pipeline --source 'D:\Temp\Ердаут\5800-1.3-АР-2_3_REV.0.dwg' --inspect-report evidence\inspection.json
C:\Python312\python.exe -X utf8 -m autocad_mcp.pipeline --source 'D:\Temp\Ердаут\5800-1.3-АР-2_3_REV.0.dwg' --output artifacts\new_model.blend
```

BLEND должен иметь новое имя. Report/PNG/log — результаты контроля, они не
служат промежуточными геометрическими файлами.

## MCP

| Инструмент | Назначение |
| --- | --- |
| diagnose_compatibility | Живая версия, typelib, чтение свойств |
| open_source_drawing(path) | DWG, ReadOnly=True; уже открытый writable файл отвергается |
| start_geometry_scan(path, scope) | Manifest, scan_id, next_page=0; максимум 8 сканов, 1 час хранения |
| read_geometry_batch(scan_id, page, limit, max_bytes) | Возобновляемые пакеты до 1 МБ, по умолчанию 256 КБ |
| close_geometry_scan(scan_id) | Освобождение ссылок, DWG остаётся открытым |
| interpret_geometry(records) | Неподтверждённые архитектурные гипотезы и возможные отметки |
| build_blender_model(path, output, rules) | COM → bpy → новый BLEND, рендеры, отчёт |
| inspect_drawing_annotations(path, max_items) | Тексты, размеры и таблицы оригинального DWG через COM |
| assemble_architectural_model(project_path, output) | Объёмная сборка из проверенных COM-данных внутри итоговых BLEND и параметров с источниками |

`scope=model` читает ModelSpace; `all` также читает блоки листов, без переключения
листа. Видовые экраны не дублируют ModelSpace. `output` ограничен
ACAD_MCP_OUTPUT_ROOT. Ответ построения компактный, подробности находятся в отчёте.

Фрагменты соединяются по `record_id` в порядке `offset` до `total_chars`, затем
разбираются как JSON; готовый `RecordAssembler` делает это автоматически.
Повтор курсора запрещён. Завершение: done=true и source_unchanged=true.
DBMOD/path/ReadOnly/количества объектов проверяются между пакетами, SHA-256 DWG —
до и после полного обхода. Внешнее редактирование памяти AutoCAD во время
скана не допускается: ActiveX не предоставляет транзакционный снимок.

Исходные редакторские функции сохранены для явного `ACAD_MCP_READ_ONLY=0`.
Для другой версии также явно задаются ACAD_MCP_PROGID и ACAD_MCP_EXPECTED_VERSION.
Для чтения оригинальных DWG оставляйте read-only режим. Новый поток никогда
не вызывает Explode/Copy/SendCommand/Save/Export, не создаёт временные сущности
и не меняет системные переменные. Legacy DXF SaveAsType выбирается по версии
(61 для 2014), но в процессе моделирования не используется.

## Координаты и геометрия

Геометрия хранится в единицах определения объекта. `world_matrix` 4×4 умножается
на столбец [x,y,z,1] и переводит в WCS миллиметры; bpy получает метры (/1000).
Неизвестный INSUNITS даёт mm_per_unit=null: построение блокируется до явного
override `mm_per_unit` с основанием `units_evidence`.

OCS полилиний учитывает Elevation и Normal. Bulge сохраняется и визуализируется
дугами с заданным допуском. Вложенные матрицы вставок учитывают точку вставки,
Origin определения, нормаль, поворот, разные и отрицательные масштабы.
Единицы блока не пересчитываются поверх уже записанного масштаба вставки;
сохраняются raw/effective scales и InsUnitsFactor для аудита. Dynamic block
читается через его фактическое Name, EffectiveName сохраняется отдельно.
Слой 0 наследует слой вставки. Каждый экземпляр получает уникальный путь handles.
Углы нового потока — **радианы**, legacy draw-инструментов — градусы.
Текущая UCS сохраняется как метаданные и не применяется повторно к WCS.

Поддержаны линии, 2D/3D-полилинии, дуги, окружности, эллипсы, точки, 3D-грани,
простые polygon meshes, параметры сплайнов, блоки, размеры, тексты и атрибуты.
Неизвестные типы и неполные данные отражаются в отчёте.

## Семантика и объёмные элементы

Названия слоёв/блоков и формы дают **гипотезы** о стенах, колоннах, плитах,
дверях, окнах, лестницах и кровле. Замкнутые цепочки линий распознаются только
для графов степени 2, без продления линий. Текст `+3.600` — возможная отметка,
без угаданной единицы, датумов и связи с элементом.

Без правил создаётся проверочная сцена исходной геометрии. Архитектурный объём
создаётся только для замкнутого горизонтального контура по явному правилу:

```json
[{"source_id":"Model/ABC","category":"Walls","confirmed":true,
  "evidence":"Назначение контура подтверждено по ...",
  "height_mm":{"value":3000,"confirmed":true,"source":"Разрез ..., размер ..."},
  "base_z_mm":{"value":0,"confirmed":true,"source":"Отметка ..., лист ..."}}]
```

Числа выше — **пример синтаксиса, не параметры вашего здания**. Передача —
MCP `rules` или CLI `--rules rules.json`. Категории распределяются по коллекциям.
Общий режим экструзии не вырезает отверстия в плитах и дверные/оконные проёмы.
Отдельный модуль архитектурной сборки поддерживает прямоугольные плиты со
сквозными ортогональными отверстиями по явно выбранным исходным COM-контурам.
Совпадение конечной и начальной вершины доказывает замыкание даже при Closed=False;
контур с зазором автоматически не замыкается.
Фасады/разрезы нельзя считать планами без подтверждения назначения и координат.

Для переданного комплекта выполнена отдельная сборка по двум планам, разрезу
АР-7 и опалубочным планам КЖ-019. Подробности, параметры и фактические проверки:
[объёмная реконструкция](ARCHITECTURAL_RECONSTRUCTION_RU.md). Эта конфигурация
привязана к 24 условным символам колонн АР для совмещения планов и handles данного проекта; она не
считается универсальным распознавателем произвольных зданий.

После исследования СПЗ добавлен профиль `overall_view`: реальные контуры КЖ
заменяют условные колонны на 22 конструктивные опоры, добавляются 37 оконных
сборок, кровля, парапет, карниз и входы. Профиль применяет явно принятые
пользователем упрощения для общего вида; неизвестные детали остаются помеченными.
Текущий BLEND — `artifacts/erdaut_overall_view.blend`, отчёт —
[исследование СПЗ](SPZ_RESEARCH_RU.md).

`tools/prepare_overview_project.py` фиксирует выборки и параметры проекта в
`evidence/architectural_project_overview.json` без геометрического обменного файла.
Через MCP вызывается существующий `assemble_architectural_model` с этим
`project_path` и **новым** именем `output`, например `new_overall_view.blend`.
Повторная проверка сохранённого файла:

```powershell
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --background artifacts\erdaut_overall_view.blend --disable-autoexec --python-exit-code 3 --python tools\verify_architecture.py
```

Для новых чтений защищённой сетевой папки `tools/stage_spz_sources.py` создаёт
побитовые локальные копии. Геометрия читается COM из этих DWG без создания
новых сетевых DWL/DWL2. `tools/verify_spz_unchanged.py` сравнивает итог с исходной
описью и хешами; он ничего не записывает в папку источников.

## Контроль и ограничения

Отчёт сопоставляет source_id и bpy objects: вершины, габариты, COM Length,
высоты и замкнутость сеток, отсутствующие/неподдержанные элементы, SHA-256,
рендеры плана/изометрии. `coordinate_checks_passed`, `full_fidelity` и
`semantic_building_verified` — разные результаты; последний автоматически
не утверждается. Сплайны сохраняют точные NURBS-данные; адаптивная визуализация
помечается как не имеющая доказанной глобальной оценки ошибки.

Аннотации сохраняют текст и свойства; шрифты и размерная графика воспроизводятся
приблизительно. Толщина QA-кривых подбирается для видимости на рендере; проверяются
вершины точной опорной mesh-геометрии. Ширина/толщина AutoCAD полилиний сохраняется
в исходных данных и отмечается как неперенесённая, если ненулевая.

ActiveX 2014 не даёт общего read-only доступа к граням ACIS-тел; PolyfaceMesh.Faces
доступен только для записи. Прокси, xref/MInsert и неизвестные типы не заменяются
bounding box. Полный автоматический перевод произвольного DWG при COM-only
требованиях не гарантирован; ограничения конкретного файла перечисляет отчёт.

## Тесты

```powershell
C:\Python312\python.exe -X utf8 -m unittest discover -s tests -v
C:\Python312\python.exe -X utf8 tools\blender_fixture.py --output artifacts\new_fixture.blend
C:\Python312\python.exe -X utf8 tools\live_acceptance.py --source 'D:\Temp\Ердаут\5800-1.3-АР-2_3_REV.0.dwg' --build new_reference.blend
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --background artifacts\erdaut_checked.blend --python-exit-code 3 --python tools\verify_blend.py
```

Unit-тесты используют fake COM. Blender fixture — настоящий bpy с синтетическими
данными. Live acceptance — настоящий MCP stdio, AutoCAD COM и Blender на DWG.
Доказательства этих трёх уровней не взаимозаменяются.

Исходные COM-записи и полный отчёт встроены в BLEND как zlib/base85-текстовые
блоки `AutoCAD source provenance.jsonl.zlib.base85` и
`AutoCAD verification report.json.zlib.base85`. Проверка поддерживает также
ранний несжатый формат. `tools/rebuild_blend.py` позволяет автоматически
пересобрать сцену после исправления адаптера из этих записей в памяти; это
операция над итоговым BLEND, оригинальный DWG не преобразуется.
