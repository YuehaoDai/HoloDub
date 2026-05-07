# HoloDub observability

This folder ships dashboards and alert rules for operating HoloDub.

## Layout

| File                          | What it is                                              |
| ----------------------------- | ------------------------------------------------------- |
| `prometheus-rules.yaml`       | Prometheus alert rules covering pipeline + dependencies |
| `grafana-dashboard.json`      | Grafana 10+ dashboard (import via UI or provisioning)   |
| `prometheus-scrape.yaml`      | Sample scrape config snippet                            |

## Metrics surface

The Go control plane exposes (`/metrics`):

- `holodub_http_requests_total{method,path,status}`
- `holodub_http_request_duration_seconds`
- `holodub_stage_runs_total{stage,status}`
- `holodub_stage_run_duration_seconds{stage}`
- `holodub_dead_letters_total`
- `holodub_external_calls_total{service,operation,result}`
- `holodub_external_call_duration_seconds{service,operation}`

The ml-service exposes (`/metrics`, port `8000`):

- `holodub_ml_http_requests_total{method,path,status}`
- `holodub_ml_http_request_duration_seconds`
- `holodub_ml_inference_duration_seconds{stage}`
- `holodub_ml_gpu_wait_seconds{stage}`
- `holodub_ml_tts_warmup_status` (gauge: 0=idle, 1=loading, 2=ready, 3=error)

## Importing the dashboard

```
# Grafana UI
Dashboards -> New -> Import -> Upload JSON file -> grafana-dashboard.json
```

Or via provisioning (`/etc/grafana/provisioning/dashboards/holodub.yaml`):

```yaml
apiVersion: 1
providers:
  - name: holodub
    type: file
    folder: HoloDub
    options:
      path: /var/lib/grafana/dashboards/holodub
```

and copy the JSON into that folder.
