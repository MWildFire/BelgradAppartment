# RentBelgrad — статус реализации ревью 08.09.2026

Обновлено 08.09.2026, 15:31 Asia/Dubai. **Первая активация7ff9512 автоматически откатилась по startup health gate. Старые сервисы работают, health/catalog200; база сохранена на0013.** Исправляется bounded startup wait; fresh0013backup в работе перед повторным release.

- План: [reliability remediation](../superpowers/plans/2026-09-08-reliability-remediation.md); [ревью F01–F23](2026-09-08-project-review.md); [исторический baseline](2026-09-08-evidence.json).
- Ветка `codex/reliability-2026-09-08`, tested implementation checkpoint `7ff9512` (после возобновления). Baseline `75f870c` (code `38ad154`).
- **Активный app: исходный после автоматического rollback7ff9512.** Live schema **0013_delivery_outcomes**: additive0012/0013 применены11:23UTC и сохранены при откате. `.env`, Caddy и старые timers восстановлены/сохранены; успешной repair-активации пока нет.
- Команда Дмитрия разрешает staged deployment/restarts в рамках ремонта. Новые покупки, тестовые сообщения людям и исторический массовый replay не выполняются. Snapshot active остаётся выключенным.
- Полная off-host generation **20260908-110709-00c120d6** прошла strict restore:1,050,621files,5149photo references,DB0011, catalog/card/photo200+decoded, fresh protected2config/2sessions, healthreceiptpublished. После actualmigration0013 для следующегоrelease требуется новый совместимыйbackup; source snapshot session18877 идёт. Исходныйpartial сохранён какисторическийseed.

| F-ID | Пункт | Статус | Доказательство / остающийся gate |
|---|---|---|---|
| F01 | Честный результат обновлений | locally_verified | Source outcomes + nonzero CLI; журнал до уведомлений; 100 item failures не дают success. Live canary/full/automatic pending |
| F02 | Неполный обход не снимает | locally_verified | Строгие completion/coverage/page/item gates, 2 weekly absence confirmations, cap; dry-run без writes/sends |
| F03 | Проверка ссылок и legacy 402 | locally_verified | Source GET/browser payload probe; UNKNOWN на challenge; явный legacy fallback opt-in off. Live маршруты pending |
| F04 | Public representative | locally_verified | Выбор внутри public; production copy: 31 cards/29 apartments →168/168, lost apartments0, новых прав публикации0 |
| F05 | Apartment availability | locally_verified | Active/UNKNOWN/DEAD едины для каталога/карты/карточки; из168 квартир active48, UNKNOWN120; неизвестные не помечены доступными |
| F06 | Общая политика снятия | locally_verified | all-DEAD, pin, live sibling, warn-only, общий cap; race-safe UPDATE при смене dup group |
| F07 | Backup/recovery | in_progress | Full0011generation verified11:18UTC, application/photo/protectedPASS. New0013snapshot18877running; recurring schedule/automaticbackup pending |
| F08 | Успешная свежесть/квота | in_progress | Fair public/broken/oldest-success queue; Ubuntu isolated20/20 verified за100.08sec,38photos. Quota2400/nightly + public due10h, окна11:30/17:30/23:30UTC реализованы; steady-state target pending |
| F09 | Attempt отдельно от success | locally_verified | Additive schema без backfill ложных successes; actual isolated20/20 successes и errors0; live production evidence pending |
| F10 | Конкуренция процессов | locally_verified | Per-platform process leases, bounded queue; 3-entrypoint/process regression. Live overlapping run pending |
| F11 | Failover HTTP/browser | locally_verified | 402/407/transport/timeout → next allowed slot, no implicit direct; offline primary/secondary/all-failed PASS |
| F12 | Честный shadow | locally_verified | Staged actual4481/4484,99.933%,49complete parity/mismatch0; source_total_changed+2legacy detail errors дают honest STOP. Snapshot readiness pending |
| F13 | История/health/monitoring | in_progress | Durable run/source journal, readiness DB503, real deactivated_at, freshness/backup/outbox health, quiet change events; capacity integration/live pending |
| F14 | Budgets/checkpoints | locally_verified | Incremental source600/run24000sec; full1800/run30000; maintenance20/70/10; bounded liveness600sec; actual OS SIGTERM→finally→exit143 PASS |
| F15 | Согласованный deploy | in_progress | Exactpacket7ff1491unit/54browser/CI PASS; actualauto-rollback preservedDB0013 and restoredservices200. Startup6sec gate failed; boundedwaitfix + newbackup + retry pending |
| F16 | Непубличные media | locally_verified | Authorized GET/HEAD photo route + traversal/private tests PASS; live Caddy /photos bypass ещё нужно переключить после app release |
| F17 | Доставка каждому получателю | locally_verified | Claim + pending/sending/sent/failed/unknown; immutable payload, partial retry only failed, historical records no replay; full album receipts |
| F18 | Нет ложного sent | locally_verified | Отсутствующий транспорт/конфигурация не sent; confirmed receipt требуется; atomic reconciliation |
| F19 | Долговечная очередь подписок | locally_verified | 100 →30/30/30/10 после72h; DEAD не блокирует поздние строки, reactivated backlog сохраняется после100h |
| F20 | HTML escaping | locally_verified | Ввод посетителя экранирован, читаемое plain-text письмо; regression PASS |
| F21 | Link queue без starvation | locally_verified | Oldest eligible checks, UNKNOWN backoff6h, bounded/fair inactive public probes; 3-row progression tests |
| F22 | 0/1/несколько фото | locally_verified | 1→sendPhoto,2–10→album;0/missing/partial receipts fail/UNKNOWN; frozen Finder caption/photo-order/recipients oracle PASS |
| F23 | CI/env/instructions | in_progress | Frozen CI unit/browser/lint/shell; matching Chromium установлен; outdated PWA/availability fixtures исправлены по продукту. GitHub34217184273 exact2f8b31a SUCCESS; live evidence pending |

**Проверки:** baseline1148 passed/1 skipped. Exact `7ff9512`: unit **1491 passed / 1 skipped / 36 warnings** (126.59s), browser **54 passed** (59.24s), changed-code lint0, shell syntax, frozen lock107packages и diff-check PASS. [GitHub Actions34219047309](https://github.com/MWildFire/BelgradAppartment/actions/runs/34219047309) — jobsunit102037573646/browser102037573868 **SUCCESS**. Packet423files SHA25624eab5705afae640ffaaabc5c40588c356559ab9aa069d42f596f163046ac053. Предыдущий2f8b31a:1440unit/54browser иCI34217184273PASS.

**Изолированные реальные проверки:** Ubuntu dry-run77details/4sources, source errors0, DBsha256 до/после совпал; liveness diagnostic ограничен, один Halo missing marker и cleanup TargetClosedError не скрываются. Отдельный write canary20records (5/source),20updated/20verified,errors0/skipped0,38photos,100.08sec, public set unchanged. Production DB/media и отправки не затронуты. Старый код0011 дважды открыл health/catalog/реальную publiccard на DB0013 (всё200), old ORM write работает, newlead/outbox rows сохранены, schema hash unchanged, FK0/integrityok; это локальная mainvenv, не прежняя productionvenv. Evidence: `2026-09-08-evaluation-ubuntu.json`, `2026-09-08-capacity-canary-ubuntu.json`, `2026-09-08-rollback-compatibility.json`.

**Открытые диагностики:**

- Telegram: watermark17176 (последняя обработка30.08); read-only запрос к той же подтверждённой source channel показывает только10 более старых сообщений, newest658 от16.05.2025. Непрерывность доступной истории не подтверждена; это не доказательство отсутствия новых постов. Backfill/replay не запускался.
- PENDING312 от12.08: clip+llm, confidence0.979; all12 inbound photos present; no PublishedPost; в соответствующем monitor log BotDeliveryError. Provider outcome подтверждён: TelegramEntityTooLarge / HTTP Request Entity Too Large у обоих recipients. Первые10 PNG107847571bytes, несколько файлов11–14MB. Новый preflight отклоняет oversized доHTTP; исторические media не менялись, replay не выполнялся.
- IPRoyal live dashboard: orders80647128 (2 ISP, expiry25.09 12:18 GMT+4) и80643004 (2 ISP, expiry25.09 11:15 GMT+4), оба Active, Subscription On. Старый trial80637001 Expired. Endpoint/credentials в артефакты не включены; оплат/настроек не меняли.
- AWS Console требует IAM/root sign-in; безопасных AWS/B2/rclone credentials на проверенных стандартных путях сервера нет. Security Group/cloud backup пока не подтверждены. Используется реально доступная внешняя защищённая копия на Mac.
- Ошибка worker QA: старый wrapper в изолированном worktree запустил SSH/rsync к obsolete VPS из теста; процессы немедленно остановлены на стадии соединения, transfer output не наблюдался. Это не доказательство zero remote effect. Дальнейшие проверки используют current wrapper и fake subprocess; повторное обращение не выполняется.

**Следующее действие:** закончить fresh complete snapshot и transfer с повторным использованием media seed; strict all-file hash + DB/application/photo restore; fresh protected proof. После независимого review и exact checks — repair release/Caddy/capacity/no-notify canary. Snapshot readiness остаётся STOP: повторный полный обход не подменяет точечную диагностику двух ошибок. Actual automatic acceptance отдельно.

## Журнал реализации

### 08.09, начало реализации (07:13 UTC / 11:13 Asia/Dubai)

- Integration owner: root, ветка `codex/reliability-2026-09-08`, owns CLI/config/schema/release/production.
- Worker W1: `codex/reliability-w1`, owns orchestrator/liveness/safety helpers/tests; worker W2: `codex/reliability-w2`, readmodel/group helper/tests; worker backup/QA: `codex/reliability-backup`, backup/restore tools/tests. Изолированные worktrees.
- W0 live: monitor/site/admin active; schema 0011; active 13189, public overlays184; nightly success 08.09 03:02–04:09 UTC (2h timeout), weekly success06.09 04:01–06:58 UTC (8h timeout). Эти success не доказывают качество.
- Следующие автоматические: smoke08.09 12:33 UTC, nightly09.09 03:04 UTC, weekly13.09 04:01 UTC. Live automatic acceptance пока pending.
- Backup только25.08 на сервере; DB1.50GB, inbound2.7GB, listing media65GB, server free60GB, Mac free628GiB. Новый backup должен учитывать нехватку места для полной второй копии на сервере.
- PENDING312 не имеет PublishedPost; source watermark17176 от30.08. Сверка source пока pending. Рассылки не выполнялись.
- Основной baseline1148 passed/1 skipped за29s, e2e исключены (54 дополнительных skipped в старом общем числе55). Production .env не используется как тестовая конфигурация; transport fakes.


### 08.09, пауза около13:05 Asia/Dubai

- По прямой просьбе Дмитрия остановлены backup stream (exit255 послеSIGINT) и staged fullshadow (exit130). Fullrestore/shadow PASS не получены.
- Сохранены неинтегрированные worker commits b2c3c70 (public cadence,92finaltargetedPASS) и af805d7 (incrementaloffhost,32PASS); независимый review/общий exactgate pending.
- Helper scripts сохранены privately в `/Users/mvstrike/Backups/RentBelgrad/2026-09-08/continuation-tools`; полный документ продолжения содержит все paths,receipts,команды и gates.
- Heartbeat/LaunchAgent не созданы. Никакое самовозобновление не разрешено до новой команды пользователя.


### 08.09, продолжение около14:25 Asia/Dubai

- Дмитрий: «Продолжай». Agents вновь работают: W2 review/fix public cadence; W1 review offhost; backup worker завершает fresh protected recovery и explicit predeploy tool/app-root options.
- Live core services4/4active; предыдущих backup/shadow процессов нет; server56Gfree.
- b2c3c70 интегрирован какe58f176. Independentreview нашёл variable nightly cadence gap20h: готовится publicdue10h/три окна11:30,17:30,23:30UTC. Target пока не заявлен достигнутым.
- Directory backup receipt validation добавлен без выдуманногоarchivehash;39release testsPASS.
- Partialarchive оборвался наjson.dumpsmanifest после передачи файлов; безопасное seed-only извлечение и локальныеhash в процессе. Seed не даёт releasePASS, требуется freshremotecompleteinventory/schema/allhash/app/photo/protectedproof.


### 08.09, 15:31 Asia/Dubai — общий gate и shadow

- Public cadence/offhost safety интегрированы; exact2f8b31a прошёл1440unit/54browser и GitHubCI34217184273. W1 проверил безопасный rollback12managed env keys и narrowCaddy helper; только local fixtures, live ещё pending.
- Из исходногоpartial безопасно извлечены1,050,621files /71,091,802,765bytes. Container error отсутствует, но manifest исходной полной копии отсутствовал: это толькоseed, неbackup. Fresh source snapshot начат10:35UTC в отдельном staging; source/media и appне изменяются.
- Staged shadow завершён10:44:44UTC:4481/4484(99.933095%),skippedpages0,sample100,complete49/fallback51,succeeded49,mismatches0,detailerrors2. Crawl завершил доступные страницы, но source total менялся: complete=false/trusted=false, readinessSTOP. Исторические значения drift не записаны; addsanitizedtelemetry и probe2IDs в работе.
- Proxy finalattempts875/failovers2/challenges2; HTTP433errors433/recovered433/residual0, browser442/errors9/recovered9; proxyerrors0. Нулевыеresidual не объясняют автоматически2detail errors. Snapshot остаётсяshadow, activeoff; no production writes/sends в этой оценке.
- Ubuntu systemd-analyze подтвердил корректные календари Mon..Sat03UTC,Sun02UTC,smoke00/12:30UTC,public11/17/23:30UTC и timeout7h30min. Это проверка формата, ещё не deployed/automatic proof.


### 08.09, 15:31 Asia/Dubai — финальный пакет, полный restore

- Frozenrelease7ff9512 prepared,1491unit/54browser/GitHubCI PASS. Protectedaf1d36a+preparedcbce289 (89combinedtests) иtelemetryffadb44 (45tests) независимо reviewedPASS, integrated1022aaf/c5a5bea/6c3b2f1.
- Source snapshot session32919 завершиласьexit0; manifest147,564,353bytes, SHA2fb81e5ce8be977a784f16fa2f3ea3ad36e654fb0922cd41ad6aa0748f497d95. Новыйsourcefullhash не повторяется. Firstoffhostgeneration started11:07UTC в `/Users/mvstrike/Backups/RentBelgrad/offhost`; ID20260908-110709-00c120d6. DB/state/media transfer завершает reuse; на15:16 полная receiverhashпроверка в процессе, acceptedbackup ещёfalse.
- Trusted recovery/protected tooling находится отдельно в root-owned `/usr/local/lib/rentbelgrad-recovery`; productionapp/env/Caddy/schema/timers ещё не менялись.
- Дополнительный actualoldproductionvenv drill не зачтён: v1 guard блокировалread-onlysystemSSLlookup, v2 publicruntimeassetdistricts.yaml. Обаfailclosedдоprodwrite; helpers исправляются узко, повтор только вNEWisolateddirectory. Основной oldcode0013compatibilityPASS из локальнойсреды сохраняется как отдельный факт.
- Two-ID probe: перваяпопытка CWDневерен, PermissionErrorbeforelease; втораябезDISPLAY HTTP403/NoProxy, не доказанный ISPfailure. Xvfbповтор11:13UTC: обаHTTP200,identityvalid,9/8photoURLs,браузерstart1PASS,proxyresidual0; обаmissingprice_eur. Проверкаисходныхpricefields pending, безfullshadowrepeat/noDB/no-media/no-sends.


### 08.09, 15:31 Asia/Dubai — release failed safely, repeated gate

- Firstoffhostgeneration complete11:18UTC/exit0: receipt `/Users/mvstrike/Backups/RentBelgrad/offhost/receipts/20260908-110709-00c120d6.json`, manifestSHA2fb81e5...,1,050,621files,5149refs, schema0011/FK/integrity/allhash/app/photoPASS. Freshprotectedproof created11:18:23Z, verified11:18:27Z,2sessions/config/Caddy/FileVault/privatepermissionsPASS; sanitizedhealthreceipt uploaded.
- Actualoldproductionvenv compatibilityv3 PASS11:19:35Z: health/catalog/publiccard200, oldORMINSERT+UPDATE committed inisolatedcopy, newlead/outbox/schema preserved,FK0/integrityok. Read-onlySSLlookup and publicdistrictsasset are explicitly constrained; noactualdata/config/sessionwrites. Earlier v1/v2harness failures preserved.
- Source2ID diagnostic11:20: bothHTTP200, realpayloadIDvalid,9/8photos; numericalpricevalue absent inboth realEstate.price and properties[0].price, no positive raw value lost byparser. BrowserstartPASS/residualproxy0. Neitherfullshadowreadiness nor2healthyfullcards claimed.
- **Release7ff9512 attempted11:22–11:23UTC; backup/tests gatespassed, newfrozenvenv/browserlaunchPASS, migrations0012/0013 applied.** Newsite began11:23:12; originalcurl allowedonly6seconds and failedconnection11:23:18 before newsite completedstartup. NoPythonexception innewsitejournal.
- Automaticcode/venv/units rollback completed11:23:22; oldsite fullystarted11:23:25. Confirmedmonitor/site/admin/Caddyactive, localhealth200/externalcatalog200. Activationreceipt resultrolled_back_code_only, previousrevisionnull. **Schema0013 retained; noDBrestore/downgrade.** Caddyphotoingress/capacity/newpublictimer were never applied. Oldtimers restored, nextsmoke12:33:09UTC.
- W1 implements bounded90s sharedhealth+readywait, actualnewvenvcoldstartprobe inpreparation. Secondfullsource snapshot started~11:27UTC, session18877, PID300379, `/opt/otz/data/backups/.manual-postmigration-20260908-1127`; applicationroot points to rejected7ff code/schema0013, notoldactive0011. Newacceptedbackupreceipt required before retry; no gatebypass.
- Stablelocaloffhostruntime prepared at `/Users/mvstrike/Backups/RentBelgrad/offhost/runtime/7ff9512/{code,venv}`, frozenno-dev installPASS. LaunchAgent andCodexheartbeat notinstalledyet.
