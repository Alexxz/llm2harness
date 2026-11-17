# Подготовка образа выполняется человеком до практики. Агент использует --pull never.
# После сборки запишите фактический image ID в свидетельства приёмки.
FROM node:22.14.0-bookworm-slim

ARG OPENSPEC_VERSION=1.16.0
RUN npm install --global "@fission-ai/openspec@${OPENSPEC_VERSION}" \
    && openspec --version \
    && bash --version

WORKDIR /work
CMD ["bash", "--noprofile", "--norc"]
