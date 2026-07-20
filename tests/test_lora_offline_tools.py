from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import wave
from array import array
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LECTURE8_AUTO_ADVANCE_WATCH_COMMAND = (
    "python tools\\mit6824_auto_advance.py --lecture 8 --watch "
    "--allow-regression-open-issues --rebuild-go-services-before-start --restart-ml-service-before-start"
)


def _write_silence_wav(path: Path, duration_ms: int, sample_rate: int = 1000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(sample_rate * duration_ms / 1000)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


def _write_constant_wav(path: Path, duration_ms: int, amplitude: int, sample_rate: int = 1000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(sample_rate * duration_ms / 1000)
    samples = array("h", [amplitude] * frames)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def _load_tool(name: str, relpath: str):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
        sys.modules.pop(name, None)
    return module


def _candidate(label: str, reject_reasons: list[str]) -> dict:
    return {
        "sample_id": "seg10569",
        "candidate_label": label,
        "candidate_label_chain": [label],
        "reject_reasons": reject_reasons,
        "score": 1.0,
    }


def test_listening_override_allows_only_soft_rejections() -> None:
    gate = _load_tool("lora_candidate_gate", "tools/lora_candidate_gate.py")
    rows = [_candidate("shard01_selected", ["flat_f0"])]

    chosen = gate.select_listening_override(
        "seg10569",
        rows,
        {
            "seg10569": [
                {
                    "sample_id": "seg10569",
                    "candidate_label": "shard01_selected",
                    "reason": "sounds more natural",
                }
            ]
        },
    )

    assert chosen is not None
    assert chosen["candidate_label"] == "shard01_selected"
    assert chosen["listening_override_applied"] is True


@pytest.mark.parametrize(
    "hard_reason",
    ["bad_join_quality", "high_drift", "high_fit_delta", "low_asr", "low_term_recall", "tail_truncation"],
)
def test_listening_override_rejects_hard_failures_even_if_requested(hard_reason: str) -> None:
    gate = _load_tool("lora_candidate_gate", "tools/lora_candidate_gate.py")
    rows = [_candidate("unsafe_candidate", [hard_reason])]

    with pytest.raises(SystemExit):
        gate.select_listening_override(
            "seg10569",
            rows,
            {
                "seg10569": [
                    {
                        "sample_id": "seg10569",
                        "candidate_label": "unsafe_candidate",
                        "allow_unsafe": True,
                    }
                ]
            },
        )


def test_candidate_gate_auto_fallback_excludes_hard_failures() -> None:
    gate = _load_tool("lora_candidate_gate_fallback", "tools/lora_candidate_gate.py")
    low_asr_near_floor = _candidate("low_asr_near_floor", ["low_asr"])
    low_asr_near_floor["similarity"] = 0.9625
    low_asr_below_floor = _candidate("low_asr_below_floor", ["low_asr"])
    low_asr_below_floor["similarity"] = 0.95
    rows = [
        _candidate("hard_term_failure", ["low_term_recall"]),
        low_asr_near_floor,
        low_asr_below_floor,
        _candidate("soft_flat_f0", ["flat_f0"]),
        _candidate("soft_fit_delta", ["high_fit_delta"]),
    ]

    fallback = gate.eligible_fallback_pool(rows)

    assert [row["candidate_label"] for row in fallback] == ["soft_flat_f0"]


def test_lora_repair_queue_routes_density_and_join_failures(tmp_path: Path) -> None:
    tool = _load_tool("lora_build_repair_queue", "tools/lora_build_repair_queue.py")
    gate_report = {
        "rows": [{"sample_id": "seg_pass"}],
        "decisions": [
            {
                "sample_id": "seg_dense",
                "choice": None,
                "no_selection_reason": "all_candidates_failed_hard_gate",
                "candidates": [
                    {
                        "label": "v8_split12",
                        "similarity": 0.9604,
                        "abs_drift_ms": 453.7,
                        "join_quality_pass": True,
                        "reject_reasons": ["low_asr"],
                    }
                ],
            },
            {
                "sample_id": "seg_join",
                "choice": None,
                "no_selection_reason": "all_candidates_failed_hard_gate",
                "candidates": [
                    {
                        "label": "v7_split8_pad09",
                        "similarity": 1.0,
                        "abs_drift_ms": 0.0,
                        "join_quality_pass": False,
                        "join_quality_reasons": ["short_pause"],
                        "reject_reasons": ["bad_join_quality"],
                    }
                ],
            },
        ],
    }
    features = tmp_path / "features.jsonl"
    features.write_text(
        "\n".join(
            [
                json.dumps({"sample_id": "seg_dense", "segment_id": 1, "target_ms": 8000, "text": "dense"}),
                json.dumps({"sample_id": "seg_join", "segment_id": 2, "target_ms": 9000, "text": "join"}),
            ]
        ),
        encoding="utf-8",
    )
    plans = {
        "planned": [
            {
                "sample_id": "seg_dense",
                "needs_text_compression": True,
                "max_required_chars_per_sec": 6.2,
                "max_part_chars_per_sec": 5.6,
                "parts": [
                    {
                        "sample_id": "seg_dense_p1",
                        "target_sec": 8.0,
                        "required_chars_per_sec": 6.2,
                        "speech_rate_risk": True,
                        "text": "too dense",
                    }
                ],
            },
            {"sample_id": "seg_join", "needs_text_compression": False, "parts": []},
        ]
    }

    payload = tool.build_queue(
        gate_report=gate_report,
        features=tool.read_jsonl_by_sample(features),
        plans={str(row["sample_id"]): row for row in plans["planned"]},
        gate_report_path=tmp_path / "gate.json",
        features_path=features,
        speech_rate_plan_path=tmp_path / "plan.json",
        listening_pack="pack/index.html",
        max_fit_delta=0.05,
    )

    by_id = {row["sample_id"]: row for row in payload["entries"]}
    assert payload["summary"]["selected_strict_count"] == 1
    assert by_id["seg_dense"]["action"] == "text_compression_before_tts"
    assert by_id["seg_dense"]["speech_rate"]["risk_parts"][0]["sample_id"] == "seg_dense_p1"
    assert by_id["seg_join"]["action"] == "join_boundary_repair"


def test_lora_text_repair_features_generate_budgeted_variants(tmp_path: Path) -> None:
    tool = _load_tool("lora_build_text_repair_features", "tools/lora_build_text_repair_features.py")
    features = tmp_path / "features.jsonl"
    features.write_text(
        json.dumps(
            {
                "sample_id": "seg_dense",
                "segment_id": 1,
                "target_ms": 10_000,
                "audio_ms": 10_000,
                "code_len": 120,
                "source_text": "The client must send a read with its last ZXID.",
                "text": "客户端在发送读取请求时，必须说明，你知道，我上次发送给主节点的写入请求的ZXID是某个值。而这个副本必须等待，直到它从主节点看到该信息。对吧？",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "sample_id": "seg_dense",
                        "action": "text_compression_before_tts",
                        "reason": "split_part_required_speech_rate_above_safe_limit",
                        "target_sec": 10.0,
                        "speech_rate": {"max_required_chars_per_sec": 6.0},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_features = tmp_path / "repair.features.jsonl"
    output_report = tmp_path / "repair.report.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "lora_build_text_repair_features.py"),
            "--features",
            str(features),
            "--repair-queue",
            str(queue),
            "--output-features",
            str(output_features),
            "--output-report",
            str(output_report),
            "--variants",
            "heuristic52",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "repaired_rows" in result.stdout
    rows = [json.loads(line) for line in output_features.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["sample_id"] == "seg_dense_repair_heuristic52"
    assert rows[0]["source_sample_id"] == "seg_dense"
    assert rows[0]["text_repair"]["target_cps"] == 5.2
    report = json.loads(output_report.read_text(encoding="utf-8"))
    assert report["summary"]["source_samples"] == 1
    assert report["rows"][0]["repaired_chars"] <= report["rows"][0]["max_chars"]


def test_mit6824_training_decision_prefers_risk_queue_over_duplicate_asr_rows(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_training_decision", "tools/mit6824_training_decision.py")
    asr_report = tmp_path / "final_mix_asr_report.json"
    risk_queue = tmp_path / "risk_queue.json"
    acceptance_status = tmp_path / "acceptance_status.json"

    asr_report.write_text(
        json.dumps(
            {
                "flagged_rows": [
                    {
                        "segment_id": 101,
                        "label": "c01_s0001_seg101",
                        "similarity": 0.5,
                        "flags": ["low_similarity:0.5"],
                    }
                ],
                "worst_rows": [],
                "gate": {"pass": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    risk_queue.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "source": "final_mix_asr.flagged_rows",
                        "priority": 1,
                        "segment_id": 101,
                        "label": "c01_s0001_seg101",
                        "similarity": 0.5,
                        "flags": ["low_similarity:0.5"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    acceptance_status.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "status": "review_blocked",
                        "human_accepted": False,
                        "artifacts": {
                            "final_mix_asr_report": {"present": True, "path": str(asr_report)},
                            "risk_queue": {"present": True, "path": str(risk_queue)},
                            "human_review_pack": {"present": False, "path": ""},
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = tool.build_decision(
        Namespace(
            acceptance_status=str(acceptance_status),
            output_json=str(tmp_path / "decision.json"),
            output_md=str(tmp_path / "decision.md"),
            min_reviewed_lectures=2,
            min_repeat_lectures=2,
            min_family_count=3,
            gold_audio_floor_minutes=30,
            gold_audio_promotion_hours=1.0,
            gold_audio_stable_hours=2.0,
        ),
        REPO_ROOT,
    )

    assert payload["summary"]["issue_count"] == 1
    assert payload["issues"][0]["source"] == "final_mix_asr.flagged_rows"
    assert payload["issues"][0]["priority"] == 1
    assert payload["summary"]["family_counts"] == {"low_asr_similarity": 1}
    assert payload["summary"]["recommendation"] == "do_not_train_insufficient_cross_lecture_evidence"
    assert payload["summary"]["training_readiness"]["per_episode_training_allowed"] is False
    assert payload["summary"]["training_readiness"]["status"] == "insufficient_cross_lecture_evidence"
    assert payload["summary"]["gold_audio"]["measured_minutes"] == 0
    assert payload["summary"]["gold_audio"]["floor_met"] is False
    assert payload["policy"]["episode_adapter_policy"] == "never_train_one_adapter_per_lecture_by_default"


def test_mit6824_training_decision_does_not_count_pending_review_as_training_evidence(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_training_decision_pending_review", "tools/mit6824_training_decision.py")
    risk_queue = tmp_path / "risk_queue.json"
    acceptance_status = tmp_path / "acceptance_status.json"
    risk_queue.write_text(
        json.dumps(
            {
                "entries": [
                    {"source": "human_review_pack", "reasons": ["tail truncation missing ending"]},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    acceptance_status.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "status": "awaiting_human_acceptance",
                        "human_accepted": False,
                        "acceptance_readiness": {"ready_for_human_review": True},
                        "artifacts": {"risk_queue": {"present": True, "path": str(risk_queue)}},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = tool.build_decision(
        Namespace(
            acceptance_status=str(acceptance_status),
            output_json=str(tmp_path / "decision.json"),
            output_md=str(tmp_path / "decision.md"),
            min_reviewed_lectures=2,
            min_repeat_lectures=2,
            min_family_count=1,
            gold_audio_floor_minutes=30,
            gold_audio_promotion_hours=1.0,
            gold_audio_stable_hours=2.0,
        ),
        REPO_ROOT,
    )

    assert payload["summary"]["reviewed_lectures"] == 0
    assert payload["summary"]["human_reviewed_lectures"] == []
    assert payload["summary"]["pending_review_lectures"] == [8]
    assert payload["summary"]["pending_content_ready_lectures"] == [8]
    assert payload["summary"]["pending_content_blocked_lectures"] == []
    assert payload["summary"]["issue_count"] == 0
    assert payload["summary"]["recommendation"] == "do_not_train_insufficient_cross_lecture_evidence"


def test_mit6824_training_decision_surfaces_pending_content_blocked_without_training_signal(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_training_decision_pending_content_blocked", "tools/mit6824_training_decision.py")
    acceptance_status = tmp_path / "acceptance_status.json"
    acceptance_status.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "status": "awaiting_human_acceptance",
                        "human_accepted": False,
                        "acceptance_readiness": {
                            "ready_for_human_review": False,
                            "content_errors": ["invalid_final_mix_asr_report_json"],
                        },
                        "artifacts": {},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = tool.build_decision(
        Namespace(
            acceptance_status=str(acceptance_status),
            output_json=str(tmp_path / "decision.json"),
            output_md=str(tmp_path / "decision.md"),
            min_reviewed_lectures=2,
            min_repeat_lectures=2,
            min_family_count=1,
            gold_audio_floor_minutes=30,
            gold_audio_promotion_hours=1.0,
            gold_audio_stable_hours=2.0,
        ),
        REPO_ROOT,
    )

    assert payload["summary"]["human_reviewed_lectures"] == []
    assert payload["summary"]["pending_review_lectures"] == [8]
    assert payload["summary"]["pending_content_ready_lectures"] == []
    assert payload["summary"]["pending_content_blocked_lectures"] == [8]
    assert payload["summary"]["issue_count"] == 0
    assert payload["summary"]["recommendation"] == "do_not_train_insufficient_cross_lecture_evidence"


def test_mit6824_training_decision_requires_gold_audio_floor_before_course_adapter(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_training_decision_gold_audio", "tools/mit6824_training_decision.py")
    acceptance_status = tmp_path / "acceptance_status.json"

    lecture_rows = []
    for lecture, minutes in [(8, 20), (9, 5)]:
        final_audio = tmp_path / f"lec-{lecture:02d}" / "final_mix_audio.wav"
        risk_queue = tmp_path / f"lec-{lecture:02d}" / "risk_queue.json"
        _write_silence_wav(final_audio, duration_ms=minutes * 60 * 1000, sample_rate=10)
        risk_queue.write_text(
            json.dumps(
                {
                    "entries": [
                        {"source": "human_review", "reasons": ["tail truncation missing ending"]},
                        {"source": "human_review", "reasons": ["tail truncation missing ending"]},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        lecture_rows.append(
            {
                "lecture": lecture,
                "title": f"Lecture {lecture}",
                "status": "accepted",
                "human_accepted": True,
                "artifacts": {
                    "final_audio": {"present": True, "path": str(final_audio)},
                    "risk_queue": {"present": True, "path": str(risk_queue)},
                    "final_mix_asr_report": {"present": False, "path": ""},
                    "human_review_pack": {"present": False, "path": ""},
                },
            }
        )

    acceptance_status.write_text(json.dumps({"lectures": lecture_rows}, ensure_ascii=False), encoding="utf-8")
    payload = tool.build_decision(
        Namespace(
            acceptance_status=str(acceptance_status),
            output_json=str(tmp_path / "decision.json"),
            output_md=str(tmp_path / "decision.md"),
            min_reviewed_lectures=2,
            min_repeat_lectures=2,
            min_family_count=3,
            gold_audio_floor_minutes=30,
            gold_audio_promotion_hours=1.0,
            gold_audio_stable_hours=2.0,
        ),
        REPO_ROOT,
    )

    assert payload["summary"]["gold_audio"]["measured_minutes"] == 25
    assert payload["summary"]["gold_audio"]["floor_met"] is False
    assert payload["summary"]["recommendation"] == "do_not_train_collect_human_approved_audio"
    assert payload["summary"]["training_readiness"]["status"] == "insufficient_human_approved_audio"
    assert payload["summary"]["training_readiness"]["per_episode_training_allowed"] is False


def test_mit6824_training_decision_allows_course_review_only_after_gold_audio_floor(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_training_decision_gold_audio_ready", "tools/mit6824_training_decision.py")
    acceptance_status = tmp_path / "acceptance_status.json"

    lecture_rows = []
    for lecture, minutes in [(8, 20), (9, 11)]:
        final_audio = tmp_path / f"lec-{lecture:02d}" / "final_mix_audio.wav"
        risk_queue = tmp_path / f"lec-{lecture:02d}" / "risk_queue.json"
        _write_silence_wav(final_audio, duration_ms=minutes * 60 * 1000, sample_rate=10)
        risk_queue.write_text(
            json.dumps(
                {
                    "entries": [
                        {"source": "human_review", "reasons": ["tail truncation missing ending"]},
                        {"source": "human_review", "reasons": ["tail truncation missing ending"]},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        lecture_rows.append(
            {
                "lecture": lecture,
                "title": f"Lecture {lecture}",
                "status": "accepted",
                "human_accepted": True,
                "artifacts": {
                    "final_audio": {"present": True, "path": str(final_audio)},
                    "risk_queue": {"present": True, "path": str(risk_queue)},
                    "final_mix_asr_report": {"present": False, "path": ""},
                    "human_review_pack": {"present": False, "path": ""},
                },
            }
        )

    acceptance_status.write_text(json.dumps({"lectures": lecture_rows}, ensure_ascii=False), encoding="utf-8")
    payload = tool.build_decision(
        Namespace(
            acceptance_status=str(acceptance_status),
            output_json=str(tmp_path / "decision.json"),
            output_md=str(tmp_path / "decision.md"),
            min_reviewed_lectures=2,
            min_repeat_lectures=2,
            min_family_count=3,
            gold_audio_floor_minutes=30,
            gold_audio_promotion_hours=1.0,
            gold_audio_stable_hours=2.0,
        ),
        REPO_ROOT,
    )

    assert payload["summary"]["gold_audio"]["measured_minutes"] == 31
    assert payload["summary"]["gold_audio"]["floor_met"] is True
    assert payload["summary"]["recommendation"] == "consider_course_level_adapter_training"
    assert payload["summary"]["training_readiness"]["status"] == "course_adapter_feasibility_review"


def test_mit6824_fit_audit_fails_selected_tempo_warp_and_recommends_training_smoke(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_fit_audit", "tools/mit6824_fit_audit.py")
    episode_segments = tmp_path / "episode_segments.json"
    episode_segments.write_text(
        json.dumps(
            [
                {
                    "segment_id": 101,
                    "original_duration_ms": 10_000,
                    "target_text": "clean selected but audibly stretched",
                    "meta": {
                        "tts_lora_candidate_runtime": {
                            "selected_family": "split_light_vector",
                            "gate_decision": {"score": 0.9, "selected_family": "split_light_vector"},
                            "audio_selection_mode": "candidate_audio",
                            "candidates": [
                                {
                                    "family": "split_light_vector",
                                    "score": 0.9,
                                    "audio_relpath": "candidate.wav",
                                    "fit_tempo_delta": 0.06,
                                    "fit_applied": True,
                                    "pre_fit_drift_ms": -600,
                                    "post_fit_drift_ms": 0,
                                },
                                {
                                    "family": "default",
                                    "fit_tempo_delta": 0.25,
                                    "fit_applied": False,
                                },
                            ],
                        }
                    },
                },
                {
                    "segment_id": 102,
                    "original_duration_ms": 20_000,
                    "target_text": "raw candidate badly mismatched",
                    "meta": {
                        "tts_lora_candidate_runtime": {
                            "selected_family": "split_mix",
                            "gate_decision": {"score": 0.8, "selected_family": "split_mix"},
                            "audio_selection_mode": "candidate_audio",
                            "candidates": [
                                {
                                    "family": "split_mix",
                                    "score": 0.8,
                                    "fit_tempo_delta": 0.01,
                                    "fit_applied": False,
                                },
                                {
                                    "family": "default",
                                    "fit_tempo_delta": 0.30,
                                    "fit_applied": False,
                                },
                                {
                                    "family": "split_light_vector",
                                    "fit_tempo_delta": 0.35,
                                    "fit_applied": False,
                                },
                            ],
                        }
                    },
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = tool.audit(
        Namespace(
            episode_segments=str(episode_segments),
            output_json=str(tmp_path / "fit_audit.json"),
            output_md=str(tmp_path / "fit_audit.md"),
            selected_fit_warning_threshold=0.05,
            selected_fit_hard_threshold=0.08,
            candidate_fit_hard_threshold=0.08,
            candidate_fit_severe_threshold=0.20,
            max_selected_fit_warning_ratio=0.0,
            top_risk_limit=10,
        ),
        tmp_path,
    )

    assert payload["status"] == "fail"
    assert payload["recommendation"] == "repair_fit_policy_and_start_duration_prosody_training_feasibility"
    assert payload["summary"]["selected_fit"]["gt_warning_threshold"] == 1
    assert payload["summary"]["candidate_fit"]["gt_severe_threshold"] == 3
    assert payload["training_signal"]["smoke_training_recommended"] is True
    assert payload["risk_segments"][0]["segment_id"] == 101


def test_mit6824_fit_audit_flags_nested_runtime_and_final_implied_fit(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_fit_audit_nested_runtime", "tools/mit6824_fit_audit.py")
    episode_segments = tmp_path / "episode_segments.json"
    episode_segments.write_text(
        json.dumps(
            [
                {
                    "segment_id": 201,
                    "original_duration_ms": 10_000,
                    "tts_duration_ms": 10_700,
                    "target_text": "fallback final audio still needs tempo audit",
                    "meta": {
                        "tts_lora_candidate": {
                            "runtime": {
                                "selected_family": "default",
                                "audio_selection_mode": "default_audio",
                                "gate_decision": {"score": 0.7, "selected_family": "default"},
                                "candidates": [
                                    {
                                        "family": "default",
                                        "score": 0.7,
                                        "fit_tempo_delta": 0.01,
                                    }
                                ],
                            }
                        },
                        "tts_agent": {
                            "final_reason": "within_threshold",
                            "final_actual_ms": 10_700,
                            "target_ms": 10_000,
                            "final_abs_drift_ms": 700,
                            "final_implied_fit_delta": 0.07,
                            "lora_selected_family": "default",
                            "lora_audio_selection_mode": "default_audio",
                        },
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = tool.audit(
        Namespace(
            episode_segments=str(episode_segments),
            output_json=str(tmp_path / "fit_audit.json"),
            output_md=str(tmp_path / "fit_audit.md"),
            selected_fit_warning_threshold=0.05,
            selected_fit_hard_threshold=0.08,
            candidate_fit_hard_threshold=0.08,
            candidate_fit_severe_threshold=0.20,
            max_selected_fit_warning_ratio=0.0,
            top_risk_limit=10,
        ),
        tmp_path,
    )

    assert payload["status"] == "fail"
    assert payload["summary"]["selected_fit"]["count"] == 1
    assert payload["summary"]["final_implied_fit"]["gt_warning_threshold"] == 1
    assert payload["risk_segments"][0]["risk_kind"] == "final_implied_fit"
    assert payload["risk_segments"][0]["final_implied_fit_delta"] == 0.07


def test_lora_dataset_manifest_loads_final_mix_asr_rows(tmp_path: Path) -> None:
    tool = _load_tool("lora_dataset_manifest_asr_rows", "tools/lora_dataset_manifest.py")
    report = tmp_path / "final_mix_asr_report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": "holodub.final_mix_asr_audit.v1",
                "rows": [
                    {
                        "segment_id": 123,
                        "similarity": 0.987,
                        "expected_text": "目标文本",
                        "asr_text": "目标文本",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = tool.load_asr_rows([str(report)])

    assert rows[123]["asr_similarity"] == 0.987
    assert rows[123]["expected_text"] == "目标文本"


def test_lora_dataset_manifest_rejects_holdout_fit_and_runtime_failures() -> None:
    tool = _load_tool("lora_dataset_manifest_rejection_reasons", "tools/lora_dataset_manifest.py")
    record = {
        "segment_id": 42,
        "audio_exists": True,
        "audio_ms": 10_000,
        "chars": 20,
        "judge_score": 1.0,
        "abs_drift_ms": 10,
        "drift_ratio": 0.001,
        "asr_similarity": 1.0,
        "critical_term_recall": 1.0,
        "fit_tempo_delta": 0.0,
        "agent_final_implied_fit_delta": 0.061,
        "final_fit_audit_pass": False,
        "final_fit_audit_implied_fit_delta": 0.073,
        "agent_hard_cap_exceeded": False,
        "runtime_fit_reject_reason": "",
        "runtime_join_quality_pass": True,
        "runtime_selected_reject_reasons": ["tail_truncation"],
    }
    args = Namespace(
        min_duration_ms=1000,
        max_duration_ms=45_000,
        min_chars=4,
        max_chars=220,
        min_judge_score=0.8,
        max_abs_drift_ms=500,
        max_abs_drift_ratio=0.05,
        min_asr_similarity=0.97,
        min_term_recall=0.8,
        max_fit_tempo_delta=0.05,
        max_agent_final_fit_delta=0.05,
        max_final_fit_delta=0.05,
        reject_runtime_hard_failures=True,
    )

    reasons = tool.rejection_reasons(record, args, {42})

    assert "excluded_segment_id" in reasons
    assert "high_agent_final_fit_delta" in reasons
    assert "final_fit_audit_failed" in reasons
    assert "high_final_fit_audit_delta" in reasons
    assert "runtime_hard_failure" in reasons


def test_mit6824_lora_training_readiness_blocks_duplicate_candidate_pack(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    reference = tmp_path / "reference"
    candidate.mkdir()
    reference.mkdir()
    row = {"sample_id": "seg1", "segment_id": 1, "audio_ms": 1000}
    for directory in (candidate, reference):
        (directory / "manifest.json").write_text(
            json.dumps({"stats": {"total_audio_hours": 0.1}, "tier": "silver"}, ensure_ascii=False),
            encoding="utf-8",
        )
        for name in ("all", "train"):
            (directory / f"{name}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        for name in ("val", "test"):
            (directory / f"{name}.jsonl").write_text("", encoding="utf-8")
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps({"status": "fail", "blockers": ["val_rows:0<6"], "warnings": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    output_json = tmp_path / "readiness.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "mit6824_lora_training_readiness.py"),
            "--candidate-manifest-dir",
            str(candidate),
            "--reference-manifest-dir",
            str(reference),
            "--audit-json",
            str(audit),
            "--output-json",
            str(output_json),
            "--min-new-source-ids",
            "1",
            "--min-total-hours",
            "0.5",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["status"] == "not_ready_to_train"
    assert "training_pack_audit_not_pass:fail" in payload["blockers"]
    assert "candidate_audio_hours:0.1<0.5" in payload["blockers"]
    assert "new_source_ids:0<1" in payload["blockers"]


def test_mit6824_finalize_writes_lora_runtime_provenance(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_finalize_lecture", "tools/mit6824_finalize_lecture.py")
    rows = [
        {
            "status": "synthesized",
            "plan_present": "true",
            "runtime_present": "true",
            "audio_selection_mode": "candidate_audio",
            "selected_family": "split_mix",
            "fallback_reason": "",
            "adapter_data_relpath": "training/lora_runs/pilot/adapter_step80.pt",
            "adapter_step": "80",
        },
        {
            "status": "synthesized",
            "plan_present": "true",
            "runtime_present": "true",
            "audio_selection_mode": "default_audio",
            "selected_family": "default",
            "fallback_reason": "all_candidates_rejected",
            "adapter_data_relpath": "training/lora_runs/pilot/adapter_step80.pt",
            "adapter_step": "80",
        },
    ]
    lora_provenance = tool.summarize_lora_provenance_rows(rows)
    lecture_dir = tmp_path / "lec-08"

    manifest_path = tool.write_acceptance_manifest(
        root=tmp_path,
        lecture=8,
        lecture_title="Zookeeper",
        lecture_dir=lecture_dir,
        episode={"id": 188, "output_rel_path": "outputs/lec08.mp4"},
        tracker_row={
            "start_payload": {
                "config": {
                    "production_profile": "mit6824-course-prod-v1",
                    "lora_candidate_opt_in": True,
                }
            }
        },
        lora_provenance=lora_provenance,
        asr_report_path=None,
        review_pack_index=None,
    )

    assert manifest_path.exists()
    provenance = json.loads((lecture_dir / "adapter_profile_provenance.json").read_text(encoding="utf-8"))
    runtime_summary = provenance["runtime"]["summary"]
    assert provenance["production_profile"] == "mit6824-course-prod-v1"
    assert provenance["lora_candidate_opt_in"] is True
    assert runtime_summary["candidate_audio_segments"] == 1
    assert runtime_summary["fallback_segments"] == 1
    assert runtime_summary["selected_family_counts"] == {"default": 1, "split_mix": 1}
    assert runtime_summary["adapter_step_counts"] == {"80": 2}


def test_mit6824_finalize_risk_queue_includes_lora_runtime_fallback(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_finalize_lecture_risk_queue", "tools/mit6824_finalize_lecture.py")

    payload = tool.build_risk_queue(
        lecture=8,
        episode={"id": 188},
        asr_report_path=None,
        review_manifest_path=None,
        lora_runtime_risks=[
            {
                "source": "lora_candidate_runtime",
                "priority": 1,
                "segment_id": 9001,
                "label": "c01_s0001_seg9001",
                "chapter_ordinal": 1,
                "segment_ordinal": 1,
                "selected_candidate": "default",
                "audio_selection_mode": "default_audio",
                "fallback_reason": "candidate_attempt_limit_reached",
                "reasons": ["candidate_fallback:candidate_attempt_limit_reached", "candidate_default_audio"],
            }
        ],
    )

    assert payload["summary"]["total_entries"] == 1
    assert payload["summary"]["by_source"] == {"lora_candidate_runtime": 1}
    assert payload["sources"]["lora_runtime_risks_present"] is True
    entry = payload["entries"][0]
    assert entry["source"] == "lora_candidate_runtime"
    assert entry["segment_id"] == 9001
    assert entry["fallback_reason"] == "candidate_attempt_limit_reached"


def test_mit6824_finalize_lora_runtime_risk_preserves_gate_diagnostics() -> None:
    tool = _load_tool("mit6824_finalize_lecture_risk_diagnostics", "tools/mit6824_finalize_lecture.py")

    row = tool.compact_lora_runtime_risk_row(
        {
            "chapter_ordinal": "2",
            "segment_ordinal": "1",
            "segment_id": "15966",
            "label": "c02_s0001_seg15966",
            "original_duration_ms": "36070",
            "tts_duration_ms": "27715",
            "tts_audio_relpath": "jobs/349/tts/vp0/segment-0001.agent_attempt04.wav",
            "audio_selection_mode": "default_audio",
            "selected_family": "default",
            "fallback_reason": "candidate_attempt_limit_reached",
            "candidate_attempt_count": "2",
            "gate_status": "skipped",
            "gate_reason": "candidate_attempt_limit_reached",
            "gate_reject_reasons": '["high_drift","high_fit_delta"]',
            "candidate_generation_stop_reason": "candidate_attempt_limit_reached",
            "target_text_preview": "hard long segment",
        }
    )

    assert row["segment_id"] == 15966
    assert row["candidate_attempt_count"] == 2
    assert row["gate_status"] == "skipped"
    assert row["gate_reason"] == "candidate_attempt_limit_reached"
    assert row["gate_reject_reasons"] == '["high_drift","high_fit_delta"]'
    assert row["candidate_generation_stop_reason"] == "candidate_attempt_limit_reached"
    assert "candidate_gate:skipped" in row["reasons"]
    assert "candidate_gate_reason:candidate_attempt_limit_reached" in row["reasons"]


def test_mit6824_finalize_flags_low_density_fragmented_source_timing_risk() -> None:
    tool = _load_tool("mit6824_finalize_lecture_source_timing", "tools/mit6824_finalize_lecture.py")

    row = tool.compact_source_timing_plan_risk_row(
        {
            "chapter_ordinal": "2",
            "segment_ordinal": "35",
            "segment_id": "16000",
            "label": "c02_s0035_seg16000",
            "original_duration_ms": "22880",
            "tts_duration_ms": "0",
            "tts_audio_relpath": "",
            "source_text": "Be. Helpful. For. People. Building. Their. Own. Distributed. Systems.",
            "target_text": "对吧？它能否切实帮助那些正在构建各自独立、彼此隔离的分布式系统的开发者们呢？比如：是否存在一种通用协调服务？",
        }
    )

    assert row is not None
    assert row["source"] == "source_timing_plan"
    assert row["segment_id"] == 16000
    assert "fragmented_source_asr" in row["reasons"]
    assert any(str(reason).startswith("low_target_density:") for reason in row["reasons"])
    assert row["metrics"]["short_source_fragment_count"] >= 4


def test_mit6824_finalize_large_drift_source_timing_risk_is_priority_zero() -> None:
    tool = _load_tool("mit6824_finalize_lecture_source_timing_drift", "tools/mit6824_finalize_lecture.py")

    row = tool.compact_source_timing_plan_risk_row(
        {
            "chapter_ordinal": "3",
            "segment_ordinal": "8",
            "segment_id": "16011",
            "label": "c03_s0008_seg16011",
            "original_duration_ms": "32040",
            "tts_duration_ms": "24904",
            "tts_audio_relpath": "jobs/350/tts/vp0/segment-0008.agent_attempt08.wav",
            "source_text": (
                "This system routes every request through the leader but real workloads are usually dominated "
                "by reads and clients often need far more read capacity than write capacity for web pages."
            ),
            "target_text": "我们来设想这样一个分布式系统：所有客户端请求，无论读写，都必须先经 leader 处理。",
        }
    )

    assert row is not None
    assert row["priority"] == 0
    assert row["drift_ms"] == -7136
    assert "large_tts_drift:-7136" in row["reasons"]


def test_mit6824_finalize_large_drift_flags_short_synthesized_segment() -> None:
    tool = _load_tool("mit6824_finalize_lecture_source_timing_short_drift", "tools/mit6824_finalize_lecture.py")

    row = tool.compact_source_timing_plan_risk_row(
        {
            "chapter_ordinal": "1",
            "segment_ordinal": "38",
            "segment_id": "15953",
            "label": "c01_s0038_seg15953",
            "original_duration_ms": "5160",
            "tts_duration_ms": "3912",
            "tts_audio_relpath": "jobs/348/tts/vp0/segment-0038.agent_attempt00.wav",
            "source_text": "Client two reads the value and the operation completes.",
            "target_text": "客户端二读取这个值，然后操作完成。",
        }
    )

    assert row is not None
    assert row["priority"] == 0
    assert row["drift_ms"] == -1248
    assert "large_tts_drift:-1248" in row["reasons"]


def test_mit6824_finalize_large_drift_flags_subsecond_synthesized_segment() -> None:
    tool = _load_tool("mit6824_finalize_lecture_source_timing_subsecond_drift", "tools/mit6824_finalize_lecture.py")

    row = tool.compact_source_timing_plan_risk_row(
        {
            "chapter_ordinal": "3",
            "segment_ordinal": "27",
            "segment_id": "16030",
            "label": "c03_s0027_seg16030",
            "original_duration_ms": "26160",
            "tts_duration_ms": "25404",
            "tts_audio_relpath": "jobs/350/tts/vp0/segment-0027.agent_attempt08.wav",
            "source_text": "The second operation completes before the third operation and the replica may still have stale data.",
            "target_text": "operation two completes before operation three, but this synthesized audio is still too short.",
        }
    )

    assert row is not None
    assert row["priority"] == 0
    assert row["drift_ms"] == -756
    assert "large_tts_drift:-756" in row["reasons"]


def test_mit6824_finalize_risk_queue_includes_source_timing_plan_risks() -> None:
    tool = _load_tool("mit6824_finalize_lecture_source_timing_queue", "tools/mit6824_finalize_lecture.py")

    payload = tool.build_risk_queue(
        lecture=8,
        episode={"id": 188},
        asr_report_path=None,
        review_manifest_path=None,
        source_timing_plan_risks=[
            {
                "source": "source_timing_plan",
                "priority": 1,
                "segment_id": 16000,
                "label": "c02_s0035_seg16000",
                "reasons": ["fragmented_source_asr", "low_target_density:2.1_units_per_sec"],
            }
        ],
    )

    assert payload["summary"]["total_entries"] == 1
    assert payload["summary"]["by_source"] == {"source_timing_plan": 1}
    assert payload["sources"]["source_timing_plan_risks_present"] is True


def test_mit6824_source_timing_preflight_builds_risk_report() -> None:
    tool = _load_tool("mit6824_source_timing_preflight", "tools/mit6824_source_timing_preflight.py")

    payload = tool.build_report(
        episode={"id": 180, "lecture": 8},
        rows=[
            {
                "chapter_ordinal": "3",
                "segment_ordinal": "8",
                "segment_id": "16011",
                "label": "c03_s0008_seg16011",
                "status": "synthesized",
                "original_duration_ms": "32040",
                "tts_duration_ms": "24904",
                "tts_audio_relpath": "jobs/350/tts/vp0/segment-0008.agent_attempt08.wav",
                "source_text": (
                    "This system routes every request through the leader but real workloads are usually dominated "
                    "by reads and clients often need far more read capacity than write capacity for web pages."
                ),
                "target_text": "我们来设想这样一个分布式系统：所有客户端请求，无论读写，都必须先经 leader 处理。",
            },
            {
                "chapter_ordinal": "2",
                "segment_ordinal": "35",
                "segment_id": "16000",
                "label": "c02_s0035_seg16000",
                "status": "translated",
                "original_duration_ms": "22880",
                "tts_duration_ms": "0",
                "tts_audio_relpath": "",
                "source_text": "Be. Helpful. For. People. Building. Their. Own. Distributed. Systems.",
                "target_text": "对吧？它能否切实帮助那些正在构建各自独立的分布式系统的开发者们呢？",
            }
        ],
    )

    assert payload["status"] == "available"
    assert payload["summary"]["analyzed_segments"] == 2
    assert payload["summary"]["risk_count"] == 2
    assert payload["summary"]["high_priority_count"] == 2
    assert payload["summary"]["by_reason"]["fragmented_source_asr"] == 1
    assert payload["summary"]["by_reason"]["large_tts_drift"] == 1
    assert payload["entries"][0]["segment_id"] == 16011
    assert payload["entries"][0]["priority"] == 0


def test_mit6824_source_timing_preflight_skips_without_active_episode() -> None:
    tool = _load_tool("mit6824_source_timing_preflight_skip", "tools/mit6824_source_timing_preflight.py")

    payload = tool.build_skipped_report(
        {"id": 0, "lecture": None, "selection": "none"},
        "no_active_or_selected_episode",
    )

    assert payload["status"] == "skipped"
    assert payload["skip_reason"] == "no_active_or_selected_episode"
    assert payload["summary"]["analyzed_segments"] == 0
    assert payload["summary"]["risk_count"] == 0
    assert payload["query_error"] is None


def test_mit6824_status_refresh_runs_source_timing_preflight() -> None:
    tool = _load_tool("mit6824_status_refresh_source_timing", "tools/mit6824_status_refresh.py")

    steps = tool.build_steps(
        Namespace(
            python=sys.executable,
            lecture_start=8,
            lecture_end=20,
            tracker="tracker.json",
            tracker_md="tracker.md",
            acceptance_status="acceptance.json",
            acceptance_status_md="acceptance.md",
            training_decision="training.json",
            training_decision_md="training.md",
            candidate_coverage="coverage.json",
            candidate_coverage_md="coverage.md",
            candidate_coverage_job_ids="1,2",
            source_timing_preflight="source_timing.json",
            source_timing_preflight_md="source_timing.md",
            course_advance_status="advance.json",
            course_advance_history="advance.jsonl",
            watchdog_status="watchdog.json",
            watchdog_status_md="watchdog.md",
            runtime_blockers="blockers.json",
            runtime_blockers_md="blockers.md",
            output_json="status.json",
            goal_status="goal.json",
            goal_status_md="goal.md",
            v25_diagnostics="v25.json",
            v25_diagnostics_md="v25.md",
            delivery_index="delivery.json",
            delivery_index_md="delivery.md",
            batch_review="batch.json",
            batch_review_md="batch.md",
            handoff_report="handoff.json",
            handoff_report_md="handoff.md",
            history_jsonl="status.jsonl",
            auto_advance_status="auto.json",
        ),
        degraded_without_docker=False,
    )

    step = next(row for row in steps if row[0] == "source_timing_preflight")
    assert "tools/mit6824_source_timing_preflight.py" in step[1]
    assert "--output-json" in step[1]
    assert "source_timing.json" in step[1]


def test_mit6824_finalize_review_pack_includes_extra_risk_segment_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool("mit6824_finalize_lecture_review_pack_extra_ids", "tools/mit6824_finalize_lecture.py")
    asr_report = tmp_path / "asr.json"
    asr_report.write_text(
        json.dumps({"flagged_rows": [{"segment_id": 16000}], "worst_rows": [{"segment_id": 16011}]}),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="{}")

    monkeypatch.setattr(tool, "run", fake_run)

    tool.run_review_pack(
        Namespace(
            python=sys.executable,
            api_base="http://127.0.0.1:8080",
            api_key="",
            data_root="data",
            skip_clips=False,
            skip_review_pack=False,
        ),
        tmp_path,
        180,
        tmp_path / "lec-08",
        asr_report,
        extra_review_segment_ids=[16020, 16011],
    )

    cmd = captured["cmd"]
    idx = cmd.index("--review-segment-ids")
    assert cmd[idx + 1] == "16000,16011,16020"


def test_mit6824_finalize_annotates_review_pack_with_acceptance_actions(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_finalize_lecture_acceptance_actions", "tools/mit6824_finalize_lecture.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    (review_dir / "review_manifest.json").write_text(
        json.dumps({"schema_version": "holodub.main_flow_review_pack.v1", "review_rows": []}),
        encoding="utf-8",
    )
    (review_dir / "index.html").write_text("<html><body><main><h1>Review</h1></main></body></html>", encoding="utf-8")

    readiness = {
        "ready_for_human_review": True,
        "required_files_present": True,
        "content_errors": [],
        "content_warnings": [],
        "metrics": {
            "asr": {"mean_similarity": 0.99, "weighted_similarity": 0.98},
            "risk_queue": {"total_entries": 2},
            "review_pack": {"review_rows": 1},
            "adapter_profile": {"candidate_audio_segments": 1, "fallback_segments": 0},
        },
    }
    training_summary = {
        "recommendation": "do_not_train_insufficient_cross_lecture_evidence",
        "training_readiness": {"status": "insufficient_cross_lecture_evidence"},
        "human_reviewed_lectures": [],
        "pending_content_ready_lectures": [8],
    }
    result = tool.annotate_review_pack_acceptance_actions(
        tmp_path,
        lecture_dir,
        8,
        acceptance_readiness=readiness,
        training_summary=training_summary,
    )
    result_again = tool.annotate_review_pack_acceptance_actions(
        tmp_path,
        lecture_dir,
        8,
        acceptance_readiness=readiness,
        training_summary=training_summary,
    )

    manifest = json.loads((review_dir / "review_manifest.json").read_text(encoding="utf-8"))
    page = (review_dir / "index.html").read_text(encoding="utf-8")
    assert result["manifest_updated"] is True
    assert result["html_updated"] is True
    assert result_again["manifest_updated"] is True
    assert result_again["html_updated"] is True
    assert manifest["acceptance_actions"]["lecture"] == 8
    assert manifest["mit6824_status_summary"]["acceptance_readiness"]["ready_for_human_review"] is True
    assert manifest["mit6824_status_summary"]["training"]["pending_content_ready_lectures"] == [8]
    assert manifest["acceptance_actions"]["accept_dry_run_report"].endswith(
        "data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json"
    )
    assert "--dry-run" in manifest["acceptance_actions"]["accept_dry_run_command"]
    assert "--dry-run-output data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json" in manifest[
        "acceptance_actions"
    ]["accept_dry_run_command"]
    assert "tools\\mit6824_accept_lecture.py --lecture 8" in manifest["acceptance_actions"]["accept_command"]
    assert "tools\\mit6824_record_review_blockers.py --lecture 8" in manifest["acceptance_actions"]["blocker_command"]
    assert page.count("mit6824-acceptance-actions:start") == 1
    assert "Acceptance readiness" in page
    assert "Dry-run accept rehearsal" in page
    assert "--dry-run" in page
    assert "acceptance_dry_run_report.json" in page
    assert "do_not_train_insufficient_cross_lecture_evidence" in page
    assert "tools\\mit6824_accept_lecture.py --lecture 8" in page
    assert "tools\\mit6824_record_review_blockers.py --lecture 8" in page


def test_mit6824_acceptance_status_surfaces_acceptance_actions(tmp_path: Path) -> None:
    finalize_tool = _load_tool(
        "mit6824_finalize_lecture_status_actions_helper",
        "tools/mit6824_finalize_lecture.py",
    )
    status_tool = _load_tool("mit6824_acceptance_status_actions", "tools/mit6824_acceptance_status.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifact_paths = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifact_paths.values():
        path.write_text("ok", encoding="utf-8")
    artifact_paths["final_mix_asr_report"].write_text(
        json.dumps(
            {
                "summary": {"mean_similarity": 0.99, "weighted_similarity": 0.99, "min_similarity": 0.97},
                "gate": {"pass": True},
                "rows": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_paths["risk_queue"].write_text(
        json.dumps({"summary": {"total_entries": 0}, "entries": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["fit_audit"].write_text(
        json.dumps(
            {
                "status": "pass",
                "recommendation": "fit_policy_ok",
                "summary": {
                    "selected_fit": {"gt_warning_threshold": 0},
                    "candidate_fit": {"gt_hard_threshold": 0},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_paths["adapter_profile_provenance"].write_text(
        json.dumps({"runtime": {"summary": {"candidate_audio_segments": 1, "fallback_segments": 0}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (review_dir / "review_manifest.json").write_text(
        json.dumps(
            {
                "review_rows": [{"sample_id": "seg1"}],
                "acceptance_actions": finalize_tool.build_acceptance_actions(8),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "title": "Zookeeper",
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                "acceptance_actions": finalize_tool.build_acceptance_actions(8),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tracker = tmp_path / "tracker.json"
    tracker.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "status": "episode_completed",
                        "episodes": [{"id": 188, "status": "completed", "output_rel_path": "outputs/lec08.mp4"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    status = status_tool.build_status(
        Namespace(
            tracker=str(tracker),
            acceptance_root=str(tmp_path / "acceptance"),
            output_json=str(tmp_path / "acceptance_status.json"),
            output_md=str(tmp_path / "acceptance_status.md"),
        ),
        tmp_path,
    )
    md_path = tmp_path / "acceptance_status.md"
    status_tool.write_markdown(md_path, status)

    row = status["lectures"][0]
    md = md_path.read_text(encoding="utf-8")
    assert row["status"] == "awaiting_human_acceptance"
    assert row["acceptance_readiness"]["ready_for_human_review"] is True
    assert row["acceptance_readiness"]["content_errors"] == []
    assert row["acceptance_readiness"]["metrics"]["review_pack"]["review_rows"] == 1
    assert "tools\\mit6824_accept_lecture.py --lecture 8" in row["acceptance_actions"]["accept_command"]
    assert "tools\\mit6824_record_review_blockers.py --lecture 8" in row["acceptance_actions"]["blocker_command"]
    assert "## Acceptance Actions" in md
    assert "Content ready for review" in md
    assert "Dry-run accept rehearsal" in md
    assert "acceptance_dry_run_report.json" in md
    assert "tools\\mit6824_accept_lecture.py --lecture 8" in md


def test_mit6824_acceptance_status_flags_invalid_content_readiness(tmp_path: Path) -> None:
    finalize_tool = _load_tool(
        "mit6824_finalize_lecture_status_invalid_helper",
        "tools/mit6824_finalize_lecture.py",
    )
    status_tool = _load_tool("mit6824_acceptance_status_invalid_content", "tools/mit6824_acceptance_status.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifact_paths = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifact_paths.values():
        path.write_text("ok", encoding="utf-8")
    artifact_paths["final_mix_asr_report"].write_text("not json", encoding="utf-8")
    artifact_paths["fit_audit"].write_text(
        json.dumps(
            {
                "status": "pass",
                "summary": {
                    "selected_fit": {"gt_warning_threshold": 0},
                    "candidate_fit": {"gt_hard_threshold": 0},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_paths["risk_queue"].write_text(json.dumps({"summary": {"total_entries": 0}}, ensure_ascii=False), encoding="utf-8")
    artifact_paths["adapter_profile_provenance"].write_text(json.dumps({"runtime": {}}, ensure_ascii=False), encoding="utf-8")
    (review_dir / "review_manifest.json").write_text(json.dumps({"review_rows": []}, ensure_ascii=False), encoding="utf-8")
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "title": "Zookeeper",
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                "acceptance_actions": finalize_tool.build_acceptance_actions(8),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tracker = tmp_path / "tracker.json"
    tracker.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "status": "episode_completed",
                        "episodes": [{"id": 188, "status": "completed", "output_rel_path": "outputs/lec08.mp4"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    status = status_tool.build_status(
        Namespace(
            tracker=str(tracker),
            acceptance_root=str(tmp_path / "acceptance"),
            output_json=str(tmp_path / "acceptance_status.json"),
            output_md=str(tmp_path / "acceptance_status.md"),
        ),
        tmp_path,
    )

    row = status["lectures"][0]
    assert row["status"] == "awaiting_human_acceptance"
    assert row["acceptance_readiness"]["ready_for_human_review"] is False
    assert "invalid_final_mix_asr_report_json" in row["acceptance_readiness"]["content_errors"]
    assert "risk_queue_missing_entries" in row["acceptance_readiness"]["content_errors"]
    assert "review_manifest_missing_acceptance_actions" in row["acceptance_readiness"]["content_errors"]
    assert status["summary"]["content_readiness_blocked"] == [8]


def test_mit6824_acceptance_status_hides_stale_artifacts_during_latest_rerun(tmp_path: Path) -> None:
    status_tool = _load_tool("mit6824_acceptance_status_stale_artifacts", "tools/mit6824_acceptance_status.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    lecture_dir.mkdir(parents=True)
    old_final = tmp_path / "data" / "episodes" / "180" / "output" / "vp0" / "final.mp4"
    old_final.parent.mkdir(parents=True)
    old_final.write_text("old video", encoding="utf-8")
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "title": "Zookeeper",
                "human_accepted": False,
                "artifacts": {"final_video": str(old_final)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tracker = tmp_path / "tracker.json"
    tracker.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "status": "in_progress",
                        "episodes": [
                            {"id": 180, "status": "completed", "output_rel_path": "episodes/180/output/vp0/final.mp4"},
                            {"id": 181, "status": "pending", "output_rel_path": ""},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    status = status_tool.build_status(
        Namespace(
            tracker=str(tracker),
            acceptance_root=str(tmp_path / "acceptance"),
            output_json=str(tmp_path / "acceptance_status.json"),
            output_md=str(tmp_path / "acceptance_status.md"),
        ),
        tmp_path,
    )

    row = status["lectures"][0]
    assert row["status"] == "in_progress"
    assert row["latest_episode"]["id"] == 181
    assert row["artifacts"]["final_video"]["present"] is False
    assert row["artifacts"]["final_video"]["path"] == ""


def test_mit6824_finalize_refreshes_status_after_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _load_tool("mit6824_finalize_lecture_status_refresh", "tools/mit6824_finalize_lecture.py")
    tracker = tmp_path / "tracker.json"
    tracker.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "episodes": [{"id": 188, "status": "completed", "output_rel_path": "outputs/lec08.mp4"}],
                        "start_payload": {
                            "config": {
                                "production_profile": "mit6824-course-prod-v1",
                                "lora_candidate_opt_in": True,
                            }
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tool,
        "load_episode_lora_provenance",
        lambda *_args, **_kwargs: {
            "schema_version": "holodub.mit6824.lora_runtime_provenance.v1",
            "status": "available",
            "summary": {"candidate_audio_segments": 1},
        },
    )
    monkeypatch.setattr(
        tool,
        "refresh_course_status",
        lambda *_args, **_kwargs: {"returncode": 7, "stdout_tail": "temporary refresh failure"},
    )

    done, row = tool.one_attempt(
        Namespace(
            lecture=8,
            episode_id=0,
            tracker=str(tracker),
            acceptance_root=str(tmp_path / "acceptance"),
            api_base="http://127.0.0.1:8080",
            data_root="data",
            python=sys.executable,
            execute=True,
            skip_asr=True,
            skip_review_pack=True,
            skip_clips=True,
            allow_incomplete=False,
            refresh_tracker=False,
            skip_status_refresh_after_finalize=False,
            postgres_service="postgres",
            postgres_user="holodub",
            postgres_db="holodub",
            compose_file=[],
        ),
        tmp_path,
    )

    assert done is True
    assert row["status"] == "finalized_for_human_review"
    assert row["status_refresh"]["returncode"] == 7
    assert row["status_refresh_warning"] == "post_finalize_status_refresh_failed"
    assert (tmp_path / "acceptance" / "lec-08" / "acceptance.json").exists()


def test_mit6824_course_advance_blocks_start_until_regression_gate_ready() -> None:
    tool = _load_tool("mit6824_course_advance", "tools/mit6824_course_advance.py")
    args = Namespace(
        lecture_start=8,
        lecture_end=20,
        allow_unaccepted_previous=False,
        python=sys.executable,
        tracker="tracker.json",
        acceptance_root="acceptance",
        finalizer_poll_sec=300.0,
        finalizer_timeout_sec=172800.0,
    )
    tracker = {"lectures": [{"lecture": 8, "status": "not_started"}]}
    acceptance = {"lectures": [{"lecture": 8, "status": "not_started"}]}

    action = tool.decide_next_action(
        args=args,
        root=REPO_ROOT,
        tracker=tracker,
        acceptance=acceptance,
        gate={"ready": False, "status": "waiting_for_finalize"},
    )

    assert action["status"] == "waiting_for_regression_fixture"
    assert action["lecture"] == 8


def test_mit6824_course_advance_requires_previous_human_acceptance() -> None:
    tool = _load_tool("mit6824_course_advance_prev", "tools/mit6824_course_advance.py")
    args = Namespace(
        lecture_start=8,
        lecture_end=20,
        allow_unaccepted_previous=False,
        python=sys.executable,
        tracker="tracker.json",
        acceptance_root="acceptance",
        finalizer_poll_sec=300.0,
        finalizer_timeout_sec=172800.0,
    )
    tracker = {
        "lectures": [
            {"lecture": 8, "status": "awaiting_human_acceptance"},
            {
                "lecture": 9,
                "title": "More Replication CRAQ",
                "status": "not_started",
                "source": {
                    "source_video_repo_relpath": "data/source_videos/mit-6.824-2020/lec-09 - More Replication CRAQ.mkv",
                    "source_file_present": True,
                    "duration_sec": 4713.028,
                },
            },
        ]
    }
    acceptance = {
        "lectures": [
            {"lecture": 8, "status": "awaiting_human_acceptance"},
            {"lecture": 9, "status": "not_started"},
        ]
    }

    action = tool.decide_next_action(
        args=args,
        root=REPO_ROOT,
        tracker=tracker,
        acceptance=acceptance,
        gate={"ready": True, "status": "ready"},
    )

    assert action["status"] == "waiting_for_human_acceptance"
    assert action["lecture"] == 8
    assert action["after_acceptance"]["status"] == "ready_to_start_after_acceptance"
    assert action["after_acceptance"]["lecture"] == 9
    assert action["after_acceptance"]["title"] == "More Replication CRAQ"
    assert action["after_acceptance"]["source_preflight"]["source_file_present"] is True
    assert action["after_acceptance"]["start_command"][-1] == "--execute"
    assert action["after_acceptance"]["start_dry_run_report"] == "acceptance/lec-09/start_dry_run_report.json"
    assert "--dry-run-output" in action["after_acceptance"]["dry_run_command"]
    assert "acceptance/lec-09/start_dry_run_report.json" in action["after_acceptance"]["dry_run_command"]
    assert "tools/mit6824_finalize_lecture.py" in action["after_acceptance"]["finalizer_command"]


def test_mit6824_course_advance_after_acceptance_preview_blocks_missing_source() -> None:
    tool = _load_tool("mit6824_course_advance_after_acceptance_source_block", "tools/mit6824_course_advance.py")
    args = Namespace(
        lecture_start=8,
        lecture_end=20,
        allow_unaccepted_previous=False,
        python=sys.executable,
        tracker="tracker.json",
        acceptance_root="acceptance",
        finalizer_poll_sec=300.0,
        finalizer_timeout_sec=172800.0,
    )
    tracker = {
        "lectures": [
            {"lecture": 8, "status": "awaiting_human_acceptance"},
            {
                "lecture": 9,
                "title": "More Replication CRAQ",
                "status": "not_started",
                "source": {
                    "source_video_repo_relpath": "data/source_videos/mit-6.824-2020/missing-lec-09.mkv",
                    "source_file_present": False,
                },
            },
        ]
    }
    acceptance = {
        "lectures": [
            {"lecture": 8, "status": "awaiting_human_acceptance"},
            {"lecture": 9, "status": "not_started"},
        ]
    }

    action = tool.decide_next_action(
        args=args,
        root=REPO_ROOT,
        tracker=tracker,
        acceptance=acceptance,
        gate={"ready": True, "status": "ready"},
    )

    preview = action["after_acceptance"]
    assert action["status"] == "waiting_for_human_acceptance"
    assert preview["status"] == "source_preflight_blocked_after_acceptance"
    assert preview["source_preflight"]["ready"] is False
    assert "source_file_missing" in preview["source_preflight"]["errors"]
    assert "start_command" not in preview
    assert "finalizer_command" not in preview


def test_mit6824_course_advance_starts_next_after_previous_acceptance() -> None:
    tool = _load_tool("mit6824_course_advance_after_acceptance", "tools/mit6824_course_advance.py")
    args = Namespace(
        lecture_start=8,
        lecture_end=20,
        allow_unaccepted_previous=False,
        python=sys.executable,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_root="acceptance",
        finalizer_poll_sec=300.0,
        finalizer_timeout_sec=172800.0,
    )
    tracker = {
        "lectures": [
            {"lecture": 8, "status": "episode_completed"},
            {
                "lecture": 9,
                "title": "More Replication CRAQ",
                "status": "not_started",
                "source": {
                    "source_video_repo_relpath": "data/source_videos/mit-6.824-2020/lec-09 - More Replication CRAQ.mkv",
                    "source_file_present": True,
                    "source_bytes": 1601235720,
                    "duration_sec": 4713.028,
                },
            },
        ]
    }
    acceptance = {
        "lectures": [
            {"lecture": 8, "status": "accepted", "human_accepted": True},
            {"lecture": 9, "status": "not_started"},
        ]
    }

    action = tool.decide_next_action(
        args=args,
        root=REPO_ROOT,
        tracker=tracker,
        acceptance=acceptance,
        gate={"ready": True, "status": "ready"},
    )

    assert action["status"] == "ready_to_start"
    assert action["lecture"] == 9
    assert action["source_preflight"]["ready"] is True
    assert action["start_command"][-1] == "--execute"
    assert action["start_dry_run_report"] == "acceptance/lec-09/start_dry_run_report.json"
    assert "--dry-run-output" in action["dry_run_command"]
    assert "acceptance/lec-09/start_dry_run_report.json" in action["dry_run_command"]
    assert "tools/mit6824_finalize_lecture.py" in action["finalizer_command"]
    assert "--tracker" in action["finalizer_command"]
    assert "tracker.json" in action["finalizer_command"]
    assert "--tracker-md" in action["finalizer_command"]
    assert "tracker.md" in action["finalizer_command"]
    assert "--acceptance-root" in action["finalizer_command"]
    assert "acceptance" in action["finalizer_command"]


def test_mit6824_course_advance_blocks_start_when_source_preflight_fails() -> None:
    tool = _load_tool("mit6824_course_advance_source_preflight", "tools/mit6824_course_advance.py")
    args = Namespace(
        lecture_start=8,
        lecture_end=20,
        allow_unaccepted_previous=False,
        python=sys.executable,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_root="acceptance",
        finalizer_poll_sec=300.0,
        finalizer_timeout_sec=172800.0,
    )
    tracker = {
        "lectures": [
            {"lecture": 8, "status": "episode_completed"},
            {
                "lecture": 9,
                "title": "More Replication CRAQ",
                "status": "not_started",
                "source": {
                    "source_video_repo_relpath": "data/source_videos/mit-6.824-2020/missing-lec-09.mkv",
                    "source_file_present": False,
                    "source_bytes": 0,
                    "duration_sec": None,
                },
            },
        ]
    }
    acceptance = {
        "lectures": [
            {"lecture": 8, "status": "accepted", "human_accepted": True},
            {"lecture": 9, "status": "not_started"},
        ]
    }

    action = tool.decide_next_action(
        args=args,
        root=REPO_ROOT,
        tracker=tracker,
        acceptance=acceptance,
        gate={"ready": True, "status": "ready"},
    )

    assert action["status"] == "waiting_for_source_preflight"
    assert action["lecture"] == 9
    assert action["source_preflight"]["ready"] is False
    assert "source_file_missing" in action["source_preflight"]["errors"]
    assert "start_command" not in action


def test_mit6824_course_advance_watch_stops_after_successful_start() -> None:
    tool = _load_tool("mit6824_course_advance_terminal_start", "tools/mit6824_course_advance.py")

    assert tool.terminal_watch_status(
        {
            "next_action": {"status": "ready_to_start", "lecture": 9},
            "execution": {"start_lecture": {"returncode": 0}},
        }
    ) is True
    assert tool.terminal_watch_status(
        {
            "next_action": {"status": "ready_to_start", "lecture": 9},
            "execution": {"start_lecture": {"returncode": 2}},
        }
    ) is True


def test_mit6824_accept_lecture_refuses_missing_artifacts(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_accept_lecture_missing", "tools/mit6824_accept_lecture.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    lecture_dir.mkdir(parents=True)
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "human_accepted": False,
                "artifacts": {
                    "final_video": "missing.mp4",
                    "final_audio": "missing.wav",
                    "final_mix_asr_report": "missing_asr.json",
                    "fit_audit": "missing_fit_audit.json",
                    "risk_queue": "missing_risk.json",
                    "human_review_pack": "missing_review/index.html",
                    "adapter_profile_provenance": "missing_provenance.json",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.accept_manifest(
        root=tmp_path,
        acceptance_root=tmp_path / "acceptance",
        lecture=8,
        reviewer="tester",
        notes="",
        risk_anchors=[],
        allow_missing_artifacts=False,
        force=False,
    )

    assert result["status"] == "missing_artifacts"
    assert set(result["missing"]) == set(tool.REQUIRED_ARTIFACTS)
    manifest = json.loads((lecture_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert manifest["human_accepted"] is False


def test_mit6824_accept_lecture_marks_manifest_when_artifacts_exist(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_accept_lecture_ok", "tools/mit6824_accept_lecture.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifact_paths = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifact_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
    actions = {
        "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8",
        "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8",
    }
    artifact_paths["final_mix_asr_report"].write_text(
        json.dumps({"summary": {"mean_similarity": 0.99}, "gate": {"pass": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["fit_audit"].write_text(
        json.dumps({"status": "pass", "summary": {"selected_fit": {}, "candidate_fit": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["risk_queue"].write_text(
        json.dumps({"summary": {"total_entries": 2}, "entries": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["adapter_profile_provenance"].write_text(
        json.dumps({"runtime": {"summary": {"candidate_audio_segments": 1}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (review_dir / "review_manifest.json").write_text(
        json.dumps({"review_rows": [{"sample_id": "seg1"}], "acceptance_actions": actions}, ensure_ascii=False),
        encoding="utf-8",
    )
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                "acceptance_actions": actions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.accept_manifest(
        root=tmp_path,
        acceptance_root=tmp_path / "acceptance",
        lecture=8,
        reviewer="tester",
        notes="sounds good",
        risk_anchors=["accepted ASR near-homophone"],
        allow_missing_artifacts=False,
        force=False,
    )

    assert result["status"] == "accepted"
    manifest = json.loads((lecture_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert manifest["human_accepted"] is True
    assert manifest["accepted_at"]
    assert manifest["human_acceptance"]["reviewer"] == "tester"
    assert manifest["human_acceptance"]["notes"] == "sounds good"
    assert manifest["human_acceptance"]["risk_queue_summary"] == {"total_entries": 2}
    assert manifest["human_acceptance"]["acceptance_readiness_snapshot"]["ready_for_human_review"] is True


def test_mit6824_accept_lecture_dry_run_does_not_write_manifest(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_accept_lecture_dry_run", "tools/mit6824_accept_lecture.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifact_paths = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifact_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
    actions = {
        "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8",
        "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8",
    }
    artifact_paths["final_mix_asr_report"].write_text(
        json.dumps({"summary": {"mean_similarity": 0.99}, "gate": {"pass": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["fit_audit"].write_text(
        json.dumps({"status": "pass", "summary": {"selected_fit": {}, "candidate_fit": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["risk_queue"].write_text(
        json.dumps({"summary": {"total_entries": 2}, "entries": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["adapter_profile_provenance"].write_text(
        json.dumps({"runtime": {"summary": {"candidate_audio_segments": 1}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (review_dir / "review_manifest.json").write_text(
        json.dumps({"review_rows": [{"sample_id": "seg1"}], "acceptance_actions": actions}, ensure_ascii=False),
        encoding="utf-8",
    )
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                "acceptance_actions": actions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.accept_manifest(
        root=tmp_path,
        acceptance_root=tmp_path / "acceptance",
        lecture=8,
        reviewer="tester",
        notes="sounds good",
        risk_anchors=[],
        allow_missing_artifacts=False,
        force=False,
        dry_run=True,
    )

    assert result["status"] == "dry_run_acceptance_ready"
    assert result["would_write_manifest"] is True
    assert result["risk_queue_summary"] == {"total_entries": 2}
    manifest = json.loads((lecture_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert manifest["human_accepted"] is False
    assert "human_acceptance" not in manifest


def test_mit6824_accept_lecture_refuses_invalid_content_readiness(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_accept_lecture_invalid_content", "tools/mit6824_accept_lecture.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifact_paths = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifact_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
    artifact_paths["final_mix_asr_report"].write_text("not json", encoding="utf-8")
    artifact_paths["fit_audit"].write_text(
        json.dumps({"status": "pass", "summary": {"selected_fit": {}, "candidate_fit": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["risk_queue"].write_text(json.dumps({"summary": {"total_entries": 1}}, ensure_ascii=False), encoding="utf-8")
    artifact_paths["adapter_profile_provenance"].write_text(json.dumps({"runtime": {}}, ensure_ascii=False), encoding="utf-8")
    (review_dir / "review_manifest.json").write_text(json.dumps({"review_rows": []}, ensure_ascii=False), encoding="utf-8")
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                "acceptance_actions": {
                    "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8",
                    "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.accept_manifest(
        root=tmp_path,
        acceptance_root=tmp_path / "acceptance",
        lecture=8,
        reviewer="tester",
        notes="",
        risk_anchors=[],
        allow_missing_artifacts=False,
        force=False,
    )

    assert result["status"] == "content_readiness_failed"
    assert "invalid_final_mix_asr_report_json" in result["acceptance_readiness"]["content_errors"]
    manifest = json.loads((lecture_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert manifest["human_accepted"] is False


def test_mit6824_accept_lecture_refuses_failed_fit_audit(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_accept_lecture_failed_fit", "tools/mit6824_accept_lecture.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifact_paths = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifact_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
    actions = {
        "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8",
        "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8",
    }
    artifact_paths["final_mix_asr_report"].write_text(
        json.dumps({"summary": {"mean_similarity": 0.99}, "gate": {"pass": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["fit_audit"].write_text(
        json.dumps(
            {
                "status": "fail",
                "recommendation": "repair_fit_policy_and_start_duration_prosody_training_feasibility",
                "summary": {
                    "selected_fit": {"gt_warning_threshold": 1},
                    "candidate_fit": {"gt_hard_threshold": 4},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_paths["risk_queue"].write_text(
        json.dumps({"summary": {"total_entries": 0}, "entries": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["adapter_profile_provenance"].write_text(
        json.dumps({"runtime": {"summary": {"candidate_audio_segments": 1}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (review_dir / "review_manifest.json").write_text(
        json.dumps({"review_rows": [{"sample_id": "seg1"}], "acceptance_actions": actions}, ensure_ascii=False),
        encoding="utf-8",
    )
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                "acceptance_actions": actions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.accept_manifest(
        root=tmp_path,
        acceptance_root=tmp_path / "acceptance",
        lecture=8,
        reviewer="tester",
        notes="",
        risk_anchors=[],
        allow_missing_artifacts=False,
        force=False,
    )

    assert result["status"] == "content_readiness_failed"
    assert "fit_audit_not_pass:fail" in result["acceptance_readiness"]["content_errors"]
    manifest = json.loads((lecture_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert manifest["human_accepted"] is False


def test_mit6824_accept_lecture_main_runs_accept_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    tool = _load_tool("mit6824_accept_lecture_main", "tools/mit6824_accept_lecture.py")
    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifact_paths = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifact_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
    actions = {
        "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8",
        "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8",
    }
    artifact_paths["final_mix_asr_report"].write_text(
        json.dumps({"summary": {"mean_similarity": 0.99}, "gate": {"pass": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["fit_audit"].write_text(
        json.dumps({"status": "pass", "summary": {"selected_fit": {}, "candidate_fit": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    artifact_paths["risk_queue"].write_text(json.dumps({"summary": {"total_entries": 0}, "entries": []}, ensure_ascii=False), encoding="utf-8")
    artifact_paths["adapter_profile_provenance"].write_text(
        json.dumps({"runtime": {"summary": {"candidate_audio_segments": 1}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (review_dir / "review_manifest.json").write_text(
        json.dumps({"review_rows": [{"sample_id": "seg1"}], "acceptance_actions": actions}, ensure_ascii=False),
        encoding="utf-8",
    )
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                "acceptance_actions": actions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "parse_args",
        lambda: Namespace(
            lecture=8,
            acceptance_root=str(tmp_path / "acceptance"),
            reviewer="tester",
            notes="accepted",
            risk_anchor=[],
            allow_missing_artifacts=False,
            allow_content_errors=False,
            force=False,
            refresh_status=False,
            auto_advance_next=False,
            tracker="tracker.json",
            tracker_md="tracker.md",
            acceptance_status="acceptance_status.json",
            acceptance_status_md="acceptance_status.md",
            course_advance_status="course_advance_status.json",
            course_advance_history="course_advance_history.jsonl",
            python=sys.executable,
        ),
    )

    assert tool.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "accepted"
    manifest = json.loads((lecture_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert manifest["human_accepted"] is True


def test_mit6824_accept_lecture_auto_advances_only_after_acceptance() -> None:
    tool = _load_tool("mit6824_accept_lecture_auto_advance", "tools/mit6824_accept_lecture.py")
    args = Namespace(
        auto_advance_next=True,
        python=sys.executable,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_root="acceptance",
        acceptance_status="acceptance_status.json",
        acceptance_status_md="acceptance_status.md",
        course_advance_status="course_advance.json",
        course_advance_history="course_advance.jsonl",
    )
    calls: list[tuple[Namespace, Path]] = []

    def fake_advance(_args, _root, **_kwargs):
        calls.append((_args, _root))
        return {"returncode": 0, "command": ["course_advance"], "stdout_tail": "started"}

    tool.run_course_advance = fake_advance

    skipped = tool.maybe_auto_advance_next(args, REPO_ROOT, {"status": "missing_artifacts"})
    advanced = tool.maybe_auto_advance_next(args, REPO_ROOT, {"status": "accepted"})

    assert skipped == {"skipped": True, "reason": "acceptance_status:missing_artifacts"}
    assert advanced["returncode"] == 0
    assert len(calls) == 1


def test_mit6824_accept_lecture_dry_run_auto_advance_uses_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _load_tool("mit6824_accept_lecture_dry_run_auto_advance", "tools/mit6824_accept_lecture.py")
    args = Namespace(
        auto_advance_next=True,
        dry_run=True,
        python=sys.executable,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_root="acceptance",
        acceptance_status="acceptance_status.json",
        acceptance_status_md="acceptance_status.md",
        course_advance_status="course_advance.json",
        course_advance_history="course_advance.jsonl",
    )
    calls: list[tuple[Namespace, Path]] = []

    def fake_preview(_args, _root, _result):
        calls.append((_args, _root))
        return {
            "returncode": 0,
            "command": ["course_advance_preview"],
            "preview": {"next_action": {"status": "ready_to_start", "lecture": 9}},
            "after_acceptance_preview": {"status": "ready_to_start_after_acceptance", "lecture": 9},
        }

    monkeypatch.setattr(tool, "run_course_advance_preview", fake_preview)

    skipped = tool.maybe_auto_advance_next(args, REPO_ROOT, {"status": "content_readiness_failed"})
    preview = tool.maybe_auto_advance_next(args, REPO_ROOT, {"status": "dry_run_acceptance_ready"})

    assert skipped == {"skipped": True, "reason": "dry_run_acceptance_status:content_readiness_failed"}
    assert preview["preview"]["next_action"]["status"] == "ready_to_start"
    assert preview["after_acceptance_preview"]["lecture"] == 9
    assert len(calls) == 1


def test_mit6824_accept_lecture_preview_has_no_real_advance_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool("mit6824_accept_lecture_preview_side_effects", "tools/mit6824_accept_lecture.py")
    args = Namespace(
        python=sys.executable,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_root="acceptance",
        acceptance_status="acceptance_status.json",
        acceptance_status_md="acceptance_status.md",
        course_advance_status="course_advance.json",
        course_advance_history="course_advance.jsonl",
    )
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        stdout = "{\"ok\": true}"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        output = Path(cmd[cmd.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "next_action": {
                        "status": "waiting_for_human_acceptance",
                        "after_acceptance": {"status": "ready_to_start_after_acceptance", "lecture": 9},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Proc()

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    result = tool.run_course_advance_preview(args, tmp_path)

    assert "--refresh" not in captured["cmd"]
    assert "--execute" not in captured["cmd"]
    assert "--spawn-finalizer" not in captured["cmd"]
    assert result["after_acceptance_preview"] == {"status": "ready_to_start_after_acceptance", "lecture": 9}
    assert result["preview_storage"]["ephemeral"] is True
    assert result["preview_storage"]["persisted"] is False
    assert "temporary_output" not in result


def test_mit6824_accept_lecture_preview_simulates_accepted_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool("mit6824_accept_lecture_preview_simulated_accept", "tools/mit6824_accept_lecture.py")
    acceptance_status = tmp_path / "acceptance_status.json"
    acceptance_status.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "status": "awaiting_human_acceptance",
                        "human_accepted": False,
                        "acceptance_readiness": {"ready_for_human_review": True},
                    },
                    {"lecture": 9, "status": "not_started", "human_accepted": False},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = Namespace(
        python=sys.executable,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_root="acceptance",
        acceptance_status=str(acceptance_status),
        acceptance_status_md="acceptance_status.md",
        course_advance_status="course_advance.json",
        course_advance_history="course_advance.jsonl",
    )
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        stdout = "{\"ok\": true}"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        simulated_status = Path(cmd[cmd.index("--acceptance-status") + 1])
        captured["simulated_status"] = json.loads(simulated_status.read_text(encoding="utf-8"))
        output = Path(cmd[cmd.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps({"next_action": {"status": "ready_to_start", "lecture": 9}}, ensure_ascii=False),
            encoding="utf-8",
        )
        return _Proc()

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    result = tool.run_course_advance_preview(
        args,
        tmp_path,
        {
            "status": "dry_run_acceptance_ready",
            "lecture": 8,
            "would_accept_at": "2026-06-24T00:00:00+00:00",
            "acceptance_manifest": "acceptance/lec-08/acceptance.json",
            "acceptance_readiness": {"ready_for_human_review": True},
        },
    )

    simulated_row = captured["simulated_status"]["lectures"][0]
    assert simulated_row["status"] == "accepted"
    assert simulated_row["human_accepted"] is True
    assert captured["simulated_status"]["summary"]["accepted"] == [8]
    assert captured["simulated_status"]["summary"]["ready_for_review"] == []
    assert captured["simulated_status"]["dry_run_preview"]["simulated_accepted_lecture"] == 8
    assert result["post_acceptance_next_action"] == {"status": "ready_to_start", "lecture": 9}
    assert result["after_acceptance_preview"] == {"status": "ready_to_start", "lecture": 9}
    assert result["acceptance_status_simulation"]["created"] is True
    assert "path" not in result["acceptance_status_simulation"]


def test_mit6824_accept_lecture_writes_dry_run_audit_report(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_accept_lecture_dry_run_output", "tools/mit6824_accept_lecture.py")
    args = Namespace(
        lecture=8,
        acceptance_root="acceptance",
        reviewer="tester",
        notes="accepted after review",
        risk_anchor=[],
        allow_missing_artifacts=False,
        allow_content_errors=False,
        auto_advance_next=True,
        tracker="tracker.json",
        acceptance_status="acceptance_status.json",
        course_advance_status="course_advance.json",
        dry_run_output="reports/acceptance_dry_run.json",
    )
    result = {
        "status": "dry_run_acceptance_ready",
        "lecture": 8,
        "auto_advance_next": {"post_acceptance_next_action": {"status": "ready_to_start", "lecture": 9}},
    }

    output = tool.maybe_write_dry_run_output(args, tmp_path, result)
    report = json.loads((tmp_path / "reports" / "acceptance_dry_run.json").read_text(encoding="utf-8"))

    assert output == {
        "path": "reports\\acceptance_dry_run.json",
        "schema_version": "holodub.mit6824.acceptance_dry_run_report.v1",
        "not_acceptance_state": True,
        "persisted": True,
    }
    assert report["not_acceptance_state"] is True
    assert report["side_effect_contract"]["acceptance_manifest_written"] is False
    assert report["side_effect_contract"]["next_lecture_started"] is False
    assert report["command_config"]["lecture"] == 8
    assert report["result"]["auto_advance_next"]["post_acceptance_next_action"]["lecture"] == 9


def test_mit6824_accept_lecture_rejects_dry_run_output_without_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool("mit6824_accept_lecture_dry_run_output_requires_flag", "tools/mit6824_accept_lecture.py")

    monkeypatch.setattr(tool, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "parse_args",
        lambda: Namespace(
            lecture=8,
            acceptance_root="acceptance",
            reviewer="tester",
            notes="accepted",
            risk_anchor=[],
            allow_missing_artifacts=False,
            allow_content_errors=False,
            force=False,
            dry_run=False,
            dry_run_output="reports/acceptance_dry_run.json",
            refresh_status=False,
            auto_advance_next=False,
            tracker="tracker.json",
            tracker_md="tracker.md",
            acceptance_status="acceptance_status.json",
            acceptance_status_md="acceptance_status.md",
            course_advance_status="course_advance_status.json",
            course_advance_history="course_advance_history.jsonl",
            python=sys.executable,
        ),
    )
    monkeypatch.setattr(
        tool,
        "accept_manifest",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("accept_manifest must not run")),
    )

    with pytest.raises(ValueError, match="--dry-run-output requires --dry-run"):
        tool.main()


def test_mit6824_start_lecture_writes_dry_run_audit_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _load_tool("mit6824_start_lecture_dry_run_output", "tools/mit6824_start_lecture.py")
    source = tmp_path / "data" / "source_videos" / "mit-6.824-2020" / "lec-09 - More Replication CRAQ.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source-video")
    tracker = {
        "lectures": [
            {
                "lecture": 9,
                "title": "More Replication CRAQ",
                "status": "not_started",
                "source": {
                    "title": "More Replication CRAQ",
                    "source_video_relpath": "source_videos/mit-6.824-2020/lec-09 - More Replication CRAQ.mkv",
                    "source_video_repo_relpath": "data/source_videos/mit-6.824-2020/lec-09 - More Replication CRAQ.mkv",
                    "source_video_abspath": str(source),
                    "source_file_present": True,
                    "source_bytes": source.stat().st_size,
                    "duration_sec": 4713.028,
                },
                "start_payload": {
                    "name": "mit6824-lec09-more-replication-craq-prod-v1",
                    "input_relpath": "source_videos/mit-6.824-2020/lec-09 - More Replication CRAQ.mkv",
                    "source_language": "en",
                    "target_language": "zh-CN",
                    "auto_start": True,
                    "max_retries": 0,
                    "config": {"course": "mit6824-2020", "lecture": 9, "lora_candidate_opt_in": True},
                },
            }
        ]
    }
    tracker_path = tmp_path / "tracker.json"
    tracker_path.write_text(json.dumps(tracker, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(tool, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "parse_args",
        lambda: Namespace(
            lecture=9,
            tracker=str(tracker_path),
            api_base="http://127.0.0.1:8080",
            api_key="",
            execute=False,
            dry_run_output="reports/start_lecture_09_dry_run.json",
            allow_non_not_started=False,
            block_active_episode_name_fragment=["lora-candidate-episode147-v25"],
            skip_active_run_check=False,
            postgres_service="postgres",
            postgres_user="holodub",
            postgres_db="holodub",
            compose_file=[],
        ),
    )

    assert tool.main() == 0
    output = json.loads(capsys.readouterr().out)
    report = json.loads((tmp_path / "reports" / "start_lecture_09_dry_run.json").read_text(encoding="utf-8"))

    assert output["status"] == "dry_run"
    assert output["dry_run_output"]["schema_version"] == "holodub.mit6824.start_dry_run_report.v1"
    assert output["payload"]["name"] == "mit6824-lec09-more-replication-craq-prod-v1"
    assert output["source"]["source_file_present"] is True
    assert report["not_start_state"] is True
    assert report["side_effect_contract"]["job_posted"] is False
    assert report["side_effect_contract"]["episode_created"] is False
    assert report["result"]["payload"]["config"]["lora_candidate_opt_in"] is True


def test_mit6824_start_lecture_rejects_dry_run_output_with_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool("mit6824_start_lecture_dry_run_output_requires_no_execute", "tools/mit6824_start_lecture.py")

    monkeypatch.setattr(tool, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "parse_args",
        lambda: Namespace(
            lecture=9,
            tracker="tracker.json",
            api_base="http://127.0.0.1:8080",
            api_key="",
            execute=True,
            dry_run_output="reports/start_lecture_09_dry_run.json",
            allow_non_not_started=False,
            block_active_episode_name_fragment=[],
            skip_active_run_check=False,
            postgres_service="postgres",
            postgres_user="holodub",
            postgres_db="holodub",
            compose_file=[],
        ),
    )

    with pytest.raises(ValueError, match="--dry-run-output is only valid without --execute"):
        tool.main()


def _write_acceptance_gate_fixture(tmp_path: Path, *, start_job_posted: bool = False) -> Namespace:
    acceptance_root = tmp_path / "acceptance"
    lec8 = acceptance_root / "lec-08"
    lec9 = acceptance_root / "lec-09"
    lec8.mkdir(parents=True)
    lec9.mkdir(parents=True)
    acceptance_status = {
        "lectures": [
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "awaiting_human_acceptance",
                "acceptance_readiness": {"ready_for_human_review": True, "content_errors": []},
            },
            {"lecture": 9, "title": "More Replication CRAQ", "status": "not_started"},
        ]
    }
    tracker = {
        "lectures": [
            {
                "lecture": 9,
                "title": "More Replication CRAQ",
                "status": "not_started",
                "source": {
                    "source_video_repo_relpath": "data/source_videos/mit-6.824-2020/lec-09 - More Replication CRAQ.mkv",
                    "source_file_present": True,
                    "source_bytes": 1601235720,
                    "duration_sec": 4713.028,
                },
            }
        ]
    }
    (tmp_path / "acceptance_status.json").write_text(json.dumps(acceptance_status, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "tracker.json").write_text(json.dumps(tracker, ensure_ascii=False), encoding="utf-8")
    (lec8 / "acceptance.json").write_text(
        json.dumps({"lecture": 8, "human_accepted": False}, ensure_ascii=False),
        encoding="utf-8",
    )
    (lec8 / "acceptance_dry_run_report.json").write_text(
        json.dumps(
            {
                "schema_version": "holodub.mit6824.acceptance_dry_run_report.v1",
                "not_acceptance_state": True,
                "side_effect_contract": {
                    "acceptance_manifest_written": False,
                    "course_advance_executed": False,
                    "finalizer_spawned": False,
                    "human_accepted_changed": False,
                    "next_lecture_started": False,
                },
                "result": {
                    "status": "dry_run_acceptance_ready",
                    "lecture": 8,
                    "acceptance_manifest": "acceptance/lec-08/acceptance.json",
                    "auto_advance_next": {
                        "post_acceptance_next_action": {
                            "status": "ready_to_start",
                            "lecture": 9,
                            "source_preflight": {"ready": True, "errors": []},
                            "start_dry_run_report": "acceptance/lec-09/start_dry_run_report.json",
                        }
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (lec9 / "start_dry_run_report.json").write_text(
        json.dumps(
            {
                "schema_version": "holodub.mit6824.start_dry_run_report.v1",
                "not_start_state": True,
                "side_effect_contract": {
                    "job_posted": start_job_posted,
                    "episode_created": False,
                    "tracker_written": False,
                    "finalizer_spawned": False,
                },
                "result": {
                    "status": "dry_run",
                    "lecture": 9,
                    "source": {"source_file_present": True},
                    "payload": {
                        "name": "mit6824-lec09-more-replication-craq-prod-v1",
                        "target_language": "zh-CN",
                        "config": {
                            "course": "mit6824-2020",
                            "lecture": 9,
                            "lora_candidate_opt_in": True,
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return Namespace(
        lecture=8,
        acceptance_root=str(acceptance_root),
        acceptance_status=str(tmp_path / "acceptance_status.json"),
        tracker=str(tmp_path / "tracker.json"),
        output_json=str(tmp_path / "gate.json"),
        output_md=str(tmp_path / "gate.md"),
    )


def test_mit6824_acceptance_gate_check_passes_with_accept_and_start_dry_runs(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_acceptance_gate_check_pass", "tools/mit6824_acceptance_gate_check.py")
    args = _write_acceptance_gate_fixture(tmp_path)

    gate = tool.build_gate(args, tmp_path)
    tool.write_markdown(tmp_path / "gate.md", gate)

    assert gate["pass"] is True
    assert gate["status"] == "pass"
    assert gate["lecture"] == 8
    assert gate["next_lecture"] == 9
    assert gate["evidence"]["start_payload_lora_opt_in"] is True
    assert "start_dry_run_report.json" in (tmp_path / "gate.md").read_text(encoding="utf-8")


def test_mit6824_acceptance_gate_check_fails_when_start_dry_run_has_side_effect(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_acceptance_gate_check_fail", "tools/mit6824_acceptance_gate_check.py")
    args = _write_acceptance_gate_fixture(tmp_path, start_job_posted=True)

    gate = tool.build_gate(args, tmp_path)

    assert gate["pass"] is False
    assert "side_effect_not_false:job_posted" in gate["summary"]["issue_codes"]


def test_mit6824_accept_lecture_course_advance_command_spawns_finalizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _load_tool("mit6824_accept_lecture_course_command", "tools/mit6824_accept_lecture.py")
    args = Namespace(
        python=sys.executable,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_root="acceptance",
        acceptance_status="acceptance_status.json",
        acceptance_status_md="acceptance_status.md",
        course_advance_status="course_advance.json",
        course_advance_history="course_advance.jsonl",
    )
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        stdout = "{\"ok\": true}"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    result = tool.run_course_advance(args, tmp_path)

    assert result["returncode"] == 0
    assert "--execute" in captured["cmd"]
    assert "--spawn-finalizer" in captured["cmd"]
    assert "--refresh" in captured["cmd"]
    assert "--tracker-md" in captured["cmd"]
    assert "tracker.md" in captured["cmd"]


def test_mit6824_accept_lecture_refresh_status_runs_full_course_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool("mit6824_accept_lecture_refresh_full", "tools/mit6824_accept_lecture.py")
    args = Namespace(
        python=sys.executable,
        tracker="tracker.json",
        acceptance_status="acceptance_status.json",
        acceptance_status_md="acceptance_status.md",
        course_advance_status="course_advance.json",
    )
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        stdout = "{\"ok\": true}"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    result = tool.refresh_status(args, tmp_path)

    assert result["returncode"] == 0
    assert "tools/mit6824_status_refresh.py" in captured["cmd"]
    assert "tools/mit6824_acceptance_status.py" not in captured["cmd"]
    assert "--acceptance-status" in captured["cmd"]
    assert "--course-advance-status" in captured["cmd"]


def test_mit6824_record_review_blockers_refresh_status_runs_full_course_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool("mit6824_record_review_blockers_refresh_full", "tools/mit6824_record_review_blockers.py")
    args = Namespace(
        python=sys.executable,
        tracker="tracker.json",
        acceptance_status="acceptance_status.json",
        acceptance_status_md="acceptance_status.md",
        course_advance_status="course_advance.json",
    )
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        stdout = "{\"ok\": true}"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    result = tool.refresh_status(args, tmp_path)

    assert result["returncode"] == 0
    assert "tools/mit6824_status_refresh.py" in captured["cmd"]
    assert "tools/mit6824_acceptance_status.py" not in captured["cmd"]
    assert "--acceptance-status" in captured["cmd"]
    assert "--course-advance-status" in captured["cmd"]


def test_mit6824_review_blockers_stop_advance_and_feed_training_decision(tmp_path: Path) -> None:
    record_tool = _load_tool("mit6824_record_review_blockers", "tools/mit6824_record_review_blockers.py")
    acceptance_tool = _load_tool("mit6824_acceptance_status_blocked", "tools/mit6824_acceptance_status.py")
    delivery_tool = _load_tool("mit6824_delivery_index_blocked", "tools/mit6824_delivery_index.py")
    advance_tool = _load_tool("mit6824_course_advance_blocked", "tools/mit6824_course_advance.py")
    training_tool = _load_tool("mit6824_training_decision_blocked", "tools/mit6824_training_decision.py")

    lecture_dir = tmp_path / "acceptance" / "lec-08"
    review_dir = lecture_dir / "human_review_pack"
    review_dir.mkdir(parents=True)
    artifacts = {
        "final_video": lecture_dir / "final.mp4",
        "final_audio": lecture_dir / "final.wav",
        "final_mix_asr_report": lecture_dir / "final_mix_asr_report.json",
        "fit_audit": lecture_dir / "fit_audit.json",
        "risk_queue": lecture_dir / "risk_queue.json",
        "human_review_pack": review_dir / "index.html",
        "adapter_profile_provenance": lecture_dir / "adapter_profile_provenance.json",
    }
    for path in artifacts.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
    artifacts["risk_queue"].write_text(json.dumps({"entries": []}, ensure_ascii=False), encoding="utf-8")
    artifacts["fit_audit"].write_text(
        json.dumps({"status": "pass", "summary": {"selected_fit": {}, "candidate_fit": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (lecture_dir / "acceptance.json").write_text(
        json.dumps(
            {
                "lecture": 8,
                "title": "Zookeeper",
                "human_accepted": False,
                "artifacts": {key: str(path) for key, path in artifacts.items()},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tracker = tmp_path / "tracker.json"
    tracker.write_text(
        json.dumps(
            {
                "lectures": [
                    {
                        "lecture": 8,
                        "title": "Zookeeper",
                        "status": "delivered",
                        "episodes": [{"id": 188, "status": "completed", "output_rel_path": "outputs/lec08.mp4"}],
                    },
                    {"lecture": 9, "title": "More Replication CRAQ", "status": "not_started", "episodes": []},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    record = record_tool.record_blockers(
        root=tmp_path,
        acceptance_root=tmp_path / "acceptance",
        lecture=8,
        reviewer="tester",
        notes="needs repair",
        blockers=["tail truncation missing ending"],
    )
    status = acceptance_tool.build_status(
        Namespace(
            tracker=str(tracker),
            acceptance_root=str(tmp_path / "acceptance"),
            output_json=str(tmp_path / "acceptance_status.json"),
            output_md=str(tmp_path / "acceptance_status.md"),
        ),
        tmp_path,
    )
    index = delivery_tool.build_index(
        root=tmp_path,
        acceptance_status=status,
        course_advance={"next_action": {"status": "waiting_for_review_blocker_repair"}},
        training_decision={"summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}},
        goal_status={"next_action": "continue_course_delivery"},
    )
    action = advance_tool.decide_next_action(
        args=Namespace(
            lecture_start=8,
            lecture_end=20,
            allow_unaccepted_previous=False,
            python=sys.executable,
            tracker=str(tracker),
            acceptance_root=str(tmp_path / "acceptance"),
            finalizer_poll_sec=300.0,
            finalizer_timeout_sec=172800.0,
        ),
        root=tmp_path,
        tracker=json.loads(tracker.read_text(encoding="utf-8")),
        acceptance=status,
        gate={"ready": True, "status": "ready"},
    )
    acceptance_status_path = tmp_path / "acceptance_status.json"
    acceptance_status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    decision = training_tool.build_decision(
        Namespace(
            acceptance_status=str(acceptance_status_path),
            output_json=str(tmp_path / "decision.json"),
            output_md=str(tmp_path / "decision.md"),
            min_reviewed_lectures=2,
            min_repeat_lectures=2,
            min_family_count=3,
            gold_audio_floor_minutes=30,
            gold_audio_promotion_hours=1.0,
            gold_audio_stable_hours=2.0,
        ),
        tmp_path,
    )

    assert record["status"] == "review_blocked"
    assert record["acceptance_readiness"]["ready_for_human_review"] is False
    assert "invalid_final_mix_asr_report_json" in record["acceptance_readiness"]["content_errors"]
    recorded_manifest = json.loads((lecture_dir / "acceptance.json").read_text(encoding="utf-8"))
    assert recorded_manifest["human_accepted"] is False
    assert recorded_manifest["accepted_at"] == ""
    assert recorded_manifest["human_review"]["status"] == "blocked"
    assert recorded_manifest["human_review"]["artifact_snapshot"]["final_video"]["present"] is True
    assert recorded_manifest["human_review"]["risk_queue_summary"]["total_entries"] == 0
    assert (
        recorded_manifest["human_review"]["acceptance_readiness_snapshot"]
        == record["acceptance_readiness"]
    )
    assert status["lectures"][0]["status"] == "review_blocked"
    assert status["summary"]["review_blocked"] == [8]
    assert index["rows"][0]["state"] == "blocked"
    assert action["status"] == "waiting_for_review_blocker_repair"
    assert decision["summary"]["family_counts"] == {"tail_truncation": 1}


def test_mit6824_regression_suite_does_not_score_drift_before_synthesis() -> None:
    tool = _load_tool("mit6824_regression_suite", "tools/mit6824_regression_suite.py")

    row = tool.anchor_status(
        10778,
        {
            "source_segment_id": 10778,
            "segment_id": 123,
            "status": "translated",
            "drift_ms": -40640,
            "runtime_fallback_reason": "",
        },
        max_drift_ms=500,
        allow_low_judge=True,
    )

    assert row["pass"] is False
    assert row["reject_reasons"] == ["not_synthesized:translated"]


def test_mit6824_regression_suite_treats_candidate_coverage_as_freeze_gate(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_regression_suite", "tools/mit6824_regression_suite.py")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "episode_candidate_monitor_final.json").write_text(
        json.dumps(
            {
                "progress": {"segments_remaining": 0, "segments_synthesized": 1, "segments_total": 1},
                "drift": {"high_drift_count": 0},
                "candidate": {"fallback_rows": []},
                "agent": {"hard_cap_count": 0, "low_judge_count": 0},
                "risk_queue": [],
                "validation_issues": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "finalize_summary.json").write_text(json.dumps({"completed": True}), encoding="utf-8")
    (run_dir / "auto_finalize_status.json").write_text(json.dumps({"completed": True}), encoding="utf-8")
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "summary": {
                    "hard_issue_count": 1,
                    "eligible_pending_segments": 0,
                    "eligible_candidate_audio_segments": 9,
                    "eligible_synthesized_segments": 10,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    suite = tool.build_suite(
        Namespace(
            run_dir=str(run_dir),
            suite_name="test",
            source_episode_id=147,
            shadow_episode_id=179,
            anchor_source_segments="",
            candidate_coverage=str(coverage),
            output_json=str(tmp_path / "suite.json"),
            output_md=str(tmp_path / "suite.md"),
            freeze=False,
            max_drift_ms=500.0,
            max_review_drift_ms=300.0,
            fail_low_judge=False,
            no_db=True,
            postgres_service="postgres",
            postgres_user="holodub",
            postgres_db="holodub",
            compose_file=[],
        ),
        REPO_ROOT,
    )

    assert "candidate_coverage_hard_issues:1" in suite["hard_issues"]
    assert suite["summary"]["candidate_coverage_hard_issue_count"] == 1


def test_mit6824_goal_status_classifies_active_regression_health() -> None:
    tool = _load_tool("mit6824_goal_status", "tools/mit6824_goal_status.py")
    args = Namespace(slow_progress_sec=900, stale_progress_sec=1800)

    recent = tool.classify_active_regression_health(
        {
            "metrics": {"total": "180", "synthesized": "64", "since_latest_synth_sec": "30"},
            "job_breakdown": [{"id": "344", "status": "running"}],
        },
        args,
    )
    slow = tool.classify_active_regression_health(
        {
            "metrics": {"total": "180", "synthesized": "64", "since_latest_synth_sec": "1200"},
            "job_breakdown": [{"id": "344", "status": "running"}],
        },
        args,
    )
    stale = tool.classify_active_regression_health(
        {
            "metrics": {"total": "180", "synthesized": "64", "since_latest_synth_sec": "2400"},
            "job_breakdown": [{"id": "344", "status": "running"}],
        },
        args,
    )

    assert recent["status"] == "running_recent"
    assert slow["status"] == "running_slow"
    assert stale["status"] == "stale_needs_diagnosis"


@pytest.mark.parametrize(
    ("health_status", "expected_action"),
    [
        ("running_recent", "wait_for_v25_candidate_regression"),
        ("running_slow", "monitor_v25_candidate_regression_slow"),
        ("stale_needs_diagnosis", "diagnose_v25_candidate_regression_stale_needs_diagnosis"),
        ("no_running_job", "diagnose_v25_candidate_regression_no_running_job"),
        ("error", "diagnose_v25_candidate_regression_error"),
    ],
)
def test_mit6824_goal_status_next_action_respects_regression_health(
    health_status: str, expected_action: str
) -> None:
    tool = _load_tool(f"mit6824_goal_status_next_{health_status}", "tools/mit6824_goal_status.py")

    action = tool.next_action(
        {
            "active_regression": {
                "metrics": {"total": "180", "synthesized": "88"},
                "health": {"status": health_status},
            }
        }
    )

    assert action == expected_action


def test_mit6824_goal_status_records_lecture_8_as_next_production_start() -> None:
    tool = _load_tool("mit6824_goal_status", "tools/mit6824_goal_status.py")
    tracker = {
        "summary": {"by_status": {"not_started": 13}, "first_not_started": 8},
        "lectures": [{"lecture": lecture, "status": "not_started"} for lecture in range(8, 21)],
    }
    acceptance = {"summary": {"by_status": {"not_started": 13}, "not_started": list(range(8, 21)), "accepted": []}}

    state = tool.course_delivery_state(tracker, acceptance)

    assert state["target_lectures"] == [8, 20]
    assert state["next_production_lecture"] == 8
    assert state["not_started_lectures"] == list(range(8, 21))
    assert state["delivery_start_rule"] == "start_lecture_8_after_lecture_7_regression_fixture_freezes"
    assert state["production_delivery_range"]["lectures"] == list(range(8, 21))
    assert state["lecture_7_role"] == "regression_fixture_only_not_a_delivery_target"


def test_mit6824_goal_status_preserves_real_objective_and_no_db_markdown(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_goal_status_no_db", "tools/mit6824_goal_status.py")
    output = tmp_path / "goal.md"
    status = {
        "generated_at": "2026-06-24T00:00:00+00:00",
        "objective": tool.TARGET_OBJECTIVE,
        "next_action": "wait_for_lecture_8_episode_or_completion",
        "course_tracker_summary": {"by_status": {"in_progress": 1, "not_started": 12}},
        "acceptance_status": {"summary": {"by_status": {"in_progress": 1, "not_started": 12}}},
        "course_delivery_state": {
            "current_production_frontier": 8,
            "not_started_lectures": list(range(9, 21)),
            "lecture_7_role": "regression_fixture_only_not_a_delivery_target",
            "delivery_start_rule": "start_lecture_8_after_lecture_7_regression_fixture_freezes",
        },
        "next_not_started_lecture": 9,
        "active_regression": {"enabled": False, "reason": "no_db_or_no_job_ids", "job_ids": []},
        "course_advance_status": {},
        "auto_advance_status": {},
        "lecture_finalize_status": {},
        "v25_finalize_status": {},
        "training_decision": {
            "summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}
        },
    }

    tool.write_markdown(output, status)

    text = output.read_text(encoding="utf-8")
    assert tool.TARGET_OBJECTIVE == "MIT 6.824 lectures 8-20 dubbing and acceptance; lecture 7 is regression fixture only"
    assert tool.TARGET_OBJECTIVE in text
    assert "Current production frontier: `8`" in text
    assert "Lecture 7 role: `regression_fixture_only_not_a_delivery_target`" in text


def test_mit6824_candidate_coverage_flags_eligible_synthesized_without_candidate_runtime() -> None:
    tool = _load_tool("mit6824_candidate_coverage", "tools/mit6824_candidate_coverage.py")
    args = Namespace(
        job_ids="1",
        min_target_sec=18.0,
        min_text_chars=180,
        dense_min_target_sec=9.0,
        dense_min_chars_per_sec=4.9,
        near_long_min_target_sec=16.0,
        near_long_min_text_chars=70,
        short_dense_min_target_sec=6.0,
        short_dense_max_target_sec=9.0,
        short_dense_min_chars_per_sec=5.5,
        short_transition_min_target_sec=5.0,
        short_transition_max_target_sec=10.0,
        short_transition_min_chars_per_sec=5.0,
        short_term_number_min_target_sec=6.0,
        short_term_number_max_target_sec=9.0,
        short_term_number_min_chars_per_sec=4.8,
    )
    rows = [
        {
            "job_id": 1,
            "segment_id": 10,
            "ordinal": 1,
            "status": "synthesized",
            "original_duration_ms": 20000,
            "tts_duration_ms": 19990,
            "target_text": "长句",
            "runtime_execution_requested": "",
            "runtime_audio_selection_mode": "",
            "runtime_selected_family": "",
            "runtime_fallback_reason": "",
        },
        {
            "job_id": 1,
            "segment_id": 11,
            "ordinal": 2,
            "status": "translated",
            "original_duration_ms": 19000,
            "tts_duration_ms": 0,
            "target_text": "待合成长句",
            "runtime_execution_requested": "",
            "runtime_audio_selection_mode": "",
            "runtime_selected_family": "",
            "runtime_fallback_reason": "",
        },
        {
            "job_id": 1,
            "segment_id": 12,
            "ordinal": 3,
            "status": "synthesized",
            "original_duration_ms": 8000,
            "tts_duration_ms": 8010,
            "target_text": "短句",
            "runtime_execution_requested": "",
            "runtime_audio_selection_mode": "",
            "runtime_selected_family": "",
            "runtime_fallback_reason": "",
        },
    ]

    report = tool.build_report(rows, args)

    assert report["summary"]["eligible_segments"] == 2
    assert report["summary"]["eligible_missing_candidate_runtime"] == 1
    assert report["summary"]["eligible_pending_segments"] == 1
    assert report["hard_rows"][0]["segment_id"] == 10
    assert report["hard_rows"][0]["issue"] == "eligible_synthesized_missing_candidate_runtime"


def test_mit6824_candidate_coverage_surfaces_pending_fallback_risk() -> None:
    tool = _load_tool("mit6824_candidate_coverage_pending_fallback", "tools/mit6824_candidate_coverage.py")
    args = Namespace(
        job_ids="1",
        min_target_sec=18.0,
        min_text_chars=180,
        dense_min_target_sec=9.0,
        dense_min_chars_per_sec=4.9,
        near_long_min_target_sec=16.0,
        near_long_min_text_chars=70,
        short_dense_min_target_sec=6.0,
        short_dense_max_target_sec=9.0,
        short_dense_min_chars_per_sec=5.5,
        short_transition_min_target_sec=5.0,
        short_transition_max_target_sec=10.0,
        short_transition_min_chars_per_sec=5.0,
        short_term_number_min_target_sec=6.0,
        short_term_number_max_target_sec=9.0,
        short_term_number_min_chars_per_sec=4.8,
    )
    rows = [
        {
            "job_id": 1,
            "segment_id": 15827,
            "ordinal": 17,
            "status": "translated",
            "original_duration_ms": 44900,
            "tts_duration_ms": 0,
            "target_text": "闀垮彞" * 220,
            "runtime_execution_requested": "true",
            "runtime_audio_selection_mode": "default_audio",
            "runtime_selected_family": "default",
            "runtime_fallback_reason": "all_candidates_rejected",
        }
    ]

    report = tool.build_report(rows, args)

    assert report["summary"]["eligible_pending_fallback_segments"] == 1
    assert report["fallback_pending_rows"][0]["segment_id"] == 15827
    assert report["fallback_pending_rows"][0]["issue"] == "eligible_pending_fallback:all_candidates_rejected"
    assert report["hard_rows"] == []


def test_mit6824_candidate_coverage_classifies_synthesized_fallback_separately() -> None:
    tool = _load_tool("mit6824_candidate_coverage_synth_fallback", "tools/mit6824_candidate_coverage.py")
    args = Namespace(
        job_ids="1",
        min_target_sec=18.0,
        min_text_chars=180,
        dense_min_target_sec=9.0,
        dense_min_chars_per_sec=4.9,
        near_long_min_target_sec=16.0,
        near_long_min_text_chars=70,
        short_dense_min_target_sec=6.0,
        short_dense_max_target_sec=9.0,
        short_dense_min_chars_per_sec=5.5,
        short_transition_min_target_sec=5.0,
        short_transition_max_target_sec=10.0,
        short_transition_min_chars_per_sec=5.0,
        short_term_number_min_target_sec=6.0,
        short_term_number_max_target_sec=9.0,
        short_term_number_min_chars_per_sec=4.8,
    )
    rows = [
        {
            "job_id": 349,
            "segment_id": 15966,
            "ordinal": 1,
            "status": "synthesized",
            "original_duration_ms": 36070,
            "tts_duration_ms": 32104,
            "target_text": "long technical segment" * 20,
            "runtime_execution_requested": "false",
            "runtime_audio_selection_mode": "default_audio",
            "runtime_selected_family": "default",
            "runtime_fallback_reason": "candidate_attempt_limit_reached",
        }
    ]

    report = tool.build_report(rows, args)

    assert report["summary"]["eligible_missing_candidate_runtime"] == 0
    assert report["summary"]["eligible_candidate_fallback_segments"] == 1
    assert report["summary"]["hard_issue_count"] == 1
    assert report["hard_rows"][0]["segment_id"] == 15966
    assert report["hard_rows"][0]["issue"] == "eligible_candidate_fallback:candidate_attempt_limit_reached"


def test_mit6824_delivery_index_marks_lecture_8_waiting_for_regression_fixture(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_delivery_index", "tools/mit6824_delivery_index.py")
    acceptance = {
        "lectures": [
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "not_started",
                "missing": ["final_video"],
                "artifacts": {
                    "final_video": {"present": False, "path": "", "size_bytes": 0},
                    "human_review_pack": {"present": False, "path": "data/validation/mit6824_acceptance/lec-08/human_review_pack/index.html"},
                    "final_mix_asr_report": {"present": False, "path": "data/validation/mit6824_acceptance/lec-08/final_mix_asr_report.json"},
                    "risk_queue": {"present": False, "path": "data/validation/mit6824_acceptance/lec-08/risk_queue.json"},
                    "adapter_profile_provenance": {"present": False, "path": "data/validation/mit6824_acceptance/lec-08/adapter_profile_provenance.json"},
                },
            },
            {
                "lecture": 9,
                "title": "More Replication CRAQ",
                "status": "not_started",
                "missing": [],
                "artifacts": {},
            },
        ]
    }
    index = tool.build_index(
        root=tmp_path,
        acceptance_status=acceptance,
        course_advance={"next_action": {"status": "waiting_for_regression_fixture"}},
        training_decision={"summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}},
        goal_status={"next_action": "wait_for_v25_candidate_regression"},
    )

    assert index["summary"]["next_not_started"] == 8
    assert index["rows"][0]["state"] == "next_to_start"
    assert index["rows"][0]["automation_action"] == "wait_for_lecture_7_regression_freeze_then_auto_start"
    assert index["rows"][1]["state"] == "queued"


def test_mit6824_delivery_index_propagates_acceptance_actions(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_delivery_index_actions", "tools/mit6824_delivery_index.py")
    acceptance = {
        "lectures": [
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "awaiting_human_acceptance",
                "missing": [],
                "acceptance_actions": {
                    "accept_dry_run_report": "data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json",
                    "accept_dry_run_command": "python tools\\mit6824_accept_lecture.py --lecture 8 --reviewer user --notes custom --refresh-status --auto-advance-next --dry-run --dry-run-output data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json",
                    "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8 --reviewer user --notes custom --refresh-status --auto-advance-next",
                    "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8 --reviewer user --notes custom --blocker custom --refresh-status",
                },
                "artifacts": {
                    "final_video": {"present": True, "path": "data/episodes/180/output/vp0/final.mp4", "size_bytes": 1},
                    "human_review_pack": {"present": True, "path": "data/validation/mit6824_acceptance/lec-08/human_review_pack/index.html", "size_bytes": 1},
                    "final_mix_asr_report": {"present": True, "path": "data/validation/mit6824_acceptance/lec-08/final_mix_asr_report.json", "size_bytes": 1},
                    "risk_queue": {"present": True, "path": "data/validation/mit6824_acceptance/lec-08/risk_queue.json", "size_bytes": 1},
                    "adapter_profile_provenance": {"present": True, "path": "data/validation/mit6824_acceptance/lec-08/adapter_profile_provenance.json", "size_bytes": 1},
                },
            }
        ]
    }

    index = tool.build_index(
        root=tmp_path,
        acceptance_status=acceptance,
        course_advance={"next_action": {"status": "waiting_for_human_acceptance"}},
        training_decision={"summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}},
        goal_status={"next_action": "human_review_ready_lectures"},
    )
    md_path = tmp_path / "delivery.md"
    tool.write_markdown(md_path, index)

    row = index["rows"][0]
    md = md_path.read_text(encoding="utf-8")
    assert row["state"] == "review_ready"
    assert row["accept_dry_run_report"] == acceptance["lectures"][0]["acceptance_actions"]["accept_dry_run_report"]
    assert row["accept_dry_run_command"] == acceptance["lectures"][0]["acceptance_actions"]["accept_dry_run_command"]
    assert row["accept_command"] == acceptance["lectures"][0]["acceptance_actions"]["accept_command"]
    assert row["blocker_command"] == acceptance["lectures"][0]["acceptance_actions"]["blocker_command"]
    assert row["acceptance_actions"] == acceptance["lectures"][0]["acceptance_actions"]
    assert "acceptance_dry_run_report.json" in md


def test_mit6824_delivery_index_does_not_mark_next_lecture_startable_before_acceptance(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_delivery_index_gated_next", "tools/mit6824_delivery_index.py")
    acceptance = {
        "lectures": [
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "awaiting_human_acceptance",
                "missing": [],
                "acceptance_readiness": {"ready_for_human_review": True},
                "artifacts": {
                    "human_review_pack": {
                        "present": True,
                        "path": "data/validation/mit6824_acceptance/lec-08/human_review_pack/index.html",
                        "size_bytes": 1,
                    }
                },
            },
            {"lecture": 9, "title": "More Replication CRAQ", "status": "not_started", "missing": [], "artifacts": {}},
        ]
    }
    course_advance = {
        "next_action": {
            "status": "waiting_for_human_acceptance",
            "lecture": 8,
            "after_acceptance": {"status": "ready_to_start_after_acceptance", "lecture": 9},
        }
    }

    index = tool.build_index(
        root=tmp_path,
        acceptance_status=acceptance,
        course_advance=course_advance,
        training_decision={"summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}},
        goal_status={"next_action": "human_review_ready_lectures"},
    )

    lecture8 = next(row for row in index["rows"] if row["lecture"] == 8)
    lecture9 = next(row for row in index["rows"] if row["lecture"] == 9)
    assert lecture8["state"] == "review_ready"
    assert lecture9["state"] == "waiting_previous_acceptance"
    assert lecture9["automation_action"] == "wait_for_previous_lecture_acceptance"
    assert index["summary"]["after_acceptance"] == {"status": "ready_to_start_after_acceptance", "lecture": 9}
    assert index["summary"]["state_counts"] == {"review_ready": 1, "waiting_previous_acceptance": 1}


def test_mit6824_delivery_index_marks_source_preflight_block(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_delivery_index_source_blocked", "tools/mit6824_delivery_index.py")
    acceptance = {
        "lectures": [
            {"lecture": 8, "title": "Zookeeper", "status": "accepted", "missing": [], "artifacts": {}},
            {"lecture": 9, "title": "More Replication CRAQ", "status": "not_started", "missing": [], "artifacts": {}},
        ]
    }

    index = tool.build_index(
        root=tmp_path,
        acceptance_status=acceptance,
        course_advance={
            "next_action": {
                "status": "waiting_for_source_preflight",
                "lecture": 9,
                "source_preflight": {"ready": False, "errors": ["source_file_missing"]},
            }
        },
        training_decision={"summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}},
        goal_status={"next_action": "continue_course_delivery"},
    )

    lecture9 = next(row for row in index["rows"] if row["lecture"] == 9)
    assert lecture9["state"] == "source_blocked"
    assert lecture9["automation_action"] == "add_or_fix_source_video_then_refresh_tracker"
    assert index["summary"]["source_blocked"] == [9]


def test_mit6824_delivery_index_blocks_accept_when_content_not_ready(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_delivery_index_content_blocked", "tools/mit6824_delivery_index.py")
    acceptance = {
        "lectures": [
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "awaiting_human_acceptance",
                "missing": [],
                "acceptance_readiness": {
                    "ready_for_human_review": False,
                    "content_errors": ["invalid_final_mix_asr_report_json"],
                    "content_warnings": [],
                },
                "acceptance_actions": {
                    "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8",
                    "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8",
                },
                "artifacts": {
                    "final_video": {"present": True, "path": "data/episodes/180/output/vp0/final.mp4", "size_bytes": 1},
                    "human_review_pack": {"present": True, "path": "data/validation/mit6824_acceptance/lec-08/human_review_pack/index.html", "size_bytes": 1},
                },
            }
        ]
    }

    index = tool.build_index(
        root=tmp_path,
        acceptance_status=acceptance,
        course_advance={"next_action": {"status": "waiting_for_human_acceptance"}},
        training_decision={"summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}},
        goal_status={"next_action": "human_review_ready_lectures"},
    )

    row = index["rows"][0]
    assert row["state"] == "content_blocked"
    assert row["accept_command"] == ""
    assert row["blocker_command"] == ""
    assert row["acceptance_readiness"]["content_errors"] == ["invalid_final_mix_asr_report_json"]
    assert index["summary"]["review_ready"] == []
    assert index["summary"]["content_blocked"] == [8]


def test_mit6824_delivery_index_does_not_start_lecture_8_when_regression_needs_diagnosis() -> None:
    tool = _load_tool("mit6824_delivery_index_diagnose", "tools/mit6824_delivery_index.py")

    action = tool.delivery_action(
        {"lecture": 8, "status": "not_started", "missing": []},
        next_not_started=8,
        course_next_action="diagnose_v25_candidate_regression_stale_needs_diagnosis",
    )

    assert action["state"] == "regression_needs_diagnosis"
    assert action["human_action"] == "inspect_v25_candidate_regression"
    assert action["automation_action"] == "diagnose_v25_candidate_regression_stale_needs_diagnosis"
    assert "mit6824_start_lecture.py" not in action["automation_action"]


def test_mit6824_course_tracker_uses_acceptance_provenance_key(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_course_tracker_required_artifacts", "tools/mit6824_course_tracker.py")
    source_dir = tmp_path / "data" / "source_videos" / "mit-6.824-2020"
    source_dir.mkdir(parents=True)
    (source_dir / "lec-08 - Zookeeper.mkv").write_bytes(b"fake")
    args = Namespace(
        source_dir=str(source_dir),
        lecture_start=8,
        lecture_end=8,
        no_db=True,
        postgres_service="postgres",
        postgres_user="holodub",
        postgres_db="holodub",
        compose_file=[],
    )

    tracker = tool.build_tracker(args, tmp_path)
    lecture = tracker["lectures"][0]

    assert lecture["status"] == "not_started"
    assert lecture["source_preflight"]["ready"] is True
    assert tracker["summary"]["source_preflight"]["ready"] == [8]
    assert tracker["summary"]["source_preflight"]["blocked"] == []
    assert lecture["start_payload"]["target_language"] == "zh-CN"
    assert lecture["start_payload"]["config"]["lora_candidate_opt_in"] is True
    assert "fit_audit" in lecture["acceptance_artifacts_required"]
    assert "adapter_profile_provenance" in lecture["acceptance_artifacts_required"]
    assert "adapter_or_profile_provenance" not in lecture["acceptance_artifacts_required"]


def test_mit6824_course_tracker_source_preflight_blocks_missing_source(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_course_tracker_source_preflight", "tools/mit6824_course_tracker.py")
    source_dir = tmp_path / "data" / "source_videos" / "mit-6.824-2020"
    source_dir.mkdir(parents=True)
    args = Namespace(
        source_dir=str(source_dir),
        lecture_start=8,
        lecture_end=9,
        no_db=True,
        postgres_service="postgres",
        postgres_user="holodub",
        postgres_db="holodub",
        compose_file=[],
    )

    tracker = tool.build_tracker(args, tmp_path)

    assert tracker["summary"]["ready_to_start"] == []
    assert tracker["summary"]["first_not_started"] is None
    assert tracker["summary"]["source_preflight"]["blocked"] == [8, 9]
    assert tracker["summary"]["source_preflight"]["by_error"] == {"source_file_missing": 2}
    assert tracker["lectures"][0]["source_preflight"]["errors"] == ["source_file_missing"]


def test_mit6824_course_tracker_completed_episode_is_not_delivered_until_acceptance() -> None:
    tool = _load_tool("mit6824_course_tracker_completed_pending_acceptance", "tools/mit6824_course_tracker.py")

    status, next_action = tool.classify_lecture(
        {"lecture": 8, "source_file_present": True},
        [{"id": "180", "status": "completed", "output_rel_path": "episodes/180/output/vp0/final.mp4"}],
    )

    assert status == "episode_completed"
    assert next_action == "finalize_acceptance_artifacts"


def test_mit6824_course_tracker_latest_rerun_overrides_old_completed_episode() -> None:
    tool = _load_tool("mit6824_course_tracker_latest_rerun", "tools/mit6824_course_tracker.py")

    status, next_action = tool.classify_lecture(
        {"lecture": 8, "source_file_present": True},
        [
            {"id": "180", "status": "completed", "output_rel_path": "episodes/180/output/vp0/final.mp4"},
            {"id": "181", "status": "pending", "output_rel_path": ""},
        ],
    )

    assert status == "in_progress"
    assert next_action == "monitor_or_finalize_episode"


def test_mit6824_course_tracker_treats_dispatched_episode_as_in_progress() -> None:
    tool = _load_tool("mit6824_course_tracker_dispatched", "tools/mit6824_course_tracker.py")

    status, next_action = tool.classify_lecture(
        {"lecture": 8, "source_file_present": True},
        [{"id": "183", "status": "dispatched", "output_rel_path": ""}],
    )

    assert status == "in_progress"
    assert next_action == "monitor_or_finalize_episode"


def test_mit6824_watchdog_reports_all_expected_watchers_running() -> None:
    tool = _load_tool("mit6824_watchdog_all_running", "tools/mit6824_watchdog.py")
    args = Namespace(status_watch_poll_sec=300.0, status_watch_timeout_sec=43200.0)
    rows = [
        {"pid": 1, "command_line": "python data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\overnight_watch_v25.py"},
        {"pid": 2, "command_line": "python data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\auto_finalize_loop.py"},
        {"pid": 3, "command_line": LECTURE8_AUTO_ADVANCE_WATCH_COMMAND},
        {"pid": 4, "command_line": "python tools\\mit6824_finalize_lecture.py --lecture 8 --watch"},
        {"pid": 5, "command_line": "python tools\\mit6824_status_refresh.py --watch --poll-sec 300"},
    ]

    report = tool.build_report(args=args, root=REPO_ROOT, rows=rows)

    assert report["summary"]["status"] == "ok"
    assert report["summary"]["missing_required"] == []
    assert report["summary"]["running_or_restarted"] == 5


def test_mit6824_watchdog_can_mark_status_refresh_restarted() -> None:
    tool = _load_tool("mit6824_watchdog_restarted", "tools/mit6824_watchdog.py")
    args = Namespace(status_watch_poll_sec=300.0, status_watch_timeout_sec=43200.0)
    rows = [
        {"pid": 1, "command_line": "python data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\overnight_watch_v25.py"},
        {"pid": 2, "command_line": "python data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\auto_finalize_loop.py"},
        {"pid": 3, "command_line": LECTURE8_AUTO_ADVANCE_WATCH_COMMAND},
        {"pid": 4, "command_line": "python tools\\mit6824_finalize_lecture.py --lecture 8 --watch"},
    ]

    report = tool.build_report(args=args, root=REPO_ROOT, rows=rows, restarted={"pid": 55})

    assert report["summary"]["status"] == "ok"
    assert report["summary"]["status_refresh_restarted"] is True
    status_row = next(row for row in report["watchers"] if row["id"] == "status_refresh_watch")
    assert status_row["status"] == "restarted"
    assert status_row["pids"] == [55]


def test_mit6824_watchdog_does_not_auto_repair_production_watchers() -> None:
    tool = _load_tool("mit6824_watchdog_missing_production", "tools/mit6824_watchdog.py")
    args = Namespace(status_watch_poll_sec=300.0, status_watch_timeout_sec=43200.0)
    rows = [
        {"pid": 5, "command_line": "python tools\\mit6824_status_refresh.py --watch --poll-sec 300"},
    ]

    report = tool.build_report(args=args, root=REPO_ROOT, rows=rows)

    assert report["summary"]["status"] == "needs_attention"
    assert "v25_watcher" in report["summary"]["missing_required"]
    assert "lecture8_auto_advance" in report["summary"]["missing_required"]
    missing_v25 = next(row for row in report["watchers"] if row["id"] == "v25_watcher")
    assert missing_v25["auto_repair"] is False
    assert "overnight_watch_v25.py" in missing_v25["restart_command"]


def test_mit6824_watchdog_stops_requiring_completed_handoff_watchers() -> None:
    tool = _load_tool("mit6824_watchdog_lifecycle", "tools/mit6824_watchdog.py")
    args = Namespace(status_watch_poll_sec=300.0, status_watch_timeout_sec=43200.0)
    rows = [
        {"pid": 4, "command_line": "python tools\\mit6824_finalize_lecture.py --lecture 8 --watch"},
        {"pid": 5, "command_line": "python tools\\mit6824_status_refresh.py --watch --poll-sec 300"},
    ]

    report = tool.build_report(
        args=args,
        root=REPO_ROOT,
        rows=rows,
        lifecycle={"v25_finalized": True, "lecture8_started": True, "lecture8_status": "in_progress"},
    )

    assert report["summary"]["status"] == "ok"
    assert report["summary"]["missing_required"] == []
    v25_finalizer = next(row for row in report["watchers"] if row["id"] == "v25_auto_finalizer")
    lecture8_auto = next(row for row in report["watchers"] if row["id"] == "lecture8_auto_advance")
    assert v25_finalizer["required"] is False
    assert lecture8_auto["required"] is False


def test_mit6824_watchdog_does_not_require_finalizer_after_human_review_ready() -> None:
    tool = _load_tool("mit6824_watchdog_review_ready", "tools/mit6824_watchdog.py")
    args = Namespace(status_watch_poll_sec=300.0, status_watch_timeout_sec=43200.0)
    rows = [
        {"pid": 5, "command_line": "python tools\\mit6824_status_refresh.py --watch --poll-sec 300"},
    ]

    report = tool.build_report(
        args=args,
        root=REPO_ROOT,
        rows=rows,
        lifecycle={
            "v25_finalized": True,
            "lecture8_started": True,
            "lecture8_status": "episode_completed",
            "lecture8_acceptance_status": "awaiting_human_acceptance",
        },
    )

    assert report["summary"]["status"] == "ok"
    assert report["summary"]["missing_required"] == []
    finalizer = next(row for row in report["watchers"] if row["id"] == "lecture8_finalizer")
    assert finalizer["required"] is False


def test_mit6824_watchdog_surfaces_docker_engine_unavailable() -> None:
    tool = _load_tool("mit6824_watchdog_docker_unavailable", "tools/mit6824_watchdog.py")
    args = Namespace(status_watch_poll_sec=300.0, status_watch_timeout_sec=43200.0)
    rows = [
        {"pid": 4, "command_line": "python tools\\mit6824_finalize_lecture.py --lecture 8 --watch"},
        {"pid": 5, "command_line": "python tools\\mit6824_status_refresh.py --watch --poll-sec 300"},
    ]

    report = tool.build_report(
        args=args,
        root=REPO_ROOT,
        rows=rows,
        lifecycle={"v25_finalized": True, "lecture8_started": True, "lecture8_status": "in_progress"},
        runtime={"docker": {"status": "unavailable", "reason": "timeout"}},
    )

    assert report["summary"]["status"] == "needs_attention"
    assert report["summary"]["missing_required"] == []
    assert report["summary"]["docker_status"] == "unavailable"
    assert "docker_engine_unavailable" in report["summary"]["missing_reasons"]


def test_mit6824_handoff_waits_when_v25_is_healthy() -> None:
    tool = _load_tool("mit6824_handoff_wait", "tools/mit6824_handoff_report.py")

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "wait_for_v25_candidate_regression"}},
        goal={"next_action": "wait_for_v25_candidate_regression"},
        delivery={"summary": {"review_ready": []}},
        watchdog={"summary": {"missing_required": []}},
        candidate_coverage={"summary": {}},
        auto_advance={"status": "waiting_for_finalize"},
        v25_finalize={"returncode": 2},
        lecture8_finalize={"status": "waiting_for_episode"},
    )

    assert action["status"] == "wait_for_v25"
    assert "mit6824_status_refresh.py" in action["commands"][0]


def test_mit6824_handoff_surfaces_pending_fallback_risk() -> None:
    tool = _load_tool("mit6824_handoff_pending_fallback", "tools/mit6824_handoff_report.py")

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "wait_for_v25_candidate_regression"}},
        goal={"next_action": "wait_for_v25_candidate_regression"},
        delivery={"summary": {"review_ready": []}},
        watchdog={"summary": {"missing_required": []}},
        candidate_coverage={"summary": {"eligible_pending_fallback_segments": 1}},
        auto_advance={"status": "waiting_for_finalize"},
        v25_finalize={"returncode": 2},
        lecture8_finalize={"status": "waiting_for_episode"},
    )

    assert action["status"] == "monitor_v25_fallback_risk"
    assert "fallback_reason" in action["reason"]
    assert any("mit6824_candidate_coverage.py" in command for command in action["commands"])


def test_mit6824_handoff_prioritizes_docker_repair_before_watchers() -> None:
    tool = _load_tool("mit6824_handoff_docker_repair", "tools/mit6824_handoff_report.py")

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "wait_for_lecture_8_episode_or_completion"}},
        goal={"next_action": "wait_for_lecture_8_episode_or_completion"},
        delivery={"summary": {"review_ready": []}},
        watchdog={
            "summary": {
                "docker_status": "unavailable",
                "missing_required": ["lecture8_finalizer", "status_refresh_watch"],
                "missing_reasons": ["lecture8_finalizer", "status_refresh_watch", "docker_engine_unavailable"],
            }
        },
        candidate_coverage={"summary": {}},
        auto_advance={"status": "started"},
        v25_finalize={"returncode": 0},
        lecture8_finalize={"status": "waiting_for_completed_episode"},
    )

    assert action["status"] == "repair_docker_engine"
    assert action["reason"] == "docker_engine_unavailable_blocks_episode8_pipeline_and_watchers"
    assert action["commands"][0].startswith("Start Docker Desktop")
    assert any("mit6824_runtime_blockers.md" in command for command in action["commands"])


def test_mit6824_handoff_routes_source_preflight_blocks() -> None:
    tool = _load_tool("mit6824_handoff_source_preflight", "tools/mit6824_handoff_report.py")

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "continue_course_delivery"}},
        goal={"next_action": "continue_course_delivery"},
        delivery={"summary": {"review_ready": [], "source_blocked": [9]}},
        watchdog={"summary": {"missing_required": []}},
        candidate_coverage={"summary": {}},
        auto_advance={"status": ""},
        v25_finalize={"returncode": 0},
        lecture8_finalize={"status": "finalized_for_human_review"},
    )

    assert action["status"] == "repair_source_preflight"
    assert action["reason"] == "next_lecture_source_preflight_failed_before_start"
    assert any("mit6824_course_tracker.py" in command for command in action["commands"])
    assert any("mit6824_course_advance.py --refresh" in command for command in action["commands"])


def test_mit6824_runtime_blockers_reports_docker_blocker() -> None:
    tool = _load_tool("mit6824_runtime_blockers", "tools/mit6824_runtime_blockers.py")

    report = tool.build_report(
        status_refresh={},
        watchdog={
            "runtime": {"docker": {"status": "unavailable", "reason": "timeout"}},
            "summary": {"docker_status": "unavailable", "missing_reasons": ["docker_engine_unavailable"]},
        },
        root=REPO_ROOT,
    )

    assert report["status"] == "needs_external_repair"
    assert report["summary"]["blocker_ids"] == ["docker_engine_unavailable"]
    blocker = report["blockers"][0]
    assert "lecture8_finalizer" in blocker["blocks"]
    assert any("docker version" == command for command in blocker["safe_recovery_commands"])
    assert any("Do not unregister" in item for item in blocker["unsafe_actions_not_taken"])


def test_mit6824_handoff_prioritizes_watcher_repair() -> None:
    tool = _load_tool("mit6824_handoff_watchers", "tools/mit6824_handoff_report.py")
    watchdog = {
        "summary": {"missing_required": ["lecture8_auto_advance"]},
        "watchers": [
            {
                "id": "lecture8_auto_advance",
                "status": "missing",
                "restart_command": "python tools\\mit6824_auto_advance.py --lecture 8 --watch",
            }
        ],
    }

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "wait_for_v25_candidate_regression"}},
        goal={"next_action": "wait_for_v25_candidate_regression"},
        delivery={},
        watchdog=watchdog,
        candidate_coverage={"summary": {}},
        auto_advance={"status": "waiting_for_finalize"},
        v25_finalize={},
        lecture8_finalize={},
    )

    assert action["status"] == "repair_watchers"
    assert action["commands"][0] == "python tools\\mit6824_watchdog.py --restart-status-watch"
    assert action["commands"][1] == "python tools\\mit6824_auto_advance.py --lecture 8 --watch"


def test_mit6824_handoff_routes_regression_freeze_failure() -> None:
    tool = _load_tool("mit6824_handoff_freeze_failed", "tools/mit6824_handoff_report.py")

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "wait_for_v25_finalize_then_auto_start_lecture_8"}},
        goal={"next_action": "wait_for_v25_finalize_then_auto_start_lecture_8"},
        delivery={},
        watchdog={"summary": {"missing_required": []}},
        candidate_coverage={"summary": {}},
        auto_advance={"status": "regression_freeze_failed", "hard_issues": ["failed_anchors:1"]},
        v25_finalize={"returncode": 0},
        lecture8_finalize={},
    )

    assert action["status"] == "inspect_regression_freeze"
    assert action["hard_issues"] == ["failed_anchors:1"]
    assert any("mit6824_regression_suite.py --freeze" in command for command in action["commands"])


def test_mit6824_handoff_surfaces_review_ready_commands() -> None:
    tool = _load_tool("mit6824_handoff_review_ready", "tools/mit6824_handoff_report.py")

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "human_review_ready_lectures"}},
        goal={"next_action": "human_review_ready_lectures"},
        delivery={
            "rows": [
                {
                    "lecture": 8,
                    "state": "review_ready",
                    "human_review_pack_abs_path": "F:\\HoloDub\\data\\validation\\mit6824_acceptance\\lec-08\\human_review_pack\\index.html",
                    "final_video_abs_path": "F:\\HoloDub\\data\\validation\\mit6824_acceptance\\lec-08\\final.mp4",
                    "accept_dry_run_report": "data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json",
                    "accept_dry_run_command": "python tools\\mit6824_accept_lecture.py --lecture 8 --refresh-status --auto-advance-next --dry-run --dry-run-output data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json",
                    "accept_command": "python tools\\mit6824_accept_lecture.py --lecture 8 --refresh-status --auto-advance-next",
                    "blocker_command": "python tools\\mit6824_record_review_blockers.py --lecture 8 --blocker \"describe blocker\" --refresh-status",
                }
            ]
        },
        watchdog={"summary": {"missing_required": []}},
        candidate_coverage={"summary": {}},
        auto_advance={"status": "started"},
        v25_finalize={"returncode": 0},
        lecture8_finalize={"status": "finalized_for_human_review"},
    )

    assert action["status"] == "human_review_ready"
    assert any("open review pack" in command for command in action["commands"])
    assert "open dry-run report" in action["commands"][2]
    assert "--dry-run" in action["commands"][3]
    assert "--dry-run-output data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json" in action["commands"][3]
    assert "mit6824_accept_lecture.py" in action["commands"][4]
    assert "mit6824_record_review_blockers.py" in action["commands"][5]


def test_mit6824_handoff_routes_content_readiness_blocks_before_acceptance() -> None:
    tool = _load_tool("mit6824_handoff_content_blocked", "tools/mit6824_handoff_report.py")

    action = tool.choose_next_action(
        status_refresh={"summary": {"next_action": "human_review_ready_lectures"}},
        goal={"next_action": "human_review_ready_lectures"},
        delivery={
            "summary": {"review_ready": [], "content_blocked": [8]},
            "rows": [
                {
                    "lecture": 8,
                    "state": "content_blocked",
                    "automation_action": "python tools\\mit6824_finalize_lecture.py --lecture 8 --refresh-tracker --watch --execute --poll-sec 300",
                    "accept_command": "",
                    "blocker_command": "",
                }
            ],
        },
        watchdog={"summary": {"missing_required": []}},
        candidate_coverage={"summary": {}},
        auto_advance={"status": "started"},
        v25_finalize={"returncode": 0},
        lecture8_finalize={"status": "finalized_for_human_review"},
    )

    assert action["status"] == "repair_acceptance_content"
    assert any("mit6824_finalize_lecture.py" in command for command in action["commands"])
    assert not any("mit6824_accept_lecture.py" in command for command in action["commands"])


def test_mit6824_handoff_estimates_progress_from_status_history() -> None:
    tool = _load_tool("mit6824_handoff_eta", "tools/mit6824_handoff_report.py")
    history = [
        {
            "generated_at": "2026-06-23T10:00:00+00:00",
            "summary": {"active_regression": {"synthesized": "80", "total": "180"}},
        },
        {
            "generated_at": "2026-06-23T11:00:00+00:00",
            "summary": {"active_regression": {"synthesized": "90", "total": "180"}},
        },
    ]

    estimate = tool.estimate_progress(history, {"synthesized": "90", "total": "180"}, window_sec=7200)

    assert estimate["status"] == "estimated"
    assert estimate["delta_segments"] == 10
    assert estimate["segments_per_hour"] == 10
    assert estimate["remaining_segments"] == 90
    assert estimate["eta_hours"] == 9


def test_mit6824_handoff_progress_estimate_reports_no_recent_delta() -> None:
    tool = _load_tool("mit6824_handoff_eta_none", "tools/mit6824_handoff_report.py")
    history = [
        {
            "generated_at": "2026-06-23T10:00:00+00:00",
            "summary": {"active_regression": {"synthesized": "90", "total": "180"}},
        },
    ]

    estimate = tool.estimate_progress(history, {"synthesized": "90", "total": "180"}, window_sec=7200)

    assert estimate["status"] == "no_recent_delta"
    assert estimate["remaining_segments"] == 90


def test_mit6824_v25_diagnostics_summarizes_jobs_and_frontier(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_v25_diagnostics", "tools/mit6824_v25_diagnostics.py")
    goal = tmp_path / "goal.json"
    status = tmp_path / "status.json"
    goal.write_text(
        json.dumps(
            {
                "active_regression": {
                    "metrics": {"synthesized": "93", "total": "180", "candidate_audio": "86", "fallback": "0"},
                    "health": {"status": "running_recent", "reason": "recent_synthesis"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    status.write_text(json.dumps({"summary": {}}, ensure_ascii=False), encoding="utf-8")
    args = Namespace(
        job_ids="342,343",
        max_frontier=3,
        goal_status=str(goal),
        status_refresh=str(status),
    )
    tool.query_job_summary = lambda _args, _root, _ids: (
        [
            {"id": "342", "status": "queued", "error_message": "", "synthesized": "19", "segments": "19"},
            {"id": "343", "status": "running", "error_message": "", "synthesized": "18", "segments": "37"},
        ],
        {"returncode": 0},
    )
    tool.query_frontier = lambda _args, _root, _ids: (
        [
            {
                "job_id": "343",
                "segment_id": "999",
                "ordinal": "18",
                "source_segment_id": "10700",
                "original_duration_ms": "12000",
                "target_text_chars": "80",
                "candidate_plan_enabled": "true",
                "target_text_preview": "preview",
            }
        ],
        {"returncode": 0},
    )
    tool.query_fallback_risks = lambda _args, _root, _ids: (
        [
            {
                "job_id": "343",
                "segment_id": "999",
                "ordinal": "18",
                "source_segment_id": "10700",
                "segment_status": "translated",
                "fallback_reason": "all_candidates_rejected",
            }
        ],
        {"returncode": 0},
    )

    report = tool.build_report(args, tmp_path)

    assert report["summary"]["running_job_ids"] == ["343"]
    assert report["summary"]["frontier_count"] == 1
    assert report["summary"]["fallback_risk_count"] == 1
    assert report["frontier"][0]["segment_id"] == "999"
    assert report["fallback_risks"][0]["fallback_reason"] == "all_candidates_rejected"


def test_mit6824_delivery_index_accept_command_auto_advances_next() -> None:
    tool = _load_tool("mit6824_delivery_index_accept_command", "tools/mit6824_delivery_index.py")
    action = tool.delivery_action(
        {"lecture": 8, "status": "awaiting_human_acceptance", "missing": []},
        next_not_started=9,
        course_next_action="deliver_lecture_8",
    )

    assert action["state"] == "review_ready"
    assert action["accept_dry_run_report"].endswith(
        "data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json"
    )
    assert "--dry-run" in action["accept_dry_run_command"]
    assert "--dry-run-output data/validation/mit6824_acceptance/lec-08/acceptance_dry_run_report.json" in action[
        "accept_dry_run_command"
    ]
    assert "--refresh-status" in action["accept_command"]
    assert "--auto-advance-next" in action["accept_command"]


def test_mit6824_batch_review_routes_repeated_issues_without_per_episode_training() -> None:
    tool = _load_tool("mit6824_batch_review", "tools/mit6824_batch_review.py")
    acceptance = {
        "lectures": [
            {"lecture": 8, "title": "Zookeeper", "status": "accepted", "human_accepted": True, "artifacts": {}},
            {"lecture": 9, "title": "More Replication CRAQ", "status": "review_blocked", "human_accepted": False, "artifacts": {}},
        ]
    }
    training = {
        "summary": {
            "recommendation": "do_not_train_use_rules_candidates_and_collect_more_evidence",
            "training_readiness": {"status": "collect_more_evidence_or_fix_with_rules_candidates"},
        },
        "families": [
            {
                "family": "join_or_pause",
                "issue_count": 2,
                "lecture_count": 2,
                "lectures": [8, 9],
                "default_action": "split_candidate_gate_or_rescue",
                "training_signal": False,
            }
        ],
        "issues": [
            {"lecture": 8, "family": "join_or_pause"},
            {"lecture": 9, "family": "join_or_pause"},
        ],
    }

    review = tool.build_review(
        root=REPO_ROOT,
        acceptance=acceptance,
        training=training,
        delivery={"summary": {"next_not_started": 10, "review_ready": [9]}},
        batch_size=3,
        min_reviewed=2,
    )

    assert review["policy"]["per_episode_training_allowed"] is False
    assert review["summary"]["reviewed_lectures"] == [8, 9]
    assert review["summary"]["pending_review_lectures"] == []
    assert review["summary"]["next_action"] == "repair_rules_candidates_or_review_anchors_then_continue"
    assert review["issue_family_counts"] == {"join_or_pause": 2}


def test_mit6824_batch_review_keeps_pending_review_out_of_reviewed_batch() -> None:
    tool = _load_tool("mit6824_batch_review_pending_review", "tools/mit6824_batch_review.py")
    acceptance = {
        "lectures": [
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "awaiting_human_acceptance",
                "human_accepted": False,
                "acceptance_readiness": {"ready_for_human_review": True},
                "artifacts": {},
            },
        ]
    }

    review = tool.build_review(
        root=REPO_ROOT,
        acceptance=acceptance,
        training={"summary": {"recommendation": "do_not_train_insufficient_cross_lecture_evidence"}},
        delivery={"summary": {"next_not_started": 9, "review_ready": [8]}},
        batch_size=3,
        min_reviewed=2,
    )

    assert review["summary"]["reviewed_lectures"] == []
    assert review["summary"]["pending_review_lectures"] == [8]
    assert review["summary"]["pending_content_ready_lectures"] == [8]
    assert review["summary"]["pending_content_blocked_lectures"] == []
    assert review["summary"]["reviewed_count"] == 0
    assert review["summary"]["pending_review_count"] == 1
    assert review["summary"]["next_action"] == "not_due_collect_more_reviewed_lectures"


def test_mit6824_auto_advance_requires_regression_freeze_before_start(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_auto_advance", "tools/mit6824_auto_advance.py")
    finalize_status = tmp_path / "auto_finalize_status.json"
    finalize_status.write_text(json.dumps({"returncode": 0, "completed": True}), encoding="utf-8")
    args = Namespace(
        lecture=8,
        lecture_start=8,
        lecture_end=20,
        tracker=str(tmp_path / "tracker.json"),
        tracker_md=str(tmp_path / "tracker.md"),
        wait_finalize_status=str(finalize_status),
        require_regression_freeze=True,
        regression_suite=str(tmp_path / "suite.json"),
        regression_suite_md=str(tmp_path / "suite.md"),
        candidate_coverage=str(tmp_path / "coverage.json"),
        candidate_coverage_md=str(tmp_path / "coverage.md"),
        candidate_coverage_job_ids="342,343",
        output=str(tmp_path / "advance.json"),
        history_jsonl=str(tmp_path / "advance.jsonl"),
        python=sys.executable,
        execute=True,
        watch=False,
        poll_sec=300.0,
        timeout_sec=0.0,
    )

    tool.freeze_regression_suite = lambda _args, _root: {
        "returncode": 2,
        "output": {"hard_issues": ["failed_anchors:1"]},
        "stdout_tail": "",
    }
    tool.refresh_tracker = lambda _args, _root: (_ for _ in ()).throw(AssertionError("tracker should not refresh"))
    tool.start_lecture = lambda _args, _root: (_ for _ in ()).throw(AssertionError("lecture should not start"))

    done, row = tool.one_attempt(args, REPO_ROOT)

    assert done is True
    assert row["status"] == "regression_freeze_failed"
    assert row["hard_issues"] == ["failed_anchors:1"]


def test_mit6824_auto_advance_restarts_ml_before_start_when_enabled(tmp_path: Path) -> None:
    tool = _load_tool("mit6824_auto_advance_restart_ml", "tools/mit6824_auto_advance.py")
    finalize_status = tmp_path / "auto_finalize_status.json"
    finalize_status.write_text(json.dumps({"returncode": 0, "completed": True}), encoding="utf-8")
    args = Namespace(
        lecture=8,
        lecture_start=8,
        lecture_end=20,
        tracker=str(tmp_path / "tracker.json"),
        tracker_md=str(tmp_path / "tracker.md"),
        wait_finalize_status=str(finalize_status),
        require_regression_freeze=True,
        regression_suite=str(tmp_path / "suite.json"),
        regression_suite_md=str(tmp_path / "suite.md"),
        candidate_coverage=str(tmp_path / "coverage.json"),
        candidate_coverage_md=str(tmp_path / "coverage.md"),
        candidate_coverage_job_ids="342,343",
        output=str(tmp_path / "advance.json"),
        history_jsonl=str(tmp_path / "advance.jsonl"),
        python=sys.executable,
        restart_ml_service_before_start=True,
        ml_service_name="ml-service",
        ml_restart_timeout_sec=300,
        compose_file=[],
        execute=True,
        watch=False,
        poll_sec=300.0,
        timeout_sec=0.0,
    )
    calls: list[str] = []

    def freeze(_args, _root):
        calls.append("freeze")
        return {"returncode": 0, "output": {"frozen": True}, "stdout_tail": ""}

    def restart(_args, _root):
        calls.append("restart_ml")
        return {"returncode": 0, "stdout_tail": "restarted"}

    def refresh(_args, _root):
        calls.append("refresh_tracker")
        return {"returncode": 0, "output": {}, "stdout_tail": ""}

    def start(_args, _root):
        calls.append("start")
        return {"returncode": 0, "output": {"status": "started"}, "stdout_tail": ""}

    tool.freeze_regression_suite = freeze
    tool.restart_ml_service = restart
    tool.refresh_tracker = refresh
    tool.start_lecture = start

    done, row = tool.one_attempt(args, REPO_ROOT)

    assert done is True
    assert row["status"] == "started"
    assert calls == ["freeze", "restart_ml", "refresh_tracker", "start"]
    assert row["ml_service_restart"]["returncode"] == 0


def test_mit6824_status_refresh_is_read_only_by_default() -> None:
    tool = _load_tool("mit6824_status_refresh", "tools/mit6824_status_refresh.py")
    args = Namespace(
        python=sys.executable,
        lecture_start=8,
        lecture_end=20,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_status="acceptance.json",
        acceptance_status_md="acceptance.md",
        training_decision="training.json",
        training_decision_md="training.md",
        candidate_coverage="coverage.json",
        candidate_coverage_md="coverage.md",
        candidate_coverage_job_ids="342,343,344,345,346,347",
        acceptance_gate_check="acceptance_gate.json",
        acceptance_gate_check_md="acceptance_gate.md",
        delivery_index="delivery.json",
        delivery_index_md="delivery.md",
        batch_review="batch.json",
        batch_review_md="batch.md",
        watchdog_status="watchdog.json",
        watchdog_status_md="watchdog.md",
        v25_diagnostics="diagnostics.json",
        v25_diagnostics_md="diagnostics.md",
        handoff_report="handoff.json",
        handoff_report_md="handoff.md",
        runtime_blockers="blockers.json",
        runtime_blockers_md="blockers.md",
        auto_advance_status="auto_advance.json",
        goal_status="goal.json",
        goal_status_md="goal.md",
        course_advance_status="advance.json",
        course_advance_history="advance.jsonl",
        output_json="status_refresh.json",
        history_jsonl="status_refresh.jsonl",
    )

    steps = tool.build_steps(args)
    flattened = [part for _, cmd, _ in steps for part in cmd]
    course_advance_cmd = next(cmd for name, cmd, _ in steps if name == "course_advance")
    handoff_cmd = next(cmd for name, cmd, _ in steps if name == "handoff_report")
    diagnostics_cmd = next(cmd for name, cmd, _ in steps if name == "v25_diagnostics")
    blockers_cmd = next(cmd for name, cmd, _ in steps if name == "runtime_blockers")
    gate_cmd = next(cmd for name, cmd, _ in steps if name == "acceptance_gate_check")

    assert "--execute" not in flattened
    assert "--output" in course_advance_cmd
    assert "advance.json" in course_advance_cmd
    assert "--history-jsonl" in course_advance_cmd
    assert "advance.jsonl" in course_advance_cmd
    assert "tracker.json" in course_advance_cmd
    assert "acceptance.json" in course_advance_cmd
    assert "handoff.json" in handoff_cmd
    assert "handoff.md" in handoff_cmd
    assert "auto_advance.json" in handoff_cmd
    assert "status_refresh.json" in handoff_cmd
    assert "status_refresh.jsonl" in handoff_cmd
    assert "coverage.json" in handoff_cmd
    assert "diagnostics.json" in diagnostics_cmd
    assert "diagnostics.md" in diagnostics_cmd
    assert "blockers.json" in blockers_cmd
    assert "blockers.md" in blockers_cmd
    assert "acceptance_gate.json" in gate_cmd
    assert "acceptance_gate.md" in gate_cmd
    assert [name for name, _, _ in steps] == [
        "course_tracker",
        "acceptance_status",
        "training_decision",
        "candidate_coverage",
        "source_timing_preflight",
        "course_advance",
        "acceptance_gate_check",
        "watchdog",
        "runtime_blockers",
        "goal_status",
        "v25_diagnostics",
        "delivery_index",
        "batch_review",
        "handoff_report",
    ]


def test_mit6824_status_refresh_degraded_mode_skips_db_steps() -> None:
    tool = _load_tool("mit6824_status_refresh_degraded", "tools/mit6824_status_refresh.py")
    args = Namespace(
        python=sys.executable,
        lecture_start=8,
        lecture_end=20,
        tracker="tracker.json",
        tracker_md="tracker.md",
        acceptance_status="acceptance.json",
        acceptance_status_md="acceptance.md",
        training_decision="training.json",
        training_decision_md="training.md",
        candidate_coverage="coverage.json",
        candidate_coverage_md="coverage.md",
        candidate_coverage_job_ids="342,343,344,345,346,347",
        acceptance_gate_check="acceptance_gate.json",
        acceptance_gate_check_md="acceptance_gate.md",
        delivery_index="delivery.json",
        delivery_index_md="delivery.md",
        batch_review="batch.json",
        batch_review_md="batch.md",
        watchdog_status="watchdog.json",
        watchdog_status_md="watchdog.md",
        v25_diagnostics="diagnostics.json",
        v25_diagnostics_md="diagnostics.md",
        handoff_report="handoff.json",
        handoff_report_md="handoff.md",
        runtime_blockers="blockers.json",
        runtime_blockers_md="blockers.md",
        auto_advance_status="auto_advance.json",
        goal_status="goal.json",
        goal_status_md="goal.md",
        course_advance_status="advance.json",
        course_advance_history="advance.jsonl",
        output_json="status_refresh.json",
        history_jsonl="status_refresh.jsonl",
    )

    steps = tool.build_steps(args, degraded_without_docker=True)
    names = [name for name, _, _ in steps]
    flattened = [part for _, cmd, _ in steps for part in cmd]
    goal_cmd = next(cmd for name, cmd, _ in steps if name == "goal_status")
    blockers_cmd = next(cmd for name, cmd, _ in steps if name == "runtime_blockers")

    assert "course_tracker" not in names
    assert "acceptance_status" not in names
    assert "candidate_coverage" not in names
    assert "course_advance" not in names
    assert "--no-db" in goal_cmd
    assert "blockers.json" in blockers_cmd
    assert "--execute" not in flattened
    assert names == [
        "training_decision",
        "acceptance_gate_check",
        "watchdog",
        "runtime_blockers",
        "goal_status",
        "delivery_index",
        "batch_review",
        "handoff_report",
    ]


def test_mit6824_status_refresh_extracts_current_tracker_episode() -> None:
    tool = _load_tool("mit6824_status_refresh_current_episode", "tools/mit6824_status_refresh.py")
    tracker = {
        "lectures": [
            {"lecture": 7, "status": "accepted", "episodes": [{"id": "179"}]},
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "in_progress",
                "episodes": [
                    {"id": "178", "name": "old"},
                    {
                        "id": "180",
                        "name": "mit6824-lec08-zookeeper-prod-v1",
                        "status": "pending",
                        "total_chapters": "4",
                    },
                ],
            },
        ]
    }

    episode = tool.current_tracker_episode(tracker)

    assert episode == {
        "lecture": 8,
        "title": "Zookeeper",
        "episode_id": 180,
        "episode_name": "mit6824-lec08-zookeeper-prod-v1",
        "episode_status": "pending",
        "total_chapters": "4",
    }


def test_mit6824_status_refresh_extracts_dispatched_episode_when_tracker_status_lags() -> None:
    tool = _load_tool("mit6824_status_refresh_dispatched_episode", "tools/mit6824_status_refresh.py")
    tracker = {
        "lectures": [
            {
                "lecture": 8,
                "title": "Zookeeper",
                "status": "has_stale_attempts",
                "episodes": [
                    {"id": "180", "name": "old", "status": "completed"},
                    {
                        "id": "183",
                        "name": "mit6824-lec08-zookeeper-prod-v4",
                        "status": "dispatched",
                        "total_chapters": "5",
                    },
                ],
            },
        ]
    }

    episode = tool.current_tracker_episode(tracker)

    assert episode == {
        "lecture": 8,
        "title": "Zookeeper",
        "episode_id": 183,
        "episode_name": "mit6824-lec08-zookeeper-prod-v4",
        "episode_status": "dispatched",
        "total_chapters": "5",
    }


def test_mit6824_status_refresh_reads_worker_queue_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _load_tool("mit6824_status_refresh_queue_keys", "tools/mit6824_status_refresh.py")

    def fake_run_text(cmd: list[str], *, cwd: Path, timeout: int = 30) -> dict[str, object]:
        joined = " ".join(cmd)
        if "LRANGE holodub:tasks " in joined:
            return {
                "returncode": 0,
                "stdout": '{"job_id":350,"stage":"tts_duration","attempt":0}\n',
            }
        if "ZRANGE holodub:tasks:delayed " in joined:
            return {
                "returncode": 0,
                "stdout": '{"job_id":348,"stage":"tts_duration","attempt":1}\n1782277449771\n',
            }
        if "LRANGE holodub:stage_tasks " in joined:
            return {
                "returncode": 0,
                "stdout": '{"job_id":999,"stage":"tts_duration","attempt":0}\n',
            }
        if "ZRANGE holodub:delayed_stage_tasks " in joined:
            return {"returncode": 0, "stdout": ""}
        if "KEYS holodub:lease:job:*:stage:*" in joined:
            return {"returncode": 0, "stdout": "holodub:lease:job:349:stage:tts_duration\n"}
        return {"returncode": 1, "stdout": joined}

    monkeypatch.setattr(tool, "run_text", fake_run_text)

    queue = tool.read_current_queue(REPO_ROOT, [348, 349, 350, 351])

    assert queue["current_episode_immediate"] == [{"job_id": 350, "stage": "tts_duration", "attempt": 0}]
    assert queue["current_episode_delayed"] == [
        {"job_id": 348, "stage": "tts_duration", "attempt": 1, "score_ms": 1782277449771}
    ]
    assert queue["current_episode_active_leases"] == ["holodub:lease:job:349:stage:tts_duration"]
    assert queue["alternate_current_episode_immediate"] == []
    assert queue["alternate_immediate"] == [{"job_id": 999, "stage": "tts_duration", "attempt": 0}]
    assert queue["read_errors"] == []


def test_mit6824_resume_stalled_stage_allows_early_stage_without_segments() -> None:
    tool = _load_tool("mit6824_resume_early_stage", "tools/mit6824_resume_stalled_stage.py")
    args = Namespace(stage="separate", min_stale_sec=900, allow_expired_lease_requeue=True)
    state = {
        "job": {"status": "running", "current_stage": "separate", "heartbeat_stale_sec": "3600"},
        "stage_run": {"status": "running", "finished_at": ""},
        "status_counts": {},
        "lease_exists": 0,
        "queue_matches": [],
        "delayed_matches": [],
    }

    safe, blockers, warnings = tool.evaluate(args, state)

    assert safe is True
    assert blockers == []
    assert "no_synthesized_segments_seen" not in warnings


def test_mit6824_resume_stalled_stage_keeps_tts_segment_prereq() -> None:
    tool = _load_tool("mit6824_resume_tts_stage", "tools/mit6824_resume_stalled_stage.py")
    args = Namespace(stage="tts_duration", min_stale_sec=900, allow_expired_lease_requeue=True)
    state = {
        "job": {"status": "running", "current_stage": "tts_duration", "heartbeat_stale_sec": "3600"},
        "stage_run": {"status": "running", "finished_at": ""},
        "status_counts": {},
        "lease_exists": 0,
        "queue_matches": [],
        "delayed_matches": [],
    }

    safe, blockers, warnings = tool.evaluate(args, state)

    assert safe is False
    assert "no_translated_segments_to_resume" in blockers
    assert "no_synthesized_segments_seen" in warnings


def test_tail_completeness_detects_truncated_sentence_end() -> None:
    gate = _load_tool("lora_candidate_gate_tail", "tools/lora_candidate_gate.py")

    tail = gate.tail_completeness("\u53ea\u662f\u4e22\u5f03\u4e86\u65e9\u671f\u7684\u6761\u76ee\u3002", "\u53ea\u662f\u4e22\u5f03\u4e86\u65e9\u671f\u7684")

    assert tail["tail_expected"] == "\u671f\u7684\u6761\u76ee"
    assert tail["tail_recall"] < 0.75


def test_spoken_text_technical_v2_rewrites_user_review_cases() -> None:
    spoken_tool = _load_tool("lora_spoken_text", "tools/lora_spoken_text.py")

    text = (
        "\u524d\u4e00\u65e5\u5fd7\u7d22\u5f15\u5b57\u6bb5\uff0c"
        "\u6070\u597d\u5c31\u662f5\u53f7\u4efb\u671f\u3002"
        "\u5b83\u521a\u521a\u8bb0\u5f55\u52305\u30016\u3002\u4e00\u53f7\u8282\u70b9\u4ecd\u7136\u5b58\u5728\u95ee\u9898\u3002"
        "\u7b2c5.4.1\u8282\u3002"
        "\u65f6\u95f4\u8981\u957f\u5f97\u591a\u3002"
        "\u56fe2\u2014\u2014\u5b83\u5076\u5c14\u4f1a\u8bf4\u3002"
    )

    spoken, rewrites = spoken_tool.normalize_spoken_text(text, "technical-v2")

    assert "\u524d\u4e00\u4e2a\u65e5\u5fd7\u7d22\u5f15\u5b57\u6bb5" in spoken
    assert "\u4e94 \u53f7\u4efb\u671f" in spoken
    assert "\u8bb0\u5f55\u5230\u4e94\u548c\u516d\u3002\u63a5\u7740\uff0c\u4e00\u53f7\u8282\u70b9" in spoken
    assert "\u7b2c\u4e94\u70b9\u56db\u70b9\u4e00\u8282" in spoken
    assert "\u4e45\u5f97\u591a" in spoken
    assert "\u56fe\u4e8c\uff0c\u5b83" in spoken
    assert {rewrite["rule"] for rewrite in rewrites} >= {
        "awkward_log_compound_insert_ge",
        "renqi_hao_to_spaced_hao_term",
        "number_list_sentence_pause_before_node",
        "section_number_to_spoken_points",
        "polyphone_chang_de_duo_to_jiu_de_duo",
        "figure_digit_to_chinese_numeral",
        "figure_dash_to_comma_pause",
    }


def test_spoken_text_technical_v3_uses_narrower_user_review_rewrites() -> None:
    spoken_tool = _load_tool("lora_spoken_text_v3", "tools/lora_spoken_text.py")

    text = (
        "\u8fd9\u4e2aAppend Entries RPC\u5305\u542b"
        "\u524d\u4e00\u65e5\u5fd7\u7d22\u5f15\u5b57\u6bb5"
        "\u4ee5\u53ca\u524d\u4e00\u65e5\u5fd7\u4efb\u671f\u5b57\u6bb5\u3002"
        "\u4efb\u671f\u7f16\u53f7\u6070\u597d\u5c31\u662f5\u53f7\u4efb\u671f\u3002"
        "\u5b83\u521a\u521a\u8bb0\u5f55\u52305\u30016\u3002\u4e00\u53f7\u8282\u70b9\u4ecd\u7136\u5b58\u5728\u95ee\u9898\u3002"
        "\u800c10\u6beb\u79d2\u7684\u65f6\u95f4\u8fdc\u6bd4\u53d1\u9001\u4e00\u4e2aRPC"
        "\u6216\u51e0\u4e4e\u4efb\u4f55\u5176\u4ed6\u4f60\u53ef\u80fd\u6267\u884c\u7684\u64cd\u4f5c\u90fd\u8981\u957f\u5f97\u591a\u3002"
        "\u6240\u4ee5Raft\u5c42\u4f1a\u5728Append Entries RPC\u540e\u8fd4\u56de\u3002"
    )

    spoken, rewrites = spoken_tool.normalize_spoken_text(text, "technical-v3")

    assert "Append Entries RPC" in spoken
    assert "\u4e00\u4e2aRPC" in spoken
    assert "\u524d\u4e00\u6761\u65e5\u5fd7\u7684\u7d22\u5f15\u5b57\u6bb5" in spoken
    assert "\u524d\u4e00\u6761\u65e5\u5fd7\u7684\u4efb\u671f\u5b57\u6bb5" in spoken
    assert "\u4efb\u671f\u7f16\u53f7\u6070\u597d\u5c31\u662f\u4e94 \u53f7\u4efb\u671f" in spoken
    assert "\u8bb0\u5f55\u5230\u4e94\u548c\u516d\u3002\u968f\u540e\uff0c\u4e00\u53f7\u8282\u70b9" in spoken
    assert "\u64cd\u4f5c\u8017\u65f6\u90fd\u8981\u66f4\u4e45" in spoken
    assert "Raft \u5c42" in spoken
    assert "RPC \u540e" in spoken
    assert "\u957f\u5f97\u591a" not in spoken
    assert "\u4e45\u5f97\u591a" not in spoken
    assert {rewrite["rule"] for rewrite in rewrites} >= {
        "previous_log_field_to_clause_v3",
        "number_list_sentence_pause_before_node_v3",
        "duration_polyphone_chang_to_haoshi_gengjiu_v3",
        "technical_latin_cjk_suffix_spacing_v3",
        "renqi_hao_to_spaced_hao_term",
    }


def test_listening_pack_resolves_data_prefixed_paths_under_data_root() -> None:
    pack_tool = _load_tool("lora_build_listening_pack_paths", "tools/lora_build_listening_pack.py")

    resolved = pack_tool.resolve_path("data/training/run/selected.wav", Path("/data"))

    assert resolved.as_posix() == "/data/training/run/selected.wav"


def test_direct_ml_candidate_smoke_payload_and_validation() -> None:
    smoke_tool = _load_tool("lora_direct_ml_candidate_smoke", "tools/lora_direct_ml_candidate_smoke.py")
    args = Namespace(
        text=smoke_tool.DEFAULT_TEXT,
        text_file="",
        feature_jsonl="",
        sample_id="",
        target_duration_sec=22.0,
        max_allowed_sec=0.0,
        voice_sample_relpath="voices/test_ref.wav",
        output_relpath="training/lora_runs/main_flow_candidate_smoke/smoke.wav",
        candidate_families=" split_light_vector, split_mix ,,",
    )

    payload = smoke_tool.build_payload(args)

    assert "\u65e5\u5fd7" in payload["text"]
    assert payload["target_language"] == "zh-CN"
    assert payload["target_duration_sec"] == 22.0
    assert payload["max_allowed_sec"] == 22.8
    assert payload["dubbing_meta"]["lora_candidate"]["candidate_families"] == [
        "split_light_vector",
        "split_mix",
    ]
    assert payload["dubbing_meta"]["lora_candidate"]["audio_selection_mode"] == "candidate_audio"

    summary = smoke_tool.summarize(
        {
            "audio_relpath": "training/lora_runs/main_flow_candidate_smoke/smoke.wav",
            "actual_duration_ms": 21989,
            "candidate_result": {
                "selected_family": "split_light_vector",
                "audio_selection_mode": "candidate_audio",
                "fallback_reason": "",
                "gate_decision": {"pass": True},
                "split_plan": {"parts": [{}, {}]},
                "candidates": [
                    {
                        "family": "split_light_vector",
                        "gate_pass": True,
                        "duration_ms": 21989,
                        "drift_ms": -10.5,
                        "similarity": 1.0,
                    }
                ],
            },
        },
        1.234,
    )

    assert summary["ok"] is True
    assert summary["validation_errors"] == []
    assert summary["split_parts"] == 2
    assert summary["candidates"][0]["drift_ms"] == -10.5

    fallback_summary = smoke_tool.summarize(
        {"audio_relpath": "default.wav", "candidate_result": {"fallback_reason": "adapter_missing"}},
        1.0,
    )
    assert fallback_summary["ok"] is False
    assert "audio_selection_mode_not_candidate_audio" in fallback_summary["validation_errors"]
    assert "fallback:adapter_missing" in fallback_summary["validation_errors"]


def test_candidate_runtime_replay_rescues_quiet_zero_pause_join_false_positive() -> None:
    replay_tool = _load_tool("lora_candidate_runtime_replay", "tools/lora_candidate_runtime_replay.py")
    runtime = {
        "selected_family": "default",
        "audio_selection_mode": "default_audio",
        "fallback_reason": "all_candidates_rejected",
        "candidates": [
            {
                "family": "default",
                "audio_relpath": "default.wav",
                "duration_ms": 22697,
                "drift_ms": -17943,
                "abs_drift_ms": 17943,
                "similarity": 0.691,
                "critical_term_recall": 1.0,
                "fit_tempo_delta": 0.4,
                "reject_reasons": ["low_asr", "high_drift", "high_fit_delta"],
                "gate_pass": False,
            },
            {
                "family": "split_mix",
                "split_strategy": "micro_clause_zero_pause",
                "audio_relpath": "split_mix_micro_clause_zero_pause.wav",
                "duration_ms": 40651,
                "drift_ms": 11.208,
                "abs_drift_ms": 11.208,
                "similarity": 1.0,
                "critical_term_recall": 1.0,
                "fit_tempo_delta": 0.069,
                "reject_reasons": ["bad_join_quality"],
                "gate_pass": False,
                "join_quality": {
                    "pass": False,
                    "reasons": ["large_edge_rms_jump"],
                    "max_edge_rms_jump_db": 18.192,
                    "joins": [
                        {
                            "index": 1,
                            "pause_sec": 0,
                            "prev_tail_dbfs": -46.63,
                            "next_head_dbfs": -64.822,
                            "edge_rms_jump_db": 18.192,
                        }
                    ],
                },
            },
        ],
    }

    report = replay_tool.replay_runtime(runtime)

    assert report["changed"] is True
    assert report["replayed_audio_selection_mode"] == "candidate_audio"
    assert report["replayed_fallback_reason"] == ""
    assert report["replayed_selected_family"] == "split_mix"
    assert report["join_quality_rescue_count"] == 1
    assert report["selected_candidate"]["join_quality_rescue"]["rule"] == "quiet_zero_pause_large_edge_rms_jump"


def test_candidate_runtime_replay_keeps_hot_join_failure_rejected() -> None:
    replay_tool = _load_tool("lora_candidate_runtime_replay_hot", "tools/lora_candidate_runtime_replay.py")
    runtime = {
        "selected_family": "default",
        "audio_selection_mode": "default_audio",
        "fallback_reason": "all_candidates_rejected",
        "candidates": [
            {
                "family": "split_mix",
                "split_strategy": "micro_clause_zero_pause",
                "audio_relpath": "hot.wav",
                "duration_ms": 40651,
                "drift_ms": 11.208,
                "abs_drift_ms": 11.208,
                "similarity": 1.0,
                "critical_term_recall": 1.0,
                "fit_tempo_delta": 0.069,
                "reject_reasons": ["bad_join_quality"],
                "gate_pass": False,
                "join_quality": {
                    "pass": False,
                    "reasons": ["large_edge_rms_jump"],
                    "max_edge_rms_jump_db": 18.192,
                    "joins": [
                        {
                            "index": 1,
                            "pause_sec": 0,
                            "prev_tail_dbfs": -29.24,
                            "next_head_dbfs": -64.822,
                            "edge_rms_jump_db": 18.192,
                        }
                    ],
                },
            },
        ],
    }

    report = replay_tool.replay_runtime(runtime)

    assert report["replayed_audio_selection_mode"] == "default_audio"
    assert report["replayed_fallback_reason"] == "all_candidates_rejected"
    assert report["gate_pass_count"] == 0
    assert report["join_quality_rescue_count"] == 0


def test_direct_ml_candidate_smoke_loads_feature_row(tmp_path: Path) -> None:
    smoke_tool = _load_tool("lora_direct_ml_candidate_smoke_feature", "tools/lora_direct_ml_candidate_smoke.py")
    features = tmp_path / "features.jsonl"
    features.write_text(
        json.dumps({"sample_id": "seg1", "text": "wrong", "target_ms": 1000}, ensure_ascii=False)
        + "\n"
        + json.dumps({"sample_id": "seg10686", "text": "chosen", "target_ms": 31700}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    args = Namespace(
        text=smoke_tool.DEFAULT_TEXT,
        text_file="",
        feature_jsonl=str(features),
        sample_id="seg10686",
        target_duration_sec=0.0,
        max_allowed_sec=0.0,
        voice_sample_relpath="voices/test_ref.wav",
        output_relpath="training/lora_runs/main_flow_candidate_smoke/smoke.wav",
        candidate_families="split_light_vector,split_mix",
    )

    payload = smoke_tool.build_payload(args)

    assert payload["text"] == "chosen"
    assert payload["target_duration_sec"] == 31.7
    assert payload["max_allowed_sec"] == 32.5


def test_go_pipeline_smoke_insert_sql_quotes_and_tags() -> None:
    smoke_tool = _load_tool("lora_go_pipeline_smoke", "tools/lora_go_pipeline_smoke.py")
    row = {
        "sample_id": "seg_quote",
        "segment_id": 42,
        "job_id": 7,
        "source_text": "server one's log",
        "text": "前一个条目的任期是5号，不是5日。",
        "audio_relpath": "jobs/7/tts/vp0/segment-0001.wav",
    }

    sql = smoke_tool.build_insert_segment_sql(row, job_id=183, ordinal=1, start_ms=0, target_ms=7520)

    assert "INSERT INTO segments" in sql
    assert "source_text, target_text" in sql
    assert "'server one''s log'" in sql
    assert "前一个条目的任期是5号" in sql
    assert '"smoke_sample_id": "seg_quote"' in sql
    assert "RETURNING id" in sql
    assert smoke_tool.parse_returning_id("11051\nINSERT 0 1\n") == 11051


def test_go_pipeline_smoke_validation_short_and_split_mix() -> None:
    smoke_tool = _load_tool("lora_go_pipeline_smoke_validation", "tools/lora_go_pipeline_smoke.py")
    short = {
        "sample_id": "seg10511",
        "status": "synthesized",
        "eligible": False,
        "target_ms": 7520,
        "tts_duration_ms": 7480,
        "drift_ms": -40,
        "tts_audio_relpath": "jobs/200/tts/vp0/segment-0001.wav",
        "candidate_plan": {
            "enabled": False,
            "audio_selection_mode": "default_audio",
        },
        "runtime": {},
    }
    long = {
        "sample_id": "seg10686",
        "status": "synthesized",
        "eligible": True,
        "target_ms": 31700,
        "tts_duration_ms": 31683,
        "drift_ms": -17,
        "tts_audio_relpath": "jobs/200/tts/vp0/segment-0002.wav",
        "candidate_plan": {
            "enabled": True,
            "audio_selection_mode": "candidate_audio",
        },
        "runtime": {
            "audio_selection_mode": "candidate_audio",
            "selected_family": "split_mix",
            "fallback_reason": "",
        },
    }

    issues = smoke_tool.validate_segments(
        [short, long],
        allow_fallback=False,
        expect_split_mix={"seg10686"},
        max_candidate_drift_ms=500,
    )

    assert issues == []

    bad_long = dict(long)
    bad_long["runtime"] = dict(long["runtime"], selected_family="split_light_vector")
    issues = smoke_tool.validate_segments(
        [bad_long],
        allow_fallback=False,
        expect_split_mix={"seg10686"},
        max_candidate_drift_ms=500,
    )

    assert issues == [
        {
            "sample_id": "seg10686",
            "reason": "expected_split_mix_not_selected",
            "value": "split_light_vector",
            "expected": "split_mix",
        }
    ]


def test_episode_candidate_monitor_summarizes_candidate_runtime() -> None:
    monitor = _load_tool("lora_episode_candidate_monitor", "tools/lora_episode_candidate_monitor.py")
    args = Namespace(
        min_target_sec=18.0,
        min_text_chars=180,
        max_candidate_drift_ms=500.0,
        review_drift_ms=300.0,
        focus_source_segments="10694,10835",
    )
    segments = [
        {
            "id": 2001,
            "chapter_id": 200,
            "chapter_ordinal": 1,
            "ordinal": 5,
            "status": "synthesized",
            "original_duration_ms": 19000,
            "tts_duration_ms": 18880,
            "tgt_text": "长文本" * 100,
            "tts_audio_path": "jobs/200/tts/vp0/segment-0005.wav",
            "meta": {
                "shadow_source_segment_id": 10694,
                "tts_lora_candidate": {
                    "enabled": True,
                    "phase": "main_flow_opt_in",
                    "audio_selection_mode": "candidate_audio",
                    "runtime": {
                        "audio_selection_mode": "candidate_audio",
                        "selected_family": "split_mix",
                        "selected_split_strategy": "automatic",
                        "fallback_reason": "",
                        "candidates": [{}, {}, {}],
                        "split_plan": {"parts": [{}, {}]},
                    },
                },
            },
        },
        {
            "id": 2002,
            "chapter_id": 200,
            "chapter_ordinal": 1,
            "ordinal": 6,
            "status": "synthesized",
            "original_duration_ms": 7000,
            "tts_duration_ms": 7010,
            "tgt_text": "短句",
            "tts_audio_path": "jobs/200/tts/vp0/segment-0006.wav",
            "meta": {
                "shadow_source_segment_id": 10835,
                "tts_lora_candidate": {
                    "enabled": False,
                    "phase": "main_flow_shadow",
                    "audio_selection_mode": "default_audio",
                },
            },
        },
        {
            "id": 2003,
            "chapter_id": 200,
            "chapter_ordinal": 1,
            "ordinal": 7,
            "status": "translated",
            "original_duration_ms": 20000,
            "tts_duration_ms": 0,
            "tgt_text": "运行中的长文本" * 80,
            "tts_audio_path": "",
            "meta": {
                "shadow_source_segment_id": 10695,
                "tts_lora_candidate_runtime": {
                    "audio_selection_mode": "default_audio",
                    "selected_family": "default",
                    "fallback_reason": "all_candidates_rejected",
                },
            },
        },
    ]

    summary = monitor.summarize(
        {"id": 153, "name": "shadow", "status": "running"},
        [{"id": 200, "status": "running"}],
        segments,
        args=args,
    )

    assert summary["candidate"]["eligible_segments"] == 2
    assert summary["candidate"]["candidate_audio_segments"] == 1
    assert summary["candidate"]["inflight_fallback_segments"] == 1
    assert [row["segment_id"] for row in summary["candidate"]["inflight_fallback_rows"]] == [2003]
    assert summary["candidate"]["selected_family_counts"] == {"split_mix": 1}
    assert summary["progress"]["chapter_progress"][0]["segments_synthesized"] == 2
    assert summary["progress"]["chapter_progress"][0]["candidate_audio_segments"] == 1
    assert summary["progress"]["chapter_progress"][0]["inflight_fallback_segments"] == 1
    assert summary["drift"]["max_abs_drift_ms"] == 120.0
    assert summary["drift"]["candidate_audio_max_abs_drift_ms"] == 120.0
    assert [row["source_segment_id"] for row in summary["focus_segments"]] == [10694, 10835]
    assert summary["risk_queue"] == [
        {
            "priority": 50,
            "reason": "inflight_candidate_fallback",
            "value": "all_candidates_rejected",
            "segment_id": 2003,
            "source_segment_id": 10695,
            "chapter_id": 200,
            "chapter_ordinal": 1,
            "ordinal": 7,
            "status": "translated",
            "target_ms": 20000,
            "tts_duration_ms": 0,
            "drift_ms": None,
            "runtime_selected_family": "default",
            "runtime_selected_split_strategy": None,
            "runtime_fallback_reason": "all_candidates_rejected",
            "agent_final_reason": None,
            "agent_hard_cap_direction": None,
            "agent_final_abs_drift_ms": None,
            "tts_audio_relpath": "",
        }
    ]
    assert summary["validation_issues"] == []


def test_episode_candidate_monitor_includes_gate_replay_for_rescuable_fallback() -> None:
    monitor = _load_tool(
        "lora_episode_candidate_monitor_replay",
        "tools/lora_episode_candidate_monitor.py",
    )
    args = Namespace(
        min_target_sec=18.0,
        min_text_chars=180,
        max_candidate_drift_ms=500.0,
        review_drift_ms=300.0,
        focus_source_segments="",
    )
    segments = [
        {
            "id": 2004,
            "chapter_id": 200,
            "chapter_ordinal": 1,
            "ordinal": 8,
            "status": "translated",
            "original_duration_ms": 40640,
            "tts_duration_ms": 0,
            "tgt_text": "long segment " * 40,
            "tts_audio_path": "",
            "meta": {
                "shadow_source_segment_id": 10778,
                "tts_lora_candidate_runtime": {
                    "audio_selection_mode": "default_audio",
                    "selected_family": "default",
                    "fallback_reason": "all_candidates_rejected",
                    "candidates": [
                        {
                            "family": "default",
                            "audio_relpath": "default.wav",
                            "duration_ms": 22697,
                            "drift_ms": -17943,
                            "abs_drift_ms": 17943,
                            "similarity": 0.69,
                            "critical_term_recall": 1.0,
                            "fit_tempo_delta": 0.4,
                            "reject_reasons": ["low_asr", "high_drift", "high_fit_delta"],
                            "gate_pass": False,
                        },
                        {
                            "family": "split_mix",
                            "split_strategy": "micro_clause_zero_pause",
                            "audio_relpath": "split_mix.wav",
                            "duration_ms": 40651,
                            "drift_ms": 11.2,
                            "abs_drift_ms": 11.2,
                            "similarity": 1.0,
                            "critical_term_recall": 1.0,
                            "fit_tempo_delta": 0.069,
                            "reject_reasons": ["bad_join_quality"],
                            "gate_pass": False,
                            "join_quality": {
                                "pass": False,
                                "reasons": ["large_edge_rms_jump"],
                                "max_edge_rms_jump_db": 18.192,
                                "joins": [
                                    {
                                        "index": 1,
                                        "pause_sec": 0,
                                        "prev_tail_dbfs": -46.63,
                                        "next_head_dbfs": -64.822,
                                        "edge_rms_jump_db": 18.192,
                                    }
                                ],
                            },
                        },
                    ],
                },
            },
        }
    ]

    summary = monitor.summarize(
        {"id": 153, "name": "shadow", "status": "running"},
        [{"id": 200, "status": "running"}],
        segments,
        args=args,
    )

    replay_rows = summary["candidate"]["gate_replay_rows"]
    assert len(replay_rows) == 1
    replay = replay_rows[0]["runtime_gate_replay"]
    assert replay["changed"] is True
    assert replay["replayed_audio_selection_mode"] == "candidate_audio"
    assert replay["replayed_selected_family"] == "split_mix"
    assert replay["join_quality_rescue_count"] == 1
    assert replay["selected_candidate"]["join_quality_rescue"]["rule"] == "quiet_zero_pause_large_edge_rms_jump"
    assert summary["risk_queue"][0]["runtime_gate_replay"]["replayed_audio_selection_mode"] == "candidate_audio"


def test_episode_candidate_monitor_flags_default_hard_drift() -> None:
    monitor = _load_tool(
        "lora_episode_candidate_monitor_hard_drift",
        "tools/lora_episode_candidate_monitor.py",
    )
    args = Namespace(
        min_target_sec=18.0,
        min_text_chars=180,
        max_candidate_drift_ms=500.0,
        review_drift_ms=300.0,
        focus_source_segments="",
    )
    segments = [
        {
            "id": 2101,
            "chapter_id": 210,
            "chapter_ordinal": 2,
            "ordinal": 12,
            "status": "synthesized",
            "original_duration_ms": 11250,
            "tts_duration_ms": 10650,
            "tgt_text": "short default path",
            "tts_audio_path": "jobs/210/tts/vp0/segment-0012.wav",
            "meta": {
                "shadow_source_segment_id": 10751,
                "tts_lora_candidate": {
                    "enabled": False,
                    "phase": "main_flow_shadow",
                    "audio_selection_mode": "default_audio",
                },
            },
        }
    ]

    summary = monitor.summarize(
        {"id": 171, "name": "shadow", "status": "running"},
        [{"id": 210, "status": "running"}],
        segments,
        args=args,
    )

    assert summary["drift"]["high_drift_count"] == 1
    assert summary["validation_issues"] == [
        {
            "source_segment_id": 10751,
            "segment_id": 2101,
            "reason": "drift_exceeds_hard_limit",
            "value": -600,
            "threshold": 500.0,
            "audio_selection_mode": None,
        }
    ]
    assert summary["risk_queue"][0]["reason"] == "drift_exceeds_hard_limit"
    assert summary["risk_queue"][0]["priority"] == 5


def test_episode_candidate_monitor_estimates_v9_dense_mid_eligibility() -> None:
    monitor = _load_tool(
        "lora_episode_candidate_monitor_dense_mid",
        "tools/lora_episode_candidate_monitor.py",
    )
    args = Namespace(
        min_target_sec=18.0,
        min_text_chars=180,
        max_candidate_drift_ms=500.0,
        review_drift_ms=300.0,
        focus_source_segments="10733",
    )
    row = {
        "id": 2201,
        "chapter_id": 220,
        "chapter_ordinal": 2,
        "ordinal": 25,
        "status": "translated",
        "original_duration_ms": 14550,
        "tts_duration_ms": 0,
        "tgt_text": "\u5b57" * 72,
        "tts_audio_path": "",
        "meta": {"shadow_source_segment_id": 10733},
    }

    compact = monitor.compact_segment(row, args)

    assert compact["eligible"] is True


def test_episode_candidate_monitor_estimates_v9_short_term_number_eligibility() -> None:
    monitor = _load_tool(
        "lora_episode_candidate_monitor_term_number",
        "tools/lora_episode_candidate_monitor.py",
    )
    args = Namespace(
        min_target_sec=18.0,
        min_text_chars=180,
        max_candidate_drift_ms=500.0,
        review_drift_ms=300.0,
        focus_source_segments="10694",
    )
    row = {
        "id": 2202,
        "chapter_id": 220,
        "chapter_ordinal": 1,
        "ordinal": 5,
        "status": "translated",
        "original_duration_ms": 7520,
        "tts_duration_ms": 0,
        "tgt_text": "x" * 34 + "\u4e94 \u53f7\u4efb\u671f",
        "tts_audio_path": "",
        "meta": {"shadow_source_segment_id": 10694},
    }

    compact = monitor.compact_segment(row, args)

    assert compact["eligible"] is True


def test_episode_candidate_monitor_lineage_backfill_sql_matches_shadow_mapping() -> None:
    monitor = _load_tool("lora_episode_candidate_monitor_lineage", "tools/lora_episode_candidate_monitor.py")

    sql = monitor.lineage_backfill_sql(147, 153)

    assert "srcj.episode_id = 147" in sql
    assert "sj.episode_id = 153" in sql
    assert "srcs.ordinal = s.ordinal" in sql
    assert "shadow_source_segment_id" in sql


def test_episode_candidate_monitor_appends_history(tmp_path: Path) -> None:
    monitor = _load_tool("lora_episode_candidate_monitor_history", "tools/lora_episode_candidate_monitor.py")
    history = tmp_path / "history.jsonl"

    monitor.append_history(
        history,
        {
            "updated_at_epoch": 123.0,
            "progress": {"segments_synthesized": 2},
            "candidate": {"candidate_audio_segments": 2},
            "drift": {"mean_abs_drift_ms": 5.5},
            "validation_issues": [],
        },
    )

    rows = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "updated_at_epoch": 123.0,
            "progress": {"segments_synthesized": 2},
            "candidate": {"candidate_audio_segments": 2},
            "drift": {"mean_abs_drift_ms": 5.5},
            "agent": None,
            "validation_issue_count": 0,
        }
    ]


def test_episode_candidate_finalize_helpers(tmp_path: Path) -> None:
    finalize = _load_tool("lora_episode_candidate_finalize", "tools/lora_episode_candidate_finalize.py")

    sql = finalize.lineage_backfill_sql(147, 153)

    assert "srcj.episode_id = 147" in sql
    assert "sj.episode_id = 153" in sql
    assert "shadow_source_segment_id" in sql
    assert "srcs.ordinal = s.ordinal" in sql

    completion_sql = finalize.episode_completion_sql(179)
    assert "where e.id = 179" in completion_sql
    assert "episode_output_relpath" in completion_sql
    assert "jobs_not_completed" in completion_sql

    monitor = {
        "progress": {"segments_remaining": 4},
        "focus_segments": [
            {"segment_id": 11061, "source_segment_id": 10694},
            {"segment_id": 11099, "source_segment_id": 10835},
        ],
        "validation_issues": [
            {"segment_id": 11100, "source_segment_id": 10866, "reason": "candidate_fallback"}
        ],
    }

    assert finalize.remaining_segments(monitor) == 4
    assert finalize.review_segment_ids(monitor) == [11061, 11099, 11100]

    assert finalize.final_mix_wait_reason(
        {
            "episode_status": "running",
            "jobs_total": 6,
            "jobs_completed": 5,
            "episode_output_relpath": "episodes/179/final.mp4",
        },
        tmp_path,
        "data",
    ) == "episode_not_completed"

    assert finalize.final_mix_wait_reason(
        {
            "episode_status": "completed",
            "jobs_total": 6,
            "jobs_completed": 5,
            "episode_output_relpath": "episodes/179/final.mp4",
        },
        tmp_path,
        "data",
    ) == "chapter_jobs_not_completed"

    assert finalize.final_mix_wait_reason(
        {
            "episode_status": "completed",
            "jobs_total": 6,
            "jobs_completed": 6,
            "episode_output_relpath": "episodes/179/final.mp4",
        },
        tmp_path,
        "data",
    ) == "final_media_missing"

    media = tmp_path / "data" / "episodes" / "179" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"fake media")
    assert finalize.final_mix_wait_reason(
        {
            "episode_status": "completed",
            "jobs_total": 6,
            "jobs_completed": 6,
            "episode_output_relpath": "episodes/179/final.mp4",
        },
        tmp_path,
        "data",
    ) == ""


def test_split_plan_force_break_preserves_pause_intent_boundary() -> None:
    split_tool = _load_tool("lora_split_plan", "tools/lora_split_plan.py")
    args = Namespace(
        max_part_chars=170,
        min_part_chars=24,
        max_part_sec=16.0,
        max_parts=4,
        pause_sec=0.28,
        duration_token_rate_policy="piecewise",
        duration_token_rate=23.4375,
        force_break_after_regex=["\u8bb0\u5f55\u5230\u4e94\u548c\u516d\u3002$"],
    )
    row = {
        "sample_id": "seg10518",
        "text": (
            "\u5e76\u7528\u8ffd\u52a0\u6761\u76ee\u4e2d\u7684\u5185\u5bb9\u66ff\u6362\u5b83\u3002"
            "\u56e0\u6b64\uff0c\u4e8c\u53f7\u8282\u70b9\u5c06\u6267\u884c\u8fd9\u4e00\u64cd\u4f5c\u3002"
            "\u73b0\u5728\u5b83\u3002\u55ef\u3002\u5b83\u521a\u521a\u8bb0\u5f55\u5230\u4e94\u548c\u516d\u3002"
            "\u968f\u540e\uff0c\u4e00\u53f7\u8282\u70b9\u4ecd\u7136\u5b58\u5728\u4e00\u4e2a\u95ee\u9898\u3002"
        ),
        "target_ms": 24100,
        "audio_ms": 24100,
    }

    parts = split_tool.planned_parts(row, args)
    texts = [part["text"] for part in parts]

    assert len(parts) >= 2
    boundary_index = next(
        index
        for index, text in enumerate(texts)
        if text.endswith("\u8bb0\u5f55\u5230\u4e94\u548c\u516d\u3002")
    )
    assert texts[boundary_index + 1].startswith("\u968f\u540e\uff0c\u4e00\u53f7\u8282\u70b9")
    assert all(part["split_pause_sec"] == 0.28 for part in parts)


def test_split_plan_preserves_critical_term_boundary() -> None:
    split_tool = _load_tool("lora_split_plan_critical_boundary", "tools/lora_split_plan.py")
    args = Namespace(
        max_part_chars=170,
        min_part_chars=24,
        max_part_sec=16.0,
        max_parts=4,
        pause_sec=0.18,
        duration_token_rate_policy="piecewise",
        duration_token_rate=23.4375,
        force_break_after_regex=[],
    )
    row = {
        "sample_id": "seg10746",
        "text": (
            "\u8fd9\u662f\u5176\u4e2d\u4e00\u79cd\u60c5\u51b5\uff0c"
            "\u9700\u8981\u56de\u6eda\u6574\u4e2a\u4efb\u671f\u7684\u65e5\u5fd7\u6761\u76ee\uff0c"
            "\u56e0\u4e3a\u8be5\u4efb\u671f\u5728\u9886\u5bfc\u8005\u5904\u5b8c\u5168\u7f3a\u5931\u3002"
        ),
        "target_ms": 6900,
        "audio_ms": 6900,
    }

    parts = split_tool.planned_parts(row, args)
    texts = [part["text"] for part in parts]

    assert len(texts) == 2
    assert "\u65e5|\u5fd7" not in "|".join(texts)
    assert any("\u65e5\u5fd7\u6761\u76ee" in part for part in texts)
    assert texts[1].startswith("\u56e0\u4e3a\u8be5\u4efb\u671f")


def test_split_plan_flags_high_required_chars_per_second() -> None:
    split_tool = _load_tool("lora_split_plan_speech_rate", "tools/lora_split_plan.py")
    args = Namespace(
        max_part_chars=170,
        min_part_chars=24,
        max_part_sec=16.0,
        max_parts=4,
        pause_sec=0.18,
        max_part_chars_per_sec=5.6,
        duration_token_rate_policy="piecewise",
        duration_token_rate=23.4375,
        force_break_after_regex=[],
    )
    row = {
        "sample_id": "seg-density",
        "text": "\u8fd9\u662f\u4e00\u4e2a\u5fc5\u987b\u88ab\u538b\u7f29\u7684\u9ad8\u5bc6\u5ea6\u6d4b\u8bd5\u7247\u6bb5\uff0c" * 8,
        "target_ms": 12000,
        "audio_ms": 12000,
    }

    parts = split_tool.planned_parts(row, args)

    assert any(part["speech_rate_risk"] for part in parts)
    assert max(float(part["required_chars_per_sec"]) for part in parts) > 5.6


def test_split_plan_should_plan_uses_density_trigger_below_legacy_threshold() -> None:
    split_tool = _load_tool("lora_split_plan_density_trigger", "tools/lora_split_plan.py")
    args = Namespace(
        min_target_sec=18.0,
        min_text_chars=180,
        min_chars_per_sec_to_split=4.9,
    )
    row = {
        "sample_id": "seg-density-short",
        "text": "\u5b57" * 65,
        "target_ms": 12000,
        "audio_ms": 12000,
    }

    assert split_tool.should_plan(row, set(), args)

    args.min_chars_per_sec_to_split = 5.6
    assert not split_tool.should_plan(row, set(), args)


def test_split_plan_enforces_soft_max_part_seconds_for_uneven_blocks() -> None:
    split_tool = _load_tool("lora_split_plan_max_part", "tools/lora_split_plan.py")
    args = Namespace(
        max_part_chars=170,
        min_part_chars=24,
        max_part_sec=16.0,
        max_parts=4,
        pause_sec=0.18,
        duration_token_rate_policy="piecewise",
        duration_token_rate=23.4375,
        force_break_after_regex=[],
    )
    row = {
        "sample_id": "seg-long",
        "text": (
            "A" * 157
            + "\u3002"
            + "B" * 60
            + "\u3002"
        ),
        "target_ms": 38730,
        "audio_ms": 38730,
    }

    parts = split_tool.planned_parts(row, args)
    max_part_sec = max(float(part["target_ms"]) / 1000.0 for part in parts)

    assert len(parts) == 3
    assert max_part_sec <= 16.0


def test_split_plan_merges_punctuation_only_islands() -> None:
    split_tool = _load_tool("lora_split_plan_tiny_islands", "tools/lora_split_plan.py")
    args = Namespace(
        max_part_chars=170,
        min_part_chars=24,
        max_part_sec=16.0,
        max_parts=4,
        pause_sec=0.18,
        duration_token_rate_policy="piecewise",
        duration_token_rate=23.4375,
        force_break_after_regex=[],
    )
    row = {
        "sample_id": "seg10616",
        "text": (
            "\u6211\u4eec\u5728\u8fd9\u91cc\u59cb\u7ec8\u5173\u6ce8Raft\u7684\u7b80\u5355\u6027\u4e0e\u5b89\u5168\u6027\uff0c\u6240\u4ee5\u4f60\u7684\u7406\u89e3\u5b8c\u5168\u6b63\u786e\u3002"
            "\u4f60\u521a\u624d\u63d0\u5230\"\u975e\u6613\u5931\"\uff0c\u62b1\u6b49\uff0c\u51c6\u786e\u6765\u8bf4\u5e94\u8be5\u662f\"\u6613\u5931\u7684\"\u3002"
            "\u8fd9\u4e9b\u5b57\u6bb5\u4e4b\u6240\u4ee5\u53ef\u4ee5\u8bbe\u4e3a\u6613\u5931\u5e76\u88ab\u4e22\u5f03\uff0c\u5176"
            "\u6839\u672c\u539f\u56e0\u5728\u4e8e\u9886\u5bfc\u8005\u5b8c\u5168\u80fd\u591f\u901a\u8fc7\u4ed4\u7ec6\u68c0\u67e5\u5b83\u81ea\u5df1\u7684\u65e5\u5fd7\uff0c\u5e76\u7ed3\u5408\u5411\u8ddf\u968f\u8005\u53d1\u9001Append Entries RPC\u540e\u6240\u6536\u5230\u7684\u54cd\u5e94\u7ed3\u679c\uff0c\u6765\u9010\u6b65\u91cd\u5efa\u51fa\u7a76\u7adf\u54ea\u4e9b\u5185\u5bb9\u5df2\u7ecf\u63d0\u4ea4\u3002"
            "\u6253\u4e2a\u6bd4\u65b9\uff0c\u5018\u82e5\u6240\u6709\u8282\u70b9\u56e0\u4e3a\u906d\u9047\u65ad\u7535\u800c\u96c6\u4f53\u91cd\u542f\uff0c\u90a3\u4e48\u5728\u6700\u521d\u9636\u6bb5\uff0c\u9886\u5bfc\u8005\u5176\u5b9e\u5e76\u4e0d\u6e05\u695a\u5230\u5e95\u54ea\u4e9b\u65e5\u5fd7\u5df2\u7ecf\u63d0\u4ea4\u3001\u54ea\u4e9b\u64cd\u4f5c\u5df2\u7ecf\u6267\u884c\u3002"
        ),
        "target_ms": 38730,
        "audio_ms": 38730,
    }

    parts = split_tool.planned_parts(row, args)
    texts = [part["text"] for part in parts]

    assert "\u3002" not in texts
    assert all(split_tool.meaningful_chars(text) > 0 for text in texts)
    assert max(float(part["target_ms"]) / 1000.0 for part in parts) <= 16.0


def test_lora_split_assemble_tailfade_repairs_loud_boundary_tail(tmp_path: Path) -> None:
    assemble_tool = _load_tool("lora_split_assemble_tailfade", "tools/lora_split_assemble.py")
    loud_tail = tmp_path / "part1.wav"
    next_part = tmp_path / "part2.wav"
    _write_constant_wav(loud_tail, duration_ms=500, amplitude=2500)
    _write_silence_wav(next_part, duration_ms=500)

    disabled_paths, disabled = assemble_tool.repair_loud_boundary_tails(
        [loud_tail, next_part],
        tmp_path / "disabled",
        "seg1",
        Namespace(
            repair_loud_boundary_tails=False,
            boundary_tail_fade_ms=120.0,
            join_window_ms=120.0,
            max_boundary_tail_dbfs=-24.0,
        ),
    )
    assert disabled_paths == [loud_tail, next_part]
    assert disabled["enabled"] is False

    before = assemble_tool.edge_rms(loud_tail, 120.0)["tail_dbfs"]
    repaired_paths, repair = assemble_tool.repair_loud_boundary_tails(
        [loud_tail, next_part],
        tmp_path / "enabled",
        "seg1",
        Namespace(
            repair_loud_boundary_tails=True,
            boundary_tail_fade_ms=120.0,
            join_window_ms=120.0,
            max_boundary_tail_dbfs=-24.0,
        ),
    )
    after = assemble_tool.edge_rms(repaired_paths[0], 120.0)["tail_dbfs"]

    assert before > -24.0
    assert after <= -24.0
    assert repaired_paths[0] != loud_tail
    assert repaired_paths[1] == next_part
    assert repair["enabled"] is True
    assert repair["repaired_part_indices"] == [1]


def test_asr_term_metrics_treat_latin_technical_terms_as_soft_risk() -> None:
    asr_tool = _load_tool("lora_asr_eval_terms", "tools/lora_asr_eval.py")

    metrics = asr_tool.critical_term_metrics(
        "\u6240\u4ee5Raft\u5c42\u4f1a\u751f\u6210\u5feb\u7167\u3002",
        "\u6240\u4ee5\u62c9\u592b\u7279\u5c42\u4f1a\u751f\u6210\u5feb\u7167\u3002",
    )

    assert "raft" not in metrics["critical_terms"]
    assert "raft" in metrics["soft_latin_terms"]
    assert "raft" in metrics["latin_surface_terms_missing"]
    assert metrics["critical_term_recall"] == 1.0

    variable_metrics = asr_tool.critical_term_metrics("\u8bfb\u53d6x\u5f97\u52301\u3002", "\u8bfb\u53d6x\u5f97\u5230\u4e00\u3002")
    assert "x" in variable_metrics["critical_terms"]


def test_canary_loop_adds_duration_fields_for_full_candidates() -> None:
    loop = _load_tool("lora_canary_loop_duration_fields", "tools/lora_canary_candidate_loop.py")
    args = Namespace(duration_token_rate_policy="constant", duration_token_rate=21.5)
    rows = [{"sample_id": "seg-full", "text": "hello", "target_ms": 2000}]

    planned = loop.add_full_duration_fields(rows, args)

    assert planned[0]["duration_token_rate"] == 21.5
    assert planned[0]["code_len"] == 43
    assert planned[0]["duration_tokens_from_target"] == 43


def test_canary_regression_check_uses_gate_summary_when_loop_summary_missing(tmp_path: Path) -> None:
    gate = tmp_path / "candidate_gate_report.json"
    gate.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "sample_id": "seg10569",
                        "candidate_label": "shard01_selected",
                        "candidate_label_chain": ["shard01_selected"],
                    }
                ],
                "summary": {
                    "selected": {
                        "count": 84,
                        "mean_abs_drift_ms": 39.0,
                        "max_abs_drift_ms": 298.0,
                        "mean_similarity": 0.993,
                        "min_similarity": 0.9625,
                        "join_quality_fail_count": 0,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "tools/lora_canary_regression_check.py",
            "--summary",
            str(tmp_path / "missing_loop_summary.json"),
            "--gate-report",
            str(gate),
            "--expect-chain",
            "seg10569=shard01_selected",
            "--forbid-chain",
            "seg10569=split_light_vector_10",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert '"ok": true' in result.stdout


def test_listening_pack_records_review_notes_lineage_and_override(tmp_path: Path) -> None:
    audio = tmp_path / "selected.wav"
    audio.write_bytes(b"not-a-real-wav-but-copyable")
    report = tmp_path / "gate.json"
    report.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "sample_id": "seg10569",
                        "candidate_label": "shard01_selected",
                        "upstream_candidate_label": "split_light_vector",
                        "candidate_label_chain": ["shard01_selected", "split_light_vector"],
                        "path": str(audio),
                        "duration_sec": 10.013,
                        "target_sec": 10.0,
                        "similarity": 0.9855,
                        "critical_term_recall": 1.0,
                        "fit_tempo_delta": 0.0615,
                        "listening_override_applied": True,
                    }
                ],
                "decisions": [
                    {
                        "sample_id": "seg10569",
                        "choice": "shard01_selected",
                        "fallback_tier": "listening_override",
                        "listening_override_applied": True,
                        "listening_override": {"reason": "sounds more natural"},
                        "chosen_reject_reasons": ["flat_f0"],
                        "candidates": [
                            {
                                "label": "shard01_selected",
                                "candidate_label_chain": ["shard01_selected", "split_light_vector"],
                                "score": 1.0,
                            },
                            {
                                "label": "split_light_vector_10",
                                "score": 0.9,
                                "abs_drift_ms": 26.6,
                                "similarity": 0.9855,
                                "f0_range_semitones_10_90": 4.98,
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    features = tmp_path / "features.jsonl"
    features.write_text(
        json.dumps({"sample_id": "seg10569", "text": "hello"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    notes = tmp_path / "notes.json"
    notes.write_text(
        json.dumps(
            {
                "notes": [
                    {
                        "sample_id": "seg10569",
                        "candidate_label": "shard01_selected",
                        "title": "人工听感 override：选择 shard01_selected",
                        "body": "听感更自然",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "pack"

    subprocess.run(
        [
            sys.executable,
            "tools/lora_build_listening_pack.py",
            "--output-dir",
            str(out_dir),
            "--features",
            str(features),
            "--report",
            f"selected={report}",
            "--review-notes",
            str(notes),
        ],
        cwd=REPO_ROOT,
        check=True,
    )

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert manifest["review_notes"][0]["sample_id"] == "seg10569"
    assert manifest["rows"][0]["candidate_label_chain"] == ["shard01_selected", "split_light_vector"]
    assert "Listening override applied" in html
    assert "人工听感 override：选择 shard01_selected" in html
    assert "lineage: shard01_selected &gt; split_light_vector" in html


def test_episode_assemble_bgm_mix_preserves_target_duration(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not available")
    assemble = _load_tool("lora_episode_assemble_duration_lock", "tools/lora_episode_assemble.py")
    voice = tmp_path / "voice.wav"
    bgm = tmp_path / "short_bgm.wav"
    out = tmp_path / "mixed.wav"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-t",
            "1.000",
            "-i",
            "sine=frequency=440:sample_rate=24000",
            "-ac",
            "1",
            str(voice),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-t",
            "0.250",
            "-i",
            "sine=frequency=220:sample_rate=24000",
            "-ac",
            "1",
            str(bgm),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assemble.apply_bgm("ffmpeg", voice, bgm, out, 1000)

    assert assemble.probe_duration_ms("ffprobe", out) == 1000


def test_episode_objective_audit_passes_hard_gates_but_requires_review(tmp_path: Path) -> None:
    audit_tool = _load_tool("lora_episode_objective_audit", "tools/lora_episode_objective_audit.py")
    run_dir = tmp_path / "episode_run"
    output_dir = run_dir / "output" / "vp0"
    audio_path = output_dir / "final_audio.wav"
    video_path = output_dir / "final.mp4"
    _write_silence_wav(audio_path, duration_ms=5000)
    video_path.write_bytes(b"placeholder")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "episode_result_summary.json").write_text(
        json.dumps(
            {
                "episode_id": 146,
                "outputs": {
                    "final_audio": str(audio_path),
                    "final_video": str(video_path),
                },
                "duration": {
                    "expected_duration_ms": 5000,
                    "final_audio_duration_ms": 5000,
                    "final_audio_abs_drift_ms": 0,
                    "final_video_duration_ms": 5043,
                    "final_video_abs_drift_ms": 43,
                },
                "coverage": {
                    "timeline_segments": 1,
                    "lora_selected_segments": 1,
                    "fallback_existing_tts_segments": 0,
                    "missing_audio_segments": 0,
                },
                "missing_audio": [],
                "lora_selected_metrics": {
                    "abs_drift_ms": {"count": 1, "mean": 10.0, "max": 10.0, "min": 10.0},
                    "similarity": {"count": 1, "mean": 1.0, "max": 1.0, "min": 1.0},
                    "join_quality_fail_count": 0,
                    "fit_tempo_delta": {"count": 1, "mean": 0.09, "max": 0.09, "min": 0.09},
                },
                "fallback_existing_tts_metrics": {
                    "abs_drift_ms": {"count": 0, "mean": 0.0, "max": 0.0, "min": 0.0},
                    "reject_reason_counts": {},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "episode_segment_lineage.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "sample_id": "seg1",
                        "source_kind": "lora_selected",
                        "candidate_label": "split_mix",
                        "abs_drift_ms": 10.0,
                        "similarity": 1.0,
                        "critical_term_recall": 1.0,
                        "fit_tempo_delta": 0.09,
                        "join_quality_pass": True,
                        "clipped_ms": 0,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audit = audit_tool.build_audit(
        Namespace(
            run_dir=str(run_dir),
            output_dir="",
            repo_root=str(REPO_ROOT),
            ffprobe="ffprobe",
            max_final_audio_drift_ms=50.0,
            max_final_video_drift_ms=100.0,
            max_lora_drift_ms=300.0,
            target_lora_mean_drift_ms=50.0,
            min_lora_mean_asr=0.99,
            min_lora_min_asr=0.96,
            max_join_quality_failures=0,
            max_tempo_delta_without_review=0.08,
            max_clip_ms_without_review=200.0,
            top_risk=5,
        )
    )

    assert audit["objective_pass"] is True
    assert audit["review_required"] is True
    assert audit["strategy_contract"]["pass"] is True
    assert len(audit["lora_gate_exceptions"]["tempo_review_required"]) == 1


def _shadow_meta(enabled: bool, split_planning: bool, families: list[str] | None = None) -> dict:
    if families is None:
        families = ["default", "split_light_vector", "split_mix"] if split_planning else ["default"]
    return {
        "schema_version": 2,
        "phase": "main_flow_shadow",
        "candidate_execution_enabled": False,
        "audio_selection_mode": "default_audio",
        "selected_family": "default",
        "enabled": enabled,
        "split_planning": split_planning,
        "candidate_families": families,
        "eligibility": {
            "duration_eligible": enabled,
            "text_eligible": False,
        },
    }


def test_main_flow_shadow_audit_accepts_expected_provenance() -> None:
    audit_tool = _load_tool("lora_main_flow_shadow_audit", "tools/lora_main_flow_shadow_audit.py")
    segments = [
        {
            "id": 1,
            "status": "synthesized",
            "start_ms": 0,
            "end_ms": 21_000,
            "tgt_text": "long segment",
            "tts_audio_path": "jobs/1/tts/vp0/segment-0001.wav",
            "tts_duration_ms": 21_000,
            "meta": {"tts_lora_candidate": _shadow_meta(True, True)},
        },
        {
            "id": 2,
            "status": "synthesized",
            "start_ms": 21_000,
            "end_ms": 24_000,
            "tgt_text": "short",
            "tts_audio_path": "jobs/1/tts/vp0/segment-0002.wav",
            "tts_duration_ms": 3_000,
            "meta": {"tts_lora_candidate": _shadow_meta(False, False)},
        },
    ]

    audit = audit_tool.build_audit(segments)

    assert audit["pass"] is True
    assert audit["counts"]["duration_or_text_eligible"] == 1
    assert audit["counts"]["with_tts_lora_candidate"] == 2


def test_main_flow_shadow_audit_rejects_missing_eligible_provenance() -> None:
    audit_tool = _load_tool("lora_main_flow_shadow_audit", "tools/lora_main_flow_shadow_audit.py")
    segments = [
        {
            "id": 1,
            "status": "synthesized",
            "start_ms": 0,
            "end_ms": 21_000,
            "tgt_text": "long segment",
            "tts_audio_path": "jobs/1/tts/vp0/segment-0001.wav",
            "tts_duration_ms": 21_000,
            "meta": {},
        }
    ]

    audit = audit_tool.build_audit(segments)

    assert audit["pass"] is False
    assert audit["issues"][0]["reason"] == "missing_tts_lora_candidate"
    assert audit["issues"][0]["eligible"] is True


def test_main_flow_shadow_audit_rejects_non_default_audio_selection() -> None:
    audit_tool = _load_tool("lora_main_flow_shadow_audit", "tools/lora_main_flow_shadow_audit.py")
    meta = _shadow_meta(True, True)
    meta["candidate_execution_enabled"] = True
    meta["audio_selection_mode"] = "candidate_audio"
    segments = [
        {
            "id": 1,
            "status": "synthesized",
            "start_ms": 0,
            "end_ms": 21_000,
            "tgt_text": "long segment",
            "tts_audio_path": "jobs/1/tts/vp0/segment-0001.wav",
            "tts_duration_ms": 21_000,
            "meta": {"tts_lora_candidate": meta},
        }
    ]

    audit = audit_tool.build_audit(segments)

    assert audit["pass"] is False
    assert {issue["reason"] for issue in audit["issues"]} >= {
        "candidate_execution_not_disabled",
        "audio_selection_not_default",
    }


def test_main_flow_review_pack_collects_low_judge_and_chapter_weakest() -> None:
    review_tool = _load_tool("lora_main_flow_review_pack", "tools/lora_main_flow_review_pack.py")
    chapters = [
        {
            "id": 10,
            "chapter_judge_meta": {
                "top_3_weakest_segments": [
                    {
                        "ordinal": 2,
                        "issue": "too verbose",
                        "recommended_fix": "trim filler",
                    }
                ]
            },
        }
    ]
    segments = [
        {
            "id": 101,
            "chapter_id": 10,
            "chapter_ordinal": 1,
            "chapter_start_ms": 1000,
            "ordinal": 1,
            "start_ms": 2000,
            "end_ms": 3000,
            "original_duration_ms": 1000,
            "tts_duration_ms": 900,
            "judge_score": 0.7,
            "judge_meta": {"verdict": "retry", "issues": ["missing term"]},
            "src_text": "read x",
            "tgt_text": "读取 x",
        },
        {
            "id": 102,
            "chapter_id": 10,
            "chapter_ordinal": 1,
            "chapter_start_ms": 1000,
            "ordinal": 2,
            "start_ms": 4000,
            "end_ms": 5000,
            "original_duration_ms": 1000,
            "tts_duration_ms": 1000,
            "judge_score": 1.0,
            "judge_meta": {"verdict": "accept"},
            "src_text": "definition",
            "tgt_text": "定义",
        },
    ]

    rows = review_tool.build_review_rows(
        chapters,
        segments,
        explicit_ids={102},
        low_judge_threshold=0.8,
    )

    assert [row["sample_id"] for row in rows] == ["seg101", "seg102"]
    assert rows[0]["absolute_start_ms"] == 3000
    assert {reason["type"] for reason in rows[0]["reasons"]} == {
        "low_segment_judge",
        "segment_judge_verdict",
    }
    assert {reason["type"] for reason in rows[1]["reasons"]} == {
        "manual_focus",
        "chapter_weakest",
    }
    assert rows[1]["reasons"][-1]["recommended_fix"] == "trim filler"


def test_final_mix_asr_manifest_uses_final_media_absolute_windows() -> None:
    asr_tool = _load_tool("lora_final_mix_asr_audit", "tools/lora_final_mix_asr_audit.py")
    rows = [
        {
            "episode_id": 147,
            "episode_name": "demo",
            "episode_duration_ms": 110_000,
            "episode_output_relpath": "episodes/147/output/vp0/final.mp4",
            "job_id": 1,
            "chapter_ordinal": 2,
            "chapter_start_ms": 100_000,
            "chapter_end_ms": 110_000,
            "segment_id": 10,
            "segment_ordinal": 1,
            "start_ms": 2_000,
            "end_ms": 5_000,
            "tts_duration_ms": 8_000,
            "target_text": "fallback text",
            "source_text": "source",
            "meta": {"tts_spoken_text": "spoken target"},
        },
        {
            "episode_id": 147,
            "episode_name": "demo",
            "episode_duration_ms": 110_000,
            "episode_output_relpath": "episodes/147/output/vp0/final.mp4",
            "job_id": 1,
            "chapter_ordinal": 2,
            "chapter_start_ms": 100_000,
            "chapter_end_ms": 110_000,
            "segment_id": 11,
            "segment_ordinal": 2,
            "start_ms": 6_800,
            "end_ms": 8_000,
            "tts_duration_ms": 900,
            "target_text": "second target",
            "source_text": "source",
            "meta": {},
        },
    ]

    manifest = asr_tool.build_manifest(rows, pre_pad_ms=0, post_pad_ms=250, next_gap_ms=50, min_window_ms=500)

    assert manifest[0]["audio_relpath"] == "episodes/147/output/vp0/final.mp4"
    assert manifest[0]["start_ms"] == 102_000
    assert manifest[0]["end_ms"] == 106_750
    assert manifest[0]["bounded_by_next_segment"] is True
    assert manifest[0]["expected_text"] == "spoken target"
    assert manifest[0]["expected_source"] == "tts_spoken_text"
    assert manifest[1]["start_ms"] == 106_800
    assert manifest[1]["expected_text"] == "second target"
    assert manifest[1]["expected_source"] == "target_text"

    wav_manifest = asr_tool.build_manifest(rows, audio_relpath_override="training/audit/final_mix_audio.wav")
    assert wav_manifest[0]["audio_relpath"] == "training/audit/final_mix_audio.wav"

    actual_timing_manifest = asr_tool.build_manifest(rows, chapter_start_overrides_ms={2: 101_000}, chapter_timing_source="actual-audio")
    assert actual_timing_manifest[0]["start_ms"] == 103_000
    assert actual_timing_manifest[0]["chapter_timing_source"] == "actual-audio"
    assert actual_timing_manifest[0]["db_chapter_start_ms"] == 100_000
    assert actual_timing_manifest[0]["asr_chapter_start_ms"] == 101_000


def test_final_mix_asr_summary_includes_full_transcript_similarity() -> None:
    asr_tool = _load_tool("lora_final_mix_asr_audit", "tools/lora_final_mix_asr_audit.py")
    manifest = [
        {
            "label": "c01_s0001_seg1",
            "segment_id": 1,
            "chapter_ordinal": 1,
            "segment_ordinal": 1,
            "expected_text": "read raft carefully",
            "start_ms": 0,
            "end_ms": 1000,
            "window_duration_ms": 1000,
        },
        {
            "label": "c01_s0002_seg2",
            "segment_id": 2,
            "chapter_ordinal": 1,
            "segment_ordinal": 2,
            "expected_text": "five day term",
            "start_ms": 1000,
            "end_ms": 2000,
            "window_duration_ms": 1000,
        },
    ]
    results = [
        {**manifest[0], "ok": True, "asr_text": "read raft carefully"},
        {**manifest[1], "ok": True, "asr_text": ""},
    ]

    rows = asr_tool.analyze_results(manifest=manifest, asr_results=results, low_similarity=0.8, very_low_similarity=0.6)
    summary = asr_tool.summarize(rows)
    gate = asr_tool.gate_summary(
        summary,
        Namespace(
            min_mean_similarity=0.86,
            min_weighted_similarity=0.88,
            min_full_transcript_similarity=0.90,
            max_low_similarity=12,
            max_very_low_similarity=0,
        ),
    )

    assert "empty_asr" in rows[1]["flags"]
    assert "very_low_similarity:0.0" in rows[1]["flags"]
    assert summary["full_transcript_similarity"] < 1.0
    assert summary["very_low_similarity_count"] == 1
    assert gate["pass"] is False


def test_record_human_acceptance_accepts_structured_decision() -> None:
    record_tool = _load_tool("lora_record_human_acceptance", "tools/lora_record_human_acceptance.py")
    status = {
        "schema_version": "holodub.human_acceptance_status.v1",
        "episode_id": 147,
        "status": "awaiting_human_listening",
        "listening_pack": {"priority_segments": ["seg10813", "seg10857"]},
        "human_review": {"accepted": None},
    }
    notes = {
        "schema_version": "holodub.human_review_notes.v1",
        "episode_id": 147,
        "decision": "accept_with_issues",
        "reviewed_at": "2026-06-19T00:00:00+00:00",
        "reviewer": "human",
        "priority_segments": {
            "seg10813": {"acceptable": True, "notes": ""},
            "seg10857": {"acceptable": False, "notes": "wordy but not blocking"},
        },
        "blocking_segments": [],
        "non_blocking_issues": [{"sample_id": "seg10857", "issue": "wordy"}],
        "global_patterns": [],
        "summary": "usable with tracked issues",
    }

    updated = record_tool.apply_human_acceptance(status, notes)

    assert updated["status"] == "human_accepted_with_issues"
    assert updated["human_review"]["accepted"] is True
    assert updated["human_review"]["decision"] == "accept_with_issues"
    assert updated["human_review"]["notes"]["summary"] == "usable with tracked issues"


def test_record_human_acceptance_rejects_template_placeholder_decision() -> None:
    record_tool = _load_tool("lora_record_human_acceptance", "tools/lora_record_human_acceptance.py")
    status = {
        "schema_version": "holodub.human_acceptance_status.v1",
        "episode_id": 147,
        "listening_pack": {"priority_segments": ["seg10813"]},
    }
    notes = {
        "schema_version": "holodub.human_review_notes.v1",
        "episode_id": 147,
        "decision": "accept | accept_with_issues | reject",
        "priority_segments": {"seg10813": {"acceptable": None, "notes": ""}},
    }

    try:
        record_tool.apply_human_acceptance(status, notes)
    except ValueError as exc:
        assert "decision must be one of" in str(exc)
    else:
        raise AssertionError("placeholder decision should be rejected")


def test_record_human_acceptance_rejects_accept_with_blockers() -> None:
    record_tool = _load_tool("lora_record_human_acceptance", "tools/lora_record_human_acceptance.py")
    status = {
        "schema_version": "holodub.human_acceptance_status.v1",
        "episode_id": 147,
        "listening_pack": {"priority_segments": ["seg10813"]},
    }
    notes = {
        "schema_version": "holodub.human_review_notes.v1",
        "episode_id": 147,
        "decision": "accept",
        "priority_segments": {"seg10813": {"acceptable": True, "notes": ""}},
        "blocking_segments": ["seg10813"],
    }

    try:
        record_tool.apply_human_acceptance(status, notes)
    except ValueError as exc:
        assert "cannot include blocking_segments" in str(exc)
    else:
        raise AssertionError("accept decision with blockers should be rejected")


def _completion_shadow_audit(tmp_path: Path) -> tuple[dict, Path]:
    final_video = tmp_path / "data" / "episodes" / "147" / "output" / "vp0" / "final.mp4"
    final_video.parent.mkdir(parents=True)
    final_video.write_bytes(b"mp4")
    audit = {
        "schema_version": "holodub.lora_main_flow_shadow_audit.v1",
        "pass": True,
        "shadow_contract_pass": True,
        "episode": {"id": 147, "status": "completed"},
        "counts": {"segments": 180, "synthesized": 180, "missing_audio": 0},
        "duration": {
            "final_output_path": str(final_video),
            "final_video_abs_drift_ms": 52,
        },
    }
    return audit, final_video


def test_shadow_baseline_builder_records_episode_quality_contract(tmp_path: Path) -> None:
    baseline_tool = _load_tool("lora_shadow_baseline", "tools/lora_shadow_baseline.py")
    segments_path = tmp_path / "episode_segments.json"
    asr_path = tmp_path / "final_mix_asr_report.json"
    review_path = tmp_path / "review_manifest.json"
    segments_path.write_text(
        json.dumps(
            [
                {
                    "segment_id": 1,
                    "job_id": 10,
                    "chapter_ordinal": 1,
                    "original_duration_ms": 1000,
                    "tts_duration_ms": 1250,
                    "meta": {
                        "tts_lora_candidate": {"enabled": True, "selected_family": "default"},
                        "tts_lora_candidate_runtime": {
                            "execution_requested": True,
                            "selected_family": "default",
                            "audio_selection_mode": "candidate_audio",
                            "candidates": [{"reject_reasons": ["high_fit_delta"]}],
                        },
                    },
                },
                {
                    "segment_id": 2,
                    "job_id": 10,
                    "chapter_ordinal": 1,
                    "original_duration_ms": 1000,
                    "tts_duration_ms": 900,
                    "meta": {},
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    asr_path.write_text(
        json.dumps(
            {
                "gate": {"pass": True},
                "summary": {
                    "total_segments": 2,
                    "ok_segments": 2,
                    "mean_similarity": 0.99,
                    "low_similarity_count": 0,
                    "very_low_similarity_count": 0,
                },
                "rows": [
                    {
                        "segment_id": 2,
                        "job_id": 10,
                        "chapter_ordinal": 1,
                        "segment_ordinal": 2,
                        "similarity": 0.98,
                        "flags": [],
                        "expected_text": "expected",
                        "asr_text": "actual",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    review_path.write_text(json.dumps({"artifacts": {"index": "pack/index.html"}, "review_rows": [{}]}), encoding="utf-8")

    baseline = baseline_tool.build_baseline(
        Namespace(
            episode_id=148,
            episode_segments=str(segments_path),
            final_mix_asr_report=str(asr_path),
            review_manifest=str(review_path),
            final_video="episodes/148/output/vp0/final.mp4",
            output=str(tmp_path / "quality_baseline.json"),
            max_over_drift_ms=300,
            max_under_drift_ms=700,
            worst_rows=5,
        )
    )

    assert baseline["status"] == "shadow_pass"
    assert baseline["drift"]["mean_abs_drift_ms"] == 175
    assert baseline["drift"]["over_drift_failure_count"] == 0
    assert baseline["final_mix_asr"]["gate_pass"] is True
    assert baseline["candidate_runtime"]["execution_requested_count"] == 1
    assert baseline["candidate_runtime"]["high_fit_delta_rejected_candidate_count"] == 1
    assert baseline["quality_contract"]["high_fit_delta_is_hard_failure"] is True
    assert baseline["quality_contract"]["soft_override_reasons"] == ["flat_f0"]


def _completion_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    clips = pack / "clips"
    clips.mkdir(parents=True)
    (pack / "index.html").write_text("<html></html>", encoding="utf-8")
    (pack / "human_acceptance_checklist.md").write_text("checklist", encoding="utf-8")
    (pack / "human_review_notes.template.json").write_text("{}", encoding="utf-8")
    (clips / "seg10813_context.m4a").write_bytes(b"audio")
    (pack / "review_manifest.json").write_text(
        json.dumps({"review_rows": [{"sample_id": "seg10813"}]}),
        encoding="utf-8",
    )
    return pack


def test_goal_completion_audit_waits_for_human_acceptance(tmp_path: Path) -> None:
    completion_tool = _load_tool("lora_goal_completion_audit", "tools/lora_goal_completion_audit.py")
    audit, _ = _completion_shadow_audit(tmp_path)
    pack = _completion_pack(tmp_path)
    status = {
        "schema_version": "holodub.human_acceptance_status.v1",
        "episode_id": 147,
        "status": "awaiting_human_listening",
        "listening_pack": {"focused_clip_count": 1, "priority_segments": ["seg10813"]},
        "human_review": {"accepted": None},
    }

    result = completion_tool.build_completion_audit(
        audit,
        status,
        pack,
        tmp_path / "data",
        require_human_accepted=True,
        repo_root=tmp_path,
    )

    assert result["objective_ready"] is True
    assert result["human_accepted"] is False
    assert result["complete"] is False
    human_gate = [gate for gate in result["gates"] if gate["name"] == "human_listening_accepted"][0]
    assert human_gate["pass"] is False


def test_goal_completion_audit_passes_after_human_acceptance(tmp_path: Path) -> None:
    completion_tool = _load_tool("lora_goal_completion_audit", "tools/lora_goal_completion_audit.py")
    audit, _ = _completion_shadow_audit(tmp_path)
    pack = _completion_pack(tmp_path)
    status = {
        "schema_version": "holodub.human_acceptance_status.v1",
        "episode_id": 147,
        "status": "human_accepted",
        "listening_pack": {"focused_clip_count": 1, "priority_segments": ["seg10813"]},
        "human_review": {"accepted": True},
    }

    result = completion_tool.build_completion_audit(
        audit,
        status,
        pack,
        tmp_path / "data",
        require_human_accepted=True,
        repo_root=tmp_path,
    )

    assert result["objective_ready"] is True
    assert result["human_accepted"] is True
    assert result["complete"] is True
