# Container watchdog

Posts to a Teams chat when a container in one of these stacks crashes, stays
down, or Docker itself stops responding:

docker-agile-classifier, docker-agile-classifier-2, docker-agile-registration,
docker-agile-registration-2, docker-ap-invoice, docker-agile-ap,
docker-agile-docalloc

The list is `WATCHED_PROJECTS` at the top of the script; `--projects a,b`
overrides it for one run. Containers from any other stack are ignored. The names
are compose project names, which are the stack's directory name unless the
compose file sets `name:` or the stack is started with `-p`.

Because every service has `restart: always`, Docker restarts a crashed container
within seconds and `docker ps` shows it as Up. So the watchdog alerts on the
container's **RestartCount going up**, not on its state.

| Alert | When |
|---|---|
| 🔁 crashed and was restarted | RestartCount went up since the last run. Includes the last 25 log lines. At most one per container every 15 min; restarts in between are counted on the next alert. |
| 🔴 is down | Not running for 2 runs in a row (so a deploy's recreate doesn't trigger it). Sent once. |
| 🟢 is running again | It came back after a down alert. |
| 🔴 is down (container removed) | A container that was there is gone for 2 runs while the rest of its stack is still up. |
| 🔴 / 🟢 has no containers | A whole stack has no containers for 2 runs (`docker compose down`, never started, or a wrong project name) / it's back. |
| 🔴 / 🟢 Docker is not responding | The daemon can't be reached / is reachable again. |

Containers recreated by a deploy (`docker compose up -d`) don't alert.

## Install on the VM

```bash
mkdir -p ~/.container-watchdog
cp container_watchdog.py ~/.container-watchdog/
# A Teams Workflows URL: the one the stacks use (TEAMS_WEBHOOK_URL in their
# .env.dev), or a different chat's. Strip any quotes around it.
nano ~/.container-watchdog/webhook
chmod 600 ~/.container-watchdog/webhook
python3 ~/.container-watchdog/container_watchdog.py --status       # every stack should list containers
python3 ~/.container-watchdog/container_watchdog.py --test-alert   # check the card arrives
python3 ~/.container-watchdog/container_watchdog.py                # records the baseline
```

Then `crontab -e`:

```
* * * * * /usr/bin/python3 $HOME/.container-watchdog/container_watchdog.py >> $HOME/.container-watchdog/watchdog.log 2>&1
```

The user running cron must be able to run `docker` without sudo.

`--dry-run` prints the cards instead of posting them.
