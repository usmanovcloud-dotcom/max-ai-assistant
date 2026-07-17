# Установка на Synology одним YAML

Файл `docker-compose.nas.yml` скачивает готовый `linux/amd64` образ из GHCR. Исходники, Dockerfile и `.env` на NAS не нужны. Данные и секреты сохраняются в bind-mounted каталогах `/volume1/docker/max-ai-assistant/data` и `/volume1/docker/max-ai-assistant/secrets`; служебный контейнер `storage-init` создаёт их и назначает владельца `1032:100`.

## Первичная публикация образа

1. Создайте публичный GitHub-репозиторий `usmanovcloud-dotcom/max-ai-assistant`.
2. Загрузите в него содержимое проекта и ветку `main`.
3. Workflow `Test and publish container` выполнит unit-тесты и опубликует `ghcr.io/usmanovcloud-dotcom/max-ai-assistant:latest`.
4. В GitHub откройте Packages → `max-ai-assistant` → Package settings → Change visibility → Public.

## Container Manager

Скачайте единственный файл:

```text
https://raw.githubusercontent.com/usmanovcloud-dotcom/max-ai-assistant/main/docker-compose.nas.yml
```

В DSM откройте Container Manager → Проект → Создать:

- имя: `max-ai-assistant`;
- источник: создать проект из YAML;
- вставить содержимое `docker-compose.nas.yml`;
- запустить проект.

Проверка в DSM: контейнер должен перейти в состояние `healthy`.

## Доступ из локальной сети

Панель публикуется в локальную сеть на порту `8765`. Для текущего NAS откройте:

```text
http://192.168.0.10:8765
```

Встроенной авторизации самой веб-панели пока нет. Не настраивайте проброс порта
`8765` на роутере и не публикуйте его в интернет.

## Чистая настройка

1. В разделе API-ключей сохраните OpenAI project key.
2. В разделе MAX отсканируйте QR-код отдельным аккаунтом ассистента.
3. Скопируйте показанную `/claim ...` команду и отправьте её основным аккаунтом в личный чат ассистента.
4. После привязки нажмите «Запустить», если AI не стартовал автоматически.

## Обновление и откат

Container Manager → Проект → `max-ai-assistant` → Действие → Сборка/запуск повторно. `pull_policy: always` скачает свежий `latest`, а bind-mounted каталоги сохранят состояние.

Для отката используйте версионный тег образа вместо `latest`, например:

```yaml
image: ghcr.io/usmanovcloud-dotcom/max-ai-assistant:v0.4.0
```

Удаление контейнера или проекта не удаляет каталоги `/volume1/docker/max-ai-assistant/data` и `/volume1/docker/max-ai-assistant/secrets`. Удаляйте их только при намеренном полном сбросе. Если установка обновляется с версии на именованных volumes, сначала выполните [`NAS_STORAGE_MIGRATION.md`](NAS_STORAGE_MIGRATION.md).
