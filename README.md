# Item Assist Reborn

Подсказчик предметов для магазина Deadlock (Panorama UI-мод). Продолжение заброшенного мода
**Item Assistant** (версия 1.1.1, данные под клиент 6635). Оригинальный рантайм и идея принадлежат
автору Item Assistant; здесь обновление под текущие версии игры и новая статистика.

## Что изменено относительно оригинала

- **Точка входа `hud_health` пересобирается из текущей игры** (`pipeline/make_layout.py`), а не
  хранится устаревшей копией. Старый мод подсовывал HP-бар версии 6635, без полосок брони Rat King
  и `shield_bar_2`.
- **Статистика собирается заново с [deadlock-api](https://deadlock-api.com)** (`pipeline/collect.py`
  + `pipeline/build.py`):
  - винрейт с поправкой на нетворс (`adjusted_win_rate` из `item-flow-stats`) вместо «что покупают
    победители»;
  - учимся у сильных игроков: оценки по лестнице рангов «все → Phantom+ → Ascendant+», где у
    про-игроков мало матчей, оценка подтягивается к более широкой выборке;
  - отдельные оценки по фазам матча (0–9, 9–20, 20–30, 30+ мин);
  - «следующий предмет» по тому, что уже куплено (переходы из `item-flow-stats`);
  - тайминги: до какой минуты предмет окупается (сравнение с предметами того же тира, купленными
    в ту же минуту);
  - матчапы против каждого героя врага с поправкой на сам матчап.

## Структура

```
pipeline/   сбор и обработка данных, генерация layout, упаковка VPK
mod_src/    исходники мода (JS, CSS, layout) + prebuilt/ уже скомпилированные ассеты
data/       сгенерированные данные (recommender_data.js), отчёты; cache/ не в git
```

## Сборка

```bash
cd pipeline
python collect.py          # ~45 мин, кэшируется в data/cache, можно прерывать
python build.py            # -> data/recommender_data.js + data/build_report.txt
python make_layout.py      # -> mod_src/panorama/layout/hud_health.xml из текущей игры
python pack.py --pak 30    # компиляция + dist/pak30_dir.vpk (--install кладёт в addons)
```

Нужны: Python 3.11+, установленная Deadlock,
[Source2Viewer CLI](https://github.com/ValveResourceFormat/ValveResourceFormat/releases) для
декомпиляции layout, CS2 с бесплатным DLC **Counter-Strike 2 Workshop Tools** (его
`resourcecompiler.exe` компилирует Panorama-ресурсы, которые понимает Deadlock).

## Установка

Готовый `pakNN_dir.vpk` кладётся в `Deadlock/game/citadel/addons/`. Номер выбирайте свободный, чтобы
не перезаписать другой мод.
