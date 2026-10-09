# RentBelgrad — продолжение 10.09.2026 вечером

Команда Дмитрия: «Продолжи с чекпоинта до конца».
Возобновление с checkpoint12:35, локальный HEAD d854c55. Работа продолжается.

## Подтверждено

- 18:42UTC: active release4684f4709df727accac20cebff598f47a272e5bd, DB0014, site/monitor/admin/Caddy active. Прежние4таймера работают, publictimer выключен.
- 18:43:53UTC: read-only operational acceptance PASS;188representatives, HTTP/catalog/map/detail witnesses согласованы. Computed health degraded: success timestamps и новые full/public journals отсутствуют. Это baseline, не отказ доступности сайта.
- Stable backup8756a6d: заново сверены432Gitblobs. Plist согласованно переведён на python/script/app-root/cwd8756; остальные параметры сохранены, прежнийplist сохранён privately.
- LaunchAgent enabled/bootstrap: настоящий RunAtLoad PID72054. Generation20260910-184241-f1d30b53, snapshot18:43:11UTC. **Полный restore принят20:21UTC; LaunchAgent штатно завершился, last exit code0.** Проверены1,053,274files/5468photo references, schema0014, app/catalog/card/photo decode, protected config и2sessions. Live authentication этим восстановлением не проверялась. Server receipt совпадает; health receipt published. Final private receipt SHA256 `f2e55cf356ff7afd9743b618d99824ca80b724a6179e0d18523edd83513ce1bd`, manifest `7ad22618d53bf0c77ffd24507bc303fc59405181cdd90b9805c92699b70b8861`.
- Свежие private evidence в `/Users/mvstrike/Backups/RentBelgrad/2026-09-10/resume-evening-*`.

- 18:57UTC: Chrome AWS Console заново открыта; IAM sign-in, поля аккаунта/пользователя пустые. Cloud Security Group/AWS backup не подтверждены без входа пользователя. Эта проверка не блокирует offhost/canary/full.

## Дальше

### 20:30–20:47UTC — реальные canary и отдельная приёмка безопасности

- Canary001 завершён как **failed**, не PASS:10attempts/7successes; supervisor остановил свой CLI после двух новых otz orphan processes без доказанного происхождения. Права/overlays/исходные данные сохранены,175media проверены; остаточных процессов нет. Не утверждаем задним числом принадлежность неизвестных процессов.
- Обнаружен пробел supervisor: double-fork мог завершить промежуточного родителя между `/proc` scans. V2 использует Linux `PR_SET_CHILD_SUBREAPER` с обязательным readback до spawn; осиротевшие потомки усыновляются точным supervisorPID. Чужие PID1/processes по-прежнему запрещены.40tests PASS; реальный Linux double-fork → adoption/ownership/reap PASS, чужой PID1 rejected, сигналов0. Новый core SHA `a531d63ff8cefd2d5c384450b95c2445a3c046d421684a5fafe76b8ff75169dc`; app code не менялся.
- Canary002 штатно обработал20:13verified/7errors/0skipped/0source_errors,47.960s. Supervisor problem=null, inherited release lock PASS, residual0. **Строгий quality canary остаётся failed**, исходный receipt не переписан. Все207public rights и overlays прежние,337media hash/decode/pHash PASS, identity/inbound preservation PASS.
- Отдельный read-only probe всех7failures:5Nekretnine возвращают404;2CETIRI_ZIDA возвращают пустые поля/фото без заголовка. Последние остаются unknown, не объявлены снятыми. Ранее принятые цена/площадь/комнаты/координаты/фотографии/успешные timestamps этих7 сохранены; failure_count+1/backoff,5DEAD соответствуют5actual404.
- Из этих фактов создан **отдельный** `canary_safety_verified_quality_degraded`: проверка безопасности допускает контролируемый full с отключёнными уведомлениями/скрытием, но не объявляет качество успешным. Все8safetychecks PASS; actual evidence positive +6negative mutation checks PASS. Receipt `/root/rentbelgrad-acceptance/public-canary-evening-002/safety-result.json`, SHA `64832359328a6c02361fb847c5346273ef945c3ea988c1df10e96b81d92f574d`. Не используется фиктивный `canary_verified` или нулевые ошибки.
- Full runner V2 pinned к новому core и допускает только полный строгий PASS либо exact отдельный safety receipt с8true checks и согласованными counts.40tests/static/Linux import PASS. SHA `1a54400cfbe6f3d99b34d03d784cb90f53c3c870a02d55dd78bfcb602bb1ed17`. One-shot unit `rentbelgrad-controlled-full-20260910`, output `/root/rentbelgrad-acceptance/full-evening-001`; результат ожидается. Это один контролируемый запуск, не heartbeat/периодическое продолжение.

### Подготовка полного контролируемого запуска

Private `rentbelgrad-full-refresh-runner.py`, SHA256 `27381da17adf98509ff4e0b8514c6e60d99ac59bf19022ef6fca3ba63a4e405a`: 31 offline tests PASS, F/E9 static checks PASS. Основан на pinned canary ebb0d66e; AST метода управления процессами совпадает после нормализации только deadline720→30120 и текста ошибки. Требует accepted0014 и exact successful canary receipt. Сохраняет/останавливает только активные старые timers, восстанавливает их с readback только после подтверждённого завершения своих процессов; при residual оставляет явный operator gate. Полный CLI `nightly-refresh --max-pages=500 --no-notify`; процессные overrides выключают публикации/скрытия/транспорты, включают реальные link/liveness checks без уведомлений. Проверяет новые full journal/source outcomes, права публикаций, исходные данные и изменившиеся public media. Установлен и проверен импортом в Linux; полный запуск ожидает canary. Нового production app code нет. Proof: private `full-runner-offline-proof.json`.

Full runner установлен root600 в `/usr/local/lib/rentbelgrad-recovery`; SHA readback совпал, Linux import/--help PASS. Production application code не менялся. Canary ebb0d66e запущен после принятого backup в `/root/rentbelgrad-acceptance/public-canary-evening-001`; итог пока ожидается.

На переданной БД сделана read-only/immutable проверка совместимости evidence:77454rows, metadata types array/array для всех; listing JSON61,163,511bytes помещается в64MiB reader. Это отдельная проверка инструмента, не замена recovery gate.

Accepted0014+matchingserverreceipt → reviewedcanary20/600s с before/after → controlledfull безуведомлений → фактический publictimer → точные F01–F23 статусы. Исторический replay/тестовыесообщения/покупки не выполняются. Snapshotshadow/activeoff и ручные права публикаций сохраняются.

## Уточнение пользователя

### Фактический automatic smoke после релиза

10.09 12:34:37–12:34:58 UTC, systemd invocation `a7a79ad66334431faab18771f01d3272`, Result=success. Последний блок scrape-smoke: Nekretnine25 previews, 4zida20, Halooglasi20, CityExpert12; все OK/detail_ok=true/drift=false. Это автоматическая list+one-detail проверка четырёх источников, не полный проход и не доказательство свежести всего каталога. Повторный запуск оператором не выполнялся.

Сообщение про Beget относилось к другому диалогу. Дмитрий подтвердил: нужен полный план RentBelgrad; пока не получается войти в AWS, завершать доступное без AWS Console. Только cloud access gate отложен, canary/full продолжаются после recovery gate.

Свежая проверка: DNS 3.121.163.130; главная, catalog, map, calculator, healthz, readyz и sitemap ответили 200. В Chrome карточка 290 отрисована, четыре основные фотографии и фотографии похожих объектов загрузились. Формы и сообщения не отправлялись.
