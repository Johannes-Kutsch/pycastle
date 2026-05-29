ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

# Install baseline tools, gh CLI, Node.js, and npm for agent CLIs.
RUN apt-get update && apt-get install -y \
  curl \
  git \
  jq \
  ripgrep \
  && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
  && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
  && apt-get update && apt-get install -y gh \
  && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
  && apt-get install -y nodejs \
  && rm -rf /var/lib/apt/lists/*

# Install supported agent CLIs at pinned versions.
RUN npm install -g @openai/codex@0.134.0 @anthropic-ai/claude-code@2.1.152 opencode-ai@1.15.12

# Create agent user with UID 1000 for compatibility with --userns=keep-id and --user 1000:1000
RUN useradd -m -u 1000 -s /bin/bash agent
USER agent

# Make console scripts installed by runtime `pip install --user` visible.
ENV PATH="/home/agent/.local/bin:$PATH"

# Trust any bind-mounted directory regardless of host ownership.
RUN git config --global --add safe.directory '*'

WORKDIR /home/agent

ENTRYPOINT ["sleep", "infinity"]
