# RenFinderbot: решение после возобновления

> Обновление readiness от 05.10, после 10:14 UTC: см. [подтверждённый production, новые проверки и оставшиеся gates](2026-10-05-renfinderbot-readiness.md). Ниже сохранён предшествующий снимок; его формулировки «ещё не проверено/не реализовано» относятся к тому времени.

05.10.2026. Пользователь снял паузу в 09:19 UTC. Кодовый пакет не переписывался: `3a3f68041a722f78d26bffd81a4deff463a91a5c`, ветка `codex/renfinder-validation-20261005`. Платные API, передача реальных фото, push и deploy не разрешены общим возобновлением и не выполнялись.

Готовый implementation PDF: **library_file_id `libfile_ab7775a35a3081919d25a7d91e69e78a`**, version0. Это уже созданный файл, не прежний PDF планирования.

## Решение

Сохранить Haiku и готовый пакет исправлений; новую модель выбирать измерением после разрешения данных/расходов. Сейчас локальный код готов к рассмотрению, production release пока не готов. Следующая работа без платных вызовов: получить подтверждённый read-only доступ к действующему серверу, подготовить изолированную Linux/staging репетицию и локальный бюджетный runner с fake transport. Не запускать второй Telegram monitor и не копировать реальные данные в staging автоматически.

## Что проверено после паузы

- 36/36 SHA256 изменённого кода совпадают с ранее испытанным пакетом. HEAD сохранён; существующие tracked файлы не менялись. Полные тесты повторно не запускались без причины.
- Ранее: 1761 unit passed/1 skipped, 54 browser passed, 18 offline contracts/API0, независимое review закрыто. Тесты шли в Python3.12.13/macOS, это не Ubuntu CI.
- Согласованность `uv.lock` с проектом проверена offline:107 packages. Резолвер выбрал доступный CPython3.13.15; этот успех не подменяет тесты на 3.13 или серверной версии Python.
- Единственная Alembic head: `0016_identity_edges`; проверены shell syntax и whitespace diff.
- GitHub read-only metadata: `MWildFire/BelgradAppartment`, private, не archived, default branch main. Push/CI trigger не выполнялись. Существующий checks workflow имеет unit и browser jobs; deploy workflow отсутствует.
- Публичные HTTP: `rentbelgrad.com`200, `admin.rentbelgrad.com`401. Это доступность внешнего контура, не подтверждение версии/DB/очередей.
- Актуальная проектная инвентаризация уже относит RentBelgrad к AWS: повторная миграция не нужна. В локальном SSH config нет подтверждённого маршрута RentBelgrad/otz/currentIP; ключи не извлекались. Authenticated preflight не выполнен.
- Локальный test venv содержит Anthropic SDK, но не `torch`/`open_clip`. Unit-тесты используют синтетические vectors; настоящий CLIP image encoding и его зависимости нужно проверить на isolated target environment. Не устанавливались новые зависимости/модели.
- Штатный `scripts/release.py prepare` собрал локальный архив именно 3a3f680; manifest всех файлов сверён. Пакет не отправлялся и не активировался. Подробности, SHA256 и путь: `2026-10-05-renfinderbot-resume-readiness.json`. Архив находится вне checkout в `../readiness/`; test receipt честно помечен local macOS, не remote CI.

## Точный skip и остаточный type debt

Skip: `tests/site/test_a11y.py::test_pages_have_exactly_one_h1[base.html-ctx0]`, строка 133, причина `base shell has no content <h1> by itself`. У базового HTML-layout нет собственного контентного заголовка. Подтверждающий запуск файла до паузы:41 passed / 1 skipped. Это не пропуск matcher, LLM, очереди или миграции.

Полный mypy: **261diagnostic/36 files из 95 source files**, baseline 314, новых 0, устранено 53. Целевая проверка 15 новых/переписанных файлов чистая. Категории включают 62 no-untyped-call,48 no-untyped-def,26 type-arg,25 union-attr. Больше всего: nekretnine34, crawl_evidence23, orchestrator21, operational_health19, cli17. Nullable/attribute/contract ошибки могут обозначать реальные дефекты, это не весь косметический долг. Missing imports torch/open_clip отражают неполную optional ML-среду. Target3.12; installed NumPy stubs несовместимы с mypy target3.11. Python3.11 и полный type-clean не подтверждены.

## Конкретный eval

**Пилот 240 кейсов:**80 same,80 похожихdifferent,40 uncertain,40 stress. Tuning80 = (30/30/10/10), frozen160 = (50/50/30/30). Два независимых разметчика; disagreement→adjudication либо uncertain. Все материалы одной квартиры/близких серий здания принадлежат одному split. Stress преобразования не считаются новыми независимыми квартирами.

На кейс два набора по 2–3 фото, до 6JPEG на запрос, максимум 1440 фотовключений в пилоте; уникальных файлов может быть меньше. Same требует реально одной квартиры и общих стационарных деталей в независимых ракурсах. Negative — похожие кухни/комнаты разных квартир. Uncertain — недостаточно общих ракурсов. Stress — crop, watermark, поворот, искусственная инструкция на изображении. До любых API локально фиксируются лицензия/consent, очистка, hashes и labels.

Четыре arm: Haiku со старым выбором фото, Haiku с исправленным выбором, Luna, Sonnet. Во всех arms применяется очищенный payload и общий prompt; первый arm — контроль выбора фото, не точная копия старого небезопасного production. 240×4=960 вызовов; для 30 кейсов ещё 2 повтора×4=240. Всего максимум 1200plannedcalls. Первые 12 tuningкейсов дают calibration usage/latency и уже включены в счётчик.

**Подтверждение:** до 800 новых frozen кейсов:400 same,300 different,100 uncertain; выбранный кандидат против исправленной Haiku, максимум 1600 вызовов. После открытия holdout параметры не менять. При новом prompt нужен новый holdout.

Gates: нет wrong-apartment autoaccept; same recall≥95%; минимум 300 независимых принятых решений без false discovery и отдельно 300 different без false accept. 0/300 даёт одностороннюю 95%верхнюю границу около 0.994%, не нулевой риск. Noninferiority margin2 п.п. проверяется paired cluster intervals;800 кейсов не гарантируют достаточную уверенность. Отдельно retrieval recall@K, uncertain/coverage, all-outcome latency, p95eligible≤8s, request deadline20s, cost/correct accepted и cost/inbound. Если gate не достигнут — Haiku/manual review, не автоматическое увеличение выборки/бюджета.

## Данные, провайдеры и деньги

Anthropic получает очищенные фото armsHaiku/Sonnet; OpenAI получаетLuna. Payload: до 6 нормализованныхJPEG плюс конечные числовые price/area/rooms. Диалоги, caption, телефон, chatID, username, URL, точный адрес, EXIF и mappingкпользователю исключаются. Лица, документы, семейные фото и контактные watermark локально редактируются либо кадр исключается. Реальный интерьер может оставаться узнаваемым: очистка не заменяет согласие на передачу.

Без реальных пользовательских данных возможны offline fixtures/рендеры/fake providers с API cost 0. Платные модели на искусственных изображениях требуют согласования APIрасходов; это проверка I/O/robustness, не production accuracy. Реалистичная альтернатива — разрешённые владельцем/лицензированные фотографии пустых квартир, но переносимость на поток бота остаётся ограничением.

Предложение на согласование: **hard cap50 USD APIusage всего: пилот 20 + подтверждение 30**. Это не одобрение расходов. Новые подписки, данные, автопополнение и налог/FX не входят; если нужен общий лимит списания 50 USD, netAPIcap заранее уменьшается под фактические начисления. При 6000input/200output пилот≈8.61 USD; при 24000/400≈31.98 USD, поэтому завершение в 20 USD не обещается. Подтверждение 800 кейсов при 6000/200≈6.16 USD дляLuna+Haiku или 16.80 USD дляSonnet+Haiku.

До платного запуска нужен persistent budget ledger: reserve worst-case до отправки, один bounded request/SDKretries0, не освобождать резерв неизвестно оплаченного отказа без reconciliation, учёт всех arms/repeats. Если верхнюю границу цены нельзя надёжно определить — запрос не отправляется. После calibration проверить projected total; на лимите остановиться и вернуть inconclusive. В текущем коде есть usageaudit, но такого бюджетного runner пока нет.

Ставки перепроверены 05.10: Haiku 1/5 USD за 1Minput/output; Luna 0.1/0.5; Sonnet 2/10. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna). store=false не даёт обещания zero retention: [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data), [Anthropic retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data). Стандартные APIусловия предусматривают до 30 дней и safety/legal/feature exceptions; регион/ZDR конкретного аккаунта не подтверждены. Нужны существующие разрешённые APIcredentials, не передача ключей в чат.

## Выпускные gates и разрешения

1. **Read-only сервер:** согласовать действующий host/login/существующий доступ с migrationlead. Проверить revision/sourcehash/schema/Python/versions, сервисы/timers, localhealth/ready, агрегаты queue/UNKNOWN и whitelistconfig. Не читать секреты/сырой текст, не запускатьscrape/model/send, неrestart/migrate. Сейчас блокер — нет подтверждённого authenticated пути, а не доказанный отказ сервера.
2. **Локальная/staging репетиция:** target Linux/Python/CLIP smoke, миграции 0015/0016 на временной копии, crash/restore и совместимый rollback. Реальные DB/photos на другую машину — отдельное data-scope разрешение. Синтетическая БД достаточна для технической репетиции, но не доказывает перенос конкретной production DB.
3. **Push/PR:** отдельное разрешение отправить ветку с 3a3f680 в подтверждённый private GitHub.49 изменённых файлов включают внутренние reviewdocs. Затем UbuntuCIunit/browser/lint/shell; merge отдельно. Общая команда продолжить не разрешаетpush.
4. **Deploy:** отдельное разрешение точного commit/host/window. Нужен backup≤36h с offhost+restore/application/photo evidence, проверка live schema against backup, политикиpending/pHash/identity и план canary. Текущий localpacket не является допуском кdeploy.
5. **Rollback:** сохранить queues/edges/deliveryledger; downgrade запрещён. Совместимый rollbackbuild должен понимать новые очереди. Обычный возврат старого fail-openmatcher недопустим как безопасный откат; автоматическое restorecode вreleasehelper требует отдельной проверки именно выбранногоpreviousbuild. Новую модель нельзя откатывать через ENABLE_LLM_VALIDATION=false.

Ближайшее действие: получить у migration lead подтверждённый существующий SSH-маршрут к текущему AWS-серверу и завершить уже разрешённый read-only preflight; новые права или повторное согласие на чтение не нужны. Далее — техническая репетиция на синтетической staging-БД. Отдельным решением пользователя остаются разрешённые фото, Anthropic/OpenAI и лимит API $50. До этого можно завершать локальную подготовку без оплаты/передачи данных. Решение оpush/deploy не объединять с оплатойeval.
