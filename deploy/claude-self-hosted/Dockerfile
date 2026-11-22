# A runner image for a self-hosted Claude environment, with what Abstract Search & Export needs.
#
# This is the recipe from Anthropic's deployment guide
# (https://code.claude.com/docs/en/self-hosted-environments-deploy, read 2026-11-21). The tool adds nothing to it:
# it needs python3, which the recipe already installs, and it arrives with the repository each session clones.
#
#   docker build --build-arg CLAUDE_CODE_VERSION="$(curl -fsSL https://downloads.claude.ai/claude-code-releases/stable)" \
#     -t claude-runner-abstract .
FROM debian:bookworm-slim
ARG CLAUDE_CODE_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client jq python3 \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION:?set with --build-arg CLAUDE_CODE_VERSION}/linux-x64/claude" \
      -o /usr/local/bin/claude && chmod +x /usr/local/bin/claude
RUN git config --system user.name "Claude" \
 && git config --system user.email "noreply@anthropic.com" \
 && git config --system --add safe.directory '*'
ENTRYPOINT ["claude"]
