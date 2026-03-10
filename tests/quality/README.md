# Quality Regression

This folder contains artifacts for production-facing quality checks.

## Goals

- verify that a completed job really produced output media
- measure rough sync quality using duration deltas
- flag segment-level TTS drift
- capture manual review notes for translation, voice, and mixing quality

## Suggested workflow

1. Run a real job to `completed`.
2. Call the API to get the final `job_id`.
3. Run the regression script:

```powershell
python .\tests\quality\run_regression.py --api-base-url http://127.0.0.1:8080 --data-root .\data --job-id 34
```

4. Compare the report against the thresholds in `manifest.example.json`.
5. Fill out `scorecard.template.md` for manual listening checks.

## Minimum acceptance

- output file exists
- output duration delta within threshold
- every segment has a TTS artifact
- every segment duration delta within threshold
- translation length ratio remains inside the configured bound
