# Scale Roadmap

> The "Multi-tenant isolation" section below is the source of `OPT-303`
> in [../roadmap/optimization-roadmap.md](../roadmap/optimization-roadmap.md).
> Other sections are scale-out concerns tracked separately.

## P2 roadmap

### Multi-node execution

- move from a single Redis queue consumer to multiple workers
- keep stage leases to avoid duplicate stage execution
- isolate GPU-heavy workloads into dedicated worker pools

### Multi-tenant isolation

- assign every job and voice profile a `tenant_key`
- isolate storage prefixes by tenant
- apply tenant-scoped quotas and usage reporting

### Autoscaling

- scale worker replicas from queue depth and GPU saturation
- separate API autoscaling from ML autoscaling
- keep delayed / dead-letter queues observable

### Quality platform

- keep a stable sample registry
- store regression reports by model version
- compare releases against the last approved baseline
