# WB-11 (Trello YVCb5HAF) — JS test suite (`node --test tests/*.mjs`), containerised.
#
# These tests are dependency-free by design (they read static/js/*.js source
# directly and wrap it in `new Function(...)` — see tests/test_settings_schema.mjs
# for the pattern), so this image only needs Node itself plus the two source
# trees the tests actually read.
FROM node:22-alpine

WORKDIR /app

COPY static ./static
COPY tests ./tests

RUN addgroup -g 1000 tester \
    && adduser -D -u 1000 -G tester tester \
    && chown -R tester:tester /app
USER tester

# Shell form so the glob expands (node --test does not glob its own argv).
CMD ["sh", "-c", "node --test tests/*.mjs"]
