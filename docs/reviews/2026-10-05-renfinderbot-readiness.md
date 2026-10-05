# RenFinderbot: готовность локального пакета

5 октября 2026. BelgradAppartment, Telegram-бот RenFinderbot. Продолжена существующая задача «Review final-check model», ID `01a0f2be-7d58-7da3-a6f5-a3fdedadd0f6`. Новая задача и параллельная реализация не создавались.

## 1. Результат и подтверждённый production

Первый переход 0014→0016 теперь имеет реализованный совместимый fallback и повторяемое восстановление после прерываний. Найдена существующая свежая off-host копия; её manifest совпал с receipt. Реализован бюджетный ограничитель для будущего eval, выполнены Linux/CLIP smoke на ARM64 и x86_64, синтетическая миграция, восстановление backup и откат кода с сохранением очередей. Исправлены замечания независимого ревью. **Новая модель не выбрана и не включена; production не изменён.**

Read-only SSH 05.10 в 10:14 UTC подтвердил release `4235abf9da9ea33d0c07aede0ae42f02dcdd1e7a`, схему `0014_group_availability_index`, Python 3.12.3 и Linux x86_64. Семь хешей исходников/lockfile совпали с release commit. Использован существующий ключ: публичный fingerprint совпал с AWS EC2 key pair, host key проверен через сохранённый `known_hosts` при StrictHostKeyChecking=yes. Никаких новых ключей, правил firewall или принятия неизвестного host key. SSM не зарегистрирован. EC2 console output не дал дополнительного fingerprint; он не выдаётся за доказательство доверия.

Monitor, site и admin active/running; nightly/weekly timers active/waiting. Subscription bot inactive/dead. В базе: 460 MATCHED, 364 NO_MATCH; 459 PUBLISHED и 1 PENDING match; 132 delivery со статусом sent. Таблиц validation_jobs, digest_work_items и listing_identity_edges ещё нет. Это текущий снимок, а не доказательство отсутствия прежних сбоев.

Журнал содержит 191 решение `claude-haiku-4-5`: 91 same, 100 different, от 12.08 до 05.10 07:58 UTC. P50 — 3308 мс, p95 — 5887 мс. Это только записанные verdicts, не все попытки и не end-to-end latency. Во всех 191 записях отсутствует usage: фактическую API-стоимость и долю ошибок восстановить нельзя. Эти same/different не являются размеченной оценкой качества. Секреты, .env, сырые диалоги и фотографии не читались; исходные defaults отдельно от фактических process overrides.

Архитектура и исходные риски зафиксированы в `2026-10-05-renfinderbot-model-plan.md`; реализованные fail-closed matching, independent photo pairs, durable work и identity — в `2026-10-05-renfinderbot-implementation.md`. Настоящий документ уточняет прежний снимок readiness после возобновления.

## 2. Изменения и закрытые риски

| Приоритет и риск | Результат | Проверка / код |
|---|---|---|
| P2: eval после сбоя или рестарта может забыть расход | SQLite ledger резервирует верхнюю стоимость до dispatch; неизвестное списание остаётся занятым. Общий, этапный и количественный лимиты сохраняются между процессами. Повтор attempt ID запрещён. | `eval_budget.py:140`; 8 процессов, 3 допуска из 8 при лимите 30 microUSD |
| P2: частичный usage ошибочно возвращает резерв | Любое неподдерживаемое billing-поле оставляет полный резерв и включает durable halt. Изменившаяся модель или превышенный quote останавливают дальнейший dispatch. | `eval_budget.py:217`, `guarded_eval.py:27`; cache-creation/unknown-dimension regressions |
| P2: округление зависит от глобальной точности Decimal | Точный Fraction и округление вверх до microUSD; Quote 99×1.05 + 1×0.01 всегда 104 microUSD, в том числе при Decimal precision=2. | `eval_budget.py:46` |
| P2: additive schema ошибочно считалась достаточной для rollback | Старый runtime должен иметь проверенный контракт совместимости, привязанный к manifest и хешам критического кода. Отсутствующий/повреждённый контракт блокирует замену кода. | `scripts/release.py:769` |
| P2: устаревшая декларация могла скрыть новую миграцию | Actual heads читаются из Alembic ScriptDirectory проверенного пакета. Контракт сверяется до stop/swap; результат upgrade — до изменения units и restart. | `scripts/release.py:1034`, `scripts/release.py:1084`; stale-head repro теперь отвергается |
| P2: прежний production не понимает очереди 0016 | `--fallback-packet` принимает отдельный проверенный runtime, fixed checks и actual migration heads. До stop готовятся оба окружения. На0014 возвращается старый код, на 0016 — совместимый fallback; 0015/unknown оставляют writers остановленными. | `release.py:892`, `release.py:577` |
| P2: прерывание между rename и journal | Atomic JSON, durable swap/restore intents и возобновление через CLI; исходный legacy archive и новые данные сохраняются. Проверены два последовательных прерывания. | `release.py:125`, `release.py:543`, `release.py:621` |
| P2: fallback заменяет собственный recovery helper | Проверенный controller сохраняется в `.releases/<candidate>/control/release.py`, SHA и команда — в activation receipt. Он работает и после установки старого helper e9dddef. Права fallback явно 0750 при обоих umask. | `release.py:968`; независимый repro закрыт |
| Контракты Python критического пути | Исправлены coroutine callback alias, типы session/Select, detail-return, snapshot buffers, source weights и metadata отмены транзакции. Алгоритмы matching и encoder не менялись. | `monitor.py:206`, `candidates.py:58`, `orchestrator.py`, `digest_parser.py` |

Файлы budget/guarded/candidates находятся в `src/matching`, monitor/digest — в `src/telegram`, orchestrator — в `src/scrapers`. Выявленные риски воспроизведены на fixtures; это не утверждение о реальном финансовом или клиентском инциденте.

Независимое ревью: 21 budget/adapter test и проверка halt после reopen; финальное release-ревью — 49 tests и отдельный stale-contract repro. Открытых блокеров в просмотренной дельте не осталось. Review не заменяет production rollout gates. Следующее независимое ревью first-transition: 73 focused passed; все воспроизведённые P2 закрыты, включая repeated interruption и реальный прежний helper e9dddef.

## 3. Проверки и их пределы

| Проверка | Результат |
|---|---|
| Полный локальный unit-набор | 1816 passed, 1 skipped, 42 warnings; Python 3.12.13/macOS |
| Browser regression | 54 passed; 61,12 с |
| Linux critical path | 97 passed в текущем release-наборе; прежний critical path — 129 passed; наборы пересекаются |
| CLIP / Linux ARM64 и x86_64 | Реальные публичные веса; finite/512/L2, single-vs-batch, повтор изображения и float16 roundtrip прошли |
| Полный mypy | 261 errors / 36 files → 231 / 32; это ещё не type-clean репозиторий |
| Целевая проверка типов | 6 изменённых модулей чисты; imports остальных модулей follow-imports=silent |
| Lint, shell, diff | Новых Ruff diagnostics 0; shell syntax и git diff --check прошли |
| Синтетическая репетиция | Backup включает committed WAL; 0014→0016, integrity/FK, restore equality/photo decode, запрет downgrade, сохранность pending/UNKNOWN при rollback |

Единственный skip: `tests/site/test_a11y.py::test_pages_have_exactly_one_h1[base.html-ctx0]`: у базового layout нет собственного контентного h1. Проверки matcher/очередей/моделей/миграций не пропущены. Первый unit-прогон обнаружил запрет loopback-портов sandbox и побочный fileConfig в rehearsal; последнее исправлено, полный повтор зелёный. Первому минимальному Linux-образу не хватало git/rsync; после установки этих тестовых утилит прогон прошёл.

Контейнеры: Python 3.12.15, torch 2.12.0+cpu, torchvision 0.27.0+cpu, open_clip 3.3.0. ML-версии совпали с production. Исходники смонтированы read-only, во время smoke сеть отключена. Публичные packages/веса скачивались отдельно; пользовательских данных в контейнерах нет. X86_64 выполнялся через эмуляцию на Mac: 25,40 с cold / 20,74 с на batch из 3 — не прогноз скорости AWS. ARM64: 1,86 с / 0,53 с. Patch-версия Python, ОС и CPU отличаются от production; native staging/CI остаётся отдельной проверкой.

**Открытая ML-особенность:** open_clip предупреждает `quick_gelu=False` против `openai` weights `quick_gelu=True`. Production использует те же ML-версии и неизменённый encoder. Автоматически включать QuickGELU нельзя: это смена пространства embeddings и возможная несовместимость с сохранённой базой. Сначала отдельный encoder version/hash и сравнение на разрешённом наборе; затем план пересчёта и dual-read/отката, если изменение оправдано. Текущий smoke не доказывает качество распознавания квартир.

## 4. Бюджет и следующий модельный eval

Текущий runner **только offline**: он не принимает live transport, не ищет credentials и не вызывает provider SDK. Суммы в его ledger — симуляция; реальные расходы этой работы — 0 model API calls. Это техническое доказательство учёта, не разрешение тратить деньги.

Заложены неизменяемые лимиты будущего предложения: $50 API usage всего; pilot $20 / 1200 запросов, confirmation $30 / 1600. Учитываются все arms, повторы и неизвестные списания. Cancel до dispatch возвращает деньги, но расходует request-count; после dispatch автоматического возврата нет. Лимит нельзя увеличить при reopen. До живого транспорта нужна проверенная верхняя граница токенов/изображений и всех billing dimensions. Без неё запрос запрещён. Ledger сам не превращает оценку входа в достоверный worst-case quote.

Выбор остаётся между Haiku baseline, Luna и Sonnet по реальным задачам бота, без обещания превосходства. Пилот: 240 кейсов (80 same, 80 похожих different, 40 uncertain, 40 stress); tuning 80 и frozen 160. Четыре arm — старый и исправленный выбор фото на Haiku, Luna, Sonnet; общий безопасный prompt и очищенный payload. Максимум 1200 запросов с повторами. Confirmation — до 800 новых кейсов, кандидат против исправленной Haiku, максимум 1600 запросов. Квартира/связанные серии здания не пересекают splits; два независимых разметчика и adjudication.

Критерии: нет wrong-apartment autoaccept; same recall ≥95%; не менее 300 независимых принятых решений и 300 different без false accept. Ноль ошибок из 300 ограничивает одностороннюю 95% верхнюю границу примерно 0,994%, а не доказывает нулевой риск. Noninferiority margin 2 п.п. — paired cluster intervals; заданный объём может дать inconclusive. Отдельно retrieval recall@K, uncertain/coverage, все исходы/timeout, p95 eligible ≤8 с и cost/correct accepted.

Ставки, сверенные в исходном плане 05.10: Haiku $1/$5, Luna $0.10/$0.50, Sonnet $2/$10 за 1M input/output. Пилот при 6000 input / 200 output ≈$8,61; при 24000/400 ≈$31,98 и не помещается в pilot cap $20. Превышение прогноза означает stop/inconclusive, а не автоматическое повышение лимита. Налоги/FX/подписки и покупка данных не включены. Источники: [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).

Перед передачей реальных фото требуется отдельное разрешение пользователя на данные и конкретных провайдеров. До 6 нормализованных JPEG плюс конечные цена/площадь/комнаты; исключены диалоги, caption, телефоны, chat/user IDs, URL, точный адрес, EXIF. Лица, документы и контактные надписи в пикселях нужно локально редактировать либо исключать кадр. Очистка не делает интерьер гарантированно анонимным. $50 и providers/data scope пока не одобрены.

## 5. Первый переход реализован; следующее согласование

**Локальный blocker первого перехода 0014→0016 закрыт.** Candidate принимает `--fallback-packet`; wrapper передаёт оба проверенных архива. Fallback — ранее испытанный commit `e9dddefc185db99a60a37199c7efec367f801199`, архив `renfinder-e9dddef-local-only.tar.gz`, SHA256 `b2091cecf32a8d76974afbe32adbcb7d03f7c4cdb4f53db86ac61e87cb5ec97c`. Новый release controller хранится вне заменяемого runtime. При откате использовать `rollback_command` из `.releases/<candidate>/activation.json`, а не helper внутри уже заменённого `scripts/`. Ни DB downgrade, ни восстановление старой DB поверх новых событий не выполняются.

25 новых synthetic integration tests используют настоящую SQLite/Alembic миграцию и файловые переименования; systemd, установка venv и HTTP-ответы перехвачены. Проверены pending validation, digest lease, identity edge/event, watermark, sent/UNKNOWN и media после ошибки/прерывания; до миграции возвращается полный legacy runtime. После миграции ставится неизменяемый fallback, проверяются его хеши, schema, effective units и readiness; timers возвращаются последними. Неподдерживаемая/частичная схема оставляет writers остановленными. Повторный запуск использует сохранённый controller с проверенным SHA. Это не доказательство production service-account/HTTP готовности; эти реальные проверки уже встроены в preflight и activation.

**Свежая копия уже существует:** `~/Backups/RentBelgrad/offhost/generations/20261004-121413-8bbe54dd`, создана 04.10 в 12:14:39 UTC. Manifest SHA совпал с существующим restore receipt; 391439 файлов, 33,68 GB логически. Receipt подтверждает offhost, catalog/apartment/photo HTTP 200, decode фотографии, config/session hash/permissions/encrypted-storage proof. Live authentication sessions ранее не проверялась; сейчас protected payload не открывался. Полное повторное хеширование 33,68 GB не выполнялось; использован уже существующий restore proof. Новая выгрузка, удаление копий или production changes не выполнялись.

Политика существующего LaunchAgent: попытка каждые 6 ч/при загрузке, snapshot не чаще 24 ч, минимум 20 GiB свободно, сохраняются две завершённые проверенные generation; ротация только после успешного verify/promote. Текущая копия удовлетворяет gate ≤36 ч до **06.10.2026 00:14:39 UTC / 04:14:39 GST**. Перед выпуском нужен повторный age/schema/proof check; если она устареет, использовать следующую уже авторизованную generation либо отдельно согласовать обновление существующей backup-процедуры. Перенос на другой host сейчас не нужен.

**Ближайшее отдельное решение:** разрешить push только ветки `codex/renfinder-validation-20261005` и запуск GitHub Actions unit/browser для точного commit. Merge/deploy в это разрешение не входят. После зелёного CI выпуск согласовывается отдельно: конкретные candidate+fallback SHA, актуальный backup receipt, окно работ на `/opt/otz`, preflight обоих venv/Chromium от otz, migration 0014→0016, возврат прежнего списка сервисов/timers, local readiness и canary monitoring. Автоматического deploy в текущем workflow нет.

Stop/canary: wrong-apartment accept, потеря queued work, duplicate delivery, рост pending/lease age, UNKNOWN delivery, schema/manifest mismatch или неподтверждённый расход. Model rollback возвращает baseline модель внутри нового fail-closed runtime. Платный eval, cap $50 и отправка реальных фото Anthropic/OpenAI требуют отдельного согласования; этой работой они не выполнялись. Production по проверенному снимку остаётся 4235abf / schema 0014.

Ветка и задача прежние. Подробные receipts/hashes — `2026-10-05-renfinderbot-first-transition-evidence.json`, предыдущий снимок — `2026-10-05-renfinderbot-readiness-evidence.json`. Точный новый commit и SHA локального candidate packet записаны в release receipt. Повторить synthetic cases: `python scripts/run_offline_tests.py tests/test_release_first_transition.py`; старая backup rehearsal остаётся в `scripts/rehearse_validation_release.py`.
