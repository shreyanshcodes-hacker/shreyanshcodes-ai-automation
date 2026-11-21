# n8n Automation Schedules

Import these workflow templates into your n8n instance (`Workflows -> Import from File`):

1. `01_daily_topic_research.json` — Runs daily at 08:00 (`0 8 * * *`) to draft topic ideas.
2. `02_shorts_twice_daily.json` — Runs twice daily (`0 10,18 * * *`) to queue 2 Shorts/day.
3. `03_long_videos_twice_weekly.json` — Runs Tuesday & Friday (`0 12 * * 2,5`) to queue 2 long-form videos/week.
4. `04_publishing_scheduler.json` — Polls completed jobs every 2 hours (`0 */2 * * *`) for private-first upload/scheduling.
5. `05_weekly_analytics.json` — Runs Sunday at 20:00 (`0 20 * * 0`) to inspect recent YouTube publish records.

## Environment Variable in n8n
Set `STUDIO_API_URL` in n8n (default: `http://localhost:8000`).
Keep all workflows inactive until `.env` credentials, FFmpeg, and YouTube OAuth are verified.
