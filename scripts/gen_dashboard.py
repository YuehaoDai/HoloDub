import json, os, datetime

with open('.taskmaster/tasks/tasks.json','r') as f:
    data = json.load(f)
tag = list(data.keys())[0]
tasks = data[tag]['tasks']

specs_dir = '.spec-workflow/specs'
specs = [d for d in os.listdir(specs_dir) if os.path.isdir(os.path.join(specs_dir, d))] if os.path.isdir(specs_dir) else []

pc = {'high':'#ef4444','medium':'#f59e0b','low':'#6b7280'}
sc = {'pending':'#9ca3af','in-progress':'#3b82f6','done':'#10b981'}
ci = {'observability':'📊','ux':'🎨','integration':'🔌','validation':'✅','llm':'🤖','feature':'⭐','infrastructure':'🏗️','ml':'🧠','research':'🔬','tooling':'🔧','cost':'💰'}
tasks.sort(key=lambda t:{'high':0,'medium':1,'low':2}.get(t['priority'],3))
rows=''
for t in tasks:
    c=t['metadata'].get('category',''); icon=ci.get(c,'📋')
    deps_raw=t.get('dependencies',[])
    deps=','.join(str(d) for d in deps_raw) if deps_raw else '—'
    est=t['metadata'].get('estimate','—')
    opt=t['metadata'].get('opt_id','')
    p=t['priority']; s=t['status']
    pcc=pc.get(p,'#999'); scc=sc.get(s,'#999')
    desc=t['description'][:120]+'…' if len(t['description'])>120 else t['description']
    title=t['title'].replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    rows+=f'<tr><td style="text-align:center;color:{pcc};font-weight:bold">#{t["id"]}</td><td style="min-width:280px"><div style="font-weight:600;margin-bottom:3px">{icon} {title}</div><div style="font-size:12px;color:#9ca3af">{desc}</div></td><td style="text-align:center"><code>{opt}</code></td><td style="text-align:center"><span style="display:inline-block;background:{pcc}22;color:{pcc};padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600">{p}</span></td><td style="text-align:center"><span style="display:inline-block;background:{scc}22;color:{scc};padding:2px 8px;border-radius:10px;font-size:12px">{s}</span></td><td style="text-align:center;font-size:12px;color:#9ca3af">{deps}</td><td style="text-align:center;font-size:12px;color:#9ca3af">{est}</td></tr>'

total=len(tasks); pending=sum(1 for t in tasks if t['status']=='pending'); high=sum(1 for t in tasks if t['priority']=='high')
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>HoloDub Project Hub</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:24px;max-width:1400px;margin:0 auto}}
h1{{font-size:22px;color:#f0f6fc}}
h2{{font-size:16px;color:#f0f6fc;margin-bottom:12px}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}}
.header-right{{font-size:12px;color:#484f58}}
.stats{{display:flex;gap:16px;margin-bottom:20px;flex-wrap:wrap}}
.stat{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 16px;text-align:center}}
.stat .num{{font-size:24px;font-weight:700}}
.stat .label{{font-size:11px;color:#8b949e;margin-top:2px}}
.num-total{{color:#58a6ff}}.num-pending{{color:#f0883e}}.num-high{{color:#ef4444}}.num-done{{color:#10b981}}
.section{{margin-bottom:32px}}
.section-header{{display:flex;align-items:center;gap:8px;margin-bottom:12px}}
.section-header .badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}}
.badge-tm{{background:#58a6ff22;color:#58a6ff}}.badge-sw{{background:#10b98122;color:#10b981}}
table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}}
th{{background:#21262d;padding:10px 14px;text-align:left;font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #30363d}}
td{{padding:12px 14px;border-bottom:1px solid #21262d;font-size:13px;vertical-align:top}}
tr:hover{{background:#1c2128}}tr:last-child td{{border-bottom:none}}
code{{font-family:SF Mono,Consolas,monospace;font-size:11px;background:#1f2937;padding:2px 6px;border-radius:4px}}
.legend{{display:flex;gap:12px;margin-bottom:14px;font-size:11px;color:#8b949e;align-items:center;flex-wrap:wrap}}
.legend span{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px}}
.steering-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}}
.steering-card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}}
.steering-card h3{{font-size:14px;color:#f0f6fc;margin-bottom:6px}}
.steering-card .path{{font-size:11px;color:#484f58;font-family:SF Mono,Consolas,monospace}}
.steering-card .desc{{font-size:12px;color:#8b949e;margin-top:4px}}
.steering-card .status{{display:inline-block;margin-top:8px;padding:2px 8px;border-radius:10px;font-size:11px;background:#10b98122;color:#10b981}}
.footer{{margin-top:32px;padding-top:16px;border-top:1px solid #21262d;font-size:11px;color:#484f58;text-align:center}}
a{{color:#58a6ff;text-decoration:none}}a:hover{{text-decoration:underline}}
@media(max-width:768px){{.steering-grid{{grid-template-columns:1fr}}}}
</style></head>
<body>
<div class="header">
<h1>🎬 HoloDub Project Hub</h1>
<div class="header-right">更新: {now} · <a href="dashboard.html">刷新</a></div>
</div>
<div class="section">
<div class="section-header"><h2>📐 Steering Documents</h2><span class="badge badge-sw">specworkflow</span></div>
<div class="steering-grid">
<div class="steering-card"><h3>🎯 Product Vision</h3><div class="path">.spec-workflow/steering/product.md</div><div class="desc">Self-hosted video translation &amp; AI dubbing toolkit. Timeline-first dubbing with semantic splitting + duration-aware TTS.</div><div class="status">✅ active</div></div>
<div class="steering-card"><h3>⚙️ Tech Architecture</h3><div class="path">.spec-workflow/steering/tech.md</div><div class="desc">Go (Gin+GORM) + Python (FastAPI+PyTorch). PostgreSQL + Redis. IndexTTS2 + Faster-Whisper. 6 key design decisions documented.</div><div class="status">✅ active</div></div>
<div class="steering-card"><h3>🗂️ Codebase Structure</h3><div class="path">.spec-workflow/steering/structure.md</div><div class="desc">Full directory map + key patterns: stage state machine, async judge, feature flags, partial UPDATE.</div><div class="status">✅ active</div></div>
</div>
</div>
<div class="section">
<div class="section-header"><h2>📋 Task Board</h2><span class="badge badge-tm">taskmaster</span></div>
<div class="stats">
<div class="stat"><div class="num num-total">{total}</div><div class="label">全部</div></div>
<div class="stat"><div class="num num-pending">{pending}</div><div class="label">待做</div></div>
<div class="stat"><div class="num num-high">{high}</div><div class="label">高优先</div></div>
</div>
<div class="legend"><span style="background:#ef4444"></span> high <span style="background:#f59e0b"></span> medium <span style="background:#6b7280"></span> low | <span style="background:#9ca3af"></span> pending <span style="background:#3b82f6"></span> in-progress <span style="background:#10b981"></span> done</div>
<table><thead><tr>
<th style="width:40px">#</th><th>任务</th><th style="width:90px">OPT-ID</th><th style="width:70px">优先级</th><th style="width:80px">状态</th><th style="width:60px">依赖</th><th style="width:50px">工期</th>
</tr></thead><tbody>{rows}</tbody></table>
</div>
<div class="footer">
<b>TaskMaster</b> (<code>.taskmaster/tasks/tasks.json</code>) — 任务规划 · 
<b>SpecWorkflow</b> (<code>.spec-workflow/</code>) — 结构化开发 · 
通过 Hermes MCP 工具集驱动
</div>
</body></html>'''

with open('.taskmaster/dashboard.html','w',encoding='utf-8') as f:
    f.write(html)
print(f'OK: {total} tasks, 3 steering docs')