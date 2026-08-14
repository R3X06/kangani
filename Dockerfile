FROM python:3.12-slim

# poppler-utils: required by pdf2image for schedule PDF import.
# Playwright's own OS-level deps (fonts, libnss3, etc.) are installed
# separately below via `playwright install --with-deps`, rather than listed
# here by hand, since that command tracks whatever the pinned Playwright
# version actually needs -- less likely to silently drift out of sync.
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installs the Chromium binary AND the OS packages it needs to run headless
# (fonts, libnss3, libatk, etc.) in one step -- this is the version-matched
# alternative to hand-maintaining an apt-get list for Chromium.
RUN playwright install --with-deps chromium

COPY . .

# Not a web service -- this is PTB's long-polling loop, so no port to expose
# or bind. Railway doesn't require one for a background worker.
CMD ["python", "bot.py"]