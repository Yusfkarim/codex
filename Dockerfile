FROM python:3.12-slim

# Codex ships inside the ChatGPT desktop package; the CLI it installs is what
# the web front end drives. We do not need a desktop session, so no X, no VNC
# and no Electron dependencies are installed here.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git procps openssh-client \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
      https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb \
      -o /tmp/chatgpt.deb \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/chatgpt.deb \
 && rm -f /tmp/chatgpt.deb \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/lib/chatgpt/resources/codex /usr/local/bin/codex \
 && codex --version

WORKDIR /app
COPY codex_web.py codex-web.py codex-web.html codex_accounts.py \
     codex_chat.py codex_history.py codex_skills.py ./
COPY official_skills ./official_skills

ENV CODEX_WEB_HOME=/data/codex-web \
    CODEX_WEB_PORT=8098 \
    CODEX_WEB_BIND=0.0.0.0 \
    PYTHONUNBUFFERED=1

VOLUME ["/data/codex-web"]
EXPOSE 8098

CMD ["python3", "codex_web.py"]
