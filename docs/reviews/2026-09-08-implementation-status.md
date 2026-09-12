# RentBelgrad — статус реализации ревью 08.09.2026

> **Продолжение 12.09,17:42UTC:** [актуальный журнал](2026-09-12-continuation.md). Production52266a2/0014 выпущен:1545unit/54browser/CI PASS, fresh backup1068893files/5631refs принят. Настоящий public timer17:32–17:39UTC:185attempts/142verified/43errors/0skipped, qualitydegraded; DB и post-run HTTP194catalog/181map/215rights PASS. Core4/4 и5timers active. Лимиты3600s/3000 применены, Finder8matches×2recipients подтверждён. Полные source-quality/freshness24h/7d, новая полная automatic backup после исправления и AWS Console остаются открытыми.

Обновляется12.09.2026, Asia/Dubai. Production52266a2/0014. Ниже уровень подтверждения каждого пункта; failed/degraded source results не заменяются PASS.

- План: [reliability remediation](../superpowers/plans/2026-09-08-reliability-remediation.md); [ревью F01–F23](2026-09-08-project-review.md); [исторический baseline](2026-09-08-evidence.json).
- Ветка `codex/reliability-2026-09-08`, замороженный кандидат `4684f4709df727accac20cebff598f47a272e5bd`, опубликован в GitHub. Общий frozen gate:1513unit PASS/1skip,54browser PASS; GitHub CI34449236488 оба jobs SUCCESS. Новый индекс — additive migration0014. Предыдущий ccafc5c не прошёл CI; его PASS не используется для нового выпуска.
- **Активный app:52266a2cce295342cd5b8a1ea3c7ceea0d1a040d, schema0014_group_availability_index.** Activation health/ready20012.309s, core4/4active;201runtime hashes совпали. Каталог194/карта181/215ручных прав сохранены; original+variant200, actualprivate404, admin401.
- Команда Дмитрия разрешает staged deployment/restarts в рамках ремонта. Новые покупки, тестовые сообщения людям и исторический массовый replay не выполняются. Snapshot active остаётся выключенным.
- История выпуска10.09 (не текущий допуск): pre-release generation **20260910-072302-c5c6aa45**: DB0013, создана06:53:33UTC, полный restore1,053,270files/5468photo references, app/card/photo/config/2sessions PASS; подтверждение на сервере. Stable0014 runtime и LaunchAgent установлены, RunAtLoad07:45:24UTC/PID46794 действительно начал новый запуск. На том этапе копия0014 ещё не была принята; её последующий restore и новую копию12.09 см. вF07.

| F-ID | Пункт | Статус | Доказательство / остающийся gate |
|---|---|---|---|
| F01 | Честный результат обновлений | live_verified | Canary20/13/7, full и два automatic nightly возвращают degraded/nonzero при частичных отказах; terminal journals согласованы с фактическими counters. |
| F02 | Неполный обход не снимает | live_verified | Full: Nekretnine/4zida timeout, Halo total changed → sweep=false; только завершённый CityExpert677/677 допускает sweep. Публичные права сохранены. |
| F03 | Проверка ссылок и legacy 402 | live_verified / source_errors_remain | Canary failures независимо проверены:5 actual404,2 empty UNKNOWN. Автоматическая link queue159checks/5errors; недостоверные ответы не считаются LIVE. |
| F04 | Public representative | live_verified | После выпуска52266a2:215ручных прав сохранены; HTTP12.09:194catalog representatives согласованы сDB,181map points соответствуют geo subset. |
| F05 | Apartment availability | live_verified | После выпуска12.09:194catalog representatives,181map points и8detail witnesses согласованы с apartment-level availability. Свежесть учитывается отдельно вF08. |
| F06 | Общая политика снятия | live_verified / auto_hide_off | Контролируемый full сохранил public rights/overlays; automatic liveness12.09:0hidden,15confirmed/24unconfirmed/168held. Автоматическое скрытие выключено. |
| F07 | Backup/recovery | live_verified / automatic_runner_repaired | Новая generation20260912-162130-917ffa02:1068893files/5631refs, full hash/DB/app/photo/config/2sessions/server receipt PASS. Метод manual fresh snapshot с reuse только unaccepted media. Snapshot race и openrsync FD exhaustion воспроизведены и исправлены. Новый frozen LaunchAgent52266a2 startup exit0/skipped_not_due; это не новая полная automatic copy. |
| F08 | Успешная свежесть/квота | in_progress | Фактический nightly1949/2400, оценка2162successes/day. Квота увеличена до3000; новый canary20/9/11 degraded, safety PASS. Настоящий public timer завершён:185/142/43,0skipped,406s; checkpoint совпал сDB. Полная свежесть не достигнута. Достижение24h/7d SLO после новых настроек требует измерения. |
| F09 | Attempt отдельно от success | live_verified | Новый canary52266a2:20attempts/9verified/11errors, terminal degraded checkpoint согласован сDB;11отказов независимо проверены read-only. Старые success/данные/фото сохранены, backoff записан;3actual404 допускают только подтверждённые DEAD outcomes. |
| F10 | Конкуренция процессов | live_verified / overlap_not_forced | Full supervisor subreaper/pidfd: residual0, только собственные процессы; timers восстановлены. Два automatic nightly завершились. Offline contention tests PASS; искусственное live overlap не создавали. |
| F11 | Failover HTTP/browser | live_verified | Full Nekretnine:1123proxyattempts,557HTTPerrors/557recovered,9browsererrors/9recovered,2failovers,proxyerrors0; terminal timeout отдельно от transport. |
| F12 | Честный shadow | live_verified / active_off | Full shadow:1837seen/1305complete/532fallback/mismatch0/activeuses0. Source coverage/timeout не позволяют объявить snapshot готовым к active; shadow сохранён. |
| F13 | История/health/monitoring | live_verified / degraded_health | Durable full и два automatic journals сохранены, systemd invocation12.09 подтверждён. HTTP200 и healthdegraded честно разделены. Свежая копия принята; ещё остаются public refresh degraded, недостаточная успешная свежесть и неподтверждённые полные обходы трёх источников. |
| F14 | Budgets/checkpoints | live_verified / new_capacity_applied | Terminal full/nightly evidence сохраняет реальный timeout/degraded outcome. Source budget1800→3600s, nightly quota2400→3000 применены17:05UTC только в двух env keys; прочие bytes/owner/mode сохранены. Следующие job processes читают новые значения; фактическая производительность ещё измеряется. |
| F15 | Согласованный deploy | live_verified | Exact52266a2 activated из проверенного435filepacket под root unit; frozen imports/browser/0014/readiness20012.309s,201runtime hashes/FK/integrity/live PASS. Прежние4timers восстановлены, новыйpublic включён17:20UTC. Rollback code-only совместим с0014. |
| F16 | Непубличные media | live_verified | Caddy bypass closed; published original+variant GET/HEAD200, file hash/decode/no-store PASS; existing private photo GET/HEAD404; admin401 |
| F17 | Доставка каждому получателю | live_verified | 12.09 15:15–15:18UTC:8новых matches доставлены обоим recipients;16sent rows с положительными provider IDs/временем, длины альбомов совпадают с frozen photos; все8имеют PublishedPost, attempts1. Только read-only проверка, sends/replay0. |
| F18 | Нет ложного sent | live_verified | Все16новых sent rows имеют подтверждённые provider IDs и delivered_at, claims сняты;8matches завершены только после обоих recipients. Негативные transport/race tests PASS; не создавали искусственные ошибки на live. |
| F19 | Долговечная очередь подписок | deployed / no_live_subscribers | Durable backlog/cursor/reactivation tests PASS.12.09 subscriptions0 и subscription_deliveries0; synthetic подписчики не создавались. |
| F20 | HTML escaping | deployed | HTML input escaping and readable plain text regression PASS; deployedhash verified; no test email sent |
| F21 | Link queue без starvation | live_verified | Automatic12.09 verify-links159checked/5errors,0deactivated; durable queue/backoff используется. Starvation/fairness ограниченной очереди закреплены regression tests. |
| F22 | 0/1/несколько фото | deployed | 0/1/album API contract and frozen Finder caption/photo-order/recipients oracle PASS; historical oversized312 not replayed |
| F23 | CI/env/instructions | live_verified | Exact52266a2:1545unit/1skip,54browser,changedlint/shell/CI34705731731 SUCCESS.435filepacket активирован; livehashes совпадают. Операционные ограничения и незавершённые source-quality gates отражены отдельно. |

**Проверки:** baseline1148 passed/1 skipped. Exact `7ff9512`: unit **1491 passed / 1 skipped / 36 warnings** (126.59s), browser **54 passed** (59.24s), changed-code lint0, shell syntax, frozen lock107packages и diff-check PASS. [GitHub Actions34219047309](https://github.com/MWildFire/BelgradAppartment/actions/runs/34219047309) — jobsunit102037573646/browser102037573868 **SUCCESS**. Packet423files SHA25624eab5705afae640ffaaabc5c40588c356559ab9aa069d42f596f163046ac053. Предыдущий2f8b31a:1440unit/54browser иCI34217184273PASS.

**Изолированные реальные проверки:** Ubuntu dry-run77details/4sources, source errors0, DBsha256 до/после совпал; liveness diagnostic ограничен, один Halo missing marker и cleanup TargetClosedError не скрываются. Отдельный write canary20records (5/source),20updated/20verified,errors0/skipped0,38photos,100.08sec, public set unchanged. Production DB/media и отправки не затронуты. Старый код0011 дважды открыл health/catalog/реальную publiccard на DB0013 (всё200), old ORM write работает, newlead/outbox rows сохранены, schema hash unchanged, FK0/integrityok; это локальная mainvenv, не прежняя productionvenv. Evidence: `2026-09-08-evaluation-ubuntu.json`, `2026-09-08-capacity-canary-ubuntu.json`, `2026-09-08-rollback-compatibility.json`.

**Открытые диагностики:**

- Telegram10.09 08:00UTC: копия настроенной monitor-сессии авторизована; exact stored peer иusername совпали. Последний видимый альбом09.09 12:50:23UTC: watermark17390, inbound leader17387 уже вDB;704rows/568distinctmessageIDs. Latest10+точечные IDs, не вся история. Originalsession не открываласьTelethon, temporarycopy удалена, sends/replay0. Старое658/May2025 наблюдение не характеризовало текущий правильный source/session; исторический gap не утверждается.
- PENDING312 от12.08: clip+llm, confidence0.979; all12 inbound photos present; no PublishedPost; в соответствующем monitor log BotDeliveryError. Provider outcome подтверждён: TelegramEntityTooLarge / HTTP Request Entity Too Large у обоих recipients. Первые10 PNG107847571bytes, несколько файлов11–14MB. Новый preflight отклоняет oversized доHTTP; исторические media не менялись, replay не выполнялся.
- IPRoyal live dashboard: orders80647128 (2 ISP, expiry25.09 12:18 GMT+4) и80643004 (2 ISP, expiry25.09 11:15 GMT+4), оба Active, Subscription On. Старый trial80637001 Expired. Endpoint/credentials в артефакты не включены; оплат/настроек не меняли.
- AWS Console требует IAM/root sign-in; безопасных AWS/B2/rclone credentials на проверенных стандартных путях сервера нет. Security Group/cloud backup пока не подтверждены. Используется реально доступная внешняя защищённая копия на Mac.
- Ошибка worker QA: старый wrapper в изолированном worktree запустил SSH/rsync к obsolete VPS из теста; процессы немедленно остановлены на стадии соединения, transfer output не наблюдался. Это не доказательство zero remote effect. Дальнейшие проверки используют current wrapper и fake subprocess; повторное обращение не выполняется.

**Следующее действие:** измерить свежесть24h/7d на следующих штатных запусках с новыми лимитами и подтвердить следующую полную автоматическую копию на52266a2. Проверять остаточные source failures по фактическим ответам. AWS Console отложена; snapshot shadow/auto-hideoff сохраняются. Запуск по public timer и его терминальный исход уже доказаны, quality остаётся degraded.

## Журнал реализации

### 10.09, 08:32–08:35UTC — пауза пользователя

- Все агенты остановлены, frozen canary ebb0d66e прошёл39offline tests/review, live не установлен/не запускался. Runbook принят локальным docs commit197161f; без production изменений.
- LaunchAgent disable/SIGINT/bootout завершились; отдельный readback disabled+unloaded PASS. Runner удалил свой source staging, interrupted generation не принята, local pending сохранён. Local/remote leases свободны, source/canary процессов0. Последняя принятая copy0013 прежняя.
- На08:34:23UTC четыре рабочие службы active/running, прежние4 timers active, новый public timer inactive. Полный checkpoint с точными шагами продолжения сохранён; самовозобновления нет.

###10.09,08:00–08:15UTC — точная source диагностика и исправление фонового backup

- Bounded source helper сначала остановился на relative Path.as_uri ValueError безnetworkproof; узкая absolute-path correction послеsymlinkguard прошла39tests+independentreview. Actual repeat08:00:09UTC PASS: authorizedmonitor, channel/username совпали, latestalbum17390/leader17387 уже вDB,6readonlyRPC, безmessages/replay; cleanup проверен отдельно.
- Первый LaunchAgent backup07:45:24–07:46:22UTC failed/acceptedfalse. Две независимые однократные launchd SSH `/usr/bin/true` проверки с исходным Downloads key истекли по35s; verbose stage markers дошли доconnected, но неidentity. С побайтно тем же ключом вprivate700/600 operationaldirectory та же launchdпроверка PASS4.208s. Секрет не выводился/непопалвrepo/vault; оригинал сохранён. МеханизмmacOSдоступа отдельнонеустанавливался.
- В установленном plist изменён только `--ssh-key`; cadence/RunAtLoad/остальныеполя неизменны, диагностическиеодноразовыезадачивыгружены. НовыйRunAtLoad08:06:22UTC:PID48490,gen20260910-080628-2989418e; насервереactualsourcePID331909,согласованныйDB0014 snapshot1,496,510,464bytes существует, к08:13 прочитано11.5GB сдиска. Manifest/restorepending; старыйbackupсохранён.
- Operational probe08:08–08:09UTC PASS понаблюдениям: все188catalogavailability/174map/8details совпали,68active120unknown. Health честноdegraded: новыхconfirmed-success timestamps0, journal/publiccheckpointпокапусты; quota2400невыдаётсяза2004требуемыхуспехов/сутки.
- Future operator tools10d545d/6ab1c9b/8756a6d: unexpectedprestartgate и staticphase/error_type/returncode, исходнаяошибка сохраняетсядажеприошибкеleasecleanup. Автор61tests, независимые12, root11reporter+46releasePASS. Этоизменениеследующихtools; productionapp/runtime4684иrunningbackup2fbнеподменены.


### 10.09, 07:38–07:46 UTC — production release и live acceptance

- Fresh offhost0013 принята доrelease:1,053,270files/5468refs, DB/app/card/photo/config/2sessions PASS, manifest7029ef4f…; serverreceipt совпал.
-4684f47 активирована0014, health/ready20011.252s. Frozen Python dependencies и browser установлен/запущен вrelease;201 runtimehashes совпали.
- Caddy изменён ровно в1photo handler; oldSHA40db198d…→newSHA9447706d…. Live весьcatalog188/map174/sitemap188 совпал сDB; rights207/SHA155e0db0… сохранены. Published photo original+variant200, actualprivate404,admin401.
-12capacitykeys применены; unmanagedenvSHAd9577df3… не изменился. Первый restarthelper остановился до мутаций, поскольку потребовал необязательный inactive subscriptionbot; исправлен под фактический baseline3services. Повтор07:44:15–23UTC PASS:3active/readiness2007.187s; прежние4timers restored, publictimeroff.
- Provider service receipt свежего read-only Chrome наблюдения установленаotz600;2active Serbia ISP orders до25.09/auto-renewalon. Credentials/покупки/настройки провайдера не затронуты.
- LaunchAgentcom.mvstrike.rentbelgrad-backup установлен600, RunAtLoad07:45:24UTC действительно запустилPID46794. Каждые6h проверяет необходимость24h копии;0014schema требуетновуюgeneration. Первый запуск завершился07:46:22UTC/exit1, backup_failed/acceptedfalse; pending20260910-074539-010c7484 пуст. Причина ещё не установлена, общий legacylog её потерял; новая копия не принята, прошлую0013 сохранили.
- Обнаружен унаследованный weeklydrop-in с runtimePlaywrightinstall: canonicalunitего не содержит, независимая review/узкая очистка готовится. BoundedTelegramhelper остановлен наoffline review4repro; productionсессии helperне использовал.


### 10.09, продолжение и устранение блокеров выпуска

- Свежая read-only проверка06:54:48 UTC: listings77454, public rights207, inbound704, published391; 23 новых права публикации относительно08.09. Rights SHA256 `155e0db0d7b0d4b331bc19a3ee31306f7b94819ef0a653801657fbab273d8a07`. Старый baseline184 не навязывается живому сервису; перед canary будет переснята текущая база.
- `8db8fa2`: пропущенные link checks при errors0 теперь дают durable degraded/nonzero. Исходное воспроизведение FAIL→PASS; root frozen64 tests и независимые95 PASS. No-notify граница сохранена.
- `41a0464`: итоговая запись доставки создаётся атомарно; конкурентный проигравший читает подтверждённый результат без rollback чужой отправки. Отдельные WAL connections, реальные receipts, сохранение ручного REJECTED: независимые48 tests +30 повторов PASS.
- `2fb3971`: покрывающий индекс dup-group availability и узкая проекция публичного readmodel. Причина медленной Linux-проверки найдена фактическим SQL/thread trace: readyz выполняется, полный scan тяжёлой listings тормозит каталог. Индекс не меняет строки/права публикации. Независимые556 tests PASS,1skip; 217 результатов старого/нового каталога, карты, фильтров, карточек и подписок полностью совпали.
- GitHub CI34448427507 на exact2fb3971: unit/browser SUCCESS. Локально1507unit PASS/1FAIL/1skip и54browser PASS: строгий umask077 выявил дополнительный дефект прав папок выпуска. Узкий fix f22bbdd проходит независимое ревью; новый exact gate обязателен. [Свежие санитизированные доказательства](2026-09-10-resume-evidence.json).
- Fresh source snapshot0013 идёт под отдельным backup lock; активные site/monitor/admin/Caddy не останавливались. Новый release/canary/full/automatic пока не состоялись. Новые helper/диагностические логи хранятся приватно в `/Users/mvstrike/Backups/RentBelgrad/2026-09-10`.


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
