# RentBelgrad — продолжение 12 сентября 2026

> **Итог 12.09,17:42UTC:** production52266a2/0014 работает; полный проверенный backup принят. Настоящий public timer завершился за406s:185попыток,142verified,43errors,0skipped, qualitydegraded. После него каталог194/карта181/215прав публикации сохранены, HTTP PASS, core4/4 и5timers active. Доступные этапы выпуска и проверки автоматического запуска завершены. Полные source-quality/freshness24h/7d, новая полная automatic backup после исправления и отложенная AWS Console остаются отдельными gates. [Машиночитаемые доказательства](2026-09-12-resume-evidence.json).


Дмитрий: «Продолжай». Продолжается полный доступный объём плана F01–F23;
вход в AWS Console отложен по предыдущему уточнению пользователя.

## Проверенные результаты до нового выпуска

- Production при возобновлении: `4684f4709df727accac20cebff598f47a272e5bd`,
  schema `0014_group_availability_index`; site, monitor, admin и Caddy работают.
- Контролируемый full 10–11 сентября завершился за 14453.664 секунды, exit 1,
  без остаточных процессов. Исходный результат `full_refresh_failed` сохранён.
  Единственное несоответствие safety comparison — ожидаемое штатное изменение
  производных dup_group_id/is_primary. Отдельный read-only аудит подтвердил:
  исходные id/platform/external_id и строки сохранены; 3381 активная группа
  имеет минимальный ID и одного primary; публикации, overlays, inbound и медиа
  сохранены. Это уточнение safety, а не подмена исходного quality failure.
- Источники full: Nekretnine 4365/4371 observed, 1836 processed, timeout 1800s;
  4zida 4612 observed, 832 processed, timeout 1800s и неверный denominator 6500;
  Halooglasi 3477 processed, denominator изменился 3566→3569; CityExpert
  677/677, полный успешный обход. Sweep разрешён только CityExpert.
- Настоящие автоматические nightly 11 и 12 сентября завершились degraded.
  Последний: `b5469a41a7ae4c86a57886426effa57c`, systemd invocation
  `6c62a25862724aaaadaf9717e2560079`, 03:05:07–05:50:38 UTC, exit 1.
  Все четыре incremental sources без ошибок; детали 1949 verified / 451 failed
  из 2400 attempts; ссылки 159 checked / 5 errors; скрыто 0.
- Свежая HTTP-проверка: каталог 188, карта 176, 8 detail witnesses согласованы
  с БД. Цели свежести 24h/7d ещё не достигнуты. Оценка требуемой производительности
  2162 successes/day, не менее 2425 attempts/day по наблюдаемой семидневной доле.

## Исправление 4zida

Commit `97d6493fc0bbe0777a54cb96a39789ab25bfc2a3` читает фактический счётчик
результатов из rendered header, исключая SEO-оценку и счётчики агентств.
Живой read-only probe: основной поиск old=6500 / new=4497;
диапазон 300–400 old=null / new=216. Записей в БД и медиа нет.

Точная версия: 1541 unit passed, 1 skipped; 54 browser passed;
changed-code lint и shell syntax passed. GitHub Actions
[34700682644](https://github.com/MWildFire/BelgradAppartment/actions/runs/34700682644):
unit и browser success, включая frozen dependencies, shell и changed lint.
Пакет подготовлен из 435 tracked files, SHA256
`874dddca1ecf5cc343624767396167b3cdb52126f71b56056ed11c0cb214b2c7`.

## Текущая работа

По состоянию на 15:05 UTC новая внешняя копия
`20260912-144639-30a67c00` ещё создаётся. Старый accepted backup старше
36 часов и не используется для разрешения нового выпуска. LaunchAgent работает;
пакет 97d6493 пока не установлен. Далее — полный restore и receipt, выпуск,
проверка сайта и сохранности прав, настройка source budget 3600s и detail quota
3000, public timer и проверяемый автоматический результат. Лимиты ещё не изменены,
public timer ещё выключен. Snapshot shadow, active off, auto-hide off.

Подробные приватные receipts: `/Users/mvstrike/Backups/RentBelgrad/2026-09-12/`.
Результаты failed/degraded сохраняются без подмены PASS.

## 15:28 UTC — полный read-only обход кандидата 4zida

Изолированный процесс использовал старый live runtime и заменил только функцию
счётчика в своей памяти; AST этой функции совпадает с exact97d6493. Production code,
БД, фото и отправки не изменялись; использован обычный source lease.
За739.462s прочитаны414страниц:4493уникальных объявления, failed0/skipped0.
Это устраняет прежние ложные failed_pages на концах диапазонов. Достоверность
стабильного знаменателя всё ещё не доказана: на99base-pages источник отдал
числа4244–4493 с62изменениями. Первый и последний count4493 совпали с collected,
но строгий результат остаётся `source_total_changed`, `crawl_complete=false`.
Разрешение на sweep из равенства этих чисел не выводится.

SSH-соединение закрылось до вывода результата; процесс штатно завершился и сохранил
отчёт на сервере. Повторный обход не запускался, отчёт получен отдельным подключением.
Для следующих продолжительных подключений используются keepalive-пакеты.

## 15:33 UTC — актуальность ISP подтверждена

Авторизованный Chrome, IPRoyal My Orders: два ISP-заказа80647128/80643004,
по2Serbia proxies, оба `Active subscription`; сроки25.09 12:18/11:15 GMT+4.
Credentials и формы управления не открывались, оплаты/изменений подписок нет.
На основании нового просмотра атомарно обновлён только безопасный operational
receipt; owner/mode сохранены, SHA256
`5432596b2d750165641fed5cf61faf9ae9640e344c0dc9fd4d400c31de3582b1`.

## 15:51–16:07 UTC — сохранённая копия дозавершается после ошибки передачи

LaunchAgent завершился с ошибкой `transfer_media / CalledProcessError / rsync11`;
новая копия не была принята. Свободно464GiB на Mac и41GiB на сервере, нехватка
диска не подтверждается. Сохранились online DB/state и исходный manifest1068429files,
SHA256 `d63f1835b243b5bb17bf2c1784210f37012feb49879d8ce3eaa0a10cc2928d09`.
Автоматический runner штатно удалил только своё remote staging.

Оператор повторил только20588изменившихся listing media с прежними параметрами
передачи под локальным и remote backup locks: exit0, stderr пустой. Причина
первоначальной ошибки не воспроизведена; отдельный dry-run openrsync также
возвращал ошибку, но реальная передача одного файла и полного набора успешна.
Тест нового вложенного каталога с изменённым содержимым того же размера/mtime
также проходит; это дополнительная проверка, а не доказательство устранённого бага.

Сейчас отдельный operator runner выполняет **штатные** `verify_directory`
для каждого файла, `verify_application`, свежий protected restore и проверенную
публикацию receipt. Исходные created_at/schema/manifest не изменяются. Для server
receipt validator восстановится только тот же manifest в новом private metadata
staging; новый source snapshot этим не объявляется. При успешном завершении метод
будет явно `operator_resumed_retained_snapshot`, а первоначальный LaunchAgent run
останется failed. До этих проверок backup и новый app release не приняты.

## 16:10–16:24 UTC — обнаружена и исправлена несогласованность snapshot

Полная проверка всех файлов старой pending generation прошла checksum stage,
но строгий `validate_restored` отверг её: индекс фотографий содержал49оригиналов
и98вариантов, которых нет в source inventory. На live сервере отсутствующих
оригиналов в текущем индексе0. Причина воспроизведена тестом concurrent publication:
индекс копировался после перечисления медиа и успевал включить новые публикации.

Commit `bde91c59ddf8c40fd8b9c865175cf8a0dc987c3a` фиксирует атомарный индекс
**до** перечисления зависимых фотографий для archive и directory snapshot.
Строгий restore validator не изменён; ранняя гипотеза о безвредном stale cache
отброшена, её изменения не сохранены. Две race fixtures и остальные recovery
проверки:53PASS; браузер54PASS. Общий unit/CI ещё выполняются.

Начата новая canonical source generation `20260912-162130-917ffa02`.
Оператор использует только unaccepted media предыдущей попытки как транспортный
кэш, затем заново проверяет весь новый inventory/checksums, DB, приложение,
фото и fresh protected snapshot. Accepted поколения неизменны, новая DB/time/
manifest будут от нового source snapshot. Исходный failed manifest сохранится
отдельно. Private state `fresh-backup-state.json`, журнал `fresh-backup.log`.

## 16:26–16:37 UTC — естественная доставка и исправление фоновой передачи

Новая read-only проверка ledger подтвердила реальную обычную доставку15:15–15:18UTC:
8matches ×2recipients =16sent, положительные provider IDs, delivered_at, снятые
claims, длины альбомов равны frozen photo_paths; все8имеют PublishedPost, attempts1.
Тестовых сообщений/replay не было. Live16:24:194catalog/181map/215public rights,
public originals+variants200, actualprivate404, admin401,201runtime hashes PASS.

Изолированный transport experiment600файлов воспроизвёл исходный rsync11:
при launchd soft limit256 macOS openrsync+GNU sender завершается после249файлов,
`Too many open files`; тот же batch при4096 проходит. Удаление лишнего
`--link-dest` из передачи **изменённых** файлов проходит600/600 при прежнем256,
seed неизменён. Отдельная regression fixture на старом коде также падает rsync11;
на новом проходит полный backup/restore. SHA-совпавшие файлы по-прежнему явно
hardlink-ятся; source/delete/inplace не добавлены.

Commit `52266a2cce295342cd5b8a1ea3c7ceea0d1a040d` включает это исправление;
54targeted PASS, полный exact unit/browser/CI34705731731 выполняются.
Canonical server source helper уже содержит проверенный snapshot-order fix;
новый frozen local runtime52266a2 подготовлен, но LaunchAgent ещё8756a6d.
Он будет переключён после завершения текущей ручной копии, не во время её lock.

## 16:58–17:05 UTC — копия принята, финальная версия выпущена

Generation `20260912-162130-917ffa02`, source snapshot16:21:55UTC:
1 068 893 файла,72 607 838 028bytes,5631DBphoto references; новая передача464media.
Строгая полная проверка inventory/SHA/DB/schema0014/integrity/FK пройдена;
изолированные catalog/card/photo200, image decode и network-disabled smoke PASS.
Fresh protected config/Caddy/2sessions/FileVault/permissions PASS; live authentication
сессий отдельно не проверялась. Local/server receipts совпадают. ManifestSHA
`da395d4abd8867f6a4fa4fee8e795b99d8726baf0b48a74cf3aee80a05e76ef4`;
accepted receiptSHA `51d6c26ed4582d5b39a4cb5dea0b9bceaf740e1ba4ea8a1ebf3901b3d0d22442`.
Метод — operator fresh snapshot с reuse только unaccepted media, не автоматический PASS.

Final52266a2:1545unit/1skip (122.97s),54browser (71.43s),54recovery-targeted,
changedlint0/shell/CI34705731731 оба jobs SUCCESS. Packet435files SHA
`115df94e84c35183de1dda986a3199d177a2373fdd0a8de3db024eb07ee7cd58`.
Activation под root unit `rentbelgrad-release-52266a2` завершилась exit0;
health/ready20012.309s, schema0014, прежние4timers восстановлены.
Read-only postdeploy:201runtime hashes,194catalog/181map/215rights unchanged,
original+variant GET/HEAD200/decode/hash/no-store, actualprivate404, admin401 PASS.
Chrome показал реальную главную со194квартирами; заявок/сообщений не отправляли.

Лимиты source1800→3600s, nightly2400→3000 применены17:05UTC, новые значения
подтверждены новым процессом. Прочие env bytes/owner/mode сохранены; дополнительных
рестартов не было. Shadow/active-off/auto-hide-false сохранены.

Frozen local runtime52266a2:435Git blobs verified,uv0.8.22/Python3.12.13/67packages.
LaunchAgent переключён на него с сохранением прочих полей; RunAtLoad exit0,
`skipped_not_due` по свежей копии. Это проверенный запуск repaired runner,
но не новая полная автоматическая копия.

## 17:11–17:20 UTC — новый canary, safety и включение расписания

Canary `26be6f37722041f997d7b00138042073`:20attempts/9verified/11errors,
3confirmed-dead outcomes,0skipped,1price change,297.062s. Исходный exit1/
`canary_failed` сохранён. Все215прав/overlays/inbound/identities сохранены;
289photo files decode/phash verified; residual0, guard failures0, concurrent activityfalse.

Независимые11read-only fetches17:14–17:19:5CETIRI empty identity-valid payloads,
3HALO TimeoutError90s,3NEKR actual404. Старые поля/фото/successes сохранены;
неподтверждённые отказы не изменили availability, всем записан backoff.
Отдельная safety acceptance прошла10checks; checker дополнительно проверен
позитивной fixture и17negative mutations (2pytest tests PASS).
Safety receiptSHA `e6ff21f20c5af718513fee57d452b2d5fa5f6482e8d6310b45a6190151ceed04`.
Это допуск scheduled public-only data refresh, не quality PASS.

`otz-public-detail-refresh.timer` включён17:20:53UTC: enabled/active,
canonical11:30/17:30/23:30UTC, jitter5min, Persistent=yes.
**Фактический первый next run17:32:32UTC; LastTrigger ещё пустой.**
До настоящего запуска timer proof не объявлен полученным.

## 17:32 UTC — расписание реально сработало

`LastTriggerUSec=17:32:37UTC`, service invocation
`d42002917ae948339d9843ab0e8a9726`, новый run
`457147e963cf49c5bc1369c1cb28fa1c`:185выбранных объявлений.
Ручной start service не выполнялся. Пока это доказательство запуска по timer,
не терминального результата или достижения freshness SLO.

## 17:39–17:42 UTC — настоящий автоматический проход завершён

Служба завершилась17:39:23UTC,406s, exit1/Result=exit-code; это ожидаемое честное
отражение частичных отказов, не success. Timer остаётся enabled/active;
следующий фактический trigger23:33:45UTC. Core4/4active, все5timers active.
MainPID=0/ControlPID=0. Invocation и terminal checkpoint связаны с реальным timer,
ручного start не было. В journal по invocation нет строк, поскольку unit пишет
stdout/stderr в отдельный файл; доказательства — systemd properties, durable
checkpoint и независимая read-only сверка фактических DB attempts/successes.

| Источник | Попытки | Подтверждены | Отказы |
|---|---:|---:|---:|
| 4zida | 44 | 26 | 18 |
| CityExpert | 28 | 28 | 0 |
| Halooglasi | 78 | 75 | 3 |
| Nekretnine | 35 | 13 | 22 |
| Всего | 185 | 142 | 43 |

Пропусков0, source-level startup errors0, photos0, price changes1;
22recorded DEAD outcomes входят в errors, это не число новых снятий с сайта.
Все6DB checks PASS:185attempts/142successes/43failures совпали с checkpoint,
всем отказам записан backoff, DEAD count согласован, все selected обработаны.

После задания: fresh operational HTTP PASS,194catalog/181map/215rights;
public rightsSHA не изменился. EvidenceSHA
`0a84bd8f31b72e2479b41cb5e9e8b6879d88a3514f43c95c82a8adeb57f97c43`.
Public source-original success24h:4zida27/49,City33/33,Halo77/83,Nekretnine15/40.
Эти знаменатели относятся к source originals в отчёте, не194представителям каталога.
142verified — число проверок в этом проходе, не142дополнительно свежих квартир.
Computed health остаётсяdegraded:public errors, недостаточная успешная свежесть
и отсутствие принятого full для трёх источников. Общий collector оставляет
`unattended_updates_proven=false`, поскольку сам не проверяет systemd;
фактический scheduled invocation/terminal proof приведён отдельно и не подменяет SLO.

Дальнейшее наблюдение: ближайший weekly full13.09 около02:14UTC уже прочитает
новый source budget3600s; nightly3000 и очередные public refresh должны дать
измерение24h/7d. По текущей выборке минимум2168successes/day/оценка2431attempts,
квота3000 не является доказательством достижения. Устранять только
воспроизводимые остаточные source failures; не включать sweep по неполному
обходу, не переводить snapshot вactive и не включать auto-hide без соответствующих gates.
Новая полная unattended backup на исправленном52266a2 ещё не наблюдалась;
свежая manual generation принята, repaired RunAtLoad exit0/skipped_not_due.
AWS Console/security group отложены пользователем. Новых Codex automations нет.
