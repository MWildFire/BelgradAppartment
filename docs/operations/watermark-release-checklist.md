# Watermark — решение о выпуске (R04, обновлено в WM05)

## WM05 (31.10.2026): ворота G1 и G2 и совместимость выпуска

Числа сняты на коде `3e01298` (итоговое слияние W1+W2+W3; дальше в ветке только документы). Сырые
доказательства — `rounds/round-wm05-20261031/status/evidence-m/` (вне Git: `quality.md`, `memory.md`,
`pytest.txt`, `e2e.txt`, `no-regress.txt`, `compat.txt`, `pairs/`).

| Ворота | Требование | Результат WM05 |
|---|---|---|
| Код | полный pytest зелёный | 2869 passed, 1 skipped, 5 subtests (R04: 2645); браузерные `tests/e2e` — 54 passed |
| Совместимость выпуска | файлы выпуска побайтно как у прода, без OpenCV | `git diff --quiet` против `PROD_REV` и `65f94a5` — код 0; `grep -rnw cv2` пусто |
| **G1** качество | фото с bad ≤ 2 из 36, ни один знак не хуже R04 | **1** bad-фото (`zida_003`) по решению O, 2 строго по слепой оценке R-W2 (+`zida_156`); R04 — 9 |
| G1, польза | центральные знаки: kept ≤ 5, bad ≤ 2, ok+minor ≥ 22; углы `lama`: bad 0, kept ≤ 3 | центральные 13 ok / 12 minor / 1 bad / 3 kept (`halo_005/006/007`, `self_check`); ok+minor = 25; углы `lama` 10 / 7 / 0 / 0 |
| **G2** память | пик ≤ 1,0 ГиБ на альбоме из 10 фото; простой бота ≤ 300 МиБ | Linux aarch64: Σ RSS бота и воркера 921–926 МиБ (углы `skip`), 977–990 (углы `lama`); простой 122–135 МиБ; лимит 1,5 ГиБ без OOM; x86_64 (эмуляция) 952 / 1009 |
| Бот | офлайн-сценарии единого демона | 134 passed (альбом 10, только оставлено, смешанный, чужой чат, оба флага выключены, сбой воркера LaMa, выгрузка по простою) |
| Не сломать | подписки, доставка, сайт не хуже `72fff40` | 1649 passed против 1640 (список O), 1784 против 1730 (grep-набор), новых failed нет |

Умолчания кода не менялись: углы Halo `skip`, `max_pixels=75000`, потоки torch 2, `WATERMARK_DISABLED_KINDS`
пусто; выбранные значения для прода — в `/opt/otz/.env` (только `WATERMARK_BOT_ENABLED=1` и
`WATERMARK_HALO_CORNER_MODE=lama` при запасе памяти — см. [runbook](watermark-remover.md), «Включение
на проде»). Запас G2 у углов `lama` небольшой, поэтому на проде есть правило отката на `skip`.
Пункты ниже про R04 — исторические; отмечены те, что закрыты в WM05.


Дата обновления:16 октября 2026. Код локального кандидата:
`9d163297c2f93f06446a768ebb17cfbf2a5914b8`. Документы поставляются отдельным commit после
этой проверенной кодовой ревизии; квитанция full-теста остаётся привязанной к коду.

## Состояние поставки

| Область | Статус |
|---|---|
| Код | R04: 2645 passed; WM05: 2869 passed / 1 skipped / 5 subtests, exit 0 |
| Качество M1 | R04: BLOCKED (9 bad). WM05: G1 выполнено — 1 bad-фото (строго R-W2 — 2), цель ≤ 2 |
| Ресурсы M3 | R04: измерений не было. WM05: Linux-замер, G2 выполнено (Σ RSS 0,90–0,97 ГиБ, простой ≈ 0,12 ГиБ) |
| Выпуск M2 | R04: маршрут не совместим. WM05: обычный маршрут «только код», пакет собирается локально; выкат — только P |
| Публикация | Локальный кандидат и local_draft_only body; внешней публикации в R04 нет |

На read-only снимке 16 октября 01:21GST main=`65f94a5073a5cd096ec84a6baf5dafd3be68f277`,
feature=`ec2340b887e28c33e917a7d0a4d058891dbbc159`.
[PR14](https://github.com/MWildFire/BelgradAppartment/pull/14) OPEN/draft с этой feature-головой.
Его сохранённый `baseRefOid=adcafcbd0054f8ea8c25d9afec38a8dc56253c27` — отдельное поле API,
его нельзя подменять текущей головой main. Локальный обычный main→candidate merge сохранён;
rebase/force push, remote integration, ready/merge PR14 и deployment не выполнялись.

## Локальные доказательства

- [x] Полный новый-head pytest закончен без failures/errors. Ранние sandbox-failed,
  user-interrupted и test-timing-failed прогоны сохранены отдельно, не сложены с targeted зелёными.
- [x] Изменённые 7 сценариев прошли в реальной среде без torch. Ранее 170pass/6skip без torch,
  21 независимый storage/delivery probe, настоящая модель,54 E2E и 24 browser views сохранены
  на их фактическом SHA `e88eaf170ea79c2c306e7bf45d7670634203dbe6`; перенос доказательств
  основан на равенстве всех неизменённых runtime-файлов, а не на переименовании квитанций.
- [x] Default skip и три продуктовых флага OFF, production timeout180 с сохранены; новые lint diagnostics0.
- [x] Полная независимая [QA R04](../reviews/2026-10-16-watermark-qa-r04.md) включает 36/46,
  все 17 углов каждой альтернативы и сохранность приватных оригиналов/модели.
- [x] Независимо приняты OG snapshot/ETag, model-appearance retry и защита от поздних поколений.
- [x] Изолированный helper проверен для exact-root dry-run; apply отключён, write-safety не заявляется.

Исходные логи, фотографии, модель, task-файлы и частные пути остаются в локальном пакете,
не в product Git. Исторические R02/D2/R03 документы не переписаны.

## M1: качество и продуктовые решения

- [x] Качество достигло ≤ 5 bad, цель ≤ 2, без новых bad — WM05: 1 (решение O) / 2 (строго R-W2), см. выше.
- [x] Выбран режим углов: `lama` (WM05: 17 углов 10 ok / 7 minor / 0 bad), если на сервере хватает памяти
  (правило в runbook); умолчание кода остаётся `skip`.
- [x] Текст бота выбран (N07/N08) и внедрён в WM05; политика старых/повторных команд не менялась.
- [ ] Отдельно решены допустимость публикации фотографий и этапы активации. Техническое удаление
  знака само по себе не устанавливает права на публикацию; права конкретных фото здесь не проверялись.

Остаточные bad R04: halo005/006/007, zida003/152, cx001/004/005/041. WM05: остался один — zida003 (пятно под креслом);
halo005/006/007 теперь честно оставляются (`self_check`), cx и zida152 — ok/minor.
Прототипы H1/H2/H3 отклонены; это завершённые отрицательные эксперименты, не улучшение продукта.

## M3: репрезентативные ресурсы

- [ ] Указаны существующая разрешённая Linux/cgroup среда, архитектура, effective ancestor/service limits,
  RAM/swap/резерв хоста, concurrency и допустимая задержка; нет выдуманных чисел бюджета.
- [ ] Измерены отдельно bot и monitor/delivery, две resident-модели idle/serial/concurrent,
  aggregate peak, cold/warm/steady-state, album10/full36, decoder boundary и реальные application
  exception/timeout/oversize/late-worker сценарии с fake transport.
- [ ] Проверены завершение всех процессов, OOM/failure receipts, сохранность и декодирование результатов.
- [ ] Владелец принял измеренный бюджет и архитектуру; необходимая реализация независимо проверена.

Исторический Mac peak5396348928bytes (5.026GiB) — один процесс, не Linux-замер.
Удвоение одиночного пика не измеряет пару. В R04 отдельный `deploy/otz-bot.service` имел
MemoryMax=2G; с WM05 этого unit нет (`otz-bot` не используется), бот живёт в
`otz-subscription-bot`, а лимиты вешаются systemd drop-in вне репозитория (см. «Включение на
проде» в [runbook](watermark-remover.md)). У [monitor unit](../../deploy/otz-monitor.service)
лимит не задан. Это не подтверждение effective production limits или конкретного OOM.

WM05 (31.10.2026) закрыл часть этого раздела измерениями, пункты выше остаются открытыми там, где
нужен сервер: Linux aarch64 и x86_64 (эмуляция) в контейнере с лимитом 1,5 ГиБ (как `MemoryMax`), альбом 10
и все 36 фото, тёплый и холодный кэш, процесс бота и воркер LaMa считаются вместе; LaMa живёт в отдельном
процессе (одна инференс-задача, потоки torch 2, выгрузка через 600 с простоя), бот импортирует `torch` ни разу.
Числа — `docs/operations/watermark-lama-runtime.md`, отчёты W3 и M. Не измерено: пара бот + монитор на
одном хосте (на проде P смотрит `free -m`, `memory.peak` юнита и swap), настоящие cgroup-лимиты сервера.

Engine-пакет имеет независимое LOCAL PREPARATION OK после RR01–RR04. Его synthetic receipts
не заменяют перечисленные измерения. Lock публикации из [store](../../src/watermark/store.py)
не сериализует inference и не освобождает две модели. Worker/очередь, новые лимиты и иная архитектура
не выбраны и не реализованы этой волной.

## M2: совместимое восстановление и выпуск

**WM05 (31.10.2026):** ветка R04 отвергалась обычным маршрутом «только код», потому что
`validate_data_only_release` видел различия в `pyproject.toml`, `uv.lock`, `src/config.py`,
`deploy/otz-bot.service` (и R04 менял `scripts/release.py`, `scripts/vps_install.sh`). W1 убрал
причины: OpenCV заменён на `src/watermark/imgops.py`, настройки вынесены в
`src/watermark/settings.py`, новый unit не нужен (бот — единый демон `otz subscription-bot`), и
все эти файлы побайтно равны коду прода / `65f94a5`. Проверка (оба кода 0):

```
git diff --quiet 0819a897f446a3850ced8b49ca1a776049183003 <RC> -- pyproject.toml uv.lock alembic.ini \
  src/config.py src/db/models.py scripts/runtime_contract.json data/districts.yaml alembic deploy
git diff --quiet 65f94a5 <RC> -- scripts/release.py scripts/vps_install.sh
grep -rnw cv2 src scripts pyproject.toml uv.lock || echo "no cv2"
```

Ниже — исторический текст R04 (до WM05): «Actual `validate_data_only_release` отвергает этот scope»;
все 43 guarded/validator файла кандидата равны независимо проверенному feature-контракту;
OFF флаги не отменяют проверку байтов. Синтетические 8 проверок и 29 release/rollback tests —
локальные контракты, не реальные backup/restore receipts. См. [release helper](../../scripts/release.py),
[releases](releases.md), [offhost backup](offhost-backup.md).

- [ ] Владелец выбрал полную защищённую копию или другой совместимый, отдельно проверенный маршрут.
- [ ] Зафиксирован реальный release SHA и требуемые точные тестовые receipts, не подставлен старый SHA.
- [ ] Есть реальная generation с исходным временем создания, source revision, schema и manifest hash;
  возраст не превышает 36 ч именно при будущей активации.
- [ ] Для выбранного маршрута подтверждены same-generation offhost/protected proof, шифрование/права,
  isolated application restore, DB/FK/schema и фотография: HTTP200, decode и визуальная проверка.
- [ ] Проверен совместимый откат кода/runtime/units при additive schema и сохранён нужный материал отката.
- [ ] Проверены effective units/timers, конкурирующие writers и ровно один реальный poller
  (`otz-subscription-bot`; unit `otz-bot` с WM05 не используется и не ставится); список имён
  SERVICES не является такой проверкой.
- [ ] Проверены full-size, превью и OG при полном визуальном откате. Флаг OFF возвращает original
  full-size и OG, но не обязательно уже созданные clean-превью; нужен revert/rebuild и readback.
- [ ] Для отдельно разрешённой активации готовы health/DB-ready/media проверки и конкретный оператор.

Ни реальная backup generation, ни protected/offhost/application restore, ни release/activation
не выполнялись здесь. Чувствительные значения конфигурации не извлекались для подготовки выпуска.
После будущего фактического релиза нужен отдельный reviewed handoff
`SOURCE_REVISION` + `HELPER_SET_SHA256` + соответствующий helper set.
Старые незавершённые epochs обрабатываются своим совместимым helper; заменой одного пина это не решается.

## Этапы остаются отдельными решениями

| Этап | Условие продвижения |
|---|---|
| Код с флагамиOFF | Принятые качество/ресурсы/совместимое восстановление и отдельное разрешение выпуска |
| Бот | Выбранные тексты/команды, allow-list, один poller, бюджет и разрешённая live-проверка |
| Доставка | Application/resource envelope, frozen original fallback и отдельное включение |
| Сайт | Public-only выбор, варианты/OG/откат и отдельное решение о публикации |

Ни один этап не запускается автоматически после review. Production/DNS/IAM/Pages, реальные
сообщения, main merge и уборка не входят в текущий scope. [Runbook](watermark-remover.md)
описывает поведение и границы, не заменяя эти решения.
