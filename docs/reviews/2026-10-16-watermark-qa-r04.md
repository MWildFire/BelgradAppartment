# Watermark QA R04 — 16 октября 2026

**Код принят локально; качество M1 BLOCKED.** Проверенная кодовая ревизия:
`9d163297c2f93f06446a768ebb17cfbf2a5914b8`. Публикации/deployment не было.
По умолчанию `skip`: **1ok /10minor /9bad /16only-kept**. Девятьbad превышают предел≤5
и цель≤2; accepted deviation отсутствует. Все три продуктовых флага OFF.

## Метод и происхождение

36 приватных фото содержат 46 ожидаемых instances:18zida,6Halo-center,5CityExpert,17Halo-corner.
Новая базовая матрица и paired/repeat серии во всех трёх режимах all matched; совпадение с D2
и повторов подтверждено по пикселям/геометрии. Исходники и pinned модель проверены хешами,
все 36 исходных фото декодируются. Нового detector/model/dependency или изменения масок в продукте нет.

Независимая N05 проверка действительно просмотрела 171 lossless страницу:36 full triptychs,
46 default instances и 17+17 alternative corner instances с контекстом 48px и масштабом 2×,
включая все широкие crop tiles. Дополнительно просмотрены 4 страницы смешанного 043 и 16 страниц
N03 regressions. Неизменные cross-mode crops перенесли оценку только после equal-pixel proof;
это не объявляется повторным свежим просмотром. Отдельное integration review просмотрело
86 выбранных QA panels и 24 синтетических browser images; этот объём не подменяет полную N05.

Фотография получает худшую оценку среди non-kept instances. Если все instances kept,
фото относится к only-kept. `kept` не означает успешное удаление. Таблицы ниже используют
консервативные независимые оценки, а не переименованные исторические D2.

| Режим | Фото ok/minor/bad/only-kept | Instances ok/minor/bad/kept | Углы ok/minor/bad/kept |
|---|---|---|---|
| skip | 1 / 10 / 9 / 16 | 4 / 16 / 9 / 17 | 0 / 0 / 0 / 17 |
| telea | 1 / 12 / 23 / 0 | 4 / 19 / 23 / 0 | 0 / 3 / 14 / 0 |
| lama | 5 / 22 / 9 / 0 | 9 / 28 / 9 / 0 | 5 / 12 / 0 / 0 |

## Все 36 фотографий

| Photo ID | skip | telea | lama |
|---|---|---|---|
| `zida_000` | minor | minor | minor |
| `zida_001` | ok | ok | ok |
| `zida_002` | minor | minor | minor |
| `zida_003` | bad | bad | bad |
| `zida_043` | minor | bad | minor |
| `zida_054` | minor | minor | minor |
| `zida_122` | minor | minor | minor |
| `zida_152` | bad | bad | bad |
| `zida_156` | minor | minor | minor |
| `cx_001` | bad | bad | bad |
| `cx_004` | bad | bad | bad |
| `cx_005` | bad | bad | bad |
| `cx_015` | minor | minor | minor |
| `cx_041` | bad | bad | bad |
| `halo_000` | only-kept | bad | minor |
| `halo_001` | only-kept | bad | ok |
| `halo_002` | only-kept | bad | minor |
| `halo_003` | only-kept | minor | ok |
| `halo_004` | only-kept | bad | minor |
| `halo_005` | bad | bad | bad |
| `halo_006` | bad | bad | bad |
| `halo_007` | bad | bad | bad |
| `halo_008` | only-kept | bad | minor |
| `halo_009` | only-kept | bad | minor |
| `halo_010` | minor | minor | minor |
| `halo_011` | only-kept | bad | minor |
| `halo_012` | minor | minor | minor |
| `halo_013` | only-kept | minor | minor |
| `halo_014` | minor | minor | minor |
| `halo_015` | only-kept | bad | minor |
| `halo_016` | only-kept | bad | minor |
| `halo_017` | only-kept | bad | minor |
| `halo_018` | only-kept | minor | minor |
| `halo_019` | only-kept | bad | ok |
| `halo_020` | only-kept | bad | ok |
| `halo_021` | only-kept | bad | minor |

## Все 46 instances

| Instance ID | Вид | skip | telea | lama |
|---|---|---|---|---|
| `zida_000/zida_logo#0` | zida_logo | minor | minor | minor |
| `zida_000/zida_logo#1` | zida_logo | ok | ok | ok |
| `zida_001/zida_logo#0` | zida_logo | ok | ok | ok |
| `zida_001/zida_logo#1` | zida_logo | ok | ok | ok |
| `zida_002/zida_logo#0` | zida_logo | minor | minor | minor |
| `zida_002/zida_logo#1` | zida_logo | minor | minor | minor |
| `zida_003/zida_logo#0` | zida_logo | minor | minor | minor |
| `zida_003/zida_logo#1` | zida_logo | bad | bad | bad |
| `zida_043/halo_corner#0` | halo_corner | kept | bad | ok |
| `zida_043/zida_logo#0` | zida_logo | minor | minor | minor |
| `zida_043/zida_logo#1` | zida_logo | minor | minor | minor |
| `zida_054/zida_logo#0` | zida_logo | minor | minor | minor |
| `zida_054/zida_logo#1` | zida_logo | minor | minor | minor |
| `zida_122/zida_logo#0` | zida_logo | minor | minor | minor |
| `zida_122/zida_logo#1` | zida_logo | ok | ok | ok |
| `zida_152/zida_logo#0` | zida_logo | bad | bad | bad |
| `zida_152/zida_logo#1` | zida_logo | minor | minor | minor |
| `zida_156/zida_logo#0` | zida_logo | minor | minor | minor |
| `zida_156/zida_logo#1` | zida_logo | minor | minor | minor |
| `cx_001/cityexpert_center#0` | cityexpert_center | bad | bad | bad |
| `cx_004/cityexpert_center#0` | cityexpert_center | bad | bad | bad |
| `cx_005/cityexpert_center#0` | cityexpert_center | bad | bad | bad |
| `cx_015/cityexpert_center#0` | cityexpert_center | minor | minor | minor |
| `cx_041/cityexpert_center#0` | cityexpert_center | bad | bad | bad |
| `halo_000/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_001/halo_corner#0` | halo_corner | kept | bad | ok |
| `halo_002/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_003/halo_corner#0` | halo_corner | kept | minor | ok |
| `halo_004/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_005/halo_center#0` | halo_center | bad | bad | bad |
| `halo_006/halo_center#0` | halo_center | bad | bad | bad |
| `halo_007/halo_center#0` | halo_center | bad | bad | bad |
| `halo_008/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_009/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_010/halo_center#0` | halo_center | minor | minor | minor |
| `halo_011/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_012/halo_center#0` | halo_center | minor | minor | minor |
| `halo_013/halo_corner#0` | halo_corner | kept | minor | minor |
| `halo_014/halo_center#0` | halo_center | minor | minor | minor |
| `halo_015/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_016/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_017/halo_corner#0` | halo_corner | kept | bad | minor |
| `halo_018/halo_corner#0` | halo_corner | kept | minor | minor |
| `halo_019/halo_corner#0` | halo_corner | kept | bad | ok |
| `halo_020/halo_corner#0` | halo_corner | kept | bad | ok |
| `halo_021/halo_corner#0` | halo_corner | kept | bad | minor |

## Девять остаточных bad

Оценки ниже — независимо наблюдавшиеся дефекты; качество не принимается по одному числу detector score.

| Фото | Наблюдение reviewer |
|---|---|
| `zida_003` | Coarse dark patch under the chair is visible at full frame. |
| `zida_152` | Prominent pale arcs/dots remain on the wall, visible full frame. |
| `cx_001` | Horizontal wall line/letter ghosts, interrupted cabinet lines and foliage smearing. |
| `cx_004` | Scalloped sofa edge, smeared books and visible wall brightness/letter ghosts. |
| `cx_005` | Repeated broken mouldings/radiator lines, prominent in the full frame. |
| `cx_041` | Orange curtain/window lines interrupted, wall ghosts and damaged sofa trim. |
| `halo_005` | Building/facade smeared; arrow head detached/broken. |
| `halo_006` | Invented rounded sofa backrest/cushion and pale point on seat are visible full frame. |
| `halo_007` | Warped rear desk chair/floor/door details and smoothed cushions. |

`halo_006` остаётсяbad из-за выдуманного выступа/контура дивана. Оранжевый предмет в `halo_012`
присутствует в оригинале; сглаживание его поверхности оцениваетсяminor, а неизвестная исходная
геометрия не придумывается. `halo_011` Telea bad/LaMa minor; `halo_015` Telea расширяет зубчатую
белую границу, LaMa ограничивает её, но немного меняет угол. В `zida_043` default сохраняет один
угол при двух обработанных знаках: это не три успешно удалённых знака.

## Отрицательные эксперименты и историческая шкала

| Эксперимент | Проверка | Итог |
|---|---|---|
| H1, горизонтальное отражение контекста Halo-center с той же маской | 6 целевых instances; полный 36/46 paired/repeat | 0ok/3minor/3bad сохраняются; отклонён |
| H2, согласие направленных интерполяций внутри существующей маски 4zida | 18 целевых instances; полный 36/46 paired/repeat | D2 5ok/11minor/2bad →1ok/14minor/3bad; новыйbad zida002#1, четыре D2ok→minor; отклонён |
| H3, affine background для оценки opacity CityExpert | 5 целевых instances; полный 36/46 paired/repeat | 0ok/1minor/4bad сохраняются; отклонён |

Это отдельные ограниченные гипотезы, не повтор выбора отвергнутых L1–L4/B1–B8 и не новая модель.
Каждый repeat воспроизводим (MAE0), заданный timing gate соблюдён; этого недостаточно для качества.
Ни один отрицательный прототип не внесён в product candidate. No-new-bad соблюдён для сохранённого
продуктового алгоритма, а не для отклонённого H2.

Исторический D2 default имеет 5ok/15minor/9bad/17kept instances. Независимая консервативная
оценка —4/16/9/17: у `zida_054/zida_logo#1` D2ok, исторический и текущий независимые reviewers minor.
Фото `zida_054` остаётсяminor при обоих подходах. Исходный D2 не переписывался; четыре D2ok→minor
в H2 включают эту ранее спорную оценку. Историческая переоценка R02 не объявляется новой регрессией.
[QA R03](2026-10-15-watermark-qa-r03.md) и старые review сохранены.

Engine-4 ACK был в лабораторном журнале, не отсутствовал. Приватный provenance сохранён вместе
с локальными evidence. В точной ревизии кода выше выбор метода —
[removal_for, строка 119](../../src/watermark/pipeline.py#L119); stale line118 не переносится.

## Интеграция и открытые границы

Новый полный pytest на 9d16329:2645passed/1expectedskip/5subtests, exit0. Изменённые 7 тестов
дополнительно прошли в actual no-torch. Независимые 21 storage/delivery cases,170no-torch/6skip,
реальная модель,54E2E и 24browser views относятся к e88eaf1 и перенесены по точному равенству
неизменных runtime/asset/lock/input байтов. На 9d16329 менялся только timing/join schedule теста;
прежний красный full не назван зелёным и не заменён targeted-прогоном. Единственный skip —
base-shell accessibility fixture без собственного content h1;44 предупреждения зависимостей сохранены.

OG ETag использует те же выбранные байты, что renderer. Появление модели позволяет повторить
недоступный corner fallback без force и без отмены индивидуального отказа. Generation/staging
исправляет позднюю публикацию старого worker после нового результата или discard; worker при этом
продолжает вычисления до выхода. Прямой original-path и private-stage model output совпали с
кодированным N05 JPEG. Default skip, модель/алгоритм и product flags не изменены.

Полный тест не принимает качество. M1 остаётся 9bad; M3 требует настоящих Linux/application
измерений и owner budget/architecture; M2 требует совместимого recovery route и реальных свежих
restore/release receipts. Bot text и old-command policy не выбраны и не внедрены.
Подробные границы: [runbook](../operations/watermark-remover.md),
[release checklist](../operations/watermark-release-checklist.md).

Таблицы содержат только стабильные обезличенные IDs и выводы. Фото, модель, исходные логи и
локальные task/session данные не публикуются. Тестовые и визуальные receipts доступны в приватном
пакете передачи с точными SHA; новый docs commit не переименовывает полный тест в release receipt.

