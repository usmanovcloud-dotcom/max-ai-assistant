# Persistent storage migration on Synology

The NAS compose file stores runtime state outside the Container Manager project:

```text
/volume1/docker/max-ai-assistant/data
/volume1/docker/max-ai-assistant/secrets
```

This prevents a project recreated from the repository YAML from receiving a new,
empty Docker volume. The `data` directory contains `assistant.sqlite3`, the MAX
session, settings, conversations, and audit data. The `secrets` directory contains
provider keys, MAX 2FA data, and n8n credentials.

## One-time migration from the previous named volumes

Do not delete either old volume. First confirm that both expected volumes already
exist and that the data volume contains the saved database:

```sh
sudo docker volume inspect max-ai-assistant-data max-ai-assistant-secrets
sudo docker run --rm --user 0:0 --entrypoint sh \
  -v max-ai-assistant-data:/source:ro \
  ghcr.io/usmanovcloud-dotcom/max-ai-assistant:latest \
  -c 'test -s /source/assistant.sqlite3 && echo settings-db-found'
```

Do not continue unless the second command prints `settings-db-found`. If either
volume is missing, find the actual old name without starting the new project:

```sh
sudo docker volume ls --format '{{.Name}}' | grep -E 'max-ai|assistant'
```

Stop the `max-ai-assistant` project, then run the following commands from an
administrator shell on the NAS:

```sh
sudo mkdir -p /volume1/docker/max-ai-assistant/data
sudo mkdir -p /volume1/docker/max-ai-assistant/secrets

sudo docker run --rm --user 0:0 --entrypoint sh \
  -v max-ai-assistant-data:/source:ro \
  -v /volume1/docker/max-ai-assistant/data:/target \
  ghcr.io/usmanovcloud-dotcom/max-ai-assistant:latest \
  -c 'cp -a /source/. /target/ && chown -R 1032:100 /target'

sudo docker run --rm --user 0:0 --entrypoint sh \
  -v max-ai-assistant-secrets:/source:ro \
  -v /volume1/docker/max-ai-assistant/secrets:/target \
  ghcr.io/usmanovcloud-dotcom/max-ai-assistant:latest \
  -c 'cp -a /source/. /target/ && chown -R 1032:100 /target'
```

After replacing the project YAML, start the project and verify:

```sh
sudo docker inspect max-ai-assistant \
  --format '{{range .Mounts}}{{println .Destination "<-" .Source}}{{end}}'
sudo docker exec max-ai-assistant sh -c \
  'test -s /data/assistant.sqlite3 && echo settings-db-ok'
```

The expected mounts point to the two `/volume1/docker/max-ai-assistant/...`
directories. Confirm the saved model, instruction, API key status, MAX connection,
and n8n status in the dashboard before removing any old volume. Keeping the old
volumes for several days provides a simple rollback.

## Subsequent updates

Update/rebuild the Container Manager project using `docker-compose.nas.yml`. Only
the container and image are replaced; the two host directories remain untouched.
Back up the entire `/volume1/docker/max-ai-assistant` directory before major
updates.
