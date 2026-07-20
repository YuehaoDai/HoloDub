from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a focused listening pack for a main-flow shadow episode."
    )
    parser.add_argument("--episode-id", type=int, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--context-before-sec", type=float, default=4.0)
    parser.add_argument("--context-after-sec", type=float, default=5.0)
    parser.add_argument("--low-judge-threshold", type=float, default=0.80)
    parser.add_argument("--review-segment-ids", default="")
    parser.add_argument("--final-mix-asr-report", default="")
    parser.add_argument("--skip-clips", action="store_true")
    return parser.parse_args()


def fetch_json(api_base: str, path: str, api_key: str = "") -> dict[str, Any]:
    url = api_base.rstrip("/") + path
    request = urllib.request.Request(url)
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


def load_episode(api_base: str, episode_id: int, api_key: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    episode = fetch_json(api_base, f"/episodes/{episode_id}", api_key)
    chapters = fetch_json(api_base, f"/episodes/{episode_id}/chapters", api_key).get("chapters") or []
    rows: list[dict[str, Any]] = []
    for chapter in chapters:
        if not isinstance(chapter, dict) or not chapter.get("id"):
            continue
        payload = fetch_json(api_base, f"/jobs/{chapter['id']}/segments", api_key)
        for raw in payload.get("segments") or []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["chapter_id"] = chapter.get("id")
            row["chapter_ordinal"] = chapter.get("chapter_ordinal")
            row["chapter_start_ms"] = int(chapter.get("chapter_start_ms") or 0)
            row["chapter_end_ms"] = int(chapter.get("chapter_end_ms") or 0)
            row["chapter_title"] = chapter.get("chapter_title") or ""
            row["chapter_title_translated"] = chapter.get("chapter_title_translated") or ""
            rows.append(row)
    return episode, [row for row in chapters if isinstance(row, dict)], rows


def parse_id_set(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        ids.add(int(item.removeprefix("seg")))
    return ids


def load_final_mix_asr_report(raw_path: str) -> dict[str, Any]:
    if not raw_path:
        return {}
    path = Path(raw_path)
    if not path.exists():
        raise FileNotFoundError(f"final-mix ASR report not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def build_asr_by_segment(report: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows = report.get("rows")
    if not isinstance(rows, list):
        return {}
    output: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            segment_id = int(row.get("segment_id") or 0)
        except (TypeError, ValueError):
            continue
        if segment_id <= 0:
            continue
        output[segment_id] = {
            "label": row.get("label"),
            "similarity": row.get("similarity"),
            "flags": row.get("flags") if isinstance(row.get("flags"), list) else [],
            "asr_text": row.get("asr_text") or "",
            "expected_text": row.get("expected_text") or "",
            "window_start_ms": row.get("window_start_ms"),
            "window_end_ms": row.get("window_end_ms"),
        }
    return output


def text_of(row: dict[str, Any], key_a: str, key_b: str) -> str:
    return str(row.get(key_a) or row.get(key_b) or "")


def judge_verdict(row: dict[str, Any]) -> str:
    meta = row.get("judge_meta")
    if isinstance(meta, dict):
        return str(meta.get("verdict") or "")
    return ""


def judge_issues(row: dict[str, Any]) -> list[str]:
    meta = row.get("judge_meta")
    if isinstance(meta, dict) and isinstance(meta.get("issues"), list):
        return [str(item) for item in meta["issues"] if item]
    return []


def abs_time(row: dict[str, Any], key: str) -> int:
    return int(row.get("chapter_start_ms") or 0) + int(row.get(key) or 0)


def seconds(ms: int | float) -> float:
    return round(float(ms) / 1000.0, 3)


def hms(value_sec: float) -> str:
    total = int(round(value_sec))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def sample_key(row: dict[str, Any]) -> str:
    return f"seg{int(row.get('id') or 0)}"


def build_review_rows(
    chapters: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    explicit_ids: set[int],
    low_judge_threshold: float,
    final_mix_asr_by_segment: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    final_mix_asr_by_segment = final_mix_asr_by_segment or {}
    by_chapter_ordinal: dict[tuple[int, int], dict[str, Any]] = {}
    by_id: dict[int, dict[str, Any]] = {}
    for row in segments:
        sid = int(row.get("id") or 0)
        by_id[sid] = row
        by_chapter_ordinal[(int(row.get("chapter_id") or 0), int(row.get("ordinal") or 0))] = row

    reasons: dict[int, list[dict[str, str]]] = {}
    for sid in explicit_ids:
        if sid in by_id:
            reasons.setdefault(sid, []).append(
                {"type": "manual_focus", "detail": "explicit main-flow shadow review focus"}
            )

    for sid, asr_row in final_mix_asr_by_segment.items():
        if sid not in by_id:
            continue
        flags = [str(item) for item in asr_row.get("flags") or [] if item]
        risk_flags = [
            flag
            for flag in flags
            if not flag.startswith("expected_mixed_tokens")
            and flag != "window_bounded_by_next_segment"
        ]
        similarity = asr_row.get("similarity")
        is_risk = bool(risk_flags)
        if similarity is not None:
            try:
                is_risk = is_risk or float(similarity) < 0.80
            except (TypeError, ValueError):
                pass
        if not is_risk:
            continue
        detail_bits: list[str] = []
        if similarity is not None:
            try:
                detail_bits.append(f"similarity={float(similarity):.4f}")
            except (TypeError, ValueError):
                detail_bits.append(f"similarity={similarity}")
        if risk_flags:
            detail_bits.append("flags=" + ",".join(risk_flags))
        reasons.setdefault(sid, []).append(
            {"type": "final_mix_asr", "detail": "; ".join(detail_bits)}
        )

    for row in segments:
        sid = int(row.get("id") or 0)
        score = row.get("judge_score")
        verdict = judge_verdict(row)
        if score is not None and float(score) <= low_judge_threshold:
            reasons.setdefault(sid, []).append(
                {"type": "low_segment_judge", "detail": f"score={float(score):.2f}"}
            )
        if verdict and verdict != "accept":
            reasons.setdefault(sid, []).append(
                {"type": "segment_judge_verdict", "detail": verdict}
            )

    for chapter in chapters:
        meta = chapter.get("chapter_judge_meta")
        if not isinstance(meta, dict):
            continue
        for weak in meta.get("top_3_weakest_segments") or []:
            if not isinstance(weak, dict):
                continue
            row = by_chapter_ordinal.get((int(chapter.get("id") or 0), int(weak.get("ordinal") or -1)))
            if not row:
                continue
            sid = int(row.get("id") or 0)
            reasons.setdefault(sid, []).append(
                {
                    "type": "chapter_weakest",
                    "detail": str(weak.get("issue") or ""),
                    "recommended_fix": str(weak.get("recommended_fix") or ""),
                }
            )

    output: list[dict[str, Any]] = []
    for sid, row_reasons in reasons.items():
        row = by_id.get(sid)
        if not row:
            continue
        start_ms = abs_time(row, "start_ms")
        end_ms = abs_time(row, "end_ms")
        output.append(
            {
                "sample_id": sample_key(row),
                "segment_id": sid,
                "chapter_id": row.get("chapter_id"),
                "chapter_ordinal": row.get("chapter_ordinal"),
                "ordinal": row.get("ordinal"),
                "absolute_start_ms": start_ms,
                "absolute_end_ms": end_ms,
                "absolute_start_sec": seconds(start_ms),
                "absolute_end_sec": seconds(end_ms),
                "timecode": hms(seconds(start_ms)),
                "duration_ms": int(row.get("original_duration_ms") or max(0, end_ms - start_ms)),
                "tts_duration_ms": row.get("tts_duration_ms"),
                "judge_score": row.get("judge_score"),
                "judge_verdict": judge_verdict(row),
                "judge_issues": judge_issues(row),
                "source_text": text_of(row, "src_text", "source_text"),
                "target_text": text_of(row, "tgt_text", "target_text"),
                "final_mix_asr": final_mix_asr_by_segment.get(sid),
                "reasons": row_reasons,
            }
        )
    output.sort(key=lambda item: (int(item["absolute_start_ms"]), int(item["segment_id"])))
    return output


def resolve_data_path(data_root: Path, raw_rel: str) -> Path:
    raw = str(raw_rel).replace("\\", "/")
    path = Path(raw)
    if path.is_absolute():
        return path
    return data_root / raw


def rel_link(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, from_dir)).as_posix()


def run_ffmpeg_clip(ffmpeg: str, final_video: Path, out_path: Path, start_sec: float, duration_sec: float) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-i",
            str(final_video),
            "-t",
            f"{duration_sec:.3f}",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(out_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def add_clips(
    rows: list[dict[str, Any]],
    output_dir: Path,
    final_video: Path,
    ffmpeg: str,
    context_before_sec: float,
    context_after_sec: float,
    episode_duration_ms: int,
    skip_clips: bool,
) -> None:
    clips_dir = output_dir / "clips"
    for row in rows:
        clip_start_ms = max(0, int(row["absolute_start_ms"]) - int(context_before_sec * 1000))
        clip_end_ms = min(
            episode_duration_ms,
            int(row["absolute_end_ms"]) + int(context_after_sec * 1000),
        )
        clip_duration_sec = max(0.1, seconds(clip_end_ms - clip_start_ms))
        clip_path = clips_dir / f"{row['sample_id']}_context.m4a"
        row["clip_start_sec"] = seconds(clip_start_ms)
        row["clip_end_sec"] = seconds(clip_end_ms)
        row["clip_duration_sec"] = clip_duration_sec
        row["clip_relpath"] = rel_link(output_dir, clip_path)
        if not skip_clips:
            run_ffmpeg_clip(ffmpeg, final_video, clip_path, seconds(clip_start_ms), clip_duration_sec)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_reasons(row: dict[str, Any]) -> str:
    items: list[str] = []
    for reason in row.get("reasons") or []:
        detail = reason.get("detail") or ""
        fix = reason.get("recommended_fix") or ""
        body = f"<strong>{html.escape(str(reason.get('type') or 'risk'))}</strong>"
        if detail:
            body += f": {html.escape(detail)}"
        if fix:
            body += f"<br><span class='fix'>fix: {html.escape(fix)}</span>"
        items.append(f"<li>{body}</li>")
    for issue in row.get("judge_issues") or []:
        items.append(f"<li><strong>segment issue</strong>: {html.escape(issue)}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def render_final_mix_asr(row: dict[str, Any]) -> str:
    asr = row.get("final_mix_asr")
    if not isinstance(asr, dict):
        return ""
    similarity = asr.get("similarity")
    if similarity is None:
        sim_text = "n/a"
    else:
        try:
            sim_text = f"{float(similarity):.4f}"
        except (TypeError, ValueError):
            sim_text = str(similarity)
    flags = ", ".join(str(item) for item in asr.get("flags") or [] if item)
    asr_text = str(asr.get("asr_text") or "")
    flag_text = f" | flags={html.escape(flags)}" if flags else ""
    return f"""
          <div class="asr-box">
            <p><b>final-mix ASR</b>: similarity={html.escape(sim_text)}{flag_text}</p>
            <p>{html.escape(asr_text)}</p>
          </div>
    """


def summary_metric(value: Any, fallback: str = "n/a") -> str:
    if value is None:
        return fallback
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_html(
    output_dir: Path,
    final_video: Path,
    episode: dict[str, Any],
    rows: list[dict[str, Any]],
    final_mix_asr_report: dict[str, Any],
) -> str:
    final_rel = rel_link(output_dir, final_video)
    asr_summary = final_mix_asr_report.get("summary")
    if not isinstance(asr_summary, dict):
        asr_summary = {}
    asr_gate = final_mix_asr_report.get("gate")
    if not isinstance(asr_gate, dict):
        asr_gate = {}
    asr_gate_label = "PASS" if asr_gate.get("pass") is True else "FAIL" if asr_gate else "n/a"
    asr_report_md = ""
    report_path_raw = str(final_mix_asr_report.get("_report_path") or "")
    if report_path_raw:
        md_path = Path(report_path_raw).with_suffix(".md")
        if md_path.exists():
            asr_report_md = rel_link(output_dir, md_path)
    rows_html = []
    for row in rows:
        seek = row["absolute_start_sec"]
        rows_html.append(
            f"""
        <article class="risk">
          <div class="risk-head">
            <div>
              <h3>{html.escape(row['sample_id'])} · ch{row['chapter_ordinal']} ord {row['ordinal']}</h3>
              <p>{html.escape(row['timecode'])} · judge={html.escape(str(row.get('judge_score')))} · verdict={html.escape(str(row.get('judge_verdict') or ''))}</p>
            </div>
            <button type="button" onclick="seekMain({seek:.3f})">Jump</button>
          </div>
          <audio controls preload="metadata" src="{html.escape(row['clip_relpath'])}"></audio>
          <div class="texts">
            <p><b>source</b>: {html.escape(row.get('source_text') or '')}</p>
            <p><b>target</b>: {html.escape(row.get('target_text') or '')}</p>
          </div>
          {render_final_mix_asr(row)}
          {render_reasons(row)}
        </article>
        """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HoloDub Main-Flow Review Pack</title>
  <style>
    :root {{
      --bg: #f7f7f4;
      --panel: #fff;
      --ink: #1f2428;
      --muted: #657079;
      --line: #d8dcd8;
      --accent: #246b5b;
      --warn: #985b17;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    main {{
      width: min(1180px, calc(100vw - 40px));
      margin: 28px auto 48px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 26px; letter-spacing: 0; }}
    h2 {{ margin: 28px 0 12px; font-size: 18px; letter-spacing: 0; }}
    h3 {{ margin: 0; font-size: 17px; letter-spacing: 0; }}
    p {{ margin: 0 0 10px; color: var(--muted); }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 10px;
      margin: 18px 0 20px;
    }}
    .metric, .risk {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .metric {{ padding: 12px; min-height: 72px; }}
    .metric strong {{ display: block; font-size: 22px; line-height: 1.1; margin-bottom: 6px; }}
    .metric span {{ color: var(--muted); font-size: 13px; }}
    video {{ width: 100%; aspect-ratio: 16 / 9; display: block; background: #111; border: 1px solid var(--line); border-radius: 8px; }}
    .notice {{
      margin: 16px 0;
      padding: 12px 14px;
      background: #fff8ec;
      border: 1px solid #e7c891;
      border-radius: 8px;
      color: var(--warn);
    }}
    .risk {{ padding: 14px; margin-bottom: 12px; }}
    .risk-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; }}
    button {{
      border: 1px solid var(--accent);
      background: var(--accent);
      color: white;
      border-radius: 6px;
      padding: 8px 12px;
      cursor: pointer;
      font: inherit;
    }}
    audio {{ width: 100%; margin: 10px 0; }}
    .texts {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .asr-box {{
      margin: 10px 0;
      padding: 10px 12px;
      background: #f3f6f4;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .asr-box p:last-child {{ margin-bottom: 0; color: var(--ink); }}
    ul {{ margin: 8px 0 0; padding-left: 20px; }}
    li {{ margin-bottom: 6px; }}
    .fix {{ color: var(--accent); }}
    a {{ color: var(--accent); }}
    @media (max-width: 820px) {{
      main {{ width: min(100vw - 24px, 1180px); margin-top: 18px; }}
      .summary, .texts {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Episode {html.escape(str(episode.get('id') or ''))} Main-Flow Shadow Review Pack</h1>
    <p>{html.escape(str(episode.get('name') or ''))}</p>
    <section class="summary" aria-label="summary">
      <div class="metric"><strong>{html.escape(asr_gate_label)}</strong><span>final-mix ASR gate</span></div>
      <div class="metric"><strong>{len(rows)}</strong><span>focused risk clips</span></div>
      <div class="metric"><strong>{html.escape(summary_metric(asr_summary.get('mean_similarity')))}</strong><span>mean ASR similarity</span></div>
      <div class="metric"><strong>{html.escape(summary_metric(asr_summary.get('weighted_similarity')))}</strong><span>weighted similarity</span></div>
      <div class="metric"><strong>{html.escape(str(asr_summary.get('low_similarity_count', 'n/a')))}/{html.escape(str(asr_summary.get('very_low_similarity_count', 'n/a')))}</strong><span>low / very low</span></div>
    </section>
    <video id="mainVideo" controls preload="metadata">
      <source src="{html.escape(final_rel)}" type="video/mp4">
    </video>
    <div class="notice">
      This pack is for human listening review of the completed main-flow episode. The final-mix ASR gate is objective risk triage, not human acceptance. Candidate execution remains opt-in.
    </div>
    <h2>Focused Clips</h2>
    {''.join(rows_html)}
    <h2>Files</h2>
    <p>
      <a href="./review_manifest.json">review_manifest.json</a> ·
      {f'<a href="{html.escape(asr_report_md)}">final_mix_asr_report.md</a> ·' if asr_report_md else ''}
      <a href="{html.escape(final_rel)}">final.mp4</a>
    </p>
  </main>
  <script>
    function seekMain(sec) {{
      const video = document.getElementById('mainVideo');
      video.currentTime = sec;
      video.scrollIntoView({{behavior: 'smooth', block: 'center'}});
      video.play();
    }}
  </script>
</body>
</html>
"""


def main() -> None:
    args = parse_args()
    data_root = Path(args.data_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    episode, chapters, segments = load_episode(args.api_base, args.episode_id, args.api_key)
    explicit_ids = parse_id_set(args.review_segment_ids)
    final_mix_asr_report = load_final_mix_asr_report(args.final_mix_asr_report)
    if final_mix_asr_report:
        final_mix_asr_report["_report_path"] = str(Path(args.final_mix_asr_report).resolve())
    rows = build_review_rows(
        chapters,
        segments,
        explicit_ids,
        args.low_judge_threshold,
        build_asr_by_segment(final_mix_asr_report),
    )

    final_rel = str(episode.get("output_relpath") or episode.get("output_rel_path") or "")
    final_video = resolve_data_path(data_root, final_rel)
    episode_duration_ms = int(episode.get("duration_ms") or 0)
    if episode_duration_ms <= 0:
        episode_duration_ms = max((int(row.get("chapter_end_ms") or 0) for row in chapters), default=0)

    add_clips(
        rows,
        output_dir,
        final_video,
        args.ffmpeg,
        args.context_before_sec,
        args.context_after_sec,
        episode_duration_ms,
        args.skip_clips,
    )

    manifest = {
        "schema_version": "holodub.main_flow_review_pack.v1",
        "episode": {
            "id": episode.get("id"),
            "name": episode.get("name"),
            "status": episode.get("status"),
            "duration_ms": episode.get("duration_ms"),
            "output_relpath": final_rel,
        },
        "inputs": {
            "api_base": args.api_base,
            "review_segment_ids": sorted(explicit_ids),
            "low_judge_threshold": args.low_judge_threshold,
            "context_before_sec": args.context_before_sec,
            "context_after_sec": args.context_after_sec,
            "final_mix_asr_report": args.final_mix_asr_report,
        },
        "final_mix_asr_summary": final_mix_asr_report.get("summary") if final_mix_asr_report else None,
        "final_mix_asr_gate": final_mix_asr_report.get("gate") if final_mix_asr_report else None,
        "review_rows": rows,
        "artifacts": {
            "index": str(output_dir / "index.html"),
            "final_video": str(final_video),
        },
    }
    write_json(output_dir / "review_manifest.json", manifest)
    (output_dir / "index.html").write_text(
        render_html(output_dir, final_video, episode, rows, final_mix_asr_report),
        encoding="utf-8",
    )
    print(json.dumps({"index": str(output_dir / "index.html"), "rows": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
