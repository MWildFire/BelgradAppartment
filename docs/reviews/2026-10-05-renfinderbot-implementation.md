# RenFinderbot: локальная реализация и приёмка

Дата: 5 октября 2026. Проект: BelgradAppartment, Telegram-бот RenFinderbot. Статус: локальный пакет подготовлен; production не изменён, новая модель не включена.

## 1. Результат и границы

После плана пользователь разрешил разработку: «Делай разработку по этому плану. Утверждаю, вперед» (05.10, 07:27 UTC). Реализованы контракты и защита обработки ошибок, детерминированный matching, независимые пары фото, долговечные очереди и связи квартир. Подготовлены offline-контракты трёх модельных профилей. Выбор модели по качеству остаётся открытым.

Ветка: `codex/renfinder-validation-20261005`. База: `5338a5e47b43422b6e7943c5af9eea75097152ac`. Изолированный checkout: `/Users/mvstrike/Documents/Codex/2026-10-05/task-2/BelgradAppartment`. Исходный checkout и пользовательские незакоммиченные документы сохранены. Кодовый commit указан в сопроводительном результате; документ входит в него.

Продолжено существующее ревью «Review final-check model», task `01a0f2be-7d58-7da3-a6f5-a3fdedadd0f6`. Полный архитектурный анализ, исходные file:line, модельные цены, критерии holdout и план выпуска: `2026-10-05-renfinderbot-model-plan.md` рядом с этим документом. Исторический production `4235abf` относится к 30.09, актуальная версия сервера не проверялась.

Готовы локальные части A/B/C и инфраструктура D. Этапы E/F (модельный benchmark, shadow, canary, выпуск) не выполнялись. Не было SSH, платных model API, отправки диалогов или фотографий провайдерам, production-миграций, перезапусков и внешних сообщений пользователям. Разрешённая координация с задачей AWS выполнена: BelgradAppartment не перезапускать из-за этой ветки.

## 2. Что исправлено по результатам ревью

| Риск | Изменение и проверяемый результат | Код текущей ветки |
|---|---|---|
| R02 / P1: порядок кандидатов менял winner и обход LLM | Сигнал считается отдельно для каждого кандидата; стабильная сортировка по числу пар, методу, score, primary, ID. Перестановка строк не меняет результат. | `matcher.py:443`, `pairs.py:30` |
| R03 / P1: ошибка API принималась как совпадение | `same`, `different`, `uncertain`, `unavailable`, `invalid_evidence`. Ошибка оставляет durable pending; Match и обычная доставка не создаются. Lease/CAS защищает конкурентное завершение. | `matcher.py:66`, `validation_jobs.py:16`, `pipeline.py` |
| R04 / P1: копии одного фото считались двумя | Дедупликация pHash/векторов, one-to-one matching максимальной мощности. Повтор снимка не повышает число независимых подтверждений. | `pairs.py:11`, `pairs.py:53` |
| R01 / P1: терялся живой sibling квартиры | Постоянные identity и прямые evidence edges отделены от активности. Merge/split/remerge обратимы, tombstone не перезаписывается автоматикой, primary выбирается среди активных. | `identity.py:17`, `dedup.py` |
| R05 / P2: частичный digest терялся после watermark | Все work items сохраняются до продвижения watermark в одной транзакции. Независимые leases, retry, уникальность source/message/share; сбой одной строки не теряет остальные. | `digest_jobs.py:20`, `monitor.py` |
| R06/R07 / P2: неверные или отсутствующие изображения | Используются найденные CLIP-пары, SHA-manifest, проверенные immutable bytes. Непригодное/неоднозначное evidence отправляется на review до SDK. | `evidence.py:107`, `photo_manifest.py`, `llm_validator.py:161` |
| R08 / P2: один кандидат и принудительный yes/no | До трёх разных identity; второй может быть принят после отказа первого. Несколько same, unresolved альтернативы или overflow не публикуются автоматически. | `matcher.py:195`, `matcher.py:213` |
| R09 / P2: вложенные retry блокировали очередь | SDK retry=0, запрос 20 s, общий этап 45 s, durable retry вместо sleep; breaker после пяти ошибок, один half-open probe через 5 min. | `llm_validator.py:206`, `circuit_breaker.py:17`, `validation_retry.py:19` |
| R10 / P2: неполный audit | Логируются все исходы, requested/actual model, prompt/evidence hash, latency и доступный token usage. `llm-report` отделяет deferred от same/different. | `llm_validator.py:93`, `cli.py:2320` |

Пути matcher/pairs/identity/evidence относятся к `src/matching/`; digest/monitor к `src/telegram/`; pipeline/retry к `src/jobs/`. Риск из исходного ревью обозначает воспроизведённое поведение прежнего кода, а не доказанный инцидент у клиента.

## 3. Защита evidence, очередей и данных

Проверка фотографий привязана к содержимому. Manifest хранит SHA256, pHash и путь; legacy-файлы сопоставляются по содержимому, а коллизии разных файлов отклоняются. Выбранные bytes фиксируются до нормализации, поэтому подмена файла между выбором и запросом не меняет evidence. Изображения декодируются, EXIF удаляется, RGB JPEG ограничивается 1600 px; есть ограничения 30 MP, 20 MiB на исходный файл и 32 MiB/64 файла на сторону альбома. Выбираются максимум три пары.

Провайдеру готовятся только нормализованные фото и конечные числовые цена/площадь/комнаты. Caption, raw address, телефон и свободный текст исключены. Надписи внутри изображения автоматически не редактируются: реальные фото нельзя считать полностью обезличенными только из-за этой фильтрации. Отдельное разрешение передачи данных остаётся обязательным.

До принятия Match повторно сверяются состояние inbound, фотографии, metadata и identity кандидата. Устаревший cache и изменение объявления во время запроса приводят к defer. Сетевые ожидания проходят вне DB-транзакции. Lease истекает; поздний worker не может завершить чужое задание. SDK stop_reason должен быть `end_turn`, даже если оборванный ответ содержит синтаксически корректный положительный JSON.

Очередь validation имеет лимит пяти реальных проходов и пятиминутную задержку; uncertain/invalid evidence требуют review. Circuit-only проход возвращает списанную попытку, смешанный проход с реальным provider error расходует её. Это отдельная регрессия финального ревью. Breaker общий для процесса monitor, не распределённый и сбрасывается после рестарта; межпроцессная защита обеспечивается DB leases.

Digest выполняется с deadline 120 s, lease 180 s, максимум пять попыток; directory принадлежит конкретной попытке. Identity сохраняет все подтверждённые прямые связи, включая треугольники, и журнал операторских решений. Защитный лимит 25 000 edges останавливает слишком плотный scan до записи. Purge не удаляет anchors и endpoints исторических edges, в том числе отозванных.

Успешный pHash bypass сохранён как прежняя политика, теперь с независимыми парами. Семантическая корректность всех pHash-объединений и transitive identity не доказана синтетическими тестами; исторические группы не импортируются как готовое подтверждение. Оператору всё ещё потребуется процедура проверки ошибочных edges; Python API для revoke/restore есть, отдельного UI нет.

## 4. Проверки и независимое ревью

Полный финальный offline-прогон: **1761 passed, 1 skipped, 42 warnings, 122.95 s**. Исходящие Python DNS/connect заблокированы runner; loopback разрешён для тестовых серверов. Предупреждения относятся к Alembic configuration, тесты не падали.

Браузерный набор существующего CI: **54 passed, 54.85 s**, отдельный изолированный сервер, временная синтетическая БД, пустые Telegram token/recipients. Общая платформа проверена для исключения регрессий от ORM/migration изменений; сайт не был самостоятельной целью разработки. Удалённый GitHub CI в этой работе не запускался.

Ruff по изменённым строкам: **0 новых замечаний**. `git diff --check` и `bash -n scripts/*.sh` прошли. Целевая mypy-проверка 15 новых/переписанных source files чистая. В полной проверке относительно базы: **314 → 261 ошибки, новых 0**. Полный репозиторий ещё не type-clean. Использован Python target 3.12: установленные NumPy stubs не разбираются с target 3.11; совместимость отдельной среды 3.11 этим не доказана.

Независимый reviewer сначала проверил основной пакет (128 passed), затем финальный breaker delta (43 passed). Он выявил расход попыток на circuit_open; после исправления подтвердил закрытие. Открытых блокеров в проверенном diff нет. Были также устранены замечания по stale cache, TOCTOU evidence, memory bound, triangle edges, purge FK и incomplete response. Это code review и синтетические регрессии, не доказательство качества реальной vision-модели.

Регрессии покрывают: повторные фото, неоднозначный pHash, поздние индексы пары, подмену bytes, stale cache, deadline/529/refusal/truncation, top-K ambiguity, concurrent leases, partial digest/replay, identity merge/split/revoke, сохранение delivery ledger при миграциях, отсутствие send/notify для pending, circuit-only recovery и mixed-attempt budget. Новый тест миграции проверяет отказ downgrade без потери данных.

Логи и машинный receipt: `2026-10-05-implementation-evidence.json`, `2026-10-05-implementation-unit.txt`, `2026-10-05-implementation-browser.txt`, `2026-10-05-implementation-types.json`. Подробные fixtures: `tests/test_validation_safety.py`, `test_identity_edges.py`, `test_migration_validation.py`, `test_circuit_breaker.py`, `test_model_adapters.py`.

## 5. Модель, стоимость и следующий eval

Runtime остаётся Anthropic Haiku 4.5 (`claude-haiku-4-5`), значение вынесено в конфигурацию. Никакой новый транспорт не подключён к боту. Offline adapters формируют native structured-output payload для Anthropic и OpenAI Responses; Luna uses `store=false`, effort `none`. Единый prompt и строгий schema-контракт поддерживают uncertain. Alias не является фиксированным snapshot: фактически возвращённая модель записывается в audit.

| Профиль | USD / 1M input / output | Оценка при 6000 input + 200 output |
|---|---|---|
| Haiku 4.5 baseline | 1 / 5 | $0.007 |
| GPT-6 Luna, кандидат | 0.10 / 0.50 | $0.0007 |
| Sonnet 5.5, кандидат | 2 / 10 | $0.014 |

Официальные ставки проверены для плана 05.10: [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [Luna capabilities](https://developers.openai.com/api/docs/models/gpt-6-luna). Это сценарная оценка без cache/налогов/failed-billed attempts, не фактический счёт. До трёх candidates может означать до трёх таких вызовов на проход; повторные проходы тоже платные. Unknown usage нельзя считать нулевой стоимостью.

Offline eval: **18 из 18 контрактных сценариев**, три профиля × шесть synthetic ответов; API calls **0**, фактическая стоимость тестов **$0**. `2026-10-05-offline-contract-eval.json` воспроизводится модулем `src.matching.offline_eval`. Он проверяет сериализацию и семантику результата, а не accuracy. Ранее подготовленные 32 acceptance specs сохранены; фотографии для model holdout не приложены.

Не завершены D/E: UI/export human review, агрегированный billing/cost dashboard, оплачиваемый hard-cap, размеченные разрешённые фото, сохранённый blind holdout и модельный benchmark. Доступный usage логируется, `llm-report` показывает outcomes/confidence, но не заменяет reconciliation со счётом. Победитель, снижение ошибок и реальная экономия не заявляются. Требуются отдельные разрешения на provider, данные и бюджет; пример $20 из плана не является одобрением расходов.

## 6. Эксплуатация, откат и следующий безопасный шаг

Пакет содержит additive migrations `0015_validation_work` и `0016_identity_edges`: photo manifests, validation/digest jobs, identity/edges/events. Legacy delivery и enum states не переписываются. Миграции запускались только на временных тестовых БД. Перед любым выпуском: свежая версия production/schema/config без секретов, выбор совместимой базы, проверенный backup с restore, rehearsal миграции и профилирование representative synthetic нагрузки. Целую локальную ветку поверх исторического production переносить нельзя без сверки divergence.

На отдельной локальной/staging копии оператор может использовать `otz validation-jobs --limit 50` для чтения очереди и `otz llm-report --days 7` для исходов. `otz retry-validation INBOUND_ID` возвращает нерешённый кейс в обработку и отказывает при живом lease либо существующем Match. После запуска worker такая команда может вызвать платную проверку; это не команда для несанкционированного production запуска. Digest needs_review пока требует инженерного разбора, безопасная массовая replay-команда не добавлена.

При выпуске наблюдать oldest pending, долю review, реальные provider errors, attempts, p95 end-to-end, unknown usage и delivery UNKNOWN. Начальные цели из плана: warning pending >15 min, critical >60 min, расследование пяти последовательных ошибок. Автоматические оповещения и бюджетные стопы не установлены. Implemented breaker: пять ошибок/5 min; число три из первоначального примерного monitoring-текста заменено этим явно тестируемым значением.

Откат сохраняет DB queues, edges, audit и delivery ledger; downgrade миграций намеренно запрещён. Откат модели — на проверенную Haiku-конфигурацию с теми же fail-closed guards. `ENABLE_LLM_VALIDATION=false` не является безопасным rollback: это открывает CLIP-путь. Код можно откатывать только на compatibility build, понимающий новые очереди; иначе остановить обработку до исправления. Возврат старого fail-open matcher без ограждения не допускается. Автоматического доказанного rollback production в этой работе не было.

Первый следующий безопасный этап: ревью локального commit, synthetic staging rehearsal с миграцией/восстановлением и утверждение операторской политики pending/pHash/identity. Затем отдельно согласовать provider/data/budget и выполнить размеченный eval с gates из плана. Shadow/canary и deploy требуют самостоятельного разрешения и координации с AWS migration lead. Новые API-ключи, подписки, GitHub push и production release в этот пакет не входят.
