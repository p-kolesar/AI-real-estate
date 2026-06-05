# Containerized scraper Function App (APP_ROLE=scraper) for Azure Container Apps.
# Flex Consumption can't run a headless browser (no custom image, no system libs),
# so the 20-min ingestion timer runs here with Chromium baked in. The read API and
# daily brief stay on the Flex app (zip-deployed from this same backend/ tree).
FROM mcr.microsoft.com/azure-functions/python:4-python3.13

# Functions runtime expects the app at /home/site/wwwroot and benefits from
# console logging in a container. Browsers go to a fixed, root-owned path that the
# worker can read at runtime (PLAYWRIGHT_BROWSERS_PATH must match build + run).
ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Python deps first for layer caching.
COPY requirements-container.txt /
RUN pip install --no-cache-dir -r /requirements-container.txt

# Chromium + the ~30 OS libraries it needs (libnss3, libatk, libgbm, …).
# --with-deps runs apt-get for the system packages; needs the root build context.
# Invoked as a module: the `playwright` console script isn't on PATH in this base
# image (only python/pip are symlinked into /usr/local/bin), so `playwright …`
# alone fails with 'not found'. `python -m playwright` uses the same interpreter
# pip installed into.
RUN python -m playwright install --with-deps chromium

# App code last (changes most often). .dockerignore keeps .venv/__pycache__ out.
COPY . /home/site/wwwroot
