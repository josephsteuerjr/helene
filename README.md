# Vera

Рантайм для создания агента на Windows. Ставит на компьютер агента с памятью в
файлах, конституцией, руками и своим расписанием, даёт на него посмотреть и
всё это настроить. Модель приходит извне: по ключу API, по подписке ChatGPT
через встроенное реле или с этого же компьютера (Ollama, LM Studio).

Поставка — один архив `Vera.zip`: установщик, окно, служба Windows, встроенный
Python со всеми пакетами, код агента. Ничего, кроме модели, не уходит с машины.

## Что внутри

| Часть | Где | На чём |
|---|---|---|
| Установщик: сцены первого запуска, тихая установка, снятие | `setup/` | Rust, Tauri 2, TypeScript |
| Окно: разговор, планы, кадр, файлы, журнал, устройство, настройки, трей, уведомления, надзор за процессами | `shell/`, `app/` | Rust, Tauri 2, TypeScript |
| Служба Windows: агент живёт до входа в систему | `svc/` | Rust |
| Труба: HTTP и WebSocket между окном, телефоном и деревом агента | `deskapp.py`, `deskd/` | Python, aiohttp |
| Локальный харнесс: рождение, ходы, доставка слова, Telegram (бот или свой аккаунт), песочница | `localharness/` | Python |
| Страница телефона (PWA, спаривание по QR) | `mobile/` | TypeScript |
| Общий стиль: токены, шрифты | `ui-kit/` | CSS |
| Конституция, карта устройства, генератор иконки | `resources/` | Markdown, Python |
| Сборка поставки, лицензии, инструкция выпуска | `installer/` | Python |

Код самого агента (`tree/` в поставке) живёт в отдельном репозитории и
попадает в архив при сборке. Что где лежит и что можно переписать, описано в
`resources/VERA-MAP.md` — этот же файл едет в поставку как
`КАК-УСТРОЕН-VERA.md`.

## Собрать

Нужны Rust (stable), Node 20+, Python 3.12+ с Pillow, 7-Zip не нужен.

```bash
npm --prefix app install && npm --prefix mobile install && npm --prefix setup/ui install
npm --prefix app run build && npm --prefix mobile run build && npm --prefix setup/ui run build
(cd shell && cargo build --release --features custom-protocol)
(cd setup && cargo build --release --features custom-protocol)
(cd svc && cargo build --release)
python installer/build_dist.py
```

Без `--features custom-protocol` Tauri соберёт dev-режим, который ждёт
dev-сервер на localhost. Итог — `installer/build/Vera.zip`. Как выпускать
версию на GitHub, записано в `installer/RELEASE.md`.

## Поставить

Распаковать архив, запустить `vera-setup.exe`, пройти сцены: имя агента и
своё, конституция, откуда приходит модель, служба, установка. Windows
SmartScreen при первом запуске просит «Подробнее → Выполнить в любом случае»:
подписи кода нет. Тихая установка для проверок:

```bash
vera-setup.exe --install решения.json --quiet
vera-setup.exe --uninstall --purge --quiet
```

## Документы

- `docs/PLAN.md` — что осталось до продового состояния, галочки только после
  проверки на столе.
- `docs/КАРТА-ДЕРЕВА.md` — карта системы для человеческих описаний.
- `CHANGELOG.md` — что менялось от выпуска к выпуску.

## Лицензия

Apache License 2.0, см. `installer/ЛИЦЕНЗИЯ.md`. Сторонние части — в
`installer/THIRD-PARTY.md`.
