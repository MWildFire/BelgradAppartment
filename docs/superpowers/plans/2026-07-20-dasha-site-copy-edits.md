# Правки Даши по сайту RentBelgrad — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Внести 17 правок Даши (заметка «Редактируем сайт» от 18.07.2026) в публичный сайт: тексты, чистка сербизмов, схлопывание микрорайонов до 10 районов, удаление страницы «Калькулятор» с заменой на кнопку «Удаленный поиск квартиры».

**Architecture:** FastAPI + Jinja2 (`src/site/`), шаблоны в `src/site/templates/`, статика в `src/site/static/`. Районы: канонический словарь `data/districts.yaml` (общий с телеграм-хэштегами — его НЕ трогаем), отображение через `src/site/format.py:human_district`, выпадающие списки через `src/site/app.py:district_options()`. Схлопывание микрорайонов делаем только на стороне сайта (в `format.py`), чтобы хэштеги канала не изменились.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, pytest (`uv run pytest`), ruff.

## Global Constraints

- Тесты: `uv run pytest tests/site -q` — зелёные после каждой задачи; финально весь `uv run pytest -q`.
- Линт: `uv run ruff check src tests` — чисто после задач, трогающих `.py`.
- Тексты из раздела «Утверждённые тексты» копировать байт-в-байт (включая «ё», тире «—», кавычки «»).
- `data/districts.yaml` не изменять (общий с телеграм-хэштегами канала).
- `vnj.html`, `catalog.html`, `apt.html`, `map.html` не трогать — правок Даши по ним нет.
- Коммит после каждой задачи, префикс `dasha-edits:`.
- Git push не делать — только локальные коммиты.

## Решения (зафиксированы Дмитрием 20.07.2026)

1. Кнопка «Удаленный поиск квартиры» ведёт на лид-форму `/#lead`; `/calculator` → 301 туда же. Отдельная страница появится, когда Даша подготовит контент.
2. Пустой квадрат «сопровождаем сделку» (секция «Почему мы») — убрать; текст занимает всю ширину.
3. В плейсхолдере поля «Что важно» убрать только «нужен договор под боравиште»; поле остаётся.

Нормализация опечаток Даши (согласовано по умолчанию): «Писк» → «Поиск»; «Подготавливаю договор» → «Подготавливаем договор» (голос сайта — «мы»); добавлена запятая «предпочтения, планируется ли»; «телеграм канала» → «телеграм-канала»; финальные точки в предложениях.

## Утверждённые тексты

| № | Где | Новый текст |
|---|-----|-------------|
| П1 | Меню (десктоп) | `Каталог квартир` |
| П2 | Вместо «Калькулятор» | `Удаленный поиск квартиры` → `/#lead` |
| П3 | Hero-лид | `Поможем вам снять квартиру на длительный срок с необходимыми документами, для оформления ВНЖ и регистрации (белый картон). Сопровождаем на всех этапах до заселения и подписания договора.` |
| П4 | Районы (10, сортировка по алфавиту) | `Вождовац, Врачар, Звездара, Земун, Нови Београд, Палилула, Раковица, Савски венац, Стари град, Чукарица` |
| П5 | Статистика | число районов = 10 (длина списка П4) |
| П6 | Подпись фида | `обновляется автоматически из телеграм-канала @RentBelgrad` |
| П7 | Интро «Как мы работаем» | `Не маркетплейс. Живой агент ведёт за руку от первого сообщения до получения ключей.` |
| П8 | Шаг 01 (описание) | `Район, бюджет, сроки, предпочтения, планируется ли оформление ВНЖ. Пара минут в Telegram.` |
| П9 | Шаг 02 | заголовок `Готовим подборку с вариантами`, описание `Отбираем вручную, предлагаем только то, что подходит именно вам, отсекая все лишнее.` |
| П10 | Шаг 03 (описание) | `Переводим, торгуемся, проверяем документы и состояние квартиры.` |
| П11 | Шаг 04 | заголовок `Подготавливаем договор`, описание `Подписание, регистрация на адресе, документы для ВНЖ. Всё под ключ.` |
| П13 | Секция «Почему мы» | заголовок `Переезд, который реально не доставит хлопот и волнений`, абзац `Поиск недвижимости в новой стране всегда достаточно стрессовая задача — разобраться в местных юридических тонкостях, особенностях районов, задать правильные вопросы владельцу. Мы берем все главные заботы на себя, вам остается только приезжать на просмотры в назначенное время и сделать свой выбор при встрече с идеальной квартирой.` |
| П14 | Чек-лист «Почему мы» | см. Task 4, четыре пункта |
| П15 | Лид-форма, подзаголовок | `Локация, бюджет, вид отопления — пара полей, и наш специалист соберёт подборку под тебя.` |
| П16 | Плейсхолдер «Что важно» | `Сроки, лифт, питомцы…` |
| П17 | Футер, описание | `Команда опытных агентов в Белграде. Помогаем релокантам снять квартиру с необходимыми документами для ВНЖ и регистрации.` |

---

### Task 1: base.html — «Каталог квартир» и описание в футере

**Files:**
- Modify: `src/site/templates/base.html:82` (навигация), `src/site/templates/base.html:115` (футер)

**Interfaces:** только текст, разметка не меняется.

- [x] **Step 1: Проверить, держат ли тесты старые строки**

Run: `grep -rn -e ">Каталог<" -e "Свой человек" tests/site/`
Expected: `test_a11y.py:144` проверяет `<h2>Каталог</h2>` в футере — это заголовок колонки, мы его НЕ меняем; других совпадений нет → тесты править не нужно.

- [x] **Step 2: Заменить пункт меню**

В `src/site/templates/base.html` строка 82, заменить:

```html
      <a href="/catalog"{% if cur_path == '/catalog' %} aria-current="page"{% endif %}>Каталог</a>
```

на:

```html
      <a href="/catalog"{% if cur_path == '/catalog' %} aria-current="page"{% endif %}>Каталог квартир</a>
```

(мобильный bottom-nav не трогаем: там `<span>Каталог</span>` с `aria-label="Каталог квартир"` — короткая подпись для таббара осознанна; заголовок колонки футера `<h2>Каталог</h2>` тоже остаётся.)

- [x] **Step 3: Заменить описание в футере**

Строка 115, заменить:

```html
      <p class="desc">Свой человек в Белграде. Помогаем релокантам снять квартиру с договором под ВНЖ и белый картон.</p>
```

на:

```html
      <p class="desc">Команда опытных агентов в Белграде. Помогаем релокантам снять квартиру с необходимыми документами для ВНЖ и регистрации.</p>
```

- [x] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/site -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/site/templates/base.html
git commit -m "dasha-edits: меню «Каталог квартир», новое описание в футере (правки 1, 17)"
```

---

### Task 2: index.html — hero-лид и подпись фида

**Files:**
- Modify: `src/site/templates/index.html:11` (hero-lede), `src/site/templates/index.html:107` (feed-note)

- [x] **Step 1: Проверить тестовые привязки**

Run: `grep -rn -e "Помогаем релокантам найти" -e "обновляется автоматически" tests/site/`
Expected: совпадений нет (если есть — обновить строки в тестах на новые ДО правки шаблона, убедиться что тест падает, затем шаг 2).

- [x] **Step 2: Заменить hero-лид**

Строка 11, заменить:

```html
      <p class="hero-lede">Помогаем релокантам найти жильё на длинный срок — с договором, который проходит на <span style="color:var(--navy);font-weight:600">ВНЖ</span> и белый картон.</p>
```

на:

```html
      <p class="hero-lede">Поможем вам снять квартиру на длительный срок с необходимыми документами, для оформления <span style="color:var(--navy);font-weight:600">ВНЖ</span> и регистрации (белый картон). Сопровождаем на всех этапах до заселения и подписания договора.</p>
```

- [x] **Step 3: Заменить подпись фида**

Строка 107, заменить:

```html
  <div class="feed-note"><span class="dot" aria-hidden="true"></span> обновляется автоматически из <b style="color:var(--ink)">@RentBelgrad</b></div>
```

на:

```html
  <div class="feed-note"><span class="dot" aria-hidden="true"></span> обновляется автоматически из телеграм-канала <b style="color:var(--ink)">@RentBelgrad</b></div>
```

- [x] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/site -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/site/templates/index.html
git commit -m "dasha-edits: hero-лид и подпись телеграм-канала (правки 3, 6)"
```

---

### Task 3: «Как мы работаем» — интро и 4 шага (index.html + how.html)

Копия шагов живёт в ДВУХ шаблонах — правим синхронно. Пятый шаг `05 / заселение` в how.html остаётся как есть.

**Files:**
- Modify: `src/site/templates/index.html:124` (интро), `:127-130` (шаги)
- Modify: `src/site/templates/how.html:14` (интро), `:21-24` (шаги)

- [x] **Step 1: Обновить тестовую привязку к «боравиште»**

`tests/site/test_render.py:144` содержит `assert "боравиште" in html or "регистрац" in html` — после правки останется истинным по «регистрац», менять не нужно. Проверить прочие:

Run: `grep -rn -e "Готовим 3–5" -e "Оформляем ugovor" -e "не проходит на боравиште" -e "счётчики" tests/`
Expected: совпадений нет; если есть — заменить в тестах на новые тексты (П9/П10/П11) до правки шаблонов.

- [x] **Step 2: index.html — интро секции**

Строка 124, заменить:

```html
    <p style="max-width:30ch;color:var(--ink-2);font-size:var(--step--1)">Не маркетплейс. Живой агент ведёт за руку от первого сообщения до регистрации адреса.</p>
```

на:

```html
    <p style="max-width:30ch;color:var(--ink-2);font-size:var(--step--1)">Не маркетплейс. Живой агент ведёт за руку от первого сообщения до получения ключей.</p>
```

- [x] **Step 3: index.html — четыре шага**

Строки 127–130, заменить:

```html
    <div class="step"><div class="no">01 / заявка</div><h3>Расскажи, что нужно</h3><p>Район, бюджет, сроки, нужен ли договор под ВНЖ. Пара минут в Telegram.</p></div>
    <div class="step"><div class="no">02 / подборка</div><h3>Готовим 3–5 вариантов</h3><p>Отбираем вручную, отсекаем то, что не проходит на боравиште.</p></div>
    <div class="step"><div class="no">03 / просмотр</div><h3>Ходим на показ вместе</h3><p>Переводим, торгуемся, проверяем счётчики и состояние.</p></div>
    <div class="step"><div class="no">04 / договор</div><h3>Оформляем ugovor</h3><p>Подписание, регистрация адреса, справки для ВНЖ. Всё под ключ.</p></div>
```

на:

```html
    <div class="step"><div class="no">01 / заявка</div><h3>Расскажи, что нужно</h3><p>Район, бюджет, сроки, предпочтения, планируется ли оформление ВНЖ. Пара минут в Telegram.</p></div>
    <div class="step"><div class="no">02 / подборка</div><h3>Готовим подборку с вариантами</h3><p>Отбираем вручную, предлагаем только то, что подходит именно вам, отсекая все лишнее.</p></div>
    <div class="step"><div class="no">03 / просмотр</div><h3>Ходим на показ вместе</h3><p>Переводим, торгуемся, проверяем документы и состояние квартиры.</p></div>
    <div class="step"><div class="no">04 / договор</div><h3>Подготавливаем договор</h3><p>Подписание, регистрация на адресе, документы для ВНЖ. Всё под ключ.</p></div>
```

- [x] **Step 4: how.html — интро**

Строка 14, заменить:

```html
    <p style="max-width:38ch;color:var(--ink-2);font-size:var(--step--1)">Не маркетплейс. Живой агент ведёт за руку от первого сообщения до заселения и регистрации адреса.</p>
```

на:

```html
    <p style="max-width:38ch;color:var(--ink-2);font-size:var(--step--1)">Не маркетплейс. Живой агент ведёт за руку от первого сообщения до получения ключей.</p>
```

- [x] **Step 5: how.html — те же четыре шага**

Строки 21–24: применить ровно ту же замену, что в Step 3 (блоки идентичны). Шаг `05 / заселение` (строка 25) не трогать.

- [x] **Step 6: Проверить синхронность и тесты**

Run: `grep -c "Готовим подборку с вариантами" src/site/templates/index.html src/site/templates/how.html`
Expected: по `1` в каждом файле.
Run: `uv run pytest tests/site -q`
Expected: PASS

- [x] **Step 7: Commit**

```bash
git add src/site/templates/index.html src/site/templates/how.html
git commit -m "dasha-edits: интро и шаги «Как мы работаем» на главной и /how (правки 7–11)"
```

---

### Task 4: Секция «Почему мы» — заголовок, абзац, чек-лист, убрать пустой квадрат

**Files:**
- Modify: `src/site/templates/index.html:135-150` (секция `#vnj`)
- Modify: `src/site/static/css/site.css` (добавить модификатор `.split.solo`)

- [x] **Step 1: Проверить тестовые привязки**

Run: `grep -rn -e "Договор, который реально" -e "Проверяем хозяина" -e "боравиштем" -e "ph-note" tests/site/`
Expected: если что-то совпало — обновить в тестах на новые тексты до правки шаблона.

- [x] **Step 2: Заменить заголовок и абзац**

Строки 139–140, заменить:

```html
      <h2>Договор, который реально проходит на ВНЖ</h2>
      <p>Большая часть объявлений в Белграде — «на честном слове». Мы работаем только с хозяевами, готовыми оформить официальный ugovor с регистрацией в МУПе.</p>
```

на:

```html
      <h2>Переезд, который реально не доставит хлопот и волнений</h2>
      <p>Поиск недвижимости в новой стране всегда достаточно стрессовая задача — разобраться в местных юридических тонкостях, особенностях районов, задать правильные вопросы владельцу. Мы берем все главные заботы на себя, вам остается только приезжать на просмотры в назначенное время и сделать свой выбор при встрече с идеальной квартирой.</p>
```

- [x] **Step 3: Заменить чек-лист**

Строки 142–145, заменить:

```html
        <div class="c"><span class="num">01</span><span><b>Проверяем хозяина</b> — есть ли право сдавать, готов ли к официальному договору.</span></div>
        <div class="c"><span class="num">02</span><span><b>Сопровождаем сделку</b> — на подписании рядом человек, говорящий по-русски и по-сербски.</span></div>
        <div class="c"><span class="num">03</span><span><b>Помогаем с боравиштем</b> — регистрация адреса и пакет справок для ВНЖ.</span></div>
        <div class="c"><span class="num">04</span><span><b>Прозрачные условия</b> — стоимость услуги называем сразу, без скрытых платежей.</span></div>
```

на:

```html
        <div class="c"><span class="num">01</span><span><b>Проверяем хозяина</b> — есть ли право сдавать квартиру в аренду, наличие квартиры в кадастре, наличие необходимых документов, нет ли нежелательных пунктов в договоре.</span></div>
        <div class="c"><span class="num">02</span><span><b>Сопровождаем сделку</b> — на подписании и всех этапах человек, говорящий по-русски и по-сербски.</span></div>
        <div class="c"><span class="num">03</span><span><b>Помогаем с ВНЖ</b> — регистрация адреса и пакет необходимых документов.</span></div>
        <div class="c"><span class="num">04</span><span><b>Прозрачные условия</b> — стоимость услуги называем сразу, без скрытых платежей.</span></div>
```

- [x] **Step 4: Убрать пустой квадрат и перевести секцию в одну колонку**

Строка 136: `<div class="split">` → `<div class="split solo">`.
Строка 148, удалить целиком:

```html
    <div class="photo" role="img" aria-label="Сопровождение сделки в Белграде"><span class="ph-note">сопровождаем сделку</span></div>
```

В `src/site/static/css/site.css` после строки 212 (правило `.split{...}`) добавить строку:

```css
.split.solo{grid-template-columns:1fr}
.split.solo h2{max-width:24ch}
.split.solo p{max-width:60ch}
```

(`.split .photo` правила не удалять — их использует та же секция на странице `/vnj`, которую не трогаем.)

- [x] **Step 5: Прогнать тесты**

Run: `uv run pytest tests/site -q`
Expected: PASS (в т.ч. `test_render.py:144` — «регистрац» встречается в чек-листе `регистрация адреса`).

- [x] **Step 6: Commit**

```bash
git add src/site/templates/index.html src/site/static/css/site.css
git commit -m "dasha-edits: секция «Почему мы» — новый текст, чек-лист, убран пустой квадрат (правки 12–14)"
```

---

### Task 5: Лид-форма — подзаголовок и плейсхолдер

**Files:**
- Modify: `src/site/templates/partials/_lead_form.html:9` (lede), `:63` (плейсхолдер)

- [x] **Step 1: Проверить тестовые привязки**

Run: `grep -rn -e "Дарья соберёт" -e "нужен договор под боравиште" tests/`
Expected: совпадений нет; если есть — обновить на новые тексты до правки.

- [x] **Step 2: Заменить подзаголовок**

Строка 9, заменить:

```html
  <p class="lede">Район, бюджет, нужен ли договор под ВНЖ — пара полей, и Дарья соберёт подборку под тебя.</p>
```

на:

```html
  <p class="lede">Локация, бюджет, вид отопления — пара полей, и наш специалист соберёт подборку под тебя.</p>
```

- [x] **Step 3: Почистить плейсхолдер**

Строка 63, заменить:

```html
      <textarea id="lead-message" name="message" maxlength="2000" placeholder="Сроки, лифт, питомцы, нужен договор под боравиште…"></textarea>
```

на:

```html
      <textarea id="lead-message" name="message" maxlength="2000" placeholder="Сроки, лифт, питомцы…"></textarea>
```

- [x] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/site -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/site/templates/partials/_lead_form.html
git commit -m "dasha-edits: лид-форма — «наш специалист», плейсхолдер без боравиште (правки 15, 16)"
```

---

### Task 6: Районы — backend: 10 районов, схлопывание микрорайонов, статистика

`data/districts.yaml` НЕ трогаем (общий с хэштегами канала). Схлопывание — только в отображении сайта.

**Files:**
- Modify: `src/site/format.py` (константы + схлопывание в `human_district`)
- Modify: `src/site/app.py:62-77` (`district_options`)
- Modify: `src/site/routes.py:430-436` (stats на главной)
- Test: `tests/site/test_format.py`

**Interfaces:**
- Produces: `format.CORE_DISTRICTS: tuple[str, ...]` (10 кириллических имён, по алфавиту) — используют `app.district_options()` и `routes.index`.
- Produces: `format._PARENT_VALUE: dict[str, str]` (латинское значение микрорайона → латинское значение района-родителя) — приватный для `human_district`.
- Consumes: существующие `district_tag`, `_value_to_cyrillic` — сигнатуры не меняются.

- [x] **Step 1: Написать падающие тесты**

В `tests/site/test_format.py` добавить (рядом с существующим параметризованным тестом `human_district`, строки ~60-75):

```python
def test_microdistricts_collapse_to_parent_district():
    # Правка 4: на витрине только 10 районов — микрорайоны схлопываются в родителя.
    from src.site.format import human_district
    assert human_district("Dorcol") == "Стари град"
    assert human_district("Dorćol") == "Стари град"
    assert human_district("Bežanijska kosa") == "Нови Београд"
    assert human_district("Belgrade Waterfront") == "Савски венац"
    assert human_district("Banovo brdo") == "Чукарица"
    assert human_district("Karaburma") == "Палилула"
    assert human_district("Slavija") == "Врачар"
    assert human_district("Gardoš") == "Земун"
    assert human_district("Petlovo brdo") == "Раковица"
    assert human_district("Profesorska kolonija") == "Звездара"


def test_core_districts_is_the_ten_approved():
    from src.site.format import CORE_DISTRICTS
    assert CORE_DISTRICTS == (
        "Вождовац", "Врачар", "Звездара", "Земун", "Нови Београд",
        "Палилула", "Раковица", "Савски венац", "Стари град", "Чукарица",
    )
```

ВАЖНО: существующие параметры `("Dorcol", "Дорћол")` и `("Dorćol", "Дорћол")` в старом тесте (строки 69–70) заменить на `("Dorcol", "Стари град")` и `("Dorćol", "Стари град")`.

- [x] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/site/test_format.py -q`
Expected: FAIL (`ImportError: CORE_DISTRICTS` / `"Дорћол" != "Стари град"`)

- [x] **Step 3: Реализовать в format.py**

После блока `_CYRILLIC_RE` (строка ~22) добавить:

```python
# Правка Даши №4 (2026-07-18): на витрине только 10 городских районов.
# Порядок — алфавитный, как выдаёт district_options().
CORE_DISTRICTS: tuple[str, ...] = (
    "Вождовац", "Врачар", "Звездара", "Земун", "Нови Београд",
    "Палилула", "Раковица", "Савски венац", "Стари град", "Чукарица",
)

# Латинское значение микрорайона (из districts.yaml) → значение района-родителя.
# Схлопывание ТОЛЬКО для витрины сайта; districts.yaml и хэштеги канала не меняются.
_PARENT_VALUE: dict[str, str] = {
    "Dorcol": "Starigrad",
    "Centar": "Starigrad",
    "BezanijskaKosa": "NoviBeograd",
    "BeogradNaVodi": "Savskivenac",
    "Dedinje": "Savskivenac",
    "Senjak": "Savskivenac",
    "TopciderskoBrdo": "Savskivenac",
    "Slavija": "Vracar",
    "Karaburma": "Palilula",
    "BanovoBrdo": "Cukarica",
    "Cerak": "Cukarica",
    "FilmskiGrad": "Cukarica",
    "PetlovoBrdo": "Rakovica",
    "Gardos": "Zemun",
    "ProfesorskaKolonija": "Zvezdara",
    "Mirijevo": "Zvezdara",
    "Konjarnik": "Zvezdara",
}
```

В `human_district` (строка ~72) после `value = tag[1:]` добавить одну строку:

```python
    value = _PARENT_VALUE.get(value, value)
```

(Внешние общины — Барајево, Гроцка, Лазаревац, Младеновац, Обреновац, Сопот, Сурчин — редки в каталоге; отображаются как есть, в выпадающий список не попадают. Осознанно, YAGNI.)

- [x] **Step 4: district_options() → CORE_DISTRICTS**

В `src/site/app.py` заменить тело функции (строки 62–77) на:

```python
@lru_cache(maxsize=1)
def district_options() -> list[str]:
    """Правка Даши №4: в район-дропдаунах только 10 городских районов.

    Раньше — все кириллические ключи districts.yaml (включая микрорайоны).
    Теперь — курируемый список format.CORE_DISTRICTS; микрорайоны схлопываются
    в родителя на отображении (format._PARENT_VALUE), districts.yaml не трогаем.
    """
    return list(CORE_DISTRICTS)
```

Добавить импорт: `from src.site.format import CORE_DISTRICTS` (рядом с существующими импортами). Убрать ставшие ненужными `DISTRICTS_PATH`/`yaml`/`_CYRILLIC_RE` из app.py ТОЛЬКО если их больше никто в этом модуле не использует (проверить `grep -n "DISTRICTS_PATH\|yaml\.\|_CYRILLIC_RE" src/site/app.py`).

- [x] **Step 5: Статистика на главной — фиксированные 10**

В `src/site/routes.py` (строки 430–436) заменить:

```python
    stats = {
        "active": facets.counts.get("total", 0),
        "districts": len(facets.districts),
```

на:

```python
    stats = {
        "active": facets.counts.get("total", 0),
        # Правка Даши №5: показываем количество покрываемых районов (10), а не фасеты.
        "districts": len(CORE_DISTRICTS),
```

Добавить импорт `CORE_DISTRICTS` в routes.py: `from src.site.format import CORE_DISTRICTS` (у format.py нет зависимостей от app/routes — циклического импорта нет).

- [x] **Step 6: Тесты и линт**

Run: `uv run pytest tests/site -q && uv run ruff check src tests`
Expected: PASS; если упали фикстуры с `("Дорћол", 3)` в `test_render.py:57` / `test_a11y.py:74` / `test_islands_catalog.py:178` — эти фикстуры задают фасеты напрямую (не через human_district), они валидны как данные; чинить только упавшие ассерты, заменяя «Дорћол» на «Стари град» по смыслу теста.

- [x] **Step 7: Commit**

```bash
git add src/site/format.py src/site/app.py src/site/routes.py tests/site/test_format.py
git commit -m "dasha-edits: 10 районов — схлопывание микрорайонов на витрине, статистика (правки 4, 5)"
```

---

### Task 7: Районы — UI: плейсхолдеры и бегущая строка

**Files:**
- Modify: `src/site/templates/index.html:16` (комбобокс), `:92-93` (marquee)
- Modify: `src/site/templates/partials/_lead_form.html:55` (поле «Районы»)

- [x] **Step 1: Плейсхолдер комбобокса**

Строка 16 index.html: `data-combobox-placeholder="Врачар, Дорћол…"` → `data-combobox-placeholder="Врачар, Земун…"`.

- [x] **Step 2: Плейсхолдер поля «Районы» в лид-форме**

Строка 55 _lead_form.html: `placeholder="Врачар, Дорћол…"` → `placeholder="Врачар, Земун…"`.

- [x] **Step 3: Бегущая строка — 10 районов**

Строки 92–93 index.html, заменить ОБЕ одинаковые строки (дубль нужен для бесшовной анимации) на:

```html
    <span>Врачар</span><i>/</i><span>Стари град</span><i>/</i><span>Нови Београд</span><i>/</i><span>Земун</span><i>/</i><span>Савски венац</span><i>/</i><span>Звездара</span><i>/</i><span>Палилула</span><i>/</i><span>Вождовац</span><i>/</i><span>Чукарица</span><i>/</i><span>Раковица</span><i>/</i>
```

- [x] **Step 4: Проверить, что Дорћол на витрине больше нет**

Run: `grep -rn "Дорћол" src/site/templates/`
Expected: пусто.
Run: `uv run pytest tests/site -q`
Expected: PASS (упавшие ассерты с «Дорћол» в плейсхолдерах — заменить на «Земун» по новому тексту).

- [x] **Step 5: Commit**

```bash
git add src/site/templates/index.html src/site/templates/partials/_lead_form.html
git commit -m "dasha-edits: районы в UI — плейсхолдеры и marquee без микрорайонов (правка 4)"
```

---

### Task 8: Калькулятор → кнопка «Удаленный поиск квартиры»

**Files:**
- Modify: `src/site/templates/base.html:86` (меню), `:120` (футер), `:146-153` (bottom-nav)
- Modify: `src/site/routes.py:556-570` (роут), `:573` (sitemap)
- Delete: `src/site/templates/calculator.html`, `src/site/static/js/calculator.js`
- Modify: `src/site/static/manifest.webmanifest` (shortcut `/calculator`)
- Test: `tests/site/test_integration.py`, `tests/site/test_analytics.py`, `tests/site/test_caching.py`, `tests/site/test_islands_extras.py`, `tests/site/test_pwa.py`, `tests/site/test_a11y.py`

**Interfaces:**
- Produces: `GET /calculator` → `301 Location: /#lead` (постоянный редирект вместо страницы).
- Кнопка `Удаленный поиск квартиры` → `href="/#lead"` (решение Дмитрия №1).

- [x] **Step 1: Обновить тесты под новое поведение (TDD)**

`tests/site/test_integration.py:290-291` — заменить тест:

```python
def test_calculator_redirects_to_lead(client):
    r = client.get("/calculator", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/#lead"
```

`tests/site/test_analytics.py` — из перечней путей на строках 177 и 267 убрать `"/calculator"`; на строке ~185 убрать кортеж `("pageview", "/calculator", None)`; из словаря шаблонов (строка 36) убрать `"calculator.html"`.
`tests/site/test_caching.py` — строка 39: `STATIC_PAGES = ("/vnj", "/how")`; из словаря на строке 31 убрать `"calculator.html"`; поправить docstring строки 3.
`tests/site/test_islands_extras.py` — удалить тесты `test_calculator_has_fields_and_script` и `test_calculator_script_is_pure_client_no_network` (строки ~66-90); из параметризации на строке 184 убрать `("calculator.html", {})`; поправить docstring строки 4.
`tests/site/test_pwa.py:124` — заменить `client.get("/calculator")` на `client.get("/how")`; строка 135 `assert 'href="/calculator"'` — удалить или заменить на `assert 'href="/#lead"' in body`.
`tests/site/test_a11y.py:101` — убрать `("calculator.html", {"csrf_token": _CSRF})` из параметризации; поправить комментарий строки 131.

- [x] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/site/test_integration.py tests/site/test_pwa.py -q`
Expected: FAIL (роут ещё отдаёт 200 со страницей).

- [x] **Step 3: routes.py — редирект вместо страницы**

Удалить хэндлер `/calculator` (строки 556–570) и вставить на его место:

```python
@router.get("/calculator")
async def calculator(request: Request) -> RedirectResponse:
    """Правка Даши №2: страницы калькулятора больше нет — постоянный редирект на лид-форму."""
    return RedirectResponse("/#lead", status_code=301)
```

Импорт: `from fastapi.responses import RedirectResponse` (проверить, нет ли уже). Строка 573: `_SITEMAP_STATIC = ("/", "/catalog", "/map", "/vnj", "/how")`. Поправить упоминания `/calculator` в комментариях (строки 193, 616, 631) — заменить на `/how`.

- [x] **Step 4: base.html — кнопка вместо пункта меню**

Строка 86, заменить:

```html
      <a href="/calculator"{% if cur_path == '/calculator' %} aria-current="page"{% endif %}>Калькулятор</a>
```

на:

```html
      <a href="/#lead">Удаленный поиск квартиры</a>
```

Строка 120 (футер «Помощь»): `<a href="/calculator">Калькулятор аренды</a>` → `<a href="/#lead">Удаленный поиск квартиры</a>`.
Bottom-nav (строки ~146-153): пункт «Калькулятор» заменить на:

```html
  <a href="/#lead" aria-label="Удаленный поиск квартиры">
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="11" cy="11" r="6.5"/><path d="m20.5 20.5-4.8-4.8"/></svg>
    <span>Поиск</span>
  </a>
```

Поправить комментарий над bottom-nav (`Каталог / Карта / Калькулятор / Telegram` → `Каталог / Карта / Поиск / Telegram`).

- [x] **Step 5: Удалить файлы и почистить PWA-манифест**

```bash
git rm src/site/templates/calculator.html src/site/static/js/calculator.js
```

В `src/site/static/manifest.webmanifest` удалить объект shortcut с `"url": "/calculator"` (около строки 48) — следить за запятыми в JSON. Проверить остальную статику:

Run: `grep -rn "calculator" src/site/static/ src/site/templates/`
Expected: пусто (если найдётся упоминание в `sw.js`/офлайн-прекэше — удалить путь из списка).

Опционально: удалить мёртвый CSS-блок `/* ───────── calculator ───────── */` в `src/site/static/css/site.css` (~строка 401) вместе с его правилами.

- [x] **Step 6: Тесты и линт**

Run: `uv run pytest tests/site -q && uv run ruff check src tests`
Expected: PASS

- [x] **Step 7: Commit**

```bash
git add -A
git commit -m "dasha-edits: калькулятор удалён, кнопка «Удаленный поиск квартиры» → /#lead, 301 (правка 2)"
```

---

### Task 9: Финальная верификация

- [x] **Step 1: Полный прогон**

Run: `uv run pytest -q`
Expected: PASS (весь набор, не только tests/site).

- [x] **Step 2: Линт**

Run: `uv run ruff check src tests`
Expected: чисто.

- [x] **Step 3: Grep-чистка витрины**

Run: `grep -rn -e "боравиште" -e "ugovor" -e "Калькулятор" -e "калькулятор" -e "Дорћол" src/site/templates/ src/site/static/js/`
Expected: совпадения только в `vnj.html` (страница «ВНЖ и договор» — вне объёма правок Даши, там термины уместны). Любое другое совпадение — недоделка, исправить.

- [x] **Step 4: Смоук рендера главной**

Run: `uv run pytest tests/site/test_render.py tests/site/test_a11y.py -q`
Expected: PASS.

- [ ] **Step 5: Ручная проверка (Дмитрий)**

Поднять сайт локально, посмотреть глазами: `/` (hero, статистика «10 районов», шаги, «Почему мы» без квадрата, лид-форма), `/how`, меню и футер, `/calculator` → редирект на форму. После одобрения — push руками.

---

## Вне объёма (зафиксировано)

- `vnj.html` — Даша не размечала; «ugovor»/«боравиште» там остаются до её решения.
- Контент страницы «Удаленный поиск квартиры» — Даша готовит отдельно; появится новая задача.
- Фото в секцию «Почему мы» — добавим отдельной правкой, когда будет файл.
- Хэштеги телеграм-канала (`data/districts.yaml`) — не меняются намеренно.
