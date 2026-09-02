# Лицензии третьих сторон

Vera собрана из открытых компонентов. Ниже — прямые зависимости поставки
и их лицензии; полные тексты лежат в исходниках каждого проекта и, для
пакетов Python, в `runtime/Lib/site-packages/<пакет>.dist-info/`.

## Программа и установщик (Rust)

- Tauri 2 и его плагины (single-instance, window-state) — MIT или Apache-2.0
- tauri-winrt-notification — MIT
- serde, serde_json — MIT или Apache-2.0
- ureq — MIT или Apache-2.0
- winreg — MIT
- windows-service (служба) — MIT или Apache-2.0

## Интерфейс (TypeScript)

- motion — MIT
- qrcode — MIT
- Vite и TypeScript используются только при сборке и в поставку не входят

## Шрифты

- Source Serif 4 — SIL Open Font License 1.1
- Golos Text — SIL Open Font License 1.1
- PT Mono — SIL Open Font License 1.1

## Встроенный Python и пакеты

- CPython — Python Software Foundation License (`runtime/LICENSE.txt`)
- aiohttp и остальные пакеты — по их `dist-info/LICENSE`
- `runtime/bash.exe` — BusyBox for Windows, GPL-2.0. Это отдельная
  программа-оболочка; её исходный код доступен на frippery.org/busybox.

## Реле подписки ChatGPT

- vera-relay.exe — MIT (исходники в репозитории автора)

## Код агента

- Дерево агента (`tree/`) — Apache-2.0, см. NOTICE рядом с исходниками.
