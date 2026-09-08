# Продолжение ремонта RentBelgrad — пауза 08.09.2026

**Пауза по прямой просьбе Дмитрия в 13:03–13:06 Asia/Dubai: он переезжает в другую точку. Не продолжать работы и не создавать автоматическое возобновление до его новой команды.**

## Начать отсюда

1. Прочитать этот документ, `HANDOFF.md`, `docs/reviews/2026-09-08-implementation-status.md` и актуальный план `docs/superpowers/plans/2026-09-08-reliability-remediation.md`. Исторические августовские ограничения не заменяют сентябрьский план.
2. Проверить текущий Git и live-состояние, остаточные процессы и свободное место. Не повторять весь исходный аудит.
3. Проверить и интегрировать **два сохранённых worker-коммита ниже**. Затем полный exact-revision unit/browser/lint/shell/lock gate и GitHub CI.
4. Завершить реальную внешнюю резервную копию и strict restore. Текущий 65 GiB `.partial` **не принят как backup**. Не выпускать приложение до восстановления приложения и фото из проверенной внешней копии.
5. После gate выпустить проверенный пакет, применить узкий Caddy photo ingress, согласованные настройки и timers. Провести no-notify canary, полный контрольный проход и реальный автоматический проход. Сохранять фактический уровень приёмки по F-ID; полный успех пока не заявлять.

## Что остановлено и что не менялось

- Production **приложение, `.env`, Caddy, schema и расписания не изменены**. Ни одной repair-версии не выпущено. Schema остаётся `0011_keep_on_site`; на последних проверках monitor/site/admin/Caddy active.
- На сервере созданы только отдельный pinned uv runtime `/opt/otz/tools/uv-venv`, изолированная оценка `/opt/otz/.evaluation/1f60abc` и временные backup/evaluation файлы.
- Полный off-host stream остановлен по просьбе пользователя: remote Python267446 получил SIGINT, в `backup-transfer.log` появился KeyboardInterrupt. Локальное SSH26271 прервано, сессия49336 завершилась255. Файл `.partial` сохранён, restore/hash gate не выполнялся.
- Read-only полный shadow на изолированной копии: Python274276 получил SIGINT, сессия36762 завершилась130. Финальный полный PASS не получен. В логе был challenge на одном сегменте; это незавершённый audit, не доказанный полный source failure.
- Workers остановлены; их процессы завершены, изменения закоммичены. Не оставляли новых фоновых refresh/deploy/backup tasks.
- Codex heartbeat и Mac LaunchAgent **не созданы/не установлены**. Возобновление только по новой команде Дмитрия.
- Snapshot остаётся `shadow`, active не включали. Покупок, тестовых сообщений людям, массового replay не было. Доставка реального monitor оставалась в прежнем рабочем режиме.

## Git и незавершённая интеграция

Main worktree: `/Users/mvstrike/Projects/BelgradAppartment`; branch `codex/reliability-2026-09-08`.

- Исходный baseline `75f870c`, code `38ad154`.
- Последний полностью проверенный и pushed implementation: `1f60abc5ab3ac5b125622a33734ddd5e6662f242`.
- Main code HEAD перед документацией паузы: **`9d93d79`**. После1f60abc: `5dc6ce2` исключает воскресный incremental; `9925de1` включает новый public job в release maintenance exclusion; `9d93d79` распознаёт реальные manual CLI-команды при выпуске/откате. Последний targeted release batch31 PASS.
- Remote private repo `git@github.com:MWildFire/BelgradAppartment.git`. Последние main commits после1f60abc ещё не pushed. Документы паузы сохраняются отдельным локальным commit.

**Worker public cadence — готов, не интегрирован:**

- Commit **`b2c3c70`**, branch `codex/public-freshness-cadence`, worktree `/Users/mvstrike/Projects/BelgradAppartment-public-cadence`; clean.
- Public due20h вместо24h; public-only fair queue с backoff, включая retained inactive public, private исключены; отдельные public cursor/checkpoint; CLI `refetch-details --public-only --budget-sec` без сообщений.
- Новый `otz-public-detail-refresh.service/.timer`:15UTC,500limit/2400sec, service45min. Добавлен в admin schedules. Новый timer необходимо явно enable/start после успешной активации: release возвращает только ранее active timers.
- Nightly Mon..Sat03UTC, timeout `7h 30min`; weekly Sun02UTC, timeout9h; smoke00:30/12:30UTC. Существующий Persistent=true сохранён. Проверить фактическую нормализацию systemd calendar/timeout на Ubuntu.
- Defaults: daily2400,heal200,links250,refetch budget19800; incremental source600/run24000; full source1800/run30000; item90/refetch90/liveness600/leasewait60. Production `.env` ещё содержит прежние значения, их нужно узко обновить.
- Admin backup health больше не принимает receipt из будущего.
- Worker сообщил full1383 PASS/55skip, после последних добавлений combined92 PASS; changed-line Ruff/diff PASS. **Независимый review ещё не выполнен**, общий exact main gate после cherry-pick обязателен.
- Root уже добавил `otz-public-detail-refresh` в release JOBS и реальные manual command names — повторно не делать.

**Worker incremental off-host backup — готов, не интегрирован:**

- Commit **`af805d7994602148f6c6e64f54e8ee108e088123`**, branch `codex/reliability-freshness`, worktree `/Users/mvstrike/Projects/BelgradAppartment-freshness`; clean.
- `scripts/offhost_backup.py run/launch-agent`; recovery `snapshot`, `verify-directory`, `verify --restore-dir`. Immutable generation: свежие online DB/state + полные media hashes, protocol29 openrsync/link-dest без inplace/delete, local/remote locks, minimum disk gate, atomic promotion, last2 retention только после нового PASS. `.partial`/failed не заменяет last good.
- При одинаковом SHA создаёт links явно: mtime после извлечения архива не должен вызвать повторную передачу65GiB. Seed используется только для media; свежая DB всегда отдельно. Backup lock не блокирует scraper: concurrent media change вызывает validation failure, а не ложный success.
- Worker32 PASS (`test_offhost_backup`+`test_recovery`,20.89s), Ruff/diff PASS; реальный local openrsync с3generations, hash/delta/new photo/hardlink/retention/failure cases и offline app/photo smoke. SSH fake; production и реальный LaunchAgent не проверены.
- Receipt: `result=data_restore_verified`, `backup_format=directory_generation`, actual snapshot `created_at`, generation_id, manifest_sha256/generation_manifest_sha256, DB/schema/counts, full files/photo refs, app catalog/card/photo200+decoded. Runner ставит `offhost_verified=true`, но **`protected_recovery_verified=false`** до отдельной свежей проверки config/session; старый true не наследует. Нет автоматического upload receipt в app health.
- **Нет archive_sha256**, потому что это directory generation. `scripts/release.py validate_backup` пока принимает только archive SHA: отдельно согласовать формат без выдуманного archive hash. Первый full-archive release gate можно выполнить прежним форматом.
- Remaining: independent review, integration, свежие protected config/session snapshots и proof, безопасная загрузка health receipt, первая реальная generation, установка/первый автоматический запуск schedule. Mac-only schedule зависит от доступности Mac/сети; не объявлять облачным backup.

## Уже реализованные исправления

- W1/W3: строгие source outcomes/coverage и nonzero degraded; partial failures сохраняются; неполный обход не снимает. Отсутствие подтверждается двумя distinct full weekly за6–9дней, cap100/source. Единая publication policy: pinned/live-sibling/UNKNOWN/warn-first/cap, race-safe UPDATE.
- Additive0012: successful detail/link отдельно от attempts/backoff, реальная deactivation/weekly evidence; historical successes не выдумываются.0013: per-recipient MatchDelivery с immutable payload и subscription cursor/outbox; destructive downgrade запрещён.
- W2: представитель выбирается внутри public, apartment availability одинакова на home/catalog/map/sitemap/card. Retained UNKNOWN/DEAD не получают ложное availability. DEAD исключены из рекомендаций/digest, но могут реактивироваться.
- W3: GET/browser payload probe вместо legacy HEAD; challenge/payment/network→UNKNOWN; failover на следующий разрешённый ISP без implicit direct. Per-platform cross-process lease, bounded waits и budgets; cooperative SIGTERM cleanup/checkpoint. Реальные final proxy metrics, empty/fallback-only/incomplete shadow не PASS.
- W4: fair public/broken/oldest-success queue, required fields+local photos/hashes/image decode для verified success; failed attempts не омолаживают запись; stage/rollback media, narrow shield DB commit; measured quotas, durable run/checkpoints/health, readiness DB503, quiet ops change detector.
- W5: authorized photo GET/HEAD route готов, но live Caddy static bypass ещё открыт; HTML email escaping. Atomic recipient claims, retry только failed recipients, unknown без blind resend, historical publications без replay. Frozen captions/photo order/recipients.1photo→sendPhoto,2–10album,0/missing/error/partial receipts явны. Oversized photo preflight доHTTP. Subscription backlog переживает72h и drains100→30/30/30/10.
- Release packet: exact tracked SHA/test/backup gates, new frozen CPU-CLIP venv, browser install+actual launch под настоящим service HOME, cohorted swap/migration/units/health/hash marker до запуска timers. Rollback только code/env/units без DB downgrade/restore новых leads/messages.

## Фактические проверки

- Exact1f60abc: **unit1372 passed/1skipped/36warnings**, browser54passed, changed-code lint0 новых, shell syntax/frozen107packages/diff PASS. Logs `/tmp/rentbelgrad-release-{unit,browser,static}.log`.
- **GitHub CI Ubuntu24.04 SUCCESS**: https://github.com/MWildFire/BelgradAppartment/actions/runs/34204460465 (unit101990591735/browser101990591552). Locked uv0.8.22. GitHub wrapper fetch_commit_workflow_runs показывает толькоPR-triggered, поэтому дляpush использовать run jobs или UI.
- Production DB copy: old31cards/29apartments→new168/168; lost apartments0, новых publication rights0;48active/120UNKNOWN. Raw184public overlays/177originals. Evidence `2026-09-08-public-selection-copy.json`.
- Isolated Ubuntu1f60abc:77actual details/4sources, source errors0, DB SHA до/после совпал, no-notify. Dry-run liveness diagnostic отдельно имел Halo missing marker/cleanup TargetClosedError; не утверждать zero errors всей диагностики. Evidence `2026-09-08-evaluation-ubuntu.json`.
- Отдельный isolated write canary:20selected/20updated/20verified (5/source),errors0/skipped0,38photos,100.08sec, publicset unchanged. `2026-09-08-capacity-canary-ubuntu.json`. Это маленький sample/экстраполяция, **не достигнутый SLO**.
- Old code0011 на isolatedDB0013: два запуска health/catalog/реальная publiccard200; old ORM insert/update работает, new Lead/outbox row hashes и schema сохранены, FK0/integrityok. `2026-09-08-rollback-compatibility.json`. Local main Python3.12.13 venv, прежний production venv не проверен этим опытом.
- Начальная попытка полного staged shadow ошибочно использовала HOME=/home/otz:0previews/browser startup errors. Фактический HOME пользователяotz — **/opt/otz**. Повтор с правильным HOME работал, но остановлен пользователем до результата. Не выдавать первый env error за отказ всех ISP.

## Backup, private artifacts и server

SSH target `ubuntu@3.121.163.130`, key `/Users/mvstrike/Downloads/beget-migration/beget_migration_key`, root `/opt/otz`, user/groupotz. Не выводить `.env`, credentials, session values. У otz HOME=/opt/otz.

Private root **`/Users/mvstrike/Backups/RentBelgrad/2026-09-08`**, directory700/files600, FileVault On:

- `pre-remediation.tar.gz.partial`: около65GiB, прерванный stream, **unverified**. Возможно все media уже переданы до обрыва manifest serialization; сначала безопасно проверить контейнер. Даже если есть полезные media, нельзя принять incomplete manifest за complete backup. Можно рассмотреть safe extraction как media seed для новой complete generation со свежим manifest/hash, чтобы не качать65GiB повторно. Ничего не удалять до решения.
- `backup-transfer.log`: interruption evidence. Remote helper `/tmp/rentbelgrad-recovery-20260908.py`; временный SQLite snapshot был `/tmp/.recovery-4von1go8/otz.db`. После resume проверить остаток/процессы до нового запуска.
- `protected-recovery.tar.gz`, `protected-recovery-receipt.json`: отдельно реально проверенные2config +2onlineSQLite session snapshots. Archive SHA791c297eab8c9d6cac001b672bb1ace45c4c7e650144b7065d6d241855de4341. Это не заменяет fullmedia restore.
- `db-before.db.gz` (410MB, schema0011), `evaluation-final.db` (migrated0013), `compatibility.db` (только isolated fixtures), evaluation/capacity/shadow JSON.
- `releases/1f60abc/tests.json`, `release.tar.gz`: подготовленный предыдущий пакет407files, SHA72e4818815b3177a6c2bfbf3f11257c41a8235773cb8e892827bdfc584a531e0. **Не использовать для финального выпуска после новых worker changes**; нужен новый exact packet.
- `continuation-tools/`: сохранены подготовленные `rentbelgrad-restore-preserve.py`, `rentbelgrad-close-photo-ingress.py`, `rentbelgrad-set-refresh-capacity.py`, `rentbelgrad-live-verify.py`, shadow/compat scripts. Это подготовленные helpers, production mutations ими не выполнялись. Прочитать перед использованием.
- Restore helper предполагает complete `.partial`, извлекает в `seed-0011` и использует **старый совместимый** `/Users/mvstrike/Projects/BelgradAppartment-backup/scripts/recovery.py` (worktree33490b0/models0011). На паузе extraction не запускалась. Smoke этого старого helper меняет пути внутри копииDB; media seed пригоден, DB не считать pristine accepted generation.
- Actual uv local `/Users/mvstrike/.local/bin/uv`, **не `.venv/bin/uv`**. На сервере `/opt/otz/tools/uv-venv/bin/uv`0.8.22. Приложение production пока со старойvenv.
- Последние disk checks: server145G/91used/54free; Mac549GiBfree до extraction. Server listingmedia65G,inbound2.7G,DB1.5G.

## Конкретные live gates после возобновления

- Independent review + integrate2commits; выполнить `.venv/bin/pytest -q --ignore=tests/e2e`, `RUN_E2E=1 .venv/bin/pytest -q tests/e2e`, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/check_changed_lint.py`, shell checks, `uv lock --check`, diff-check. Не подменять последнююrevision прошлым PASS. GitHub frozen environment обязателен.
- Complete verified offhost backup <=36h, correct schema/FK/counts/allmedia/manifest, actual isolated catalog/card/photo200+decoded; отдельно protected restore. Exact receipt/archive or verified generation format. Не обходить failing gate.
- `scripts/release.py prepare` и `scripts/deploy_packet.py` с explicit target/root/key/pinneduv/packet/backup receipt; `--sudo --activate` в рамках возобновлённого ремонта. Перед release idle jobs/timers; после actual activation.json,hashes,schema/FK,cohort andhealth.
- Узко заменить в Caddy `handle_path /photos/* { root ...; file_server ...; immutable }` на `handle /photos/* { reverse_proxy127.0.0.1:8001; private,no-store }` **с сохранением prefix**. Protected backup,validate,reload. Не печатать остальной Caddy config с auth. Helper подготовлен.
- Узко применить non-secret capacity settings, сохранив private `.env` rollback copy и hash TG/source/matching config. Перезапустить соответствующие app processes для нового Settings; подтвердить shadow/auto-hideFalse/legacy fallbackoff.
- Новый public timer явно enable/start послеhealth/marker. Проверить effective units/dropins, actual next runs; defaults выше не доказательство фактического расписания.
- Live verifier подotz/mainvenv с `--expected-revision FULLSHA`; проверяет DB+runtimehash, catalog/map/mapJSON/sitemap sets,realcard,actualpublicoriginal/variantGET/HEAD200+hash/decode/no-store, existingprivatephoto403/404 и admin401. Worker10fake/synthetic tests; production не запускался. Не читать body private photo при неожиданном200.
- Bounded production canary через actual CLI `nightly-refresh --incremental --max-pages1 --no-notify` с processenv лимитами: detail20,heal0,links20,refetch600,source180,run1800,liveness60; auto-publish/auto-hide off для ясного DB/public before/after сравнения. Не использовать scheduled legacy service дляno-notify: OnFailure/ship-log отправляют сообщения.
- Затем полный контрольный проход через тот же no-notify interface с честнойjournal/coverage/freshness и проверкойснятий. Не делать blindDB rollback после новыхleads/messages.
- Реальный automatic refresh acceptance отдельно. При далёкомnext запуске можно создать **после новой команды пользователя** quiet Codex heartbeat в этой задаче. Docs/tool изучены, но automation не создана. Уведомлять meaningful completion/failure/change; не спамить неизменныйstatus. Не назначать самовозобновление во время нынешней паузы.

## Диагностики и ограничения

- Telegram watermark17176,lastprocessed30.08. Read-only temporary sessioncopy подтвердила samechannel; доступно только10старыхсообщений,newest658 от16.05.2025. История непрерывно не подтверждена: нельзя заключать «нет новых постов» или доказанный пропуск. Replay не запускали.
- PENDING312 (12.08,clip+llm.979): оба recipients TelegramEntityTooLarge / Request Entity Too Large;12PNG4032x3024 присутствуют,first10=107847571bytes,несколько11–14MB; noPublishedPost. Новый preflightдоHTTP. Старые файлы не менялись/не рассылались; адресное уменьшение и повтор требуют отдельного решения, не массового replay. BotAPI sendPhoto limit10MB подтверждён официально.
- Live IPRoyal08.09: orders80647128 expiry25.09 12:18GMT+4 и80643004 expiry25.09 11:15GMT+4, по2ISP,Active,SubscriptionOn; trial80637001ExpiredOff. Никакихcredentials в артефакты/заметки. Дляadmin подготовитьsafeproxy_service_receipt послеrelease; verificationdate реальная08.09 примерно08:06UTC, не обновлятьдату без нового просмотра.
- AWS требуетlogin; instancei-0b0a410a1dedc28eb,SGname launch-wizard-1,нетIAMrole. ActualSGinboundrules/cloudbackupнепроверены, стандартныхAWS/B2/rclonecredentialsнеобнаружено. Не объявлять конфигурацию облака хорошей/плохой без доступа, не покупатьресурсы.
- WorkerQAincident: старый wrapper в отдельном worktree попытался SSH/rsync кobsolete90.156.254.159; процессыостановлены до08:07:18UTC на observedconnectionstage,transferoutputнебыло. Это **не доказательство zero remote effect**; не повторять. Currentwrapperrequires explicittarget, testsfake.

**Точка продолжения:** новая команда Дмитрия → короткая проверка stopped state и partial archive → independent review/integration b2c3c70 + af805d7 и complete offhost restore → exact gates → staged release. До этого production repair не выпущен и автоматические исправления не подтверждены.
