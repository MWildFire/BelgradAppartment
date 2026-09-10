# RentBelgrad — статус реализации ревью 08.09.2026

Обновлено 10.09.2026, около12:15 Asia/Dubai. **Работа выполняется до конца по команде Дмитрия.** Выпущена4684f47, liveDB0014; сохранены207 прав публикации. Проверены все188 квартир каталога,174 точки карты,188 sitemap URL, доступ к опубликованным/закрытым фото и admin auth. Новые настройки применены к работающим службам. Настоящий автоматический backup0014 запущен через RunAtLoad; первый запуск завершился exit1 без принятой копии. Фоновый доступ исправлен заменой только пути к тому же ключу; новый настоящий RunAtLoad08:06:22UTC создаёт0014 generation. Data canary, полный и автоматический проходы ещё не приняты.

- План: [reliability remediation](../superpowers/plans/2026-09-08-reliability-remediation.md); [ревью F01–F23](2026-09-08-project-review.md); [исторический baseline](2026-09-08-evidence.json).
- Ветка `codex/reliability-2026-09-08`, замороженный кандидат `4684f4709df727accac20cebff598f47a272e5bd`, опубликован в GitHub. Общий frozen gate:1513unit PASS/1skip,54browser PASS; GitHub CI34449236488 оба jobs SUCCESS. Новый индекс — additive migration0014. Предыдущий ccafc5c не прошёл CI; его PASS не используется для нового выпуска.
- **Активный app:4684f4709df727accac20cebff598f47a272e5bd, schema0014_group_availability_index.** Activation health/ready20011.252s; послеcapacity restart2007.187s, все3 службы active.201 runtime hashes совпали. Caddy узко переведён на authorized photo route.
- Команда Дмитрия разрешает staged deployment/restarts в рамках ремонта. Новые покупки, тестовые сообщения людям и исторический массовый replay не выполняются. Snapshot active остаётся выключенным.
- Принятая pre-release generation **20260910-072302-c5c6aa45**: DB0013, создана06:53:33UTC, полный restore1,053,270files/5468photo references, app/card/photo/config/2sessions PASS; подтверждение на сервере. Stable0014 runtime и LaunchAgent установлены, RunAtLoad07:45:24UTC/PID46794 действительно начал новый запуск. Внешняя копия0014 ещё не принята.

| F-ID | Пункт | Статус | Доказательство / остающийся gate |
|---|---|---|---|
| F01 | Честный результат обновлений | deployed | Честные source/step outcomes и nonzero CLI;1513unit/54browser/CI PASS. Live canary/full/automatic pending |
| F02 | Неполный обход не снимает | deployed | Completion/coverage/page/item gates, последовательные absence confirmations, cap; безопасный eval PASS. Actual full/automatic pending |
| F03 | Проверка ссылок и legacy 402 | deployed | GET/browser payload probe, UNKNOWN на challenge; legacy fallback opt-in off. Source functional canary pending |
| F04 | Public representative | live_verified | 207 исходных прав неизменны; все8 страниц каталога188=DB representatives, карта174=geo subset, sitemap188; loss0 |
| F05 | Apartment availability | live_verified | Все188 catalog representatives и174 map points согласованы сDB:68 active/120 unknown.8 detail witnesses включают inactive original с active sibling; статусы совпали. Freshness — отдельный F08 |
| F06 | Общая политика снятия | deployed | all-DEAD/pin/live sibling/warn-only/cap, race-safe policy;207 live rights unchanged. Controlled data pass pending |
| F07 | Backup/recovery | in_progress | Свежая внешняя0013 generation полностью принята доrelease;0014 automatic RunAtLoad07:45:24UTC/PID46794 начат, первый automatic запуск failed/acceptedfalse; key-path fix подтверждён, новый RunAtLoad08:06:22UTC/gen080628 выполняется |
| F08 | Успешная свежесть/квота | in_progress | Quota2400/nightly реально применена. Baseline active13360, подтверждённых новых successes0; требуется минимум2004 successes/day, реальная success rate ещёнеизмерена. Public timer offдоcanary;24h/7d targets pending |
| F09 | Attempt отдельно от success | deployed | Additive0014 без ложного backfill successes. Isolated20/20verified PASS; первый production data canary pending |
| F10 | Конкуренция процессов | deployed | Per-platform leases/bounded wait/3process tests PASS. Live concurrent automatic outcomes pending |
| F11 | Failover HTTP/browser | deployed | Primary/secondary/all-failed and no implicit direct tests PASS; actual newruntime source checks pending |
| F12 | Честный shadow | deployed | Actual shadow4481/4484,49complete/mismatch0, source_total_changed+2legacy errors → honest STOP; activeoff |
| F13 | История/health/monitoring | in_progress | Durable journals/readiness/freshness/backup/outbox health deployed; health/ready200, provider receipt verified; automatic outcomes pending |
| F14 | Budgets/checkpoints | deployed | Budget/checkpoint code and12actual env settings verified; services restarted. Canary and full terminal outcomes pending |
| F15 | Согласованный deploy | in_progress | Exact4684 activated,201runtimehashes and0014integrity/FK PASS, rollback compatibility preserved. Legacy weekly ExecStartPre удалён07:48:44UTC, readbackPASS07:50:38UTC; weekly не запускался. Future releaseguard10d545d/46relatedtestsPASS |
| F16 | Непубличные media | live_verified | Caddy bypass closed; published original+variant GET/HEAD200, file hash/decode/no-store PASS; existing private photo GET/HEAD404; admin401 |
| F17 | Доставка каждому получателю | deployed | Atomic finalizer: real WAL race reproduced/fixed;48tests+30repeat checks and100/100stress PASS. No historical replay/test sends; natural delivery observation pending |
| F18 | Нет ложного sent | deployed | Unconfigured/unconfirmed transport never sent; receipt reconciliation tests PASS. No real test messages sent |
| F19 | Долговечная очередь подписок | deployed | Durable100→30/30/30/10 after72h and reactivated backlog tests PASS; live subscriptions0, no fake subscribers created |
| F20 | HTML escaping | deployed | HTML input escaping and readable plain text regression PASS; deployedhash verified; no test email sent |
| F21 | Link queue без starvation | deployed | Oldest eligible/backoff/fair inactive public queue tests PASS; live limited runs pending |
| F22 | 0/1/несколько фото | deployed | 0/1/album API contract and frozen Finder caption/photo-order/recipients oracle PASS; historical oversized312 not replayed |
| F23 | CI/env/instructions | in_progress | Exact4684:1513unit/54browser/static/CI34449236488 SUCCESS; actual frozen release and201hashes PASS. Final docs/automatic evidence pending |

**Проверки:** baseline1148 passed/1 skipped. Exact `7ff9512`: unit **1491 passed / 1 skipped / 36 warnings** (126.59s), browser **54 passed** (59.24s), changed-code lint0, shell syntax, frozen lock107packages и diff-check PASS. [GitHub Actions34219047309](https://github.com/MWildFire/BelgradAppartment/actions/runs/34219047309) — jobsunit102037573646/browser102037573868 **SUCCESS**. Packet423files SHA25624eab5705afae640ffaaabc5c40588c356559ab9aa069d42f596f163046ac053. Предыдущий2f8b31a:1440unit/54browser иCI34217184273PASS.

**Изолированные реальные проверки:** Ubuntu dry-run77details/4sources, source errors0, DBsha256 до/после совпал; liveness diagnostic ограничен, один Halo missing marker и cleanup TargetClosedError не скрываются. Отдельный write canary20records (5/source),20updated/20verified,errors0/skipped0,38photos,100.08sec, public set unchanged. Production DB/media и отправки не затронуты. Старый код0011 дважды открыл health/catalog/реальную publiccard на DB0013 (всё200), old ORM write работает, newlead/outbox rows сохранены, schema hash unchanged, FK0/integrityok; это локальная mainvenv, не прежняя productionvenv. Evidence: `2026-09-08-evaluation-ubuntu.json`, `2026-09-08-capacity-canary-ubuntu.json`, `2026-09-08-rollback-compatibility.json`.

**Открытые диагностики:**

- Telegram10.09 08:00UTC: копия настроенной monitor-сессии авторизована; exact stored peer иusername совпали. Последний видимый альбом09.09 12:50:23UTC: watermark17390, inbound leader17387 уже вDB;704rows/568distinctmessageIDs. Latest10+точечные IDs, не вся история. Originalsession не открываласьTelethon, temporarycopy удалена, sends/replay0. Старое658/May2025 наблюдение не характеризовало текущий правильный source/session; исторический gap не утверждается.
- PENDING312 от12.08: clip+llm, confidence0.979; all12 inbound photos present; no PublishedPost; в соответствующем monitor log BotDeliveryError. Provider outcome подтверждён: TelegramEntityTooLarge / HTTP Request Entity Too Large у обоих recipients. Первые10 PNG107847571bytes, несколько файлов11–14MB. Новый preflight отклоняет oversized доHTTP; исторические media не менялись, replay не выполнялся.
- IPRoyal live dashboard: orders80647128 (2 ISP, expiry25.09 12:18 GMT+4) и80643004 (2 ISP, expiry25.09 11:15 GMT+4), оба Active, Subscription On. Старый trial80637001 Expired. Endpoint/credentials в артефакты не включены; оплат/настроек не меняли.
- AWS Console требует IAM/root sign-in; безопасных AWS/B2/rclone credentials на проверенных стандартных путях сервера нет. Security Group/cloud backup пока не подтверждены. Используется реально доступная внешняя защищённая копия на Mac.
- Ошибка worker QA: старый wrapper в изолированном worktree запустил SSH/rsync к obsolete VPS из теста; процессы немедленно остановлены на стадии соединения, transfer output не наблюдался. Это не доказательство zero remote effect. Дальнейшие проверки используют current wrapper и fake subprocess; повторное обращение не выполняется.

**Следующее действие:** принять настоящий automatic backup0014 generation20260910-080628-2989418e, затем public canary20/600s с запретом сообщений и before/after/media proof; после него полный и автоматический проходы. Root единственный live operator. Новые операторские guards8756a6d готовятся отдельно; работающий app остаётся4684f47, backup runtime2fb3971 не меняется во время процесса.

## Журнал реализации

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
