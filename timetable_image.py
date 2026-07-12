"""Renders the timetable Jinja templates to PNG via a headless Chromium.

Playwright (real Chromium) is used rather than a PIL-based drawer so the
webfonts, CSS, SVG markers and hand-drawn touches render exactly as they did
in the approved mockups. The browser is expensive to cold-launch, so a single
instance is started once at bot startup (see bot.post_init) and passed into
every render call rather than launched per request.

Autoescape is on for the Jinja environment, so user-supplied strings (module
names, locations, task/reminder text) are HTML-escaped automatically -- the
templating equivalent of the html.escape the text views do by hand.
"""

from datetime import date

from jinja2 import Environment, FileSystemLoader, select_autoescape

import timetable_data

_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


async def _render_html_to_png(browser, html: str) -> bytes:
    """Load an HTML string into a fresh page and full-page-screenshot it.

    Width is fixed at 1080 (phone-friendly) at 2x device scale for crisp text;
    height is left to `full_page=True` so it adapts to content instead of being
    clipped or padded. `networkidle` waits for the Google Fonts to load so the
    render matches the mockups rather than falling back to a system font.
    """
    page = await browser.new_page(
        viewport={"width": 1080, "height": 800}, device_scale_factor=2
    )
    try:
        await page.set_content(html, wait_until="networkidle")
        return await page.screenshot(full_page=True)
    finally:
        await page.close()


async def render_daily_image(browser, chat_id: int, target_date: date) -> bytes:
    context = timetable_data.build_daily_context(chat_id, target_date)
    html = _env.get_template("daily.html").render(**context)
    return await _render_html_to_png(browser, html)


async def render_weekly_image(
    browser, chat_id: int, week_number: int | None = None
) -> bytes:
    context = timetable_data.build_weekly_context(chat_id, week_number)
    html = _env.get_template("weekly.html").render(**context)
    return await _render_html_to_png(browser, html)


async def render_monthly_image(browser, chat_id: int, year: int, month: int) -> bytes:
    context = timetable_data.build_monthly_context(chat_id, year, month)
    html = _env.get_template("monthly.html").render(**context)
    return await _render_html_to_png(browser, html)
