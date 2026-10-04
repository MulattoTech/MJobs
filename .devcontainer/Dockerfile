FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

USER root

# The current Python devcontainer base includes the legacy Yarn APT source.
# Its rotated signing key can make any later Feature that runs apt-get update
# fail before the Codespace is created. MJobs uses Node via the Dev Container
# Node feature and does not need the base image's Yarn repository.
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn*.list \
    && apt-get update \
    && rm -rf /var/lib/apt/lists/*
